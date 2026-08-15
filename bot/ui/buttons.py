"""Interactive views: dashboards, management panels, confirmations and flows."""

from __future__ import annotations

import discord
import database.models as dbm
from ui.embeds import status_text
from ui.modals import CreateVPSModal, AdminCreateVPSModal, SettingsValueModal
from ui.selects import VPSSelect, OSSelect, PlanSelect, HelpCategorySelect


def format_resources(ram: float, cpu: float, disk: float) -> str:
    return f"{ram:g} GB RAM • {cpu:g} CPU • {disk:g} GB Disk"


class _OwnerCheckView(discord.ui.View):
    def __init__(self, *, user_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.user_id = user_id

    def is_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _update(self, interaction: discord.Interaction, embed, view=None) -> None:
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)


# ==========================================================================
# VPS dashboard
# ==========================================================================
class DashboardView(_OwnerCheckView):
    def __init__(self, app, user_id: int, user: discord.User):
        super().__init__(user_id=user_id)
        self.app = app
        self.user = user
        self.vps_id: str | None = None

    # ------------------------------------------------------------------
    def _vps_list(self) -> list[dict]:
        return dbm.get_user_vps(self.app.db, self.user_id)

    def _vps(self) -> dict | None:
        vps_list = self._vps_list()
        if not vps_list:
            return None
        if self.vps_id:
            for vps in vps_list:
                if vps["id"] == self.vps_id:
                    return vps
        return vps_list[0]

    def _os_name(self, vps: dict) -> str:
        image = self.app.settings.image_by_key(vps["os"])
        return image["name"] if image else vps["os"]

    def _dash_embed(self) -> discord.Embed:
        emb = self.app.embeds
        vps = self._vps()
        title = emb.text("dashboard_title", "Your VPS Dashboard")
        welcome = emb.text("dashboard_welcome", "Welcome back, {user}", user=self.user.mention)
        if vps is None:
            embed = emb.primary(title=title, description=welcome)
            embed.add_field(
                name="☁️ VPS",
                value=emb.text("dashboard_no_vps", "You don't have any VPS yet. Use /create to deploy your first one."),
                inline=False,
            )
            return embed
        name_field = (
            f"{status_text(vps['status'], vps['suspended'])} **{vps['name']}**\n"
            f"{self._os_name(vps)}\n"
            f"{format_resources(vps['ram'], vps['cpu'], vps['disk'])}"
        )
        embed = emb.primary(title=title, description=welcome)
        embed.add_field(name="☁️ VPS", value=name_field, inline=False)
        embed.add_field(
            name="ℹ️",
            value=emb.text("dashboard_prompt", "Select an action below."),
            inline=False,
        )
        return embed

    def build_view(self) -> "DashboardView":
        view = DashboardView(self.app, self.user_id, self.user)
        vps_list = self._vps_list()
        view.vps_id = self.vps_id
        if len(vps_list) > 0:
            view.add_item(VPSSelect(view, vps_list))
        view._add_buttons(len(vps_list))
        return view

    def _add_buttons(self, has_vps: bool) -> None:
        if has_vps:
            self.add_item(ManageButton(self))
            self.add_item(StatsButton(self))
            self.add_item(InfoButton(self))
            self.add_item(SSHButton(self, row=1))
            self.add_item(LogsButton(self, row=2))
            self.add_item(RegenButton(self, row=2))
            self.add_item(DeleteButton(self, row=2))
            self.add_item(RefreshButton(self, row=2))

    async def render(self, interaction: discord.Interaction) -> None:
        await self._update(interaction, self._dash_embed(), self.build_view())

    async def send(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=self._dash_embed(), view=self.build_view(), ephemeral=True)

    async def refresh(self, interaction: discord.Interaction) -> None:
        await self.render(interaction)

    async def show_manage(self, interaction: discord.Interaction) -> None:
        vps = self._vps()
        if not vps:
            await self.render(interaction)
            return
        await ManageView(self.app, self.user, vps).render(interaction)

    async def show_stats(self, interaction: discord.Interaction) -> None:
        vps = self._vps()
        if not vps:
            await self.render(interaction)
            return
        await self.app.views.render_stats(self, interaction, vps)

    async def show_info(self, interaction: discord.Interaction) -> None:
        vps = self._vps()
        if not vps:
            await self.render(interaction)
            return
        await self.app.views.render_info(self, interaction, vps)

    async def show_logs(self, interaction: discord.Interaction) -> None:
        vps = self._vps()
        if not vps:
            await self.render(interaction)
            return
        await self.app.views.render_logs(self, interaction, vps)

    async def do_ssh(self, interaction: discord.Interaction) -> None:
        vps = self._vps()
        if not vps:
            await self.render(interaction)
            return
        await self.app.views.do_ssh(self, interaction, vps)

    async def do_regen(self, interaction: discord.Interaction) -> None:
        vps = self._vps()
        if not vps:
            await self.render(interaction)
            return
        await self.app.views.do_regen(self, interaction, vps)

    async def do_delete(self, interaction: discord.Interaction) -> None:
        vps = self._vps()
        if not vps:
            await self.render(interaction)
            return
        await self.app.views.do_delete(self, interaction, vps)


