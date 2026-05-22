# AWS MGN migration helpers

These playbooks and scripts support migrations from vCenter to AWS EC2 with the AWS Application Migration Service agent.

The normal flow is now Ansible driven:

1. Run preflight checks on every host.
2. Reboot supported Linux hosts that are not running their latest installed kernel.
3. Downgrade Ubuntu 24.04 kernels to the pinned MGN-compatible kernel.
4. Install and run the Linux or Windows AWS MGN agent.
5. After cutover, run the kernel revert flow on Ubuntu hosts that were downgraded.

## Required controller environment

Set short-lived AWS credentials before running the agent playbooks:

```bash
export AWS_REGION=us-east-1
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...
```

`AWS_DEFAULT_REGION` is also accepted when `AWS_REGION` is not set.

## Main playbooks

Run the full migration preparation and agent install flow:

```bash
ansible-playbook -i inventory site.yml
```

Run only the common readiness checks:

```bash
ansible-playbook -i inventory preflight.yml
```

Run Linux preparation only:

```bash
ansible-playbook -i inventory linux_prepare.yml
```

Install only the Linux agent:

```bash
ansible-playbook -i inventory agent.yml
```

Install only the Windows agent:

```bash
ansible-playbook -i inventory windows_agent.yml
```

## Free-space gate

All install paths check that the root drive has enough free space before proceeding:

- Linux: `/`
- Windows: `C:`

The default threshold is `2 GiB`. Override it per run or inventory group:

```bash
ansible-playbook -i inventory site.yml -e mgn_min_root_free_gib=4
```

Consider using a higher value for Ubuntu 24.04 hosts that may need kernel package downloads.

## Ubuntu 24.04 kernel downgrade

`downgrade.yml` now gathers facts and only runs on Ubuntu 24.04 hosts. It copies and runs:

- `downgrade.sh`
- `revert.sh`
- `kernel-grub-reset.sh`

The shell script still validates that the host is Ubuntu noble and records state under `/var/lib/kernel-migration`.

The pinned target kernel remains configured in `downgrade.sh`:

```bash
TARGET_VERSION="6.8.0-88"
```

## Post-migration revert

After cutover, run the revert playbook on hosts that were downgraded:

```bash
ansible-playbook -i inventory revert.yml
```

This restores GRUB behavior, reboots, removes the downgraded kernel packages if this tooling installed them, and reboots again.

## Windows agent install

`windows_agent.yml` replaces the paste-generated PowerShell flow in `win_agent_install/generate_mgn_agent_ps1.sh`.

Optional Windows variables:

- `mgn_installer_dir`, default `C:\Temp\AwsMgnAgent`
- `mgn_installer_url`, default regional AWS URL
- `mgn_user_provided_id`
- `mgn_endpoint`
- `mgn_s3_endpoint`
- `mgn_no_replication`
- `mgn_dualstack`

Credential-bearing Windows and Linux installer execution tasks use `no_log: true`.
