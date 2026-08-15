"""The bot's design system - every embed flows through this builder.

Colors, footer, author, thumbnail, image and timestamp are all driven by
``appearance`` / ``branding`` configuration, so the whole bot re-skins itself
without touching code.
"""

from __future__ import annotations

import discord

STATUS_META = {
    "running": ("🟢", "Running"),
    "stopped": ("🔴", "Stopped"),
    "starting": ("🟡", "Starting"),
    "stopping": ("🟠", "Stopping"),
    "reinstalling": ("🔵", "Reinstalling"),
    "creating": ("🟣", "Creating"),
    "suspended": ("⚠️", "Suspended"),
    "error": ("❌", "Error"),
}


def status_text(status: str, suspended: bool = False) -> str:
    if suspended:
        return f"{STATUS_META['suspended'][0]} Suspended"
    emoji, label = STATUS_META.get(status, ("⬜", status.title()))
    return f"{emoji} {label}"


class EmbedBuilder:
    def __init__(self, settings):
        self.settings = settings

    # ------------------------------------------------------------------
    # Core builder
    # ------------------------------------------------------------------
    def base(self, kind: str = "primary", *, title: str | None = None,
             description: str | None = None, **kwargs) -> discord.Embed:
        embed = discord.Embed(
            color=self.settings.color(kind),
            title=title,
            description=description,
            **kwargs,
        )
        appearance = self.settings.get("appearance", {})
        branding = self.settings.get("branding", {})

        if appearance.get("author_name") or branding.get("name"):
            embed.set_author(
                name=appearance.get("author_name") or branding.get("name", ""),
                icon_url=appearance.get("author_icon") or branding.get("avatar_url") or None,
            )
        if appearance.get("thumbnail_url"):
            embed.set_thumbnail(url=appearance["thumbnail_url"])
        if appearance.get("image_url"):
            embed.set_image(url=appearance["image_url"])

        footer_text = self.settings.footer()
        footer_icon = appearance.get("footer_icon") or branding.get("avatar_url") or None
        embed.set_footer(text=footer_text, icon_url=footer_icon or None)

        if appearance.get("show_timestamp", True):
            embed.timestamp = discord.utils.utcnow()
        return embed

    def primary(self, **kwargs) -> discord.Embed:
        return self.base("primary", **kwargs)

    def success(self, **kwargs) -> discord.Embed:
        return self.base("success", **kwargs)

    def error(self, **kwargs) -> discord.Embed:
        return self.base("error", **kwargs)

    def warning(self, **kwargs) -> discord.Embed:
        return self.base("warning", **kwargs)

    def info(self, **kwargs) -> discord.Embed:
        return self.base("info", **kwargs)

    # ------------------------------------------------------------------
    # Text helpers (configurable copy)
    # ------------------------------------------------------------------
    def text(self, key: str, default: str = "", **kwargs) -> str:
        return self.settings.text(key, default, **kwargs)

    # ------------------------------------------------------------------
    # Domain embeds
    # ------------------------------------------------------------------
    def error_message(self, action: str, reason: str | None = None) -> discord.Embed:
        desc = self.text("error_desc", "{action} failed.", action=action)
        if reason:
            desc += f"\n\n**{self.text('error_reason', 'Reason')}:**\n{reason}"
        desc += f"\n\n{self.text('error_contact', 'Please contact support if this continues.')}"
        return self.error(title=self.text("error_title", "Something went wrong"), description=desc)

    def resource_embed(self, *, title: str, description: str = "", kind: str = "primary",
                       os_name: str | None = None, ram: float | None = None, cpu: float | None = None,
                       disk: float | None = None, status: str | None = None, ssh: str | None = None,
                       extra_fields: list[tuple[str, str, bool]] | None = None) -> discord.Embed:
        embed = self.base(kind, title=title, description=description)
        fields = []
        if os_name:
            fields.append(("🖥️ OS", os_name, True))
        if ram:
            fields.append(("🧠 RAM", f"{ram:g} GB", True))
        if cpu:
            fields.append(("⚡ CPU", f"{cpu:g} Cores", True))
        if disk:
            fields.append(("💾 Disk", f"{disk:g} GB", True))
        if status:
            fields.append(("🟢 Status", status, True))
        for name, value, inline in extra_fields or []:
            fields.append((name, value, inline))
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
        if ssh:
            embed.add_field(name=self.text("vps_created_label", "SSH Access"), value=f"`{ssh}`", inline=False)
        return embed
