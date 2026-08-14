"""Generate a unique cloud-init configuration for every deployed VM.

Every VPS gets its own meta-data, user-data and a seed ISO produced with
``cloud-localds``. No passwords are ever hard-coded; access is delivered
through a freshly generated SSH key and tmate.
"""

import os
import subprocess

from config import config


def build_user_data(vps_id, vm_name, ssh_pubkey):
    """Return the #cloud-config payload for a single VPS."""
    lines = [
        "#cloud-config",
        f"hostname: {vm_name}",
        "manage_etc_hosts: true",
        "disable_root: false",
        "ssh_pwauth: false",
        "package_update: true",
        "packages:",
        "  - openssh-server",
        "  - curl",
        "  - wget",
        "  - sudo",
        "  - ca-certificates",
        "  - tmate",
        "  - qemu-guest-agent",
        "users:",
        f"  - name: {config.initial_user}",
        "    groups: [sudo, users]",
        "    sudo: ALL=(ALL) NOPASSWD:ALL",
        "    shell: /bin/bash",
        "    lock_passwd: true",
        "    ssh_authorized_keys:",
        f"      - {ssh_pubkey}",
        f"  - name: {config.ssh_user}",
        "    lock_passwd: false",
        "    ssh_authorized_keys:",
        f"      - {ssh_pubkey}",
        # NoCloud networking comes from this key when no network-config file
        # is present on the seed.
        "network:",
        "  version: 2",
        "  ethernets:",
        "    any:",
        "      match:",
        '        name: "en*"',
        "      dhcp4: true",
        "write_files:",
        "  - path: /etc/motd",
        "    content: |",
        f"      {config.bot_name} Virtual Private Server",
        f"      VPS ID: {vps_id}",
        "runcmd:",
        "  - [ systemctl, enable, --now, qemu-guest-agent ]",
        f"  - echo 'vpsbot cloud-init complete' > /var/log/vpsbot-init.log",
    ]
    return "\n".join(lines)


def build_meta_data(vm_name):
    return "\n".join(
        [
            "instance-id: " + vm_name,
            "local-hostname: " + vm_name,
        ]
    )


def write_seed(instance_dir, vm_name, ssh_pubkey):
    """Write meta-data + user-data and build seed.iso with cloud-localds."""
    os.makedirs(instance_dir, exist_ok=True)
    vps_id = vm_name.replace(config.vm_prefix + "-", "VPS-")
    user_data = build_user_data(vps_id, vm_name, ssh_pubkey)
    meta_data = build_meta_data(vm_name)

    user_data_path = os.path.join(instance_dir, "user-data")
    meta_data_path = os.path.join(instance_dir, "meta-data")
    seed_path = os.path.join(instance_dir, "seed.iso")

    with open(user_data_path, "w") as fh:
        fh.write(user_data + "\n")
    with open(meta_data_path, "w") as fh:
        fh.write(meta_data + "\n")

    try:
        subprocess.run(
            ["cloud-localds", seed_path, user_data_path, meta_data_path],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(
            "cloud-localds (cloud-image-utils) is required to build cloud-init seeds: %s" % exc
        )
    if not os.path.exists(seed_path):
        raise RuntimeError("cloud-localds did not produce a seed ISO")
    return seed_path
