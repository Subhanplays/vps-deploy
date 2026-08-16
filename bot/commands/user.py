"""User-facing commands: /create, /list, /ssh, /help, /about, /ping."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import database.models as dbm
from ui.buttons import DashboardView, CreateSetupView, HelpView, _OwnerCheckView
from ui.embeds import status_text
from ui.selects import VPSSelect


class SSHFlowView(_OwnerCheckView):
    """Pick a VPS, then generate a fresh SSH session (DM only)."""

    def __init__(self, app, user: discord.User, vps_list: list[dict]):
        super().__init__(user_id=user.id)
        self.app = app
        self.user = user
        self.vps_id: str | None = None
        self.add_item(VPSSelect(self, vps_list))
        self.add_item(SSHGenerateButton(self, row=4))

    async def render(self, interaction: discord.Interaction) -> None:
        embed = self.app.embeds.primary(
            title=self.app.embeds.text("ssh_title", "SSH Access"),
            description="Choose a VPS and a fresh SSH session will be sent to your DMs.",
        )
        await self._update(interaction, embed, self)

    async def send(self, interaction: discord.Interaction) -> None:
        embed = self.app.embeds.primary(
            title=self.app.embeds.text("ssh_title", "SSH Access"),
            description="Choose a VPS and a fresh SSH session will be sent to your DMs.",
        )
        await interaction.response.send_message(embed=embed, view=self, ephemeral=True)


class SSHGenerateButton(discord.ui.Button):
    def __init__(self, view, row=4):
        self._view = view
        super().__init__(label="Generate SSH", style=discord.ButtonStyle.success, emoji="🔐", row=row)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.view.user_id:
            await interaction.response.send_message("These controls belong to someone else.", ephemeral=True)
            return
        if not self.view.vps_id:
            await interaction.response.send_message("Select a VPS first.", ephemeral=True)
            return
        vps = dbm.get_vps(self.view.app.db, self.view.vps_id, self.view.user_id)
        if not vps:
            await interaction.response.send_message("VPS not found.", ephemeral=True)
            return
        await self.view.app.views.do_ssh(self.view, interaction, vps)


class UserCommands(commands.Cog):
    def __init__(self, bot: commands.Bot, app):
        self.bot = bot
        self.app = app

    # ------------------------------------------------------------------
    @app_commands.command(name="create", description="Deploy a brand new VPS")
    async def create(self, interaction: discord.Interaction):
        emb = self.app.embeds
        embed = emb.primary(
            title=emb.text("create_select_os_title", "Create a VPS"),
            description=emb.text("create_select_os_desc", "Select an operating system to get started."),
        )
        await interaction.response.send_message(embed=embed, view=CreateSetupView(self.app, interaction.user), ephemeral=True)

    # ------------------------------------------------------------------
    @app_commands.command(name="list", description="List all of your VPS instances")
    async def list_vps(self, interaction: discord.Interaction):
        vps_list = dbm.get_user_vps(self.app.db, interaction.user.id)
        emb = self.app.embeds
        if not vps_list:
            await interaction.response.send_message(
                embed=emb.error(description=emb.text("list_empty", "You have no VPS instances. Use /create to deploy one.")),
                ephemeral=True,
            )
            return
        embed = emb.primary(title=emb.text("list_title", "Your VPS Instances"))
        for vps in vps_list[:25]:
            image = self.app.settings.image_by_key(vps["os"])
            os_name = image["name"] if image else vps["os"]
            embed.add_field(
                name=f"{status_text(vps['status'], vps['suspended'])} {vps['name']}",
                value=f"{os_name}\n{vps['ram']:g} GB • {vps['cpu']:g} CPU • {vps['disk']:g} GB",
                inline=False,
            )

        class _OpenDashboard(discord.ui.View):
            def __init__(self, app, user):
                super().__init__(timeout=300)
                self.app = app
                self.user = user

            @discord.ui.button(label="Open Dashboard", style=discord.ButtonStyle.primary, emoji="🖥️")
            async def open_dash(self, inter, _):
                if inter.user.id != self.user.id:
                    await inter.response.send_message("This belongs to someone else.", ephemeral=True)
                    return
                await DashboardView(self.app, self.user.id, self.user).render(inter)

        await interaction.response.send_message(embed=embed, view=_OpenDashboard(self.app, interaction.user), ephemeral=True)

    # ------------------------------------------------------------------
    @app_commands.command(name="ssh", description="Generate a fresh SSH session for a VPS")
    async def ssh(self, interaction: discord.Interaction):
        vps_list = dbm.get_user_vps(self.app.db, interaction.user.id)
        if not vps_list:
            await interaction.response.send_message(
                embed=self.app.embeds.error(description="You have no VPS instances."), ephemeral=True
            )
            return
        if len(vps_list) == 1:
            vps = vps_list[0]
            if vps["status"] != "running":
                await interaction.response.send_message(
                    embed=self.app.embeds.error_message("ssh", "The VPS must be running to generate SSH."), ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            ok, _, ssh_line = await self.app.vps.regenerate_ssh(vps["id"], interaction.user.id)
            if not ok:
                await interaction.followup.send(embed=self.app.embeds.error_message("ssh", ssh_line or "Failed."), ephemeral=True)
                return
            sent = await self.app.views.dm_ssh(interaction.user, ssh_line)
            note = (self.app.embeds.text("ssh_dm_sent", "New SSH session sent to your DMs.")
                    if sent else self.app.embeds.text("ssh_dm_failed", "Could not send to DMs."))
            await interaction.followup.send(embed=self.app.embeds.success(title="🔐 SSH", description=note), ephemeral=True)
            return
        await SSHFlowView(self.app, interaction.user, vps_list).send(interaction)

    # ------------------------------------------------------------------
    @app_commands.command(name="help", description="Open the interactive help center")
    async def help_cmd(self, interaction: discord.Interaction):
        await HelpView(self.app, interaction.user.id).send(interaction)

    # ------------------------------------------------------------------
    @app_commands.command(name="about", description="About this service")
    async def about(self, interaction: discord.Interaction):
        settings = self.app.settings
        emb = self.app.embeds
        name = settings.brand_name()
        embed = emb.primary(
            title=emb.text("about_title", "About {name}", name=name),
            description=emb.text("about_desc", "{description}", description=settings.get_str("branding.description", "A modern Discord VPS management platform.")),
        )
        embed.add_field(name="🖥️ Platform", value=f"**{name}**, version {self.app.version}", inline=True)
        embed.add_field(name="⚙️ Framework", value="discord.py", inline=True)
        embed.add_field(name="🖥️ Backend", value="LXD", inline=True)
        embed.add_field(name="💾 Database", value="SQLite", inline=True)
        embed.add_field(name="👨‍💻 Developer", value=settings.get_str("branding.developer_text", "Operated by YourBrand"), inline=False)
        links = []
        website = settings.get_str("branding.website")
        if website:
            links.append(f"[Website]({website})")
        support = settings.get_str("branding.support_server")
        if support:
            links.append(f"[Support]({support})")
        docs = settings.get_str("branding.docs_url")
        if docs:
            links.append(f"[Documentation]({docs})")
        if links:
            embed.add_field(name="🔗 Links", value=" • ".join(links), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    @app_commands.command(name="ping", description="Check the bot's latency")
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        embed = self.app.embeds.success(
            title=self.app.embeds.text("ping_title", "Pong!"),
            description=self.app.embeds.text("ping_desc", "Latency: **{latency}ms**", latency=latency),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(UserCommands(bot, bot.app))