# ==========================================================================
# Management panel
# ==========================================================================
class ManageView(_OwnerCheckView):
    def __init__(self, app, user: discord.User, vps: dict):
        super().__init__(user_id=user.id)
        self.app = app
        self.user = user
        self.vps = vps
        self.add_item(StartButton(self, row=0))
        self.add_item(StopButton(self, row=0))
        self.add_item(RestartButton(self, row=0))
        self.add_item(SSHButton(self, row=0))
        self.add_item(StatsButton(self, row=1))
        self.add_item(LogsButton(self, row=1))
        self.add_item(ReinstallButton(self, row=1))
        self.add_item(DeleteButton(self, row=1))
        self.add_item(BackButton(self, row=2))

    async def run_action(self, interaction: discord.Interaction, label: str, coro) -> None:
        await interaction.response.defer(ephemeral=True)
        ok, msg = await coro
        if not ok:
            await self._update(interaction, self.app.embeds.error_message(label, msg), self)
            return
        self._reload()
        await self._update(interaction, self._embed(), self)

    async def show_stats(self, interaction: discord.Interaction) -> None:
        await self.app.views.render_stats(self, interaction, self.vps)

    async def show_logs(self, interaction: discord.Interaction) -> None:
        await self.app.views.render_logs(self, interaction, self.vps)

    async def do_ssh(self, interaction: discord.Interaction) -> None:
        await self.app.views.do_ssh(self, interaction, self.vps)

    async def do_regen(self, interaction: discord.Interaction) -> None:
        await self.app.views.do_regen(self, interaction, self.vps)

    async def do_delete(self, interaction: discord.Interaction) -> None:
        await self.app.views.do_delete(self, interaction, self.vps)

    def _reload(self) -> None:
        fresh = dbm.get_vps(self.app.db, self.vps["id"], self.user_id)
        if fresh:
            self.vps = fresh

    def _embed(self) -> discord.Embed:
        emb = self.app.embeds
        self._reload()
        vps = self.vps
        image = self.app.settings.image_by_key(vps["os"])
        os_name = image["name"] if image else vps["os"]
        embed = emb.primary(title=emb.text("manage_title", "VPS Management"))
        embed.add_field(name="☁️ VPS", value=f"**{vps['name']}** ({os_name})", inline=False)
        embed.add_field(name="🖥️ OS", value=os_name, inline=True)
        embed.add_field(name="🟢 Status", value=status_text(vps["status"], vps["suspended"]), inline=True)
        embed.add_field(name="🧠 Resources", value=format_resources(vps["ram"], vps["cpu"], vps["disk"]), inline=False)
        embed.add_field(name="ℹ️", value=emb.text("manage_prompt", "What would you like to do?"), inline=False)
        return embed

    async def render(self, interaction: discord.Interaction) -> None:
        await self._update(interaction, self._embed(), self)

    async def back(self, interaction: discord.Interaction) -> None:
        await DashboardView(self.app, self.user_id, self.user).render(interaction)


