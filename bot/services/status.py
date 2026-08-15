"""Dynamic presence rotation and live status reconciliation."""

from __future__ import annotations

import asyncio
import logging

import discord

import database.models as dbm

logger = logging.getLogger("vpsbot.status")


class StatusService:
    def __init__(self, app):
        self.app = app
        self.bot = None  # set at startup
        self.settings = app.settings
        self.db = app.db
        self.vps = app.vps
        self._index = 0
        self._closed = False

    def attach(self, bot) -> None:
        self.bot = bot

    async def close(self) -> None:
        self._closed = True

    # ------------------------------------------------------------------
    # Presence rotation
    # ------------------------------------------------------------------
    def _counts(self) -> dict:
        return {
            "vps_count": dbm.count_vps(self.db),
            "running_vps": dbm.count_running_vps(self.db),
            "user_count": dbm.count_users(self.db),
            "banned_count": len(dbm.list_bans(self.db)),
        }

    def _build_activity(self, template: str) -> discord.Activity:
        counts = self._counts()
        try:
            text = template.format_map(counts)
        except (KeyError, ValueError):
            text = template
        activity_type = self.settings.status_activity()
        if activity_type == "watching":
            return discord.Activity(type=discord.ActivityType.watching, name=text)
        if activity_type == "listening":
            return discord.Activity(type=discord.ActivityType.listening, name=text)
        if activity_type == "streaming":
            return discord.Activity(type=discord.ActivityType.streaming, name=text, url="https://www.twitch.tv/")
        if activity_type == "competing":
            return discord.Activity(type=discord.ActivityType.competing, name=text)
        return discord.Activity(type=discord.ActivityType.playing, name=text)

    async def rotate_once(self) -> None:
        if self.bot is None:
            return
        rotation_enabled = self.settings.get_bool("status_rotation.enabled", True)
        messages = self.settings.get_list("status_rotation.messages") if rotation_enabled else []
        fallback = self.settings.get_str("bot.status", "your VPS")
        status_type = self.settings.status_type()
        try:
            presence = discord.Status(status_type)
        except ValueError:
            presence = discord.Status.online
        if messages:
            template = messages[self._index % len(messages)]
            self._index += 1
            await self.bot.change_presence(
                status=presence,
                activity=self._build_activity(template),
            )
        else:
            await self.bot.change_presence(
                status=presence,
                activity=self._build_activity(fallback),
            )

    async def run(self) -> None:
        """Background loop - rotates presence on the configured interval."""
        if self.bot is None:
            return
        await self.bot.wait_until_ready()
        await self.rotate_once()
        interval = max(5, self.settings.get_int("status_rotation.interval", self.settings.get_int("system.status_interval", 30)))
        while not self._closed:
            await asyncio.sleep(interval)
            if self._closed:
                break
            try:
                await self.rotate_once()
            except Exception:  # noqa: BLE001
                logger.exception("Status rotation failed")

    # ------------------------------------------------------------------
    # Status reconciliation loop
    # ------------------------------------------------------------------
    async def sync_loop(self) -> None:
        """Background loop - reconciles DB status with real container state."""
        if self.bot is None:
            return
        await self.bot.wait_until_ready()
        interval = max(15, self.settings.get_int("system.sync_interval", 60))
        while not self._closed:
            await asyncio.sleep(interval)
            if self._closed:
                break
            try:
                changed = await self.vps.sync_statuses()
                if changed:
                    logger.info("Reconciled %s VPS statuses", changed)
            except Exception:  # noqa: BLE001
                logger.exception("Status sync loop failed")
