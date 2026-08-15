"""Discord select menus (VPS picker, OS picker, plan picker, help picker)."""

from __future__ import annotations

import discord


class VPSSelect(discord.ui.Select):
    """Pick one of the caller's VPS instances."""

    def __init__(self, view, vps_list: list[dict]):
        self.view = view
        options = []
        for vps in vps_list[:25]:
            label = vps["name"][:100]
            desc = f"{vps['status']} • {vps['ram']:g}GB / {vps['cpu']:g} CPU"[:100]
            options.append(discord.SelectOption(label=label, value=vps["id"], description=desc))
        if not options:
            options.append(discord.SelectOption(label="No VPS", value="none", description="You have no VPS yet."))
        super().__init__(
            placeholder="Choose a VPS…",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.view.user_id:
            await interaction.response.send_message("This dashboard belongs to someone else.", ephemeral=True)
            return
        value = self.values[0]
        if value == "none":
            return
        self.view.vps_id = value
        await self.view.render(interaction)


class OSSelect(discord.ui.Select):
    """Pick an operating system image from the configured image map."""

    def __init__(self, view, images: dict, placeholder: str = "Select an operating system…"):
        self.view = view
        options = [
            discord.SelectOption(
                label=image["name"][:100],
                value=key,
                description=f"Image: {image['image']}"[:100],
            )
            for key, image in images.items()
        ]
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        if not self.view.is_owner(interaction):
            await interaction.response.send_message("This setup belongs to someone else.", ephemeral=True)
            return
        await self.view.on_os_selected(interaction, self.values[0])


class PlanSelect(discord.ui.Select):
    """Pick a resource plan, or choose 'custom' to enter your own resources."""

    def __init__(self, view, plans: dict, include_custom: bool = True):
        self.view = view
        options = []
        for key, plan in plans.items():
            options.append(
                discord.SelectOption(
                    label=plan["name"][:100],
                    value=key,
                    description=f"{plan['ram']:g}GB RAM • {plan['cpu']:g} CPU • {plan['disk']:g}GB"[:100],
                )
            )
        if include_custom:
            options.append(discord.SelectOption(label="Custom Resources", value="__custom__", description="Configure RAM, CPU and disk yourself"))
        super().__init__(placeholder="Choose a plan…", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        if not self.view.is_owner(interaction):
            await interaction.response.send_message("This setup belongs to someone else.", ephemeral=True)
            return
        await self.view.on_plan_selected(interaction, self.values[0])


class HelpCategorySelect(discord.ui.Select):
    """Interactive help - choose a category."""

    CATEGORIES = {
        "vps": ("🖥️ VPS Commands", "VPS management and deployment"),
        "access": ("🔐 Access", "SSH access and sessions"),
        "monitoring": ("📊 Monitoring", "Statistics, logs and info"),
        "admin": ("⚙️ Admin", "Administrator controls"),
        "info": ("❓ Information", "About and help"),
    }

    def __init__(self, view):
        self.view = view
        options = [
            discord.SelectOption(label=label, value=key, description=desc[:100])
            for key, (label, desc) in self.CATEGORIES.items()
        ]
        super().__init__(placeholder="Select a category…", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        if not self.view.is_owner(interaction):
            await interaction.response.send_message("This help panel belongs to someone else.", ephemeral=True)
            return
        await self.view.show_category(interaction, self.values[0])
