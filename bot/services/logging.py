"""Audit logging: structured file logs + optional Discord log channel.

Every meaningful action is recorded in the ``audit_logs`` table and forwarded
to a configurable Discord channel when one is set (``access.log_channel``).
"""

from __future__ import annotations

import asyncio
import json
import logging

import database.models as dbm

logger = logging.getLogger("vpsbot.audit")


class AuditLogger:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.settings = app.settings

    # ------------------------------------------------------------------
    # Core recording
    # ------------------------------------------------------------------
    def record(self, action: str, details: str = "", user_id: int | None = None, **extra) -> None:
        payload = {"action": action, "user_id": user_id, "details": details, **extra}
        logger.info("AUDIT %s", json.dumps(payload, ensure_ascii=False))
        dbm.add_audit_log(self.db, user_id=user_id, action=action, details=details)
        self._dispatch_discord(action, details, user_id)

    def log_action(self, vps_id: str, user_id: int, action: str, details: str = "") -> None:
        self.record(action, f"{details} | vps={vps_id}", user_id)

    def log_vps_created(self, user_id: int, name: str, os_key: str, ram: float, cpu: float, disk: float, *, status: str = "running") -> None:
        details = f"name={name} os={os_key} ram={ram}g cpu={cpu} disk={disk}g status={status}"
        self.record("vps_created", details, user_id, name=name, os=os_key, ram=ram, cpu=cpu, disk=disk)

    def log_vps_deleted(self, vps_id: str, user_id: int, name: str) -> None:
        self.record("vps_deleted", f"name={name} vps={vps_id}", user_id)

    def log_vps_failed(self, vps_id: str, error: str) -> None:
        self.record("vps_failed", f"vps={vps_id} error={error}")

    def log_status_change(self, vps_id: str, old: str, new: str) -> None:
        self.record("vps_status_change", f"vps={vps_id} {old} -> {new}")

    def log_admin(self, admin_id: int, action: str, details: str = "") -> None:
        self.record(f"admin_{action}", details, admin_id)

    # ------------------------------------------------------------------
    # Discord log channel dispatch
    # ------------------------------------------------------------------
    def _dispatch_discord(self, action: str, details: str, user_id: int | None) -> None:
        channel_id = self.settings.get_int("access.log_channel", 0) or self.settings.get_int("logging.discord_channel", 0)
        bot = getattr(self.app, "bot", None)
        if not channel_id or bot is None:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._post(channel_id, action, details, user_id))
        except RuntimeError:
            pass

    async def _post(self, channel_id: int, action: str, details: str, user_id: int | None) -> None:
        bot = getattr(self.app, "bot", None)
        if bot is None:
            return
        channel = bot.get_channel(channel_id)
        if channel is None:
            return
        embed = self.app.embeds.info(
            title=f"📋 {action.upper().replace('_', ' ')}",
            description=f"**User:** <@{user_id}>\n```\n{details}\n```",
        )
        try:
            await channel.send(embed=embed)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to send audit log to channel %s", channel_id)
