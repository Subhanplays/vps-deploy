"""Administrator commands: /admin dashboard + focused admin actions."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import database.models as dbm
from ui.buttons import AdminDashboardView, ConfirmView, SettingsView
from ui.embeds import status_text
from commands.settings import SETTING_GROUPS


async def _require_admin(interaction: discord.Interaction, app) -> bool:
    if app.settings.is_admin(interaction):
        return True
    await interaction.response.send_message(
        embed=app.embeds.error(description="This command is restricted to administrators only."),
        ephemeral=True,
    )
    return False


class AdminCommands(commands.Cog):
    def __init__(self, bot: commands.Bot, app):
        self.bot = bot
        self.app = app

    admin = app_commands.Group(name="admin", description="Hosting control panel", guild_only=True)

    # ------------------------------------------------------------------
    @admin.command(name="dashboard", description="Open the hosting control panel")
    async def admin_dashboard(self, interaction: discord.Interaction):
        if not await _require_admin(interaction, self.app):
            return
        await AdminDashboardView(self.app, interaction.user).send(interaction)

    # ------------------------------------------------------------------
    @admin.command(name="ban", description="Ban a user from using the bot")
    @app_commands.describe(target_user="The user to ban", reason="Optional reason")
    async def admin_ban(self, interaction: discord.Interaction, target_user: discord.User, reason: str = ""):
        if not await _require_admin(interaction, self.app):
            return
        if target_user.id in self.app.settings.admin_user_ids():
            await interaction.response.send_message(
                embed=self.app.embeds.error(description="You cannot ban an administrator."), ephemeral=True
            )
            return
        dbm.add_ban(self.app.db, target_user.id, reason=reason, banned_by=interaction.user.id)
        self.app.audit.log_admin(interaction.user.id, "ban", f"user={target_user.id} reason={reason}")
        await interaction.response.send_message(
            embed=self.app.embeds.success(title="🛡️ User Banned", description=f"<@{target_user.id}> has been banned."),
            ephemeral=True,
        )

    @admin.command(name="unban", description="Unban a user")
    @app_commands.describe(target_user="The user to unban")
    async def admin_unban(self, interaction: discord.Interaction, target_user: discord.User):
        if not await _require_admin(interaction, self.app):
            return
        dbm.remove_ban(self.app.db, target_user.id)
        self.app.audit.log_admin(interaction.user.id, "unban", f"user={target_user.id}")
        await interaction.response.send_message(
            embed=self.app.embeds.success(title="✅ User Unbanned", description=f"<@{target_user.id}> has been unbanned."),
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    @admin.command(name="delete", description="Delete all VPS instances for a user")
    @app_commands.describe(target_user="The target user")
    async def admin_delete(self, interaction: discord.Interaction, target_user: discord.User):
        if not await _require_admin(interaction, self.app):
            return
        vps_list = dbm.get_user_vps(self.app.db, target_user.id)
        if not vps_list:
            await interaction.response.send_message(
                embed=self.app.embeds.info(description=f"<@{target_user.id}> has no VPS instances."), ephemeral=True
            )
            return
        emb = self.app.embeds
        embed = emb.warning(
            title="🗑️ Delete all VPS?",
            description=(
                f"This will permanently delete **{len(vps_list)}** VPS instance(s) "
                f"belonging to <@{target_user.id}>.\n\nThis action **cannot be undone**."
            ),
        )

        async def on_confirm(inter: discord.Interaction):
            await inter.response.defer(ephemeral=True)
            deleted = 0
            for vps in vps_list:
                await self.app.vps.delete(vps["id"], target_user.id)
                deleted += 1
            self.app.audit.log_admin(interaction.user.id, "delete_user", f"user={target_user.id} count={deleted}")
            await inter.edit_original_response(
                embed=emb.success(title="🗑️ User Cleaned", description=f"Deleted **{deleted}** VPS instances for <@{target_user.id}>."),
                view=None,
            )

        await ConfirmView(
            user_id=interaction.user.id,
            embed=embed,
            confirm_label="Delete All",
            confirm_style=discord.ButtonStyle.danger,
            on_confirm=on_confirm,
        ).send(interaction)

    # ------------------------------------------------------------------
    @admin.command(name="kill", description="Stop every running VPS on the host")
    async def admin_kill(self, interaction: discord.Interaction):
        if not await _require_admin(interaction, self.app):
            return
        running = [v for v in dbm.get_all_vps(self.app.db) if v["status"] == "running" and v.get("container_id")]
        emb = self.app.embeds
        if not running:
            await interaction.response.send_message(
                embed=emb.info(description="There are no running VPS instances."), ephemeral=True
            )
            return
        embed = emb.warning(
            title="⛔ Stop all running VPS?",
            description=f"This will stop **{len(running)}** running VPS instance(s).\nInstances stay intact and can be started again later.",
        )

        async def on_confirm(inter: discord.Interaction):
            await inter.response.defer(ephemeral=True)
            stopped = 0
            for vps in running:
                await self.app.lxd.stop(vps["container_id"])
                dbm.update_vps_status(self.app.db, vps["id"], "stopped")
                stopped += 1
            self.app.audit.log_admin(interaction.user.id, "kill_all", f"stopped={stopped}")
            await inter.edit_original_response(
                embed=emb.success(title="⛔ Kill All", description=f"Stopped **{stopped}** running VPS instances."), view=None
            )

        await ConfirmView(
            user_id=interaction.user.id,
            embed=embed,
            confirm_label="Stop All",
            confirm_style=discord.ButtonStyle.danger,
            on_confirm=on_confirm,
        ).send(interaction)

    # ------------------------------------------------------------------
    @admin.command(name="stats", description="Hosting statistics")
    async def admin_stats(self, interaction: discord.Interaction):
        if not await _require_admin(interaction, self.app):
            return
        await interaction.response.defer(ephemeral=True)
        totals = dbm.allocated_totals(self.app.db)
        host = await self.app.stats.host_resources()
        emb = self.app.embeds
        embed = emb.info(title="📊 Statistics")
        embed.add_field(name="👥 Users", value=dbm.count_users(self.app.db), inline=True)
        embed.add_field(name="🖥️ Total VPS", value=totals["count"], inline=True)
        embed.add_field(name="🟢 Running", value=totals["running"], inline=True)
        embed.add_field(name="⚡ CPU Allocated", value=f"{totals['cpu']:g} cores", inline=True)
        embed.add_field(name="🧠 RAM Allocated", value=f"{totals['ram']:g} GB", inline=True)
        embed.add_field(name="💾 Disk Allocated", value=f"{totals['disk']:g} GB", inline=True)
        embed.add_field(name="🛡️ Banned", value=len(dbm.list_bans(self.app.db)), inline=True)
        embed.add_field(name="🖥️ Host CPU", value=str(host["cpus"]), inline=True)
        embed.add_field(name="🧠 Host RAM", value=self.app.stats.format_gb(host["mem_total_gb"]), inline=True)
        embed.add_field(name="💾 Host Disk Free", value=self.app.stats.format_gb(host["disk_free_gb"]), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin.command(name="resources", description="Host resources and allocation overview")
    async def admin_resources(self, interaction: discord.Interaction):
        if not await _require_admin(interaction, self.app):
            return
        await interaction.response.defer(ephemeral=True)
        host = await self.app.stats.host_resources()
        allocated = dbm.running_allocated(self.app.db)
        limits = self.app.resources.config_limits()
        emb = self.app.embeds
        embed = emb.info(title="🛠️ Resources")
        embed.add_field(name="🖥️ Host CPU", value=f"{host['cpus']} cores", inline=True)
        embed.add_field(name="🧠 Host RAM", value=self.app.stats.format_gb(host["mem_total_gb"]), inline=True)
        embed.add_field(name="💾 Host Disk Free", value=self.app.stats.format_gb(host["disk_free_gb"]), inline=True)
        embed.add_field(name="⚡ CPU In Use", value=f"{allocated['cpu']:g} cores", inline=True)
        embed.add_field(name="🧠 RAM In Use", value=f"{allocated['ram']:g} GB", inline=True)
        embed.add_field(name="💾 Disk In Use", value=f"{allocated['disk']:g} GB", inline=True)
        embed.add_field(name="🚫 Max CPU", value=f"{limits['max_cpu']:g}", inline=True)
        embed.add_field(name="🚫 Max RAM", value=f"{limits['max_ram']:g} GB", inline=True)
        embed.add_field(name="🚫 Max Disk", value=f"{limits['max_disk']:g} GB", inline=True)
        embed.add_field(name="🚫 Global Limit", value=str(limits["global_limit"]), inline=True)
        embed.add_field(name="🚫 Per User", value=str(limits["max_per_user"]), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    @admin.command(name="logs", description="Recent audit logs")
    async def admin_logs(self, interaction: discord.Interaction):
        if not await _require_admin(interaction, self.app):
            return
        embed = self.app.embeds.primary(title="📋 Audit Logs")
        logs = dbm.recent_audit_logs(self.app.db, 30)
        for entry in logs:
            embed.add_field(
                name=f"`{entry['created_at']}` {entry['action']}",
                value=f"<@{entry['user_id']}> • {entry['details'] or '—'}",
                inline=False,
            )
        if not logs:
            embed.description = "No audit logs yet."
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    @admin.command(name="settings", description="Open the bot settings panel")
    async def admin_settings(self, interaction: discord.Interaction):
        if not await _require_admin(interaction, self.app):
            return
        await SettingsView(self.app, interaction.user.id, SETTING_GROUPS).send(interaction)

    # ------------------------------------------------------------------
    @admin.command(name="user", description="Inspect a user and their VPS")
    @app_commands.describe(target_user="The target user")
    async def admin_user(self, interaction: discord.Interaction, target_user: discord.User):
        if not await _require_admin(interaction, self.app):
            return
        vps_list = dbm.get_user_vps(self.app.db, target_user.id)
        embed = self.app.embeds.primary(title=f"👤 {target_user}")
        embed.add_field(name="ID", value=f"`{target_user.id}`", inline=True)
        embed.add_field(name="Total VPS", value=str(len(vps_list)), inline=True)
        if self.app.settings.is_banned(target_user.id):
            embed.add_field(name="🛡️ Status", value="Banned", inline=True)
        if not vps_list:
            embed.add_field(name="VPS", value="None", inline=False)
        for vps in vps_list[:10]:
            embed.add_field(
                name=f"{status_text(vps['status'], vps['suspended'])} {vps['name']}",
                value=f"{vps['ram']:g} GB • {vps['cpu']:g} CPU • {vps['disk']:g} GB\n`{vps['id'][:8]}…`",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin.command(name="vps", description="Inspect a VPS by its ID")
    @app_commands.describe(vps_id="The VPS UUID (full or short)")
    async def admin_vps(self, interaction: discord.Interaction, vps_id: str):
        if not await _require_admin(interaction, self.app):
            return
        candidates = [v for v in dbm.get_all_vps(self.app.db) if v["id"].startswith(vps_id.lower())]
        if len(candidates) != 1:
            await interaction.response.send_message(
                embed=self.app.embeds.error(description="VPS not found. Provide a full or unique short ID."), ephemeral=True
            )
            return
        vps = candidates[0]
        image = self.app.settings.image_by_key(vps["os"])
        os_name = image["name"] if image else vps["os"]
        emb = self.app.embeds
        embed = emb.primary(title=f"🖥️ {vps['name']}")
        embed.add_field(name="Owner", value=f"<@{vps['user_id']}>", inline=True)
        embed.add_field(name="OS", value=os_name, inline=True)
        embed.add_field(name="Status", value=status_text(vps["status"], vps["suspended"]), inline=True)
        embed.add_field(name="RAM", value=f"{vps['ram']:g} GB", inline=True)
        embed.add_field(name="CPU", value=f"{vps['cpu']:g}", inline=True)
        embed.add_field(name="Disk", value=f"{vps['disk']:g} GB", inline=True)
        embed.add_field(name="Instance", value=f"`{vps['container_id'] or '—'}`", inline=False)
        embed.add_field(name="Created", value=vps["created_at"], inline=True)
        embed.add_field(name="ID", value=f"`{vps['id']}`", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCommands(bot, bot.app))