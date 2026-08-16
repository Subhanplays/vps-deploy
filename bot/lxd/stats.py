"""Statistics, uptime, logs and host resource reporting for LXD hosts."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import time

logger = logging.getLogger("vpsbot.lxd")


class LxdStatsService:
    def __init__(self, app=None):
        self.app = app
        self.lxd = getattr(app, "lxd", None)
        self.settings = getattr(app, "settings", None)
        self._cpu_samples: dict[str, tuple[float, int]] = {}

    def _pool_name(self) -> str:
        if self.settings:
            return self.settings.get_str("lxd.storage_pool", "default")
        return "default"

    # ------------------------------------------------------------------
    # Instance stats
    # ------------------------------------------------------------------
    async def container_stats(self, instance: str) -> dict:
        info = await self.lxd.instance_info(instance)
        resources = info.get("resources", {}) or {}

        memory = resources.get("memory", {}) or {}
        usage = memory.get("usage")
        limit = (info.get("config", {}) or {}).get("limits.memory")
        if usage:
            used = usage / (1024 ** 3)
            mem_str = f"{used:.2f}GiB / {limit}" if limit else f"{used:.2f}GiB"
        else:
            mem_str = "N/A"

        cpu_usage = (resources.get("cpu", {}) or {}).get("usage")
        cpu_str = self._cpu_percent(instance, cpu_usage)

        rx = tx = 0
        for _iface, data in (resources.get("network", {}) or {}).items():
            counters = data.get("counters", {}) or {}
            rx += counters.get("bytes_received", 0) or 0
            tx += counters.get("bytes_sent", 0) or 0
        net_str = f"↓ {self._fmt_bytes(rx)} ↑ {self._fmt_bytes(tx)}"

        return {"cpu": cpu_str, "mem": mem_str, "net": net_str}

    def _cpu_percent(self, instance: str, usage_list) -> str:
        if not usage_list:
            return "N/A"
        try:
            total = sum(int(u) for u in usage_list)
            cores = max(1, len(usage_list))
        except (TypeError, ValueError):
            return "N/A"
        now = time.monotonic()
        prev = self._cpu_samples.get(instance)
        self._cpu_samples[instance] = (now, total)
        if prev is None:
            return "—"
        elapsed = now - prev[0]
        delta = total - prev[1]
        if elapsed <= 0 or delta < 0:
            return "—"
        percent = (delta / elapsed) / 1e9 / cores * 100
        return f"{min(percent, 100.0):.1f}%"

    @staticmethod
    def _fmt_bytes(value) -> str:
        n = float(value or 0)
        for unit in ("B", "KiB", "MiB", "GiB"):
            if n < 1024:
                return f"{n:.0f} {unit}"
            n /= 1024
        return f"{n:.1f} TiB"

    async def uptime(self, instance: str) -> str:
        result = await self.lxd.exec(instance, ["cat", "/proc/uptime"], timeout=15.0)
        if not result.ok:
            return "Not running"
        try:
            seconds = float(result.stdout.split()[0])
        except (ValueError, IndexError):
            return "Unknown"
        days, rem = divmod(int(seconds), 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        return f"{days}d {hours}h {minutes}m"

    async def logs(self, instance: str, lines: int = 50) -> str:
        n = max(1, min(int(lines), 200))
        result = await self.lxd.exec(instance, ["journalctl", "-n", str(n), "--no-pager"], timeout=30.0)
        if not result.ok:
            return "Failed to fetch logs."
        content = result.stdout or result.stderr
        return content[-3500:]

    # ------------------------------------------------------------------
    # Host resources
    # ------------------------------------------------------------------
    async def host_resources(self) -> dict:
        """Physical host capacity. Returns None for values we cannot measure."""
        pool = await self.lxd.storage_pool(pool=self._pool_name())
        driver = pool.get("driver", "unknown")
        source = pool.get("source", "")

        cpus = os.cpu_count()

        mem_total = None
        try:
            with open("/proc/meminfo", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        mem_total = int(line.split()[1]) * 1024  # kB -> bytes
                        break
        except OSError:
            pass

        disk_total = disk_free = None
        for path in (source, "/", "."):
            if not path:
                continue
            try:
                usage = shutil.disk_usage(path)
                disk_total = usage.total / (1024 ** 3)
                disk_free = usage.free / (1024 ** 3)
                break
            except OSError:
                continue

        return {
            "cpus": cpus,
            "mem_total_gb": (mem_total / (1024 ** 3)) if mem_total else None,
            "disk_total_gb": disk_total,
            "disk_free_gb": disk_free,
            "driver": driver,
            "pool": self._pool_name(),
            "os": platform.system(),
            "kernel": platform.release(),
        }

    def format_gb(self, value: float | None) -> str:
        if value is None:
            return "N/A"
        return f"{value:.1f} GB"