# ==========================================================================
# Confirmation dialog
# ==========================================================================
class ConfirmView(_OwnerCheckView):
    def __init__(self, *, user_id: int, embed: discord.Embed, confirm_label: str,
                 confirm_style: discord.ButtonStyle = discord.ButtonStyle.danger,
                 on_confirm, on_cancel=None):
        super().__init__(user_id=user_id)
        self.embed = embed
        self.on_confirm = on_confirm
        self.on_cancel = on_cancel
        self.add_item(CancelButton(self, row=0))
        self.add_item(ConfirmButton(self, label=confirm_label, style=confirm_style, row=0))

    async def confirm(self, interaction: discord.Interaction) -> None:
        if self.on_confirm:
            await self.on_confirm(interaction)

    async def cancel(self, interaction: discord.Interaction) -> None:
        if self.on_cancel:
            await self.on_cancel(interaction)
        else:
            await self._update(interaction, self.embed)

    async def send(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=self.embed, view=self, ephemeral=True)


# ==========================================================================
# Create flow (user)
# ==========================================================================
class CreateSetupView(_OwnerCheckView):
    def __init__(self, app, user: discord.User):
        super().__init__(user_id=user.id)
        self.app = app
        self.user = user
        self.os_key: str | None = None
        self.add_item(CancelButton(self, row=4))
        self._render_os_select()

    def _render_os_select(self) -> None:
        self.add_item(OSSelect(self, self.app.settings.image_map()))

    async def on_os_selected(self, interaction: discord.Interaction, os_key: str) -> None:
        self.os_key = os_key
        plans = self.app.settings.enabled_plans()
        if plans:
            view = CreateSetupView(self.app, self.user)
            view.clear_items()
            view.os_key = os_key
            view.add_item(PlanSelect(view, plans))
            view.add_item(CancelButton(view, row=4))
            embed = self.app.embeds.primary(
                title=self.app.embeds.text("create_select_plan_title", "Choose a Plan"),
                description=self.app.embeds.text("create_select_plan_desc", "Choose a resource plan, or configure custom resources."),
            )
            await self._update(interaction, embed, view)
        else:
            await self._open_modal(interaction)

    async def on_plan_selected(self, interaction: discord.Interaction, plan_key: str) -> None:
        if plan_key == "__custom__":
            await self._open_modal(interaction)
            return
        spec = self.app.resources.plan_spec(plan_key)
        if not spec:
            await self._open_modal(interaction)
            return
        await self._start_create(interaction, ram=spec["ram"], cpu=spec["cpu"], disk=spec["disk"], name=None)

    async def _open_modal(self, interaction: discord.Interaction) -> None:
        cfg = self.app.settings.get("vps", {})
        allow_name = self.app.settings.get_bool("vps.allow_custom_name", True)
        modal = CreateVPSModal(
            self,
            title=self.app.embeds.text("create_resources_title", "Configure Resources"),
            default_ram=self.app.resources.parse_size(cfg.get("default_ram", "2GB")),
            default_cpu=self.app.resources.parse_cpu(cfg.get("default_cpu", 1)),
            default_disk=self.app.resources.parse_size(cfg.get("default_disk", "10GB")),
            allow_name=allow_name,
        )
        await interaction.response.send_modal(modal)

    async def on_resources_submitted(self, interaction: discord.Interaction, values: dict) -> None:
        try:
            ram = float(values["ram"])
            cpu = float(values["cpu"])
            disk = float(values["disk"])
        except (TypeError, ValueError):
            await interaction.response.send_message("RAM, CPU and disk must be valid numbers.", ephemeral=True)
            return
        name = values.get("name") or None
        await self._start_create(interaction, ram=ram, cpu=cpu, disk=disk, name=name)

    async def _start_create(self, interaction: discord.Interaction, *, ram: float, cpu: float, disk: float, name: str | None) -> None:
        await interaction.response.defer(ephemeral=True)
        emb = self.app.embeds
        creating = emb.info(
            title=emb.text("create_started", "Creating your VPS..."),
            description=emb.text("create_started_eta", "Provisioning usually takes about a minute."),
        )
        message = await interaction.followup.send(embed=creating, ephemeral=True)

        async def on_progress(stage: str):
            try:
                progress_embed = emb.info(
                    title=emb.text("create_started", "Creating your VPS..."),
                    description=f"{stage}\n\n{emb.text('create_started_eta', '')}",
                )
                await message.edit(embed=progress_embed)
            except Exception:  # noqa: BLE001
                pass

        result = await self.app.vps.create(
            user_id=self.user_id,
            username=str(self.user),
            os_key=self.os_key,
            ram=ram,
            cpu=cpu,
            disk=disk,
            name=name,
            on_progress=on_progress,
        )
        await self._finish_create(interaction, message, result)

    async def _finish_create(self, interaction: discord.Interaction, message, result: dict) -> None:
        emb = self.app.embeds
        if not result["ok"]:
            await message.edit(embed=emb.error_message("create", result.get("error")))
            return
        image = self.app.settings.image_by_key(result["os_key"])
        os_name = image["name"] if image else result["os_key"]
        embed = emb.success(title=emb.text("vps_created_title", "VPS Created"))
        embed.add_field(name="🖥️ OS", value=os_name, inline=True)
        embed.add_field(name="🧠 RAM", value=f"{result['ram']:g} GB", inline=True)
        embed.add_field(name="⚡ CPU", value=f"{result['cpu']:g} Cores", inline=True)
        embed.add_field(name="💾 Disk", value=f"{result['disk']:g} GB", inline=True)
        embed.add_field(name="☁️ Name", value=f"`{result['name']}`", inline=False)
        if not result.get("disk_enforced"):
            embed.add_field(
                name="ℹ️",
                value="Note: this host's storage driver does not enforce the disk quota.",
                inline=False,
            )
        await message.edit(embed=embed)
        await self.app.views.dm_ssh(interaction.user, result["ssh"])
        await self.app.views.try_followup(interaction, emb.text("vps_created_dm_note", "Your access details have been sent to your DMs."))


