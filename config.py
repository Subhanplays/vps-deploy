"""Central configuration loader.

Everything the bot does is driven by this file. All values can be
overridden in a .env file next to the project, or via the host_settings
table at runtime (see database.py). Nothing in the bot is hard-coded to a
specific provider name so it stays fully white-label.
"""

import os
import re

from dotenv import load_dotenv

load_dotenv()


def _int(key, default):
    try:
        return int(os.getenv(key, str(default)).strip())
    except (TypeError, ValueError):
        return int(default)


def _bool(key, default="false"):
    value = os.getenv(key, str(default)).strip().lower()
    return value in ("1", "true", "yes", "on")


def parse_size(value):
    """Parse a human readable size string into GiB (float).

    "120GB"  -> 120.0
    "500MB"  -> 0.49
    "2TB"    -> 2048.0
    "1.5G"   -> 1.5
    "100"    -> 100.0   (assumed GiB)
    """
    if isinstance(value, (int, float)):
        return float(value)
    match = re.match(r"^(\d+(?:\.\d+)?)\s*(K|M|G|T)?B?$", str(value).strip().upper())
    if not match:
        return 0.0
    number = float(match.group(1))
    unit = match.group(2) or "G"
    multiplier = {"K": 1 / (1024 ** 2), "M": 1 / 1024, "G": 1, "T": 1024}[unit]
    return round(number * multiplier, 2)


