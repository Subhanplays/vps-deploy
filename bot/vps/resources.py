"""Resource parsing and validation.

Every VPS allocation is checked twice:

1. against configured limits (``resources.max_*`` / plans) and
2. against the *real* physical host capacity reported by Docker.

Requests are rejected unless the host can genuinely provide them, so we never
advertise resources that do not exist.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import database.models as dbm

_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(K|M|G|T)?B?$", re.IGNORECASE)
_MULT = {"K": 1 / (1024 ** 2), "M": 1 / 1024, "G": 1.0, "T": 1024.0, None: 1.0}


def parse_size(value) -> float:
    """Parse a human readable size into GiB (float). ``"2GB" -> 2.0``."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    match = _SIZE_RE.match(str(value).strip())
    if not match:
        return 0.0
    return round(float(match.group(1)) * _MULT[(match.group(2) or "G").upper()], 3)


def parse_cpu(value) -> float:
    try:
        cpu = float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0
    return round(max(cpu, 0.0), 2)


def format_gb(gb: float) -> str:
    return f"{gb:g} GB"


class ResourceError(Exception):
    """Raised when a resource request is invalid."""


class ResourceValidator:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.settings = app.settings
        self.stats = app.stats

    @staticmethod
    def parse_size(value) -> float:
        return parse_size(value)

    @staticmethod
    def parse_cpu(value) -> float:
        return parse_cpu(value)

    # ------------------------------------------------------------------
    # Config limits
    # ------------------------------------------------------------------
    def config_limits(self) -> dict:
        cfg = self.settings.get("resources", {})
        vps_cfg = self.settings.get("vps", {})
        return {
            "max_ram": parse_size(cfg.get("max_ram", vps_cfg.get("max_ram", "8GB"))),
            "max_cpu": parse_cpu(cfg.get("max_cpu", vps_cfg.get("max_cpu", 4))),
            "max_disk": parse_size(cfg.get("max_disk", vps_cfg.get("max_disk", "100GB"))),
            "max_per_user": int(cfg.get("max_vps_per_user", vps_cfg.get("max_per_user", 2))),
            "global_limit": int(cfg.get("global_vps_limit", vps_cfg.get("global_limit", 50))),
        }

    def validate_spec(self, *, ram: float, cpu: float, disk: float) -> tuple[bool, str]:
        limits = self.config_limits()
        if ram <= 0 or cpu <= 0 or disk <= 0:
            return False, "RAM, CPU and disk must all be greater than zero."
        if cpu > limits["max_cpu"]:
            return False, f"CPU exceeds the maximum of {format_gb(limits['max_cpu'])} cores."
        if ram > limits["max_ram"]:
            return False, f"RAM exceeds the maximum of {format_gb(limits['max_ram'])}."
        if disk > limits["max_disk"]:
            return False, f"Disk exceeds the maximum of {format_gb(limits['max_disk'])}."
        return True, ""

    # ------------------------------------------------------------------
    # User / global limits
    # ------------------------------------------------------------------
    def check_limits(self, *, user_id: int) -> tuple[bool, str]:
        limits = self.config_limits()
        user_count = dbm.count_user_vps(self.db, user_id)
        if user_count >= limits["max_per_user"]:
            return False, f"You have reached the limit of {limits['max_per_user']} VPS instances."
        total = dbm.count_vps(self.db)
        if total >= limits["global_limit"]:
            return False, f"The global VPS limit ({limits['global_limit']}) has been reached."
        return True, ""

    def check_cooldown(self, *, user_id: int) -> tuple[bool, int]:
        cooldown = self.settings.get_int("security.creation_cooldown", self.settings.get_int("vps.creation_cooldown", 0))
        if cooldown <= 0:
            return True, 0
        last = dbm.get_last_create(self.db, user_id)
        if not last:
            return True, 0
        try:
            last_ts = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return True, 0
        remaining = int(cooldown - (time.time() - last_ts))
        if remaining > 0:
            return False, remaining
        return True, 0

    def record_creation(self, *, user_id: int) -> None:
        dbm.set_last_create(self.db, user_id)

    # ------------------------------------------------------------------
    # Host capacity
    # ------------------------------------------------------------------
    async def check_host(self, *, ram: float, cpu: float, disk: float) -> tuple[bool, str]:
        host = await self.stats.host_resources()
        if host["cpus"] is None or host["mem_total_gb"] is None:
            return False, "Host resource information is unavailable. Please contact support."

        cfg = self.settings.get("resources", {})
        headroom = int(cfg.get("host_headroom_percent", 10))
        cpu_over = float(cfg.get("cpu_oversubscribe", 1.0))
        ram_over = float(cfg.get("ram_oversubscribe", 1.0))

        allocated = dbm.running_allocated(self.db)
        usable_cpu = host["cpus"] * cpu_over
        usable_ram = host["mem_total_gb"] * ram_over * (1 - headroom / 100.0)

        avail_cpu = usable_cpu - allocated["cpu"]
        avail_ram = usable_ram - allocated["ram"]

        if cpu > avail_cpu:
            return False, (
                f"Not enough CPU capacity on the host "
                f"({cpu:g} cores requested, ~{avail_cpu:.1f} available)."
            )
        if ram > avail_ram:
            return False, (
                f"Not enough memory on the host "
                f"({format_gb(ram)} requested, ~{format_gb(max(avail_ram, 0))} available)."
            )

        if host["disk_free_gb"] is not None:
            disk_used = allocated["disk"]
            avail_disk = host["disk_free_gb"] - disk_used
            if disk > avail_disk:
                return False, (
                    f"Not enough disk space on the host "
                    f"({format_gb(disk)} requested, ~{format_gb(max(avail_disk, 0))} available)."
                )
        return True, ""

    # ------------------------------------------------------------------
    # Plans
    # ------------------------------------------------------------------
    def plan_spec(self, plan_key: str) -> dict | None:
        plan = self.settings.plan(plan_key)
        if not plan:
            return None
        return {
            "plan": plan_key,
            "name": plan.get("name", plan_key),
            "ram": parse_size(plan.get("ram", 0)),
            "cpu": parse_cpu(plan.get("cpu", 0)),
            "disk": parse_size(plan.get("disk", 0)),
        }