# ==========================================================================
# Admin create flow
# ==========================================================================
class AdminCreateSetupView(_OwnerCheckView):
    def __init__(self, app, user: discord.User):
        super().__init__(user_id=user.id)
        self.app = app
        self.user = user
        self.os_key: str | None = None
        self.add_item(CancelButton(self, row=4))
        self.add_item(OSSelect(self, self.app.settings.image_map()))

    async def on_os_selected(self, interaction: discord.Interaction, os_key: str) -> None:
        self.os_key = os_key
        cfg = self.app.settings.get("vps", {})
        modal = AdminCreateVPSModal(
            self,
            os_key=os_key,
            default_ram=self.app.resources.parse_size(cfg.get("default_ram", "2GB")),
            default_cpu=self.app.resources.parse_cpu(cfg.get("default_cpu", 1)),
            default_disk=self.app.resources.parse_size(cfg.get("default_disk", "10GB")),
        )
        await interaction.response.send_modal(modal)

    async def on_resources_submitted(self, interaction: discord.Interaction, values: dict) -> None:
        target = values.get("target", "").strip()
        if not target.lstrip("-").isdigit():
            await interaction.response.send_message("Please provide a valid target user ID.", ephemeral=True)
            return
        target_id = int(target)
        try:
            target_user = await self.app.bot.fetch_user(target_id)
        except Exception:  # noqa: BLE001
            await interaction.response.send_message("Could not find that Discord user.", ephemeral=True)
            return
        try:
            ram, cpu, disk = float(values["ram"]), float(values["cpu"]), float(values["disk"])
        except (TypeError, ValueError):
            await interaction.response.send_message("RAM, CPU and disk must be valid numbers.", ephemeral=True)
            return
        name = values.get("name") or None
        await interaction.response.defer(ephemeral=True)
        result = await self.app.vps.create(
            user_id=target_id,
            username=str(target_user),
            os_key=self.os_key,
            ram=ram,
            cpu=cpu,
            disk=disk,
            name=name,
        )
        emb = self.app.embeds
        if result["ok"]:
            embed = emb.success(title="Admin: VPS Created", description=f"VPS **{result['name']}** created for <@{target_id}>.")
            if result["ssh"]:
                await self.app.views.dm_ssh(target_user, result["ssh"])
                embed.description += "\nSSH details sent to the user's DMs."
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(embed=emb.error_message("create", result.get("error")), ephemeral=True)


