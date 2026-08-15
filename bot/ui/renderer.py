"""Shared rendering helpers used by interactive views.

Keeps embed-building for statistics/info/logs and admin panels in one place so
the views stay small and consistent.
"""

from __future__ import annotations

import discord

import database.models as dbm
from ui.buttons import ConfirmView
from ui.embeds import status_text


class ViewRenderer:
    def __init__(self, app):
        self.app = app

    # ------------------------------------------------------------------
    # SSH helpers
    # ------------------------------------------------------------------
    async def dm_ssh(self, user: discord.User, ssh_line: str) -> bool:
        emb = self.app.embeds
        embed = emb.success(
            title=emb.text("ssh_title", "SSH Access"),
            description=f"```\n{ssh_line}\n```\n\n⚠️ {emb.text('ssh_warning', 'Keep this information private.')}",
        )
        try:
            await user.send(embed=embed)
            return True
        except discord.Forbidden:
            return False

    async def try_followup(self, interaction: discord.Interaction, text: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content=text, ephemeral=True)
            else:
                await interaction.response.send_message(content=text, ephemeral=True)
        except Exception:  # noqa: BLE001
            pass

    def _keep_view(self, view):
        """Rebuild a dashboard view so buttons persist after a panel swap."""
        if hasattr(view, "build_view"):
            return view.build_view()
        return view

    # ------------------------------------------------------------------
    # VPS panels
    # ------------------------------------------------------------------
    async def render_stats(self, view, interaction: discord.Interaction, vps: dict) -> None:
        await interaction.response.defer(ephemeral=True)
        snapshot = await self.app.vps.info_snapshot(vps)
        stats = snapshot["stats"]
        emb = self.app.embeds
        embed = emb.info(title=f"{emb.text('stats_title', 'Live Statistics')} • {vps['name']}")
        embed.add_field(name="⚡ CPU Usage", value=stats.get("cpu", "N/A"), inline=True)
        embed.add_field(name="🧠 Memory", value=stats.get("mem", "N/A"), inline=True)
        embed.add_field(name="📡 Network", value=stats.get("net", "N/A"), inline=True)
        embed.add_field(name="⏱️ Uptime", value=snapshot.get("uptime", "N/A"), inline=True)
        embed.add_field(name="🟢 Status", value=status_text(vps["status"], vps["suspended"]), inline=True)
        await interaction.edit_original_response(embed=embed, view=self._keep_view(view))

    async def render_info(self, view, interaction: discord.Interaction, vps: dict) -> None:
        await interaction.response.defer(ephemeral=True)
        snapshot = await self.app.vps.info_snapshot(vps)
        stats = snapshot["stats"]
        image = self.app.settings.image_by_key(vps["os"])
        os_name = image["name"] if image else vps["os"]
        emb = self.app.embeds
        embed = emb.primary(title=f"{emb.text('info_title', 'VPS Information')} • {vps['name']}")
        embed.add_field(name="🖥️ OS", value=os_name, inline=True)
        embed.add_field(name="🟢 Status", value=status_text(vps["status"], vps["suspended"]), inline=True)
        embed.add_field(name="🏷️ Hostname", value=f"`{vps['hostname']}`", inline=True)
        embed.add_field(name="🧠 RAM", value=f"{vps['ram']:g} GB", inline=True)
        embed.add_field(name="⚡ CPU", value=f"{vps['cpu']:g} Cores", inline=True)
        embed.add_field(name="💾 Disk", value=f"{vps['disk']:g} GB", inline=True)
        embed.add_field(name="⚡ CPU Usage", value=stats.get("cpu", "N/A"), inline=True)
        embed.add_field(name="🧠 RAM Usage", value=stats.get("mem", "N/A"), inline=True)
        embed.add_field(name="📡 Network", value=stats.get("net", "N/A"), inline=True)
        embed.add_field(name="⏱️ Uptime", value=snapshot.get("uptime", "N/A"), inline=True)
        embed.add_field(name="🗓️ Created", value=vps["created_at"], inline=True)
        embed.add_field(name="🆔 ID", value=f"`{vps['id'][:8]}…`", inline=True)
        embed.add_field(name="🛠️ Container", value=f"`{vps['container_id'][:12]}…`" if vps["container_id"] else "—", inline=False)
        await interaction.edit_original_response(embed=embed, view=self._keep_view(view))

    async def render_logs(self, view, interaction: discord.Interaction, vps: dict) -> None:
        await interaction.response.defer(ephemeral=True)
        if not vps["container_id"]:
            await interaction.edit_original_response(
                embed=self.app.embeds.error_message("logs", "No container exists for this VPS."),
                view=self._keep_view(view),
            )
            return
        logs = await self.app.stats.logs(vps["container_id"], 60)
        emb = self.app.embeds
        embed = emb.info(title=f"{emb.text('logs_title', 'VPS Logs')} • {vps['name']}")
        embed.add_field(name="📜 Recent Logs", value=f"```\n{logs}\n```", inline=False)
        await interaction.edit_original_response(embed=embed, view=self._keep_view(view))

    async def do_ssh(self, view, interaction: discord.Interaction, vps: dict) -> None:
        await interaction.response.defer(ephemeral=True)
        emb = self.app.embeds
        if vps["status"] != "running":
            await interaction.edit_original_response(
                embed=emb.error_message("ssh", "The VPS must be running to access SSH."),
                view=self._keep_view(view),
            )
            return
        ssh_line = vps["ssh_command"]
        if not ssh_line:
            ok, _, ssh_line = await self.app.vps.regenerate_ssh(vps["id"], self.view_owner(view))
            if not ok:
                await interaction.edit_original_response(
                    embed=emb.error_message("ssh", "Failed to generate an SSH session."),
                    view=self._keep_view(view),
                )
                return
        user = await self.app.bot.fetch_user(self.view_owner(view))
        sent = await self.dm_ssh(user, ssh_line)
        note = emb.text("ssh_dm_sent", "New SSH session sent to your DMs.") if sent else emb.text("ssh_dm_failed", "Could not send to DMs.")
        await interaction.edit_original_response(
            embed=emb.success(title="🔐 SSH", description=note), view=self._keep_view(view)
        )

    async def do_regen(self, view, interaction: discord.Interaction, vps: dict) -> None:
        await interaction.response.defer(ephemeral=True)
        emb = self.app.embeds
        ok, msg, ssh_line = await self.app.vps.regenerate_ssh(vps["id"], self.view_owner(view))
        if not ok:
            await interaction.edit_original_response(
                embed=emb.error_message("ssh", msg), view=self._keep_view(view)
            )
            return
        user = await self.app.bot.fetch_user(self.view_owner(view))
        sent = await self.dm_ssh(user, ssh_line)
        note = emb.text("ssh_dm_sent", "New SSH session sent to your DMs.") if sent else emb.text("ssh_dm_failed", "Could not send to DMs.")
        await interaction.edit_original_response(
            embed=emb.success(title="🔄 SSH Regenerated", description=note), view=self._keep_view(view)
        )

    async def do_delete(self, view, interaction: discord.Interaction, vps: dict) -> None:
        emb = self.app.embeds
        owner_id = self.view_owner(view)
        embed = emb.warning(
            title=emb.text("confirm_delete_title", "Delete VPS?"),
            description=emb.text(
                "confirm_delete_desc",
                "This permanently removes:\n\n**{name}**\nRAM: {ram}\nCPU: {cpu} cores\nDisk: {disk}\n\nThis action **cannot be undone**.",
                name=vps["name"],
                ram=f"{vps['ram']:g} GB",
                cpu=f"{vps['cpu']:g}",
                disk=f"{vps['disk']:g} GB",
            ),
        )

        async def on_confirm(inter: discord.Interaction):
            await inter.response.defer(ephemeral=True)
            ok, msg = await self.app.vps.delete(vps["id"], owner_id)
            if ok:
                await inter.edit_original_response(
                    embed=emb.success(title=emb.text("vps_deleted_title", "VPS Deleted"), description=msg), view=None
                )
            else:
                await inter.edit_original_response(embed=emb.error_message("delete", msg), view=None)

        async def on_cancel(inter: discord.Interaction):
            if hasattr(view, "render"):
                await view.render(inter)
            else:
                await inter.response.defer(ephemeral=True)
                await inter.edit_original_response(embed=self.app.embeds.primary(description="Cancelled."), view=None)

        confirm = ConfirmView(
            user_id=owner_id,
            embed=embed,
            confirm_label="Delete VPS",
            confirm_style=discord.ButtonStyle.danger,
            on_confirm=on_confirm,
            on_cancel=on_cancel,
        )
        await view._update(interaction, embed, confirm)

    def view_owner(self, view) -> int:
        return getattr(view, "user_id", 0)

    # ------------------------------------------------------------------
    # Admin panels
    # ------------------------------------------------------------------
    async def render_users(self, view, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = self.app.db.query(
            """
            SELECT u.user_id, u.username, COUNT(v.id) AS total_vps,
                   COALESCE(SUM(CASE WHEN v.status = 'running' THEN 1 ELSE 0 END), 0) AS running_vps,
                   EXISTS(SELECT 1 FROM bans b WHERE b.user_id = u.user_id) AS banned
            FROM users u LEFT JOIN vps v ON u.user_id = v.user_id
            GROUP BY u.user_id, u.username
            ORDER BY total_vps DESC
            """
        )
        embed = self.app.embeds.primary(title="👥 Users")
        for row in rows[:25]:
            embed.add_field(
                name=f"{'🛡️' if row['banned'] else ''} {row['username']}",
                value=f"Total VPS: {row['total_vps']} • Running: {row['running_vps']} • <@{row['user_id']}>",
                inline=False,
            )
        if not rows:
            embed.description = "No users found."
        embed.set_footer(text=f"{self.app.settings.footer()} • Showing {len(rows[:25])} of {len(rows)}")
        await interaction.edit_original_response(embed=embed, view=self._keep_view(view))

    async def render_all_vps(self, view, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = dbm.get_all_vps(self.app.db)
        embed = self.app.embeds.primary(title="🖥️ All VPS Instances")
        for row in rows[:20]:
            embed.add_field(
                name=f"{status_text(row['status'], row['suspended'])} {row['name']} ({row['owner_name']})",
                value=f"{row['ram']:g} GB • {row['cpu']:g} CPU • {row['disk']:g} GB\n`{row['id'][:8]}…`",
                inline=True,
            )
        if not rows:
            embed.description = "No VPS instances."
        embed.set_footer(text=f"{self.app.settings.footer()} • {len(rows)} total")
        await interaction.edit_original_response(embed=embed, view=self._keep_view(view))

    async def render_admin_stats(self, view, interaction: discord.Interaction) -> None:
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
        embed.add_field(name="🧩 Driver", value=host["driver"], inline=True)
        await interaction.edit_original_response(embed=embed, view=self._keep_view(view))

    async def render_bans(self, view, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        bans = dbm.list_bans(self.app.db)
        embed = self.app.embeds.primary(title="🛡️ Banned Users")
        for ban in bans[:25]:
            embed.add_field(
                name=f"<@{ban['user_id']}>",
                value=f"Reason: {ban['reason'] or '—'}\nSince: {ban['created_at']}",
                inline=False,
            )
        if not bans:
            embed.description = "No banned users."
        await interaction.edit_original_response(embed=embed, view=self._keep_view(view))

    async def render_audit_logs(self, view, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        logs = dbm.recent_audit_logs(self.app.db, 30)
        embed = self.app.embeds.primary(title="📋 Audit Logs")
        for entry in logs[:30]:
            embed.add_field(
                name=f"`{entry['created_at']}` {entry['action']}",
                value=f"User: <@{entry['user_id']}> • {entry['details'] or '—'}",
                inline=False,
            )
        if not logs:
            embed.description = "No audit logs yet."
        await interaction.edit_original_response(embed=embed, view=self._keep_view(view))
