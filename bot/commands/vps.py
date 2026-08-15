"""The /vps command - opens the interactive dashboard."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ui.buttons import DashboardView


class VPSCommands(commands.Cog):
    def __init__(self, bot: commands.Bot, app):
        self.bot = bot
        self.app = app

    @app_commands.command(name="vps", description="Open your interactive VPS dashboard")
    async def vps(self, interaction: discord.Interaction):
        await DashboardView(self.app, interaction.user.id, interaction.user).send(interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(VPSCommands(bot, bot.app))