# ==========================================================================
# Reinstall flow
# ==========================================================================
class ReinstallSetupView(_OwnerCheckView):
    def __init__(self, app, user: discord.User, vps: dict):
        super().__init__(user_id=user.id)
        self.app = app
        self.user = user
        self.vps = vps
        self.add_item(CancelButton(self, row=4))
        self.add_item(OSSelect(self, self.app.settings.image_map(), placeholder="Select the new operating system…"))

    async def render_select(self, interaction: discord.Interaction) -> None:
        emb = self.app.embeds
        embed = emb.primary(title="♻️ Reinstall VPS", description=f"Select the new OS for **{self.vps['name']}**.")
        await self._update(interaction, embed, self)

    async def on_os_selected(self, interaction: discord.Interaction, os_key: str) -> None:
        image = self.app.settings.image_by_key(os_key)
        os_name = image["name"] if image else os_key
        emb = self.app.embeds
        embed = emb.warning(
            title="♻️ Reinstall VPS?",
            description=(
                f"**{self.vps['name']}** will be reinstalled with **{os_name}**.\n"
                "All data on the current OS will be erased.\n\nThis action cannot be undone."
            ),
        )
        async def on_confirm(inter: discord.Interaction):
            await inter.response.defer(ephemeral=True)
            ok, msg = await self.app.vps.reinstall(self.vps["id"], self.user_id, os_key)
            emb = self.app.embeds
            if ok:
                await inter.edit_original_response(
                    embed=emb.success(title="VPS Reinstalled", description=msg), view=None
                )
            else:
                await inter.edit_original_response(embed=emb.error_message("reinstall", msg), view=None)

        view = ConfirmView(
            user_id=self.user_id,
            embed=embed,
            confirm_label="Reinstall",
            confirm_style=discord.ButtonStyle.danger,
            on_confirm=on_confirm,
            on_cancel=lambda inter: ManageView(self.app, self.user, self.vps).render(inter),
        )
        await self._update(interaction, embed, view)


# ==========================================================================
# Help
# ==========================================================================
class HelpView(_OwnerCheckView):
    CATEGORIES = {
        "vps": ("🖥️ VPS Commands", [
            ("/create", "Deploy a new VPS - choose an OS, plan and resources."),
            ("/vps", "Open your interactive VPS dashboard."),
            ("/list", "List all of your VPS instances."),
            ("/vps start · stop · restart", "Control a VPS from the management panel."),
            ("/vps delete", "Permanently delete a VPS."),
            ("/vps reinstall", "Reinstall a VPS with a different OS."),
        ]),
        "access": ("🔐 Access", [
            ("/ssh", "Generate a fresh SSH session for a VPS."),
            ("🔄 Regenerate SSH", "Get a new session from the VPS dashboard."),
            ("DM-only", "SSH credentials are only ever sent to your DMs - never in public channels."),
        ]),
        "monitoring": ("📊 Monitoring", [
            ("/vps info", "Full VPS details: resources, usage, uptime."),
            ("📊 Statistics", "Live CPU, memory and network usage."),
            ("📜 Logs", "Recent container logs."),
        ]),
        "admin": ("⚙️ Admin", [
            ("/admin", "Open the hosting control panel."),
            ("/admin ban · unban", "Manage user bans."),
            ("/settings", "Configure the bot without touching code."),
            ("/admin kill", "Stop every running VPS at once."),
        ]),
        "info": ("❓ Information", [
            ("/about", "About this service."),
            ("/ping", "Check bot latency."),
            ("/help", "This help center."),
        ]),
    }

    def __init__(self, app, user_id: int):
        super().__init__(user_id=user_id)
        self.app = app
        self.add_item(HelpCategorySelect(self))

    def _embed(self) -> discord.Embed:
        return self.app.embeds.primary(
            title=self.app.embeds.text("help_title", "Help Center"),
            description=self.app.embeds.text("help_desc", "Select a category to learn more about {name}.", name=self.app.settings.brand_name()),
        )

    async def render(self, interaction: discord.Interaction) -> None:
        await self._update(interaction, self._embed(), self)

    async def send(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=self._embed(), view=self, ephemeral=True)

    async def show_category(self, interaction: discord.Interaction, key: str) -> None:
        emb = self.app.embeds
        label, commands = self.CATEGORIES[key]
        embed = emb.info(title=f"{label}")
        for name, desc in commands:
            embed.add_field(name=f"`{name}`", value=desc, inline=False)
        await self._update(interaction, embed, self)


