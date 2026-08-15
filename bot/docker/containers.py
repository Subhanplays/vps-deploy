"""Container creation with honest resource limits.

RAM and CPU are enforced directly by Docker via ``--memory`` and ``--cpus``.
Disk is enforced with ``--storage-opt size=`` when the host storage driver
supports it; when it does not, creation falls back cleanly and reports that
disk is advisory so we never *advertise* a limit that is not real.
"""

from __future__ import annotations

import logging

from .manager import DockerManager, DockerResult

logger = logging.getLogger("vpsbot.docker")

# Storage drivers that honour `--storage-opt size=` for container quotas.
_SIZE_OPT_DRIVERS = {"zfs", "btrfs", "devicemapper"}

# Error signatures that mean "the daemon cannot enforce the storage quota"
# even though the driver nominally supports `--storage-opt size=` (e.g. btrfs
# needs quota support on the filesystem, which is not always enabled).
_QUOTA_FAILURE_MARKERS = (
    "storage driver does not support",
    "operation not permitted",
    "quota",
    "not supported",
)


def _quota_failed(result: DockerResult) -> bool:
    stderr = (result.stderr or "").lower()
    return any(marker in stderr for marker in _QUOTA_FAILURE_MARKERS)


def _has_flag(args: list[str], flag: str) -> bool:
    return any(arg == flag or arg.startswith(f"{flag}=") for arg in args)


class ContainerSpec:
    def __init__(
        self,
        *,
        image: str,
        name: str,
        hostname: str,
        ram_gb: float,
        cpu: float,
        disk_gb: float,
        restart_policy: str = "unless-stopped",
        privileged: bool = True,
        cap_add: list[str] | None = None,
        storage_opt_enabled: bool = True,
        entrypoint: list[str] | None = None,
        extra_args: list[str] | None = None,
    ):
        self.image = image
        self.name = name
        self.hostname = hostname
        self.ram_gb = ram_gb
        self.cpu = cpu
        self.disk_gb = disk_gb
        self.restart_policy = restart_policy
        self.privileged = privileged
        self.cap_add = cap_add or []
        self.storage_opt_enabled = storage_opt_enabled
        self.entrypoint = entrypoint or ["tail", "-f", "/dev/null"]
        self.extra_args = extra_args or []


def _memory_bytes(ram_gb: float) -> int:
    return max(int(round(ram_gb * 1024 ** 3)), 4 * 1024 * 1024)


def _disk_bytes(disk_gb: float) -> int:
    return max(int(round(disk_gb * 1024 ** 3)), 16 * 1024 * 1024)


def build_run_args(spec: ContainerSpec, *, with_storage_opt: bool = True) -> list[str]:
    args = [
        "run", "-d",
        "--name", spec.name,
        "--hostname", spec.hostname,
        "--memory", str(_memory_bytes(spec.ram_gb)),
        "--memory-swap", str(_memory_bytes(spec.ram_gb)),
        "--cpus", str(spec.cpu),
        "--restart", spec.restart_policy,
        "--label", "managed-by=vpsbot",
    ]
    if spec.privileged:
        args.append("--privileged")
    for cap in spec.cap_add:
        args.extend(["--cap-add", cap])
    if with_storage_opt and spec.storage_opt_enabled:
        args.extend(["--storage-opt", f"size={_disk_bytes(spec.disk_gb)}"])
    args.extend(spec.extra_args)
    args.append(spec.image)
    args.extend(spec.entrypoint)
    return args


async def ensure_image(docker: DockerManager, image: str) -> DockerResult | None:
    """Pull the image if it is not already present. Returns None on success."""
    if await docker.image_exists(image):
        return None
    logger.info("Pulling image %s", image)
    result = await docker.pull_image(image)
    if not result.ok:
        return result
    return None


async def create_container(docker: DockerManager, spec: ContainerSpec) -> tuple[str | None, bool, str]:
    """Create the container.

    Returns ``(container_id, disk_enforced, error_message)``.
    """
    if pull_error := await ensure_image(docker, spec.image):
        return None, False, pull_error.message or "Failed to pull image."

    driver = await _storage_driver(docker)
    driver_supports = driver in _SIZE_OPT_DRIVERS
    logger.info("Docker storage driver '%s' - storage-opt disk quota: %s", driver, driver_supports)

    used_args = build_run_args(spec, with_storage_opt=True)
    result = await docker.run_container(used_args)
    if (
        not result.ok
        and _has_flag(used_args, "--storage-opt")
        and _quota_failed(result)
    ):
        logger.warning("Storage quota cannot be enforced on this host; retrying without it.")
        # A failed `docker run` may leave a container registered under the
        # requested name - clear it so the retry does not hit a name conflict.
        await docker._run(["rm", "-f", spec.name], timeout=30.0)
        used_args = build_run_args(spec, with_storage_opt=False)
        result = await docker.run_container(used_args)

    disk_enforced = _has_flag(used_args, "--storage-opt") and driver_supports

    if not result.ok:
        return None, disk_enforced, result.message or "Docker failed to create the container."
    return result.stdout, disk_enforced, ""


async def _storage_driver(docker: DockerManager) -> str:
    result = await docker._run(["info", "--format", "{{.Driver}}"], timeout=30.0)
    return result.stdout or "unknown"
