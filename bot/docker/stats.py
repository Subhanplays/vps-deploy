"""Statistics, uptime, logs and host resource reporting."""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone

logger = logging.getLogger("vpsbot.docker")


class StatsService:
    def __init__(self, app=None):
        self.docker = getattr(app, "docker", None)

    # ------------------------------------------------------------------
    # Container stats
    # ------------------------------------------------------------------
    async def container_stats(self, container_id: str) -> dict:
        result = await self.docker._run(
            [
                "stats", "--no-stream", "--format",
                "{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.NetIO}}",
                container_id,
            ],
            timeout=30.0,
        )
        if not result.ok:
            return {"cpu": "N/A", "mem": "N/A", "net": "N/A"}
        parts = result.stdout.split("\t")
        if len(parts) >= 4:
            return {
                "cpu": parts[0],
                "mem": parts[1],
                "mem_perc": parts[2],
                "net": parts[3],
            }
        return {"cpu": "N/A", "mem": "N/A", "net": "N/A"}

    async def uptime(self, container_id: str) -> str:
        started = await self.docker.started_at(container_id)
        if not started:
            return "Not running"
        try:
            start = datetime.fromisoformat(started.replace("Z", "+00:00"))
            uptime = datetime.now(timezone.utc) - start
            days = uptime.days
            hours, rem = divmod(uptime.seconds, 3600)
            minutes, _ = divmod(rem, 60)
            return f"{days}d {hours}h {minutes}m"
        except (ValueError, TypeError):
            return "Unknown"

    async def logs(self, container_id: str, lines: int = 50) -> str:
        result = await self.docker._run(
            ["logs", "--tail", str(max(1, min(int(lines), 200))), container_id],
            timeout=30.0,
        )
        if not result.ok:
            return "Failed to fetch logs."
        content = result.stdout or result.stderr
        return content[-3500:]

    # ------------------------------------------------------------------
    # Host resources
    # ------------------------------------------------------------------
    async def host_resources(self) -> dict:
        """Physical host capacity. Returns zeros/None when unavailable."""
        result = await self.docker._run(
            ["info", "--format", "{{json .}}"], timeout=30.0
        )
        info = {}
        if result.ok:
            try:
                info = json.loads(result.stdout)
            except json.JSONDecodeError:
                logger.warning("Could not parse `docker info` JSON output")

        cpus = info.get("NCPU")
        mem_total = info.get("MemTotal")
        docker_root = info.get("DockerRootDir", ".")

        disk_total = disk_free = None
        try:
            usage = shutil.disk_usage(docker_root)
            disk_total = usage.total / (1024 ** 3)
            disk_free = usage.free / (1024 ** 3)
        except OSError:
            try:
                usage = shutil.disk_usage(".")
                disk_total = usage.total / (1024 ** 3)
                disk_free = usage.free / (1024 ** 3)
            except OSError:
                pass

        return {
            "cpus": float(cpus) if isinstance(cpus, (int, float)) else None,
            "mem_total_gb": (float(mem_total) / (1024 ** 3)) if isinstance(mem_total, (int, float)) else None,
            "disk_total_gb": disk_total,
            "disk_free_gb": disk_free,
            "driver": info.get("Driver", "unknown"),
            "os": info.get("OperatingSystem", "unknown"),
            "kernel": info.get("KernelVersion", "unknown"),
            "docker_root": docker_root,
        }

    def format_gb(self, value: float | None) -> str:
        if value is None:
            return "N/A"
        return f"{value:.1f} GB"