# ==========================================================================
# Settings
# ==========================================================================
class SettingsView(_OwnerCheckView):
    def __init__(self, app, user_id: int, groups: dict):
        super().__init__(user_id=user_id)
        self.app = app
        self.groups = groups
        self.selected_key: str | None = None
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        self.clear_items()
        self.add_item(CategorySelect(self))
        self.add_item(SettingSelect(self))
        self.add_item(EditButton(self, row=4))
        self.add_item(BackButton(self, row=4))

    def _all_items(self) -> list[tuple[str, str, str]]:
        items = []
        for group in self.groups.values():
            items.extend(group)
        return items

    def _item(self, key: str) -> tuple[str, str, str]:
        for name, label, kind in self._all_items():
            if name == key:
                return name, label, kind
        return key, key, "str"

    def _embed(self) -> discord.Embed:
        emb = self.app.embeds
        groups = self.groups
        embed = emb.primary(title="⚙️ Bot Settings", description="Runtime configuration - changes apply immediately without code edits.")
        for group_name, items in groups.items():
            lines = [f"`{label}` - **{self._display(name)}**" for name, label, kind in items[:8]]
            embed.add_field(name=group_name, value="\n".join(lines), inline=True)
        if self.selected_key:
            name, label, kind = self._item(self.selected_key)
            embed.add_field(
                name="Selected Setting",
                value=f"`{name}`\n**{label}**\nCurrent: `{self._display(name)}`",
                inline=False,
            )
        embed.add_field(name="ℹ️", value="Choose a category and a setting, then press **Edit**.", inline=False)
        return embed

    def _display(self, key: str) -> str:
        value = self.app.settings.get(key)
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        if isinstance(value, bool):
            return "enabled" if value else "disabled"
        return str(value) if value is not None else ""

    def _current_raw(self, key: str) -> str:
        value = self.app.settings.get(key)
        if isinstance(value, list):
            return " | ".join(str(v) for v in value)
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value) if value is not None else ""

    async def render(self, interaction: discord.Interaction) -> None:
        self._refresh_controls()
        await self._update(interaction, self._embed(), self)

    async def send(self, interaction: discord.Interaction) -> None:
        self._refresh_controls()
        await interaction.response.send_message(embed=self._embed(), view=self, ephemeral=True)

    async def back(self, interaction: discord.Interaction) -> None:
        await self.render(interaction)

    async def on_category_selected(self, interaction: discord.Interaction, group_name: str) -> None:
        await self.render(interaction)

    async def on_setting_selected(self, interaction: discord.Interaction, key: str) -> None:
        self.selected_key = key
        await self.render(interaction)

    async def on_edit(self, interaction: discord.Interaction) -> None:
        if not self.selected_key:
            await interaction.response.send_message("Select a setting first.", ephemeral=True)
            return
        name, label, kind = self._item(self.selected_key)
        modal = SettingsValueModal(self, key=name, label=label, current=self._current_raw(name))
        await interaction.response.send_modal(modal)

    async def on_value_submitted(self, interaction: discord.Interaction, key: str, raw: str) -> None:
        name, label, kind = self._item(key)
        try:
            parsed = self.app.value_parser.parse(kind, raw)
        except ValueError as exc:
            await interaction.response.send_message(f"Invalid value: {exc}", ephemeral=True)
            return
        self.app.settings.set(key, parsed)
        self.app.settings.reload()
        self.selected_key = key
        await interaction.response.defer(ephemeral=True)
        await self.render(interaction)


