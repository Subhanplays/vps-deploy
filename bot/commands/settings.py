"""The /settings command - runtime bot configuration for administrators.

Every value is stored in the ``settings`` table and merged over ``config.json``,
so admins can re-brand and re-tune the bot without touching any code.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ui.buttons import SettingsView

SETTING_GROUPS: dict[str, list[tuple[str, str, str]]] = {
    "General": [
        ("branding.name", "Brand name", "str"),
        ("branding.short_name", "Short name", "str"),
        ("branding.description", "Description", "str"),
        ("branding.footer", "Footer text", "str"),
        ("branding.watermark", "Watermark", "str"),
        ("branding.website", "Website URL", "url"),
        ("branding.support_server", "Support server URL", "url"),
        ("branding.docs_url", "Documentation URL", "url"),
        ("branding.invite_url", "Discord invite URL", "url"),
        ("branding.developer_text", "Developer text", "str"),
        ("bot.status", "Status text", "str"),
        ("bot.status_type", "Status type", "choice:playing,watching,listening,streaming,competing"),
        ("bot.online_status", "Online status", "choice:online,idle,dnd,invisible"),
    ],
    "Appearance": [
        ("appearance.primary_color", "Primary color", "color"),
        ("appearance.success_color", "Success color", "color"),
        ("appearance.error_color", "Error color", "color"),
        ("appearance.warning_color", "Warning color", "color"),
        ("appearance.info_color", "Info color", "color"),
        ("appearance.footer_icon", "Footer icon URL", "url"),
        ("appearance.author_name", "Author name", "str"),
        ("appearance.author_icon", "Author icon URL", "url"),
        ("appearance.thumbnail_url", "Thumbnail URL", "url"),
        ("appearance.image_url", "Image URL", "url"),
        ("appearance.show_timestamp", "Show timestamps", "bool"),
    ],
    "VPS": [
        ("vps.default_ram", "Default RAM (GB)", "float"),
        ("vps.default_cpu", "Default CPU cores", "float"),
        ("vps.default_disk", "Default disk (GB)", "float"),
        ("resources.max_ram", "Max RAM (GB)", "float"),
        ("resources.max_cpu", "Max CPU cores", "float"),
        ("resources.max_disk", "Max disk (GB)", "float"),
        ("resources.max_vps_per_user", "Max VPS per user", "int"),
        ("resources.global_vps_limit", "Global VPS limit", "int"),
        ("vps.creation_cooldown", "Creation cooldown (s)", "int"),
        ("vps.allow_custom_name", "Allow custom names", "bool"),
    ],
    "LXD": [
        ("lxd.container_prefix", "Instance prefix", "str"),
        ("vps.hostname_prefix", "Hostname prefix", "str"),
        ("lxd.autostart", "Autostart on boot", "bool"),
        ("lxd.storage_quota", "Enforce disk quota", "bool"),
        ("lxd.security_privileged", "Privileged instance", "bool"),
        ("lxd.storage_pool", "Storage pool name", "str"),
        ("ssh.install_timeout", "Install timeout (s)", "int"),
        ("ssh.session_timeout", "SSH session timeout (s)", "int"),
    ],
    "Access": [
        ("access.admin_ids", "Admin user IDs", "ids"),
        ("access.admin_roles", "Admin role IDs", "ids"),
        ("access.log_channel", "Log channel ID", "int"),
        ("access.support_channel", "Support channel ID", "int"),
        ("access.dm_ssh", "DM SSH credentials", "bool"),
    ],
    "Security": [
        ("security.creation_cooldown", "Creation cooldown (s)", "int"),
        ("security.confirm_destructive", "Require confirmations", "bool"),
        ("system.audit_log_retention_days", "Audit log retention (days)", "int"),
    ],
    "Status Rotation": [
        ("status_rotation.enabled", "Enable rotation", "bool"),
        ("status_rotation.interval", "Rotate interval (s)", "int"),
        ("status_rotation.messages", "Status templates (| separated)", "templates"),
    ],
    "Plans": [
        ("plans.free.enabled", "Free plan enabled", "bool"),
        ("plans.basic.enabled", "Basic plan enabled", "bool"),
        ("plans.pro.enabled", "Pro plan enabled", "bool"),
    ],
}


class SettingsCommands(commands.Cog):
    def __init__(self, bot: commands.Bot, app):
        self.bot = bot
        self.app = app

    @app_commands.command(name="settings", description="Configure the bot at runtime (admins only)")
    @app_commands.guild_only()
    async def settings(self, interaction: discord.Interaction):
        if not self.app.settings.is_admin(interaction):
            await interaction.response.send_message(
                embed=self.app.embeds.error(description="This command is restricted to administrators only."),
                ephemeral=True,
            )
            return
        await SettingsView(self.app, interaction.user.id, SETTING_GROUPS).send(interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(SettingsCommands(bot, bot.app))