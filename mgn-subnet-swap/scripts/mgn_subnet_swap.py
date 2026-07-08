#!/usr/bin/env python3
"""
Review AWS MGN source server launch templates and optionally update launch settings.

Default behavior is dry-run. Pass --execute to create new launch template versions
and set those versions as default.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import uuid
from datetime import date, datetime
from typing import Any

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover - dependency is validated at runtime
    boto3 = None
    BotoCoreError = ClientError = Exception


DEFAULT_FAMILY_MAP = {
    "c5": "c6i",
    "m5": "m6i",
    "r5": "r6i",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect AWS MGN source servers, find matching launch templates, and "
            "optionally create default template versions with subnet and/or instance "
            "type changes."
        )
    )
    parser.add_argument("--region", help="AWS region. Uses normal boto3 resolution if omitted.")
    parser.add_argument("--profile", help="AWS profile. Uses normal boto3 resolution if omitted.")
    parser.add_argument("--from-subnet", help="Existing subnet ID to replace.")
    parser.add_argument("--to-subnet", help="Replacement subnet ID.")
    parser.add_argument(
        "--upgrade-instance-types",
        action="store_true",
        help=(
            "Upgrade mapped 5-series EC2 instance families in the launch template "
            "InstanceType field. Default mappings: c5=c6i, m5=m6i, r5=r6i."
        ),
    )
    parser.add_argument(
        "--family-map",
        action="append",
        default=[],
        metavar="FROM=TO",
        help=(
            "Instance family mapping to apply with --upgrade-instance-types. "
            "Repeatable. Default mappings: c5=c6i, m5=m6i, r5=r6i."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform writes. Without this flag, the script only reports intended changes.",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived MGN source servers. Archived servers are skipped by default.",
    )
    parser.add_argument(
        "--application-id",
        action="append",
        default=[],
        help="Only include source servers in this MGN application ID. Repeatable.",
    )
    parser.add_argument(
        "--source-server-id-regex",
        help="Only include MGN source servers whose sourceServerID matches this regex.",
    )
    parser.add_argument(
        "--server-name-regex",
        help=(
            "Only include source servers whose hostname, FQDN, AWS instance ID, "
            "or Name tag matches this regex."
        ),
    )
    parser.add_argument(
        "--launch-template-id-regex",
        help="Only include EC2 launch template IDs matching this regex.",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Only include source servers with this exact tag. Repeatable.",
    )
    parser.add_argument(
        "--output",
        choices=["json", "text"],
        default="text",
        help="Output format. Default: text.",
    )
    return parser.parse_args()


def json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def compile_regex(pattern: str | None, label: str) -> re.Pattern[str] | None:
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise SystemExit(f"Invalid {label} regex: {exc}") from exc


def parse_tag_filters(tag_args: list[str]) -> dict[str, str]:
    tags: dict[str, str] = {}
    for item in tag_args:
        if "=" not in item:
            raise SystemExit(f"Invalid --tag value {item!r}; expected KEY=VALUE")
        key, value = item.split("=", 1)
        if not key:
            raise SystemExit(f"Invalid --tag value {item!r}; tag key cannot be empty")
        tags[key] = value
    return tags


def parse_family_map(family_map_args: list[str]) -> dict[str, str]:
    family_map = dict(DEFAULT_FAMILY_MAP)
    for item in family_map_args:
        if "=" not in item:
            raise SystemExit(f"Invalid --family-map value {item!r}; expected FROM=TO")
        from_family, to_family = item.split("=", 1)
        if not from_family or not to_family:
            raise SystemExit(f"Invalid --family-map value {item!r}; both families are required")
        family_map[from_family] = to_family
    return family_map


def make_session(args: argparse.Namespace):
    if boto3 is None:
        raise SystemExit("boto3 is required. Install with: pip install boto3")
    kwargs: dict[str, str] = {}
    if args.profile:
        kwargs["profile_name"] = args.profile
    if args.region:
        kwargs["region_name"] = args.region
    return boto3.Session(**kwargs)


def describe_all_source_servers(mgn_client) -> list[dict[str, Any]]:
    servers: list[dict[str, Any]] = []
    request: dict[str, Any] = {}
    while True:
        response = mgn_client.describe_source_servers(**request)
        servers.extend(response.get("items", []))
        next_token = response.get("nextToken")
        if not next_token:
            return servers
        request["nextToken"] = next_token


def get_launch_template_default(ec2_client, launch_template_id: str) -> dict[str, Any]:
    response = ec2_client.describe_launch_template_versions(
        LaunchTemplateId=launch_template_id,
        Versions=["$Default"],
    )
    versions = response.get("LaunchTemplateVersions", [])
    if not versions:
        raise RuntimeError(f"No default version found for launch template {launch_template_id}")
    return versions[0]


def source_server_names(server: dict[str, Any]) -> list[str]:
    names: list[str] = []
    tags = server.get("tags") or {}
    if tags.get("Name"):
        names.append(str(tags["Name"]))

    hints = ((server.get("sourceProperties") or {}).get("identificationHints") or {})
    for key in ("hostname", "fqdn", "awsInstanceID"):
        value = hints.get(key)
        if value:
            names.append(str(value))
    return names


def source_server_matches(
    server: dict[str, Any],
    args: argparse.Namespace,
    tag_filters: dict[str, str],
    source_id_re: re.Pattern[str] | None,
    name_re: re.Pattern[str] | None,
) -> tuple[bool, str | None]:
    source_server_id = server.get("sourceServerID", "")
    if server.get("isArchived") and not args.include_archived:
        return False, "archived"

    if args.application_id:
        application_id = server.get("applicationID")
        if application_id not in args.application_id:
            return False, "application-filter"

    if source_id_re and not source_id_re.search(str(source_server_id)):
        return False, "source-server-id-filter"

    if name_re:
        names = source_server_names(server)
        if not any(name_re.search(name) for name in names):
            return False, "server-name-filter"

    server_tags = server.get("tags") or {}
    for key, value in tag_filters.items():
        if server_tags.get(key) != value:
            return False, f"tag-filter:{key}"

    return True, None


def replace_subnet_ids(
    launch_template_data: dict[str, Any],
    from_subnet: str | None,
    to_subnet: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    changes: list[dict[str, Any]] = []

    network_interfaces = copy.deepcopy(launch_template_data.get("NetworkInterfaces") or [])
    if not from_subnet or not to_subnet:
        return network_interfaces, changes

    for index, network_interface in enumerate(network_interfaces):
        if network_interface.get("SubnetId") == from_subnet:
            network_interface["SubnetId"] = to_subnet
            changes.append(
                {
                    "path": f"LaunchTemplateData.NetworkInterfaces[{index}].SubnetId",
                    "from": from_subnet,
                    "to": to_subnet,
                }
            )

    return network_interfaces, changes


def upgraded_instance_type(instance_type: str, family_map: dict[str, str]) -> str | None:
    if "." not in instance_type:
        return None

    family, size = instance_type.split(".", 1)
    to_family = family_map.get(family)
    if not to_family:
        return None

    return f"{to_family}.{size}"


def build_launch_template_updates(
    launch_template_data: dict[str, Any],
    args: argparse.Namespace,
    family_map: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    updates: dict[str, Any] = {}
    reasons: list[str] = []

    network_interfaces, changes = replace_subnet_ids(
        launch_template_data,
        args.from_subnet,
        args.to_subnet,
    )
    if changes:
        updates["NetworkInterfaces"] = network_interfaces
    elif args.from_subnet and args.to_subnet:
        reasons.append("from-subnet-not-found-in-default-template")

    if args.upgrade_instance_types:
        instance_type = launch_template_data.get("InstanceType")
        if instance_type:
            new_instance_type = upgraded_instance_type(str(instance_type), family_map)
            if new_instance_type:
                updates["InstanceType"] = new_instance_type
                changes.append(
                    {
                        "path": "LaunchTemplateData.InstanceType",
                        "from": instance_type,
                        "to": new_instance_type,
                    }
                )
            else:
                reasons.append("instance-type-not-mapped")
        else:
            reasons.append("no-instance-type-in-default-template")

    return updates, changes, reasons


def create_and_default_launch_template_version(
    ec2_client,
    launch_template_id: str,
    source_version: str,
    launch_template_data: dict[str, Any],
) -> str:
    current_default = get_launch_template_default(ec2_client, launch_template_id)
    current_default_version = str(current_default["VersionNumber"])
    if current_default_version != source_version:
        raise RuntimeError(
            f"Default version for launch template {launch_template_id} changed "
            f"from {source_version} to {current_default_version}; aborting"
        )

    response = ec2_client.create_launch_template_version(
        LaunchTemplateId=launch_template_id,
        SourceVersion=source_version,
        VersionDescription="MGN launch template update",
        LaunchTemplateData=launch_template_data,
        ClientToken=str(uuid.uuid4()),
    )
    version = str(response["LaunchTemplateVersion"]["VersionNumber"])
    current_default = get_launch_template_default(ec2_client, launch_template_id)
    current_default_version = str(current_default["VersionNumber"])
    if current_default_version != source_version:
        raise RuntimeError(
            f"Default version for launch template {launch_template_id} changed "
            f"from {source_version} to {current_default_version} after creating "
            f"version {version}; leaving default unchanged"
        )

    ec2_client.modify_launch_template(
        LaunchTemplateId=launch_template_id,
        DefaultVersion=version,
    )
    return version


def inspect_server(
    mgn_client,
    ec2_client,
    server: dict[str, Any],
    args: argparse.Namespace,
    family_map: dict[str, str],
    launch_template_id_re: re.Pattern[str] | None,
) -> dict[str, Any]:
    source_server_id = server["sourceServerID"]
    result: dict[str, Any] = {
        "sourceServerID": source_server_id,
        "names": source_server_names(server),
        "applicationID": server.get("applicationID"),
        "archived": bool(server.get("isArchived")),
        "status": "pending",
    }

    launch_config = mgn_client.get_launch_configuration(sourceServerID=source_server_id)
    launch_template_id = launch_config.get("ec2LaunchTemplateID")
    result["launchTemplateId"] = launch_template_id

    if not launch_template_id:
        result["status"] = "skipped"
        result["reason"] = "no-ec2-launch-template-id"
        return result

    if launch_template_id_re and not launch_template_id_re.search(launch_template_id):
        result["status"] = "skipped"
        result["reason"] = "launch-template-id-filter"
        return result

    default_version = get_launch_template_default(ec2_client, launch_template_id)
    source_version = str(default_version["VersionNumber"])
    result["sourceVersion"] = source_version

    launch_template_data = default_version.get("LaunchTemplateData") or {}
    if launch_template_data.get("InstanceType"):
        result["instanceType"] = launch_template_data.get("InstanceType")

    launch_template_updates, changes, skipped_reasons = build_launch_template_updates(
        launch_template_data=launch_template_data,
        args=args,
        family_map=family_map,
    )
    result["changes"] = changes

    if not changes:
        result["status"] = "skipped"
        result["reason"] = ", ".join(skipped_reasons) or "no-matching-launch-template-settings"
        return result

    if not args.execute:
        result["status"] = "would-update"
        return result

    new_version = create_and_default_launch_template_version(
        ec2_client=ec2_client,
        launch_template_id=launch_template_id,
        source_version=source_version,
        launch_template_data=launch_template_updates,
    )
    result["status"] = "updated"
    result["newDefaultVersion"] = new_version
    return result


def print_text_report(results: list[dict[str, Any]]) -> None:
    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1

    print("Summary:")
    for status in sorted(counts):
        print(f"  {status}: {counts[status]}")
    print()

    for result in results:
        source_id = result["sourceServerID"]
        template_id = result.get("launchTemplateId") or "-"
        version = result.get("sourceVersion") or "-"
        instance_type = result.get("instanceType") or "-"
        names = ", ".join(result.get("names") or []) or "-"
        print(
            f"{result['status']}: {source_id} names={names} "
            f"lt={template_id} version={version} instance={instance_type}"
        )
        if result.get("reason"):
            print(f"  reason: {result['reason']}")
        for change in result.get("changes") or []:
            print(f"  {change['path']}: {change['from']} -> {change['to']}")
        if result.get("newDefaultVersion"):
            print(f"  new default version: {result['newDefaultVersion']}")


def main() -> int:
    args = parse_args()
    if bool(args.from_subnet) != bool(args.to_subnet):
        raise SystemExit("--from-subnet and --to-subnet must be supplied together")
    if args.from_subnet and args.from_subnet == args.to_subnet:
        raise SystemExit("--from-subnet and --to-subnet must be different")
    if not args.from_subnet and not args.upgrade_instance_types:
        raise SystemExit("Supply --from-subnet/--to-subnet, --upgrade-instance-types, or both")

    family_map = parse_family_map(args.family_map)
    tag_filters = parse_tag_filters(args.tag)
    source_id_re = compile_regex(args.source_server_id_regex, "--source-server-id-regex")
    name_re = compile_regex(args.server_name_regex, "--server-name-regex")
    launch_template_id_re = compile_regex(args.launch_template_id_regex, "--launch-template-id-regex")

    try:
        session = make_session(args)
        mgn_client = session.client("mgn")
        ec2_client = session.client("ec2")

        results: list[dict[str, Any]] = []
        for server in describe_all_source_servers(mgn_client):
            matches, reason = source_server_matches(
                server=server,
                args=args,
                tag_filters=tag_filters,
                source_id_re=source_id_re,
                name_re=name_re,
            )
            if not matches:
                results.append(
                    {
                        "sourceServerID": server.get("sourceServerID"),
                        "names": source_server_names(server),
                        "applicationID": server.get("applicationID"),
                        "archived": bool(server.get("isArchived")),
                        "status": "skipped",
                        "reason": reason,
                    }
                )
                continue

            try:
                results.append(inspect_server(mgn_client, ec2_client, server, args, family_map, launch_template_id_re))
            except (BotoCoreError, ClientError, RuntimeError) as exc:
                results.append(
                    {
                        "sourceServerID": server.get("sourceServerID"),
                        "names": source_server_names(server),
                        "applicationID": server.get("applicationID"),
                        "archived": bool(server.get("isArchived")),
                        "status": "error",
                        "reason": str(exc),
                    }
                )

        if args.output == "json":
            print(json.dumps(results, indent=2, default=json_default))
        else:
            print_text_report(results)

    except (BotoCoreError, ClientError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