# ==========================================================================
# Admin dashboard
# ==========================================================================
class AdminDashboardView(_OwnerCheckView):
    def __init__(self, app, user: discord.User):
        super().__init__(user_id=user.id)
        self.app = app
        self.user = user
        self.add_item(UsersButton(self, row=0))
        self.add_item(AdminVpsButton(self, row=0))
        self.add_item(AdminStatsButton(self, row=0))
        self.add_item(BansButton(self, row=0))
        self.add_item(AdminSettingsButton(self, row=1))
        self.add_item(AdminLogsButton(self, row=1))
        self.add_item(CleanupButton(self, row=1))
        self.add_item(AdminRefreshButton(self, row=1))

    async def _embed(self) -> discord.Embed:
        emb = self.app.embeds
        stats = dbm.allocated_totals(self.app.db)
        host = await self.app.stats.host_resources()
        embed = emb.primary(title="⚙️ Hosting Control Panel", description=f"Welcome, {self.user.mention}")
        embed.add_field(name="👥 Users", value=dbm.count_users(self.app.db), inline=True)
        embed.add_field(name="🖥️ VPS Instances", value=stats["count"], inline=True)
        embed.add_field(name="🟢 Running", value=stats["running"], inline=True)
        embed.add_field(name="⚡ CPU Allocated", value=f"{stats['cpu']:g} cores", inline=True)
        embed.add_field(name="🧠 RAM Allocated", value=f"{stats['ram']:g} GB", inline=True)
        embed.add_field(name="💾 Disk Allocated", value=f"{stats['disk']:g} GB", inline=True)
        embed.add_field(name="🛡️ Banned Users", value=len(dbm.list_bans(self.app.db)), inline=True)
        embed.add_field(name="🖥️ Host", value=f"{self.app.stats.format_gb(host['mem_total_gb'])} RAM • {host['cpus']} CPU", inline=True)
        embed.add_field(name="💾 Host Disk Free", value=self.app.stats.format_gb(host["disk_free_gb"]), inline=True)
        embed.add_field(name="ℹ️", value="Select an action below.", inline=False)
        return embed

    async def render(self, interaction: discord.Interaction) -> None:
        await self._update(interaction, await self._embed(), self)

    async def send(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=await self._embed(), view=self, ephemeral=True)

    async def show_users(self, interaction: discord.Interaction) -> None:
        await self.app.views.render_users(self, interaction)

    async def show_vps(self, interaction: discord.Interaction) -> None:
        await self.app.views.render_all_vps(self, interaction)

    async def show_stats(self, interaction: discord.Interaction) -> None:
        await self.app.views.render_admin_stats(self, interaction)

    async def show_bans(self, interaction: discord.Interaction) -> None:
        await self.app.views.render_bans(self, interaction)

    async def show_logs(self, interaction: discord.Interaction) -> None:
        await self.app.views.render_audit_logs(self, interaction)

    async def show_settings(self, interaction: discord.Interaction) -> None:
        from commands.settings import SETTING_GROUPS
        await SettingsView(self.app, self.user_id, SETTING_GROUPS).render(interaction)

    async def do_cleanup(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await self.app.cleanup.cleanup_ui(self.user_id)
        embed = self.app.embeds.success(
            title="🧹 Cleanup Complete",
            description=f"Audit logs pruned: **{result['pruned_logs']}**\nOrphan containers removed: **{result['orphans_removed']}**",
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


# ==========================================================================
# Selects used by Settings
# ==========================================================================
class CategorySelect(discord.ui.Select):
    def __init__(self, view: SettingsView):
        self._view = view
        options = [
            discord.SelectOption(label=name, value=name, description=f"{len(items)} settings")
            for name, items in view.groups.items()
        ]
        super().__init__(placeholder="Choose a category…", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.view.user_id:
            await interaction.response.send_message("This panel belongs to someone else.", ephemeral=True)
            return
        await self.view.on_category_selected(interaction, self.values[0])


class SettingSelect(discord.ui.Select):
    def __init__(self, view: SettingsView):
        self._view = view
        options = []
        for name, label, kind in view._all_items()[:25]:
            options.append(discord.SelectOption(label=label[:100], value=name, description=f"{kind} • {name}"[:100]))
        super().__init__(placeholder="Choose a setting…", min_values=1, max_values=1, options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.view.user_id:
            await interaction.response.send_message("This panel belongs to someone else.", ephemeral=True)
            return
        await self.view.on_setting_selected(interaction, self.values[0])


# ==========================================================================
# Shared buttons
# ==========================================================================
class _Button(discord.ui.Button):
    view: _OwnerCheckView

    def __init__(self, view, label, style=discord.ButtonStyle.secondary, row=None, emoji=None):
        self._view = view
        super().__init__(label=label, style=style, row=row, emoji=emoji)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.view.user_id:
            await interaction.response.send_message("These controls belong to someone else.", ephemeral=True)
            return False
        return True


class ManageButton(_Button):
    def __init__(self, view, row=1):
        super().__init__(view, "Manage VPS", emoji="🖥️", row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.show_manage(interaction)


class StatsButton(_Button):
    def __init__(self, view, row=1):
        super().__init__(view, "Statistics", emoji="📊", row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.show_stats(interaction)


class InfoButton(_Button):
    def __init__(self, view, row=1):
        super().__init__(view, "VPS Info", emoji="📋", row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.show_info(interaction)


class SSHButton(_Button):
    def __init__(self, view, row=0):
        super().__init__(view, "SSH Access", emoji="🔐", row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.do_ssh(interaction)


class LogsButton(_Button):
    def __init__(self, view, row=1):
        super().__init__(view, "Logs", emoji="📜", row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.show_logs(interaction)


class RegenButton(_Button):
    def __init__(self, view, row=2):
        super().__init__(view, "Regenerate SSH", emoji="🔄", row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.do_regen(interaction)


class DeleteButton(_Button):
    def __init__(self, view, row=2):
        super().__init__(view, "Delete", emoji="🗑️", style=discord.ButtonStyle.danger, row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.do_delete(interaction)


class RefreshButton(_Button):
    def __init__(self, view, row=2):
        super().__init__(view, "Refresh", emoji="🔄", row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.refresh(interaction)


class StartButton(_Button):
    def __init__(self, view, row=0):
        super().__init__(view, "Start", emoji="🟢", style=discord.ButtonStyle.success, row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.run_action(
            interaction, "start",
            self.view.app.vps.start(self.view.vps["id"], self.view.user_id),
        )


class StopButton(_Button):
    def __init__(self, view, row=0):
        super().__init__(view, "Stop", emoji="🔴", style=discord.ButtonStyle.danger, row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.run_action(
            interaction, "stop",
            self.view.app.vps.stop(self.view.vps["id"], self.view.user_id),
        )


class RestartButton(_Button):
    def __init__(self, view, row=0):
        super().__init__(view, "Restart", emoji="🔄", row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.run_action(
            interaction, "restart",
            self.view.app.vps.restart(self.view.vps["id"], self.view.user_id),
        )


class ReinstallButton(_Button):
    def __init__(self, view, row=1):
        super().__init__(view, "Reinstall", emoji="♻️", style=discord.ButtonStyle.danger, row=row)

    async def callback(self, interaction: discord.Interaction):
        await ReinstallSetupView(self.view.app, self.view.user, self.view.vps).render_select(interaction)


class BackButton(_Button):
    def __init__(self, view, row=2):
        super().__init__(view, "Back", emoji="◀️", row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.back(interaction)


class CancelButton(_Button):
    def __init__(self, view, row=4):
        super().__init__(view, "Cancel", style=discord.ButtonStyle.secondary, row=row, emoji="❌")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await interaction.edit_original_response(
            embed=self.view.app.embeds.info(description=self.view.app.embeds.text("create_cancel", "Cancelled.")), view=None
        )


class ConfirmButton(_Button):
    def __init__(self, view, *, label, style, row):
        super().__init__(view, label, style=style, row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.confirm(interaction)


class EditButton(_Button):
    def __init__(self, view, row=4):
        super().__init__(view, "Edit", style=discord.ButtonStyle.primary, row=row, emoji="✏️")

    async def callback(self, interaction: discord.Interaction):
        await self.view.on_edit(interaction)


# Admin buttons
class UsersButton(_Button):
    def __init__(self, view, row=0):
        super().__init__(view, "Users", emoji="👥", row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.show_users(interaction)


class AdminVpsButton(_Button):
    def __init__(self, view, row=0):
        super().__init__(view, "VPS", emoji="🖥️", row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.show_vps(interaction)


class AdminStatsButton(_Button):
    def __init__(self, view, row=0):
        super().__init__(view, "Statistics", emoji="📊", row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.show_stats(interaction)


class BansButton(_Button):
    def __init__(self, view, row=0):
        super().__init__(view, "Bans", emoji="🛡️", row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.show_bans(interaction)


class AdminSettingsButton(_Button):
    def __init__(self, view, row=1):
        super().__init__(view, "Settings", emoji="⚙️", row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.show_settings(interaction)


class AdminLogsButton(_Button):
    def __init__(self, view, row=1):
        super().__init__(view, "Logs", emoji="📋", row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.show_logs(interaction)


class CleanupButton(_Button):
    def __init__(self, view, row=1):
        super().__init__(view, "Cleanup", emoji="🧹", row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.do_cleanup(interaction)


class AdminRefreshButton(_Button):
    def __init__(self, view, row=1):
        super().__init__(view, "Refresh", emoji="🔄", row=row)

    async def callback(self, interaction: discord.Interaction):
        await self.view.render(interaction)
