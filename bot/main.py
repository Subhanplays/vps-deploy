"""VPS Bot v2 - entry point.

Usage:
    python bot/main.py

Expects a `.env` file (see .env.example) with at least DISCORD_TOKEN.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from app import AppContext
from config.settings import BASE_DIR

load_dotenv(BASE_DIR / ".env")


def _setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_file = os.getenv("LOG_FILE", "logs/bot.log")
    if not os.path.isabs(log_file):
        log_file = str((BASE_DIR / log_file).resolve())
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=handlers,
    )


async def main() -> None:
    _setup_logging()
    logger = logging.getLogger("vpsbot")

    app = AppContext()
    token = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")
    if not token:
        logger.error("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)

    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix=discord.utils.MISSING, intents=intents, help_command=None)
    bot.app = app
    app.bot = bot
    app.status.attach(bot)
    app.cleanup.attach(bot)

    cogs = (
        "commands.user",
        "commands.vps",
        "commands.admin",
        "commands.settings",
    )
    for cog in cogs:
        await bot.load_extension(cog)
        logger.info("Loaded cog: %s", cog)

    # Background services
    asyncio.get_running_loop().create_task(app.status.run())
    asyncio.get_running_loop().create_task(app.status.sync_loop())
    asyncio.get_running_loop().create_task(app.cleanup.run())

    @bot.event
    async def on_ready():
        logger.info("Bot ready: %s (guilds=%s)", bot.user, len(bot.guilds))
        try:
            synced = await bot.tree.sync()
            logger.info("Synced %s application commands", len(synced))
        except Exception as exc:  # noqa: BLE001
            logger.error("Command sync failed: %s", exc)

    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        original = getattr(error, "original", error)
        logger.error("Command %s failed for %s: %s",
                     interaction.command.name if interaction.command else "?",
                     interaction.user, original)
        embed = app.embeds.error_message(
            "This action",
            _friendly_error(original),
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send error response")

    @bot.event
    async def on_command_error(ctx, error):
        logger.warning("Prefix command error (not used): %s", error)

    try:
        await bot.start(token)
    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        await app.status.close()
        await app.cleanup.close()
        await bot.close()


def _friendly_error(error: Exception) -> str:
    """Map exceptions to user-safe messages - never leak tracebacks."""
    if isinstance(error, app_commands.CommandOnCooldown):
        return f"You are on cooldown. Try again in {error.retry_after:.1f}s."
    if isinstance(error, app_commands.MissingPermissions):
        return "You do not have permission to use this command."
    if isinstance(error, app_commands.NoPrivateMessage):
        return "This command can only be used in a server."
    if isinstance(error, app_commands.errors.CommandNotFound):
        return "That command does not exist."
    message = str(error)
    if message:
        return message[:400]
    return "An unexpected error occurred. Please try again."


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass