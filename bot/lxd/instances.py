"""Instance creation with honest resource limits.

LXD system containers provide a full, isolated OS per user - the closest thing
to a dedicated VPS a host can give.

- RAM and CPU are enforced via ``limits.memory`` and ``limits.cpu``.
- Disk is enforced via the root device ``size=`` when the storage pool driver
  supports quotas (zfs/btrfs/lvm). When it cannot (e.g. ``dir`` pools, or a
  host where quota support is unavailable) creation falls back cleanly and
  reports that disk is advisory, so we never advertise a limit that is fake.
"""

from __future__ import annotations

import logging

from .manager import LxdManager, LxdResult

logger = logging.getLogger("vpsbot.lxd")

# Storage drivers that honour root device `size=` quotas for instances.
_QUOTA_DRIVERS = {"zfs", "btrfs", "lvm"}

# Error signatures that mean "the daemon cannot enforce the storage quota"
# even though the pool driver nominally supports it.
_QUOTA_FAILURE_MARKERS = (
    "quota",
    "root disk size",
    "not supported",
    "operation not permitted",
    "cannot set size",
)


def _has_flag(args: list[str], flag: str) -> bool:
    return any(arg == flag or arg.startswith(f"{flag}=") for arg in args)


def _quota_failed(result: LxdResult) -> bool:
    stderr = (result.stderr or "").lower()
    return any(marker in stderr for marker in _QUOTA_FAILURE_MARKERS)


def _size_value(gb: float) -> str:
    return f"{gb:g}GiB"


def _cpu_value(cpu: float) -> str:
    if cpu.is_integer():
        return str(int(cpu))
    # Fractional cores must be expressed as a percentage in LXD.
    return f"{int(round(cpu * 100))}%"


class InstanceSpec:
    def __init__(
        self,
        *,
        image: str,
        name: str,
        hostname: str,
        ram_gb: float,
        cpu: float,
        disk_gb: float,
        autostart: bool = True,
        storage_quota_enabled: bool = True,
        storage_pool: str = "default",
        profiles: list[str] | None = None,
        security_privileged: bool = False,
    ):
        self.image = image
        self.name = name
        self.hostname = hostname
        self.ram_gb = ram_gb
        self.cpu = cpu
        self.disk_gb = disk_gb
        self.autostart = autostart
        self.storage_quota_enabled = storage_quota_enabled
        self.storage_pool = storage_pool
        self.profiles = profiles or ["default"]
        self.security_privileged = security_privileged


def build_launch_args(spec: InstanceSpec, *, with_quota: bool = True) -> list[str]:
    config: dict[str, str] = {
        "limits.memory": _size_value(spec.ram_gb),
        "limits.cpu": _cpu_value(spec.cpu),
    }
    if spec.autostart:
        config["boot.autostart"] = "true"
    if spec.security_privileged:
        config["security.privileged"] = "true"

    args = ["launch", spec.image, spec.name]
    for key, value in config.items():
        args.extend(["--config", f"{key}={value}"])
    if with_quota and spec.storage_quota_enabled:
        args.extend(["--device", f"root,size={_size_value(spec.disk_gb)}"])
    for profile in spec.profiles:
        args.extend(["--profile", profile])
    return args


async def create_instance(lxd: LxdManager, spec: InstanceSpec) -> tuple[str | None, bool, str]:
    """Create the instance.

    Returns ``(instance_name, disk_enforced, error_message)``.
    """
    pool = await lxd.storage_pool(pool=spec.storage_pool)
    driver = pool.get("driver", "unknown")
    pool_supports = driver in _QUOTA_DRIVERS
    logger.info("LXD storage driver '%s' - disk quota: %s", driver, pool_supports)

    used_args = build_launch_args(spec, with_quota=True)
    result = await lxd.launch(used_args)
    if (
        not result.ok
        and _has_flag(used_args, "--device")
        and _quota_failed(result)
    ):
        logger.warning("LXD cannot enforce the disk quota on this host; retrying without it.")
        # A failed `lxc launch` may leave a partial instance registered under
        # the requested name - clear it so the retry does not hit a conflict.
        await lxd.delete(spec.name, force=True)
        used_args = build_launch_args(spec, with_quota=False)
        result = await lxd.launch(used_args)

    disk_enforced = _has_flag(used_args, "--device") and pool_supports

    if not result.ok:
        return None, disk_enforced, result.message or "LXD failed to create the instance."
    return spec.name, disk_enforced, ""
