"""Periodic maintenance: prune audit logs, clean orphaned containers."""

from __future__ import annotations

import asyncio
import logging

import database.models as dbm

logger = logging.getLogger("vpsbot.cleanup")


class CleanupService:
    def __init__(self, app):
        self.app = app
        self.bot = None
        self.db = app.db
        self.settings = app.settings
        self.vps = app.vps
        self.docker = app.docker
        self._closed = False

    def attach(self, bot) -> None:
        self.bot = bot

    async def close(self) -> None:
        self._closed = True

    async def run(self) -> None:
        if self.bot is None:
            return
        await self.bot.wait_until_ready()
        interval = max(60, self.settings.get_int("system.cleanup_interval", 600))
        while not self._closed:
            await asyncio.sleep(interval)
            if self._closed:
                break
            try:
                await self.cleanup_once()
            except Exception:  # noqa: BLE001
                logger.exception("Cleanup loop failed")

    async def cleanup_once(self) -> dict:
        result: dict = {"pruned_logs": 0, "orphans_removed": 0}
        try:
            keep_days = self.settings.get_int("system.audit_log_retention_days", 90)
            result["pruned_logs"] = dbm.prune_audit_logs(self.db, keep_days)
        except Exception:  # noqa: BLE001
            logger.exception("Audit log pruning failed")

        # Remove containers that no longer have a database record (safety net).
        try:
            known = {
                v["container_id"]
                for v in dbm.get_all_vps(self.db)
                if v.get("container_id")
            }
            listed = await self._list_containers()
            for container_id in listed:
                if container_id not in known and container_id.startswith(self.settings.get_str("docker.container_prefix", "vps")):
                    result["orphans_removed"] += 1
                    await self.docker.remove(container_id)
        except Exception:  # noqa: BLE001
            logger.exception("Orphan container cleanup failed")
        return result

    async def _list_containers(self) -> list[str]:
        res = await self.docker._run(
            ["ps", "-aq", "--filter", "label=managed-by=vpsbot"], timeout=30.0
        )
        if not res.ok:
            return []
        return [line for line in res.stdout.splitlines() if line]

    async def cleanup_ui(self, user_id: int) -> dict:
        """Admin-triggered cleanup (also removes dangling data rows)."""
        return await self.cleanup_once()
