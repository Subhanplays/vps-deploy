"""Discord modal dialogs for structured text input."""

from __future__ import annotations

import discord


class CreateVPSModal(discord.ui.Modal):
    """Resource configuration for a new VPS."""

    def __init__(self, view, *, title: str, default_ram: float, default_cpu: float,
                 default_disk: float, allow_name: bool = True):
        super().__init__(title=title[:45], timeout=300)
        self.view = view
        self.add_item(
            discord.ui.TextInput(
                label="RAM (GB)",
                default=f"{default_ram:g}",
                min_length=1,
                max_length=6,
                required=True,
                placeholder="e.g. 4",
            )
        )
        self.add_item(
            discord.ui.TextInput(
                label="CPU (cores)",
                default=f"{default_cpu:g}",
                min_length=1,
                max_length=4,
                required=True,
                placeholder="e.g. 2",
            )
        )
        self.add_item(
            discord.ui.TextInput(
                label="Disk (GB)",
                default=f"{default_disk:g}",
                min_length=1,
                max_length=6,
                required=True,
                placeholder="e.g. 20",
            )
        )
        if allow_name:
            self.add_item(
                discord.ui.TextInput(
                    label="VPS name (optional)",
                    required=False,
                    max_length=32,
                    placeholder="leave empty to auto-generate",
                )
            )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.view.is_owner(interaction):
            await interaction.response.send_message("This setup belongs to someone else.", ephemeral=True)
            return
        values = {
            "ram": self.children[0].value.strip(),
            "cpu": self.children[1].value.strip(),
            "disk": self.children[2].value.strip(),
            "name": self.children[3].value.strip() if len(self.children) > 3 else "",
        }
        await self.view.on_resources_submitted(interaction, values)


class AdminCreateVPSModal(discord.ui.Modal):
    """Admin: create a VPS for another user with custom resources."""

    def __init__(self, view, *, os_key: str, default_ram: float, default_cpu: float, default_disk: float):
        super().__init__(title=f"Create VPS - {os_key}", timeout=300)
        self.view = view
        self.os_key = os_key
        self.add_item(discord.ui.TextInput(label="Target user ID", required=True, max_length=24, placeholder="Discord user ID"))
        self.add_item(discord.ui.TextInput(label="RAM (GB)", default=f"{default_ram:g}", required=True, max_length=6))
        self.add_item(discord.ui.TextInput(label="CPU (cores)", default=f"{default_cpu:g}", required=True, max_length=4))
        self.add_item(discord.ui.TextInput(label="Disk (GB)", default=f"{default_disk:g}", required=True, max_length=6))
        self.add_item(discord.ui.TextInput(label="VPS name (optional)", required=False, max_length=32))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.view.is_owner(interaction):
            await interaction.response.send_message("This panel belongs to someone else.", ephemeral=True)
            return
        values = {
            "target": self.children[0].value.strip(),
            "ram": self.children[1].value.strip(),
            "cpu": self.children[2].value.strip(),
            "disk": self.children[3].value.strip(),
            "name": self.children[4].value.strip(),
        }
        await self.view.on_resources_submitted(interaction, values)


class SettingsValueModal(discord.ui.Modal):
    """Generic single-value editor for a settings key."""

    def __init__(self, view, *, key: str, label: str, current: str, required: bool = False):
        super().__init__(title=f"Set {label}"[:45], timeout=300)
        self.view = view
        self.key = key
        self.add_item(
            discord.ui.TextInput(
                label=label[:45],
                default=current,
                required=required,
                max_length=2000,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.view.is_owner(interaction):
            await interaction.response.send_message("This panel belongs to someone else.", ephemeral=True)
            return
        await self.view.on_value_submitted(interaction, self.key, self.children[0].value)