class Config:
    def __init__(self):
        # ------------------------------------------------------------------
        # Discord / security
        # ------------------------------------------------------------------
        self.token = os.getenv("DISCORD_TOKEN", "")
        self.admin_role_id = os.getenv("ADMIN_ROLE_ID", "")
        self.admin_user_ids = set()
        for raw in os.getenv("ADMIN_USER_IDS", "").split(","):
            raw = raw.strip()
            if raw.lstrip("-").isdigit():
                self.admin_user_ids.add(int(raw))
        # Compatibility with the old single-file bot.
        if os.getenv("ADMIN_ID", "").strip().lstrip("-").isdigit():
            self.admin_user_ids.add(int(os.getenv("ADMIN_ID")))

        # ------------------------------------------------------------------
        # White-label branding. A provider only has to change these values.
        # ------------------------------------------------------------------
        self.bot_name = os.getenv("BOT_NAME", "MyHost")
        self.bot_status = os.getenv("BOT_STATUS", "Powered by MyHost")
        self.bot_activity_type = os.getenv("BOT_ACTIVITY_TYPE", "Watching").strip().lower()
        self.bot_footer = os.getenv("BOT_FOOTER", "MyHost VPS")
        self.bot_author = os.getenv("BOT_AUTHOR", self.bot_name)
        self.bot_emoji = os.getenv("BOT_EMOJI", "🖥️")
        self.bot_logo = os.getenv("BOT_LOGO", "")
        self.bot_website = os.getenv("BOT_WEBSITE", "")
        self.bot_support = os.getenv("BOT_SUPPORT_SERVER", "")
        self.bot_stream_url = os.getenv("BOT_STREAM_URL", self.bot_website or "https://example.com")
        try:
            self.bot_color = int(os.getenv("BOT_COLOR", "#5865F2").lstrip("#"), 16)
        except ValueError:
            self.bot_color = 0x5865F2

        # ------------------------------------------------------------------
        # RAM overcommit / overselling (admin controlled)
        # ------------------------------------------------------------------
        self.ram_overcommit = _bool("RAM_OVERCOMMIT")
        self.max_allocated_ram = parse_size(os.getenv("MAX_ALLOCATED_RAM", "120GB"))

        # ------------------------------------------------------------------
        # CPU overcommit (admin controlled)
        # ------------------------------------------------------------------
        self.cpu_overcommit = _bool("CPU_OVERCOMMIT")
        self.max_allocated_cpu = _int("MAX_ALLOCATED_CPU", 32)

        # ------------------------------------------------------------------
        # Per-VPS limits
        # ------------------------------------------------------------------
        self.max_vps_per_user = _int("MAX_VPS_PER_USER", 3)
        self.max_disk_per_vps = parse_size(os.getenv("MAX_DISK_PER_VPS", "500GB"))
        self.max_ram_per_vps = parse_size(os.getenv("MAX_RAM_PER_VPS", "120GB"))
        self.max_cpu_per_vps = _int("MAX_CPU_PER_VPS", 16)

        # ------------------------------------------------------------------
        # Storage paths (must exist / be writable on the KVM host)
        # ------------------------------------------------------------------
        self.data_dir = os.getenv("DATA_DIR", "/var/lib/vpsbot")
        self.storage_dir = os.getenv("STORAGE_DIR", "/var/lib/vpsbot/disks")
        self.instances_dir = os.getenv("INSTANCES_DIR", "/var/lib/vpsbot/instances")
        self.keys_dir = os.getenv("KEYS_DIR", "/var/lib/vpsbot/keys")
        self.images_dir = os.getenv("IMAGES_DIR", "/var/lib/vpsbot/images")
        self.ubuntu_image = os.getenv(
            "UBUNTU_IMAGE", "/var/lib/vpsbot/images/ubuntu/ubuntu.qcow2"
        )
        self.debian_image = os.getenv(
            "DEBIAN_IMAGE", "/var/lib/vpsbot/images/debian/debian.qcow2"
        )
        self.database_file = os.getenv("DATABASE_FILE", "/var/lib/vpsbot/vpsbot.db")

        self.ssh_priv_key = os.getenv("SSH_PRIVATE_KEY", os.path.join(self.keys_dir, "id_vpsbot"))
        self.ssh_pub_key = self.ssh_priv_key + ".pub"

        # ------------------------------------------------------------------
        # VM defaults
        # ------------------------------------------------------------------
        self.vm_prefix = os.getenv("VM_PREFIX", "vps")
        self.hostname_prefix = os.getenv("HOSTNAME_PREFIX", "vps")
        # One of: NETWORK_NAME (libvirt NAT) or VM_BRIDGE (bridge).
        self.network_name = os.getenv("NETWORK_NAME", "default")
        self.bridge_name = os.getenv("VM_BRIDGE", "")
        self.ssh_user = os.getenv("SSH_USER", "root")
        self.initial_user = os.getenv("INITIAL_USER", "user")

        # ------------------------------------------------------------------
        # Virtualization behaviour
        # ------------------------------------------------------------------
        self.tmate_enabled = _bool("TMATE_ENABLED", "true")
        self.deploy_timeout = _int("DEPLOY_TIMEOUT", 600)
        self.rate_limit_seconds = _int("RATE_LIMIT_SECONDS", 60)

        # QEMU software emulation (TCG) fallback for hosts without /dev/kvm
        # (containers, sandboxes, VPSs without nested virtualization). These
        # are still real QEMU VMs, just slow and clearly labelled as such.
        self.allow_software_emulation = _bool("ALLOW_SOFTWARE_EMULATION", "true")
        # Force software emulation even when KVM is present (testing).
        self.force_software_emulation = _bool("FORCE_SOFTWARE_EMULATION", "false")
        # libvirt connection URI. Use qemu:///session in containers/sandboxes
        # where the system libvirtd service is not available.
        self.libvirt_uri = os.getenv("LIBVIRT_URI", "qemu:///system")
        # VM backend: auto | virsh | direct.
        #  - 'virsh'  : manage VMs through the libvirt daemon (virsh).
        #  - 'direct' : launch QEMU processes directly (no libvirt daemon),
        #               the most reliable option inside containers/sandboxes
        #               that lack /dev/kvm AND a libvirt daemon.
        #  - 'auto'   : use virsh when it works, otherwise fall back to direct.
        self.virt_backend = os.getenv("VIRT_BACKEND", "auto").strip().lower()
        # Fixed guest IP assigned by QEMU user-mode networking (SLIRP), used
        # only in software emulation mode.
        self.slirp_ip = os.getenv("SLIRP_IP", "10.0.2.15")

        self.image_map = {
            "ubuntu": self.ubuntu_image,
            "debian": self.debian_image,
        }
        self.os_names = {
            "ubuntu": "Ubuntu 24.04",
            "debian": "Debian 12",
        }


config = Config()
