"""White-label Discord VPS deployment bot (real KVM/QEMU via libvirt).

Entry point: python bot.py

Flow:
    Discord -> /vps -> Create VPS -> OS -> RAM -> Storage -> CPU -> Confirm
        -> KVM VM created -> cloud-init runs -> tmate session -> SSH sent to owner
"""

import asyncio
import logging
import os
import sys
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import database as db
import vps as vpslib
import tmate as tmate_lib
from config import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("vps_bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("vpsbot")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --------------------------------------------------------------------------
# Branding helpers
# --------------------------------------------------------------------------
def brand_embed(title=None, description=None, color=None):
    embed = discord.Embed(
        title=title,
        description=description,
        color=color or discord.Color(config.bot_color),
    )
    if config.bot_logo:
        embed.set_author(name=config.bot_author, icon_url=config.bot_logo)
        embed.set_thumbnail(url=config.bot_logo)
    embed.set_footer(
        text=config.bot_footer,
        icon_url=config.bot_logo or None,
    )
    return embed


def is_admin(interaction: discord.Interaction):
    if interaction.user.id in config.admin_user_ids:
        return True
    if config.admin_role_id and isinstance(interaction.user, discord.Member):
        try:
            role_id = int(config.admin_role_id)
        except (TypeError, ValueError):
            role_id = None
        if role_id and any(r.id == role_id for r in interaction.user.roles):
            return True
    return False


# --------------------------------------------------------------------------
# Rate limiting + deployment locks
# --------------------------------------------------------------------------
_last_use = {}
_locks = {}


def rate_limited(user_id, seconds=None):
    seconds = seconds or config.rate_limit_seconds
    now = time.time()
    if user_id in _last_use and now - _last_use[user_id] < seconds:
        return int(seconds - (now - _last_use[user_id]))
    _last_use[user_id] = now
    return 0


def user_lock(user_id):
    if user_id not in _locks:
        _locks[user_id] = asyncio.Lock()
    return _locks[user_id]


_global_lock = None


def global_lock():
    global _global_lock
    if _global_lock is None:
        _global_lock = asyncio.Lock()
    return _global_lock


# --------------------------------------------------------------------------
# Progress rendering
# --------------------------------------------------------------------------
DEPLOY_STEPS = [
    "Allocating resources",
    "Creating virtual disk",
    "Configuring cloud-init",
    "Creating virtual machine",
    "Starting VPS",
    "Configuring network",
    "Installing tmate",
    "Generating SSH access",
]


class Progress:
    def __init__(self, steps):
        self.steps = steps
        self.current = 0

    async def mark(self, step_text):
        cleaned = step_text.lstrip("✅🔄⏳🔲 ").strip().lower()
        for i, step in enumerate(self.steps):
            if step.lower() == cleaned:
                self.current = max(self.current, i + 1)
                return
        # Allow any completion text to count as done for known prefixes.
        for i, step in enumerate(self.steps):
            if cleaned.startswith(step.lower()):
                self.current = max(self.current, i + 1)
                return

    def render(self):
        return "\n".join(
            f"✅ {step}" if i < self.current else f"⏳ {step}"
            for i, step in enumerate(self.steps)
        )


# --------------------------------------------------------------------------
# Deployment core
# --------------------------------------------------------------------------
def resolve_vps(identifier):
    """Accept 'VPS-0001', 'vps-0001' or '0001'."""
    raw = str(identifier).strip()
    raw_upper = raw.upper()
    if raw_upper.startswith("VPS-"):
        return db.get_vps_by_vps_id(raw_upper)
    if raw_upper.startswith("VPS"):
        return db.get_vps_by_vps_id("VPS-" + raw_upper[3:].zfill(4))
    if raw.isdigit():
        return db.get_vps_by_vps_id(f"VPS-{int(raw):04d}")
    return db.get_vps_by_vps_id(raw_upper)


def spec_embed(interaction, spec, title="🚀 VPS Configuration"):
    os_name = config.os_names.get(spec["os"], spec["os"].title())
    embed = brand_embed(
        title=title,
        description=(
            f"**OS:** {os_name}\n"
            f"**CPU:** {spec['cpu']} Cores\n"
            f"**RAM:** {spec['ram']} GB\n"
            f"**Storage:** {spec['disk']} GB\n"
        ),
    )
    return embed


def _serial_tail(vps, lines=40):
    """Return the tail of a VPS's serial console log, for diagnostics."""
    instance_dir = (vps or {}).get("instance_dir")
    if not instance_dir:
        return ""
    log_path = os.path.join(instance_dir, "serial.log")
    if not os.path.exists(log_path):
        return ""
    try:
        with open(log_path, errors="replace") as fh:
            data = fh.read()
        return "\n".join(data.splitlines()[-lines:])
    except OSError:
        return ""


async def _run_deployment(interaction, spec, vps, progress_embed_msg, progress):
    """Run the full KVM deployment. Cleans up on any failure."""
    job_id = vps["vps_id"]
    db.create_job(job_id, vps["vps_id"], vps["discord_user_id"])
    db.update_job(job_id, status="running")
    pubkey = await asyncio.to_thread(vpslib.ensure_ssh_key)
    ip_address = None
    tmate_session = None
    try:
        disk_path, seed_path, instance_dir = await vpslib.create_vm(
            vps, pubkey, progress=progress.mark
        )
        db.update_vps(
            vps["vps_id"],
            disk_path=disk_path,
            seed_path=seed_path,
            instance_dir=instance_dir,
        )
        await progress_embed_msg(progress)

        ip_address = await vpslib.wait_for_ip(
            vps["vm_name"], timeout=180, progress=progress.mark
        )
        db.update_vps(vps["vps_id"], ip_address=ip_address)
        await progress_embed_msg(progress)

        await tmate_lib.wait_for_ssh(ip_address, timeout=config.boot_timeout)
        await tmate_lib.wait_for_cloud_init(ip_address, timeout=config.boot_timeout)
        await progress.mark("Installing tmate")
        await progress_embed_msg(progress)

        if config.tmate_enabled:
            await tmate_lib.install_tmate(ip_address, timeout=300)
            tmate_session = await tmate_lib.start_tmate(ip_address, timeout=240)
        await progress.mark("Generating SSH access")
        await progress_embed_msg(progress)

        db.update_vps(
            vps["vps_id"],
            status="running",
            tmate_session=tmate_session or None,
        )
        db.update_job(job_id, status="done", message="deployed")
        db.log_audit(vps["discord_user_id"], "vps_deployed", vps["vps_id"])
        return tmate_session, ip_address
    except Exception as exc:  # noqa: BLE001 - full cleanup on any failure
        detail = str(exc)
        tail = _serial_tail(vps)
        if tail:
            detail += "\n\n--- serial log (tail) ---\n" + tail
        logger.error("Deployment failed for %s: %s", vps["vps_id"], detail)
        await vpslib.cleanup_failed(vps)
        db.update_job(job_id, status="failed", message=detail[-500:])
        raise RuntimeError(detail) from exc


# --------------------------------------------------------------------------
# Discord views
# --------------------------------------------------------------------------
class HomeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Create VPS", style=discord.ButtonStyle.success, emoji="🚀")
    async def create(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            embed=brand_embed(
                title="🚀 Create VPS",
                description="Select Operating System:\n\n🐧 Ubuntu\n🔴 Debian",
            ),
            view=OsSelectView(),
        )

    @discord.ui.button(label="My VPS", style=discord.ButtonStyle.primary, emoji="📦")
    async def my_vps(self, interaction: discord.Interaction, _: discord.ui.Button):
        await show_my_vps(interaction)

    @discord.ui.button(label="Manage VPS", style=discord.ButtonStyle.secondary, emoji="⚙️")
    async def manage(self, interaction: discord.Interaction, _: discord.ui.Button):
        await show_manage(interaction)


class OsSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Ubuntu", style=discord.ButtonStyle.primary, emoji="🐧")
    async def ubuntu(self, interaction: discord.Interaction, _: discord.ui.Button):
        await start_ram_step(interaction, "ubuntu")

    @discord.ui.button(label="Debian", style=discord.ButtonStyle.danger, emoji="🔴")
    async def debian(self, interaction: discord.Interaction, _: discord.ui.Button):
        await start_ram_step(interaction, "debian")


class NextStepView(discord.ui.View):
    """Confirmation message + Continue button between wizard steps.

    Discord does not allow a modal to be opened directly in response to a
    modal submit, so each step sends this message and the button opens the
    next modal.
    """

    def __init__(self, spec, step):
        super().__init__(timeout=180)
        self.spec = spec
        self.step = step

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary, emoji="➡️")
    async def cont(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.step == "storage":
            await interaction.response.send_modal(StorageModal(self.spec))
        elif self.step == "cpu":
            await interaction.response.send_modal(CpuModal(self.spec))
        else:
            await interaction.response.send_message(
                embed=brand_embed(title="Error", description="Unknown wizard step.", color=discord.Color.red()),
                ephemeral=True,
            )


class RamModal(discord.ui.Modal, title="💾 RAM"):
    ram = discord.ui.TextInput(
        label="RAM (GB)",
        placeholder="e.g. 8",
        min_length=1,
        max_length=4,
        required=True,
    )

    def __init__(self, spec):
        super().__init__()
        self.spec = spec

    async def on_submit(self, interaction: discord.Interaction):
        try:
            self.spec["ram"] = int(self.ram.value)
        except ValueError:
            await interaction.response.send_message(
                embed=brand_embed(title="Invalid RAM", description="Please enter a whole number of GB.", color=discord.Color.red()),
                ephemeral=True,
            )
            return
        await start_storage_step(interaction, self.spec)


class StorageModal(discord.ui.Modal, title="💿 Storage"):
    disk = discord.ui.TextInput(
        label="Storage (GB)",
        placeholder="e.g. 100",
        min_length=1,
        max_length=5,
        required=True,
    )

    def __init__(self, spec):
        super().__init__()
        self.spec = spec

    async def on_submit(self, interaction: discord.Interaction):
        try:
            self.spec["disk"] = int(self.disk.value)
        except ValueError:
            await interaction.response.send_message(
                embed=brand_embed(title="Invalid Storage", description="Please enter a whole number of GB.", color=discord.Color.red()),
                ephemeral=True,
            )
            return
        await start_cpu_step(interaction, self.spec)


class CpuModal(discord.ui.Modal, title="⚙️ CPU"):
    cpu = discord.ui.TextInput(
        label="CPU Cores",
        placeholder="e.g. 4",
        min_length=1,
        max_length=2,
        required=True,
    )

    def __init__(self, spec):
        super().__init__()
        self.spec = spec

    async def on_submit(self, interaction: discord.Interaction):
        try:
            self.spec["cpu"] = int(self.cpu.value)
        except ValueError:
            await interaction.response.send_message(
                embed=brand_embed(title="Invalid CPU", description="Please enter a whole number of cores.", color=discord.Color.red()),
                ephemeral=True,
            )
            return
        await show_confirmation(interaction, self.spec)


class ConfirmView(discord.ui.View):
    def __init__(self, spec):
        super().__init__(timeout=180)
        self.spec = spec

    @discord.ui.button(label="Deploy VPS", style=discord.ButtonStyle.success, emoji="✅")
    async def deploy(self, interaction: discord.Interaction, _: discord.ui.Button):
        await deploy_vps(interaction, self.spec)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            embed=brand_embed(title="Cancelled", description="VPS deployment cancelled.", color=discord.Color.red()),
            view=None,
        )


class VpsSelectView(discord.ui.View):
    def __init__(self, vps_list):
        super().__init__(timeout=180)
        self.vps_list = vps_list
        self.selector = discord.ui.Select(
            placeholder="Select a VPS to manage...",
            options=[
                discord.SelectOption(
                    label=v["vps_id"],
                    description=f"{config.os_names.get(v['os'], v['os'])} | {v['ram']} GB | {v['cpu']} CPU",
                    value=v["vps_id"],
                )
                for v in vps_list
            ],
        )
        self.selector.callback = self.on_select
        self.add_item(self.selector)

    async def on_select(self, interaction: discord.Interaction):
        vps_id = self.selector.values[0]
        await show_manage_actions(interaction, vps_id)


class ManageActionsView(discord.ui.View):
    def __init__(self, vps_id):
        super().__init__(timeout=180)
        self.vps_id = vps_id

    async def _operation(self, interaction, action):
        vps = db.get_vps_by_vps_id(self.vps_id)
        if not vps:
            await interaction.response.edit_message(
                embed=brand_embed(title="Error", description="VPS no longer exists.", color=discord.Color.red()),
                view=None,
            )
            return
        if vps["discord_user_id"] != interaction.user.id and not is_admin(interaction):
            await interaction.response.send_message(
                embed=brand_embed(title="Denied", description="You do not own this VPS.", color=discord.Color.red()),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        await run_management_action(interaction, vps, action)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success, emoji="▶️")
    async def start(self, interaction, _):
        await self._operation(interaction, "start")

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.secondary, emoji="⏹️")
    async def stop(self, interaction, _):
        await self._operation(interaction, "stop")

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.primary, emoji="🔄")
    async def restart(self, interaction, _):
        await self._operation(interaction, "restart")

    @discord.ui.button(label="SSH", style=discord.ButtonStyle.secondary, emoji="🔐")
    async def ssh(self, interaction, _):
        await self._operation(interaction, "ssh")

    @discord.ui.button(label="Reinstall", style=discord.ButtonStyle.danger, emoji="♻️")
    async def reinstall(self, interaction, _):
        await self._operation(interaction, "reinstall")

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete(self, interaction, _):
        await self._operation(interaction, "delete")


class ConfirmActionView(discord.ui.View):
    def __init__(self, vps_id, action):
        super().__init__(timeout=60)
        self.vps_id = vps_id
        self.action = action

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        vps = db.get_vps_by_vps_id(self.vps_id)
        if not vps:
            await interaction.response.edit_message(
                embed=brand_embed(title="Error", description="VPS no longer exists.", color=discord.Color.red()),
                view=None,
            )
            return
        if vps["discord_user_id"] != interaction.user.id and not is_admin(interaction):
            await interaction.response.edit_message(
                embed=brand_embed(title="Denied", description="You do not own this VPS.", color=discord.Color.red()),
                view=None,
            )
            return
        if self.action == "delete":
            await _confirm_delete(interaction, vps)
        else:
            await _confirm_reinstall(interaction, vps)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            embed=brand_embed(title="Cancelled", description="Action cancelled.", color=discord.Color.red()),
            view=None,
        )


class AdminView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="View All VPS", style=discord.ButtonStyle.primary, emoji="📋")
    async def all_vps(self, interaction: discord.Interaction, _):
        await show_all_vps(interaction)

    @discord.ui.button(label="Host Resources", style=discord.ButtonStyle.primary, emoji="🖥️")
    async def resources(self, interaction: discord.Interaction, _):
        await show_host_resources(interaction)

    @discord.ui.button(label="Settings", style=discord.ButtonStyle.secondary, emoji="⚙️")
    async def settings(self, interaction: discord.Interaction, _):
        await show_settings(interaction)

    @discord.ui.button(label="Toggle Creation", style=discord.ButtonStyle.danger, emoji="🔒")
    async def toggle(self, interaction: discord.Interaction, _):
        current = db.settings_bool("creation_enabled", True)
        db.set_setting("creation_enabled", "false" if current else "true")
        db.log_audit(interaction.user.id, "toggle_creation", f"enabled={not current}")
        await interaction.response.edit_message(
            embed=brand_embed(
                title="VPS Creation",
                description=f"VPS creation is now **{'enabled' if not current else 'disabled'}**.",
                color=discord.Color.green() if not current else discord.Color.red(),
            ),
            view=None,
        )

    @discord.ui.button(label="Audit Log", style=discord.ButtonStyle.secondary, emoji="📝")
    async def audit(self, interaction: discord.Interaction, _):
        await show_audit(interaction)


# --------------------------------------------------------------------------
# Flow steps
# --------------------------------------------------------------------------
async def start_ram_step(interaction: discord.Interaction, os_name):
    spec = {"os": os_name}
    await interaction.response.send_modal(RamModal(spec))


async def start_storage_step(interaction: discord.Interaction, spec):
    os_name = config.os_names.get(spec["os"], spec["os"].title())
    await interaction.response.send_message(
        embed=brand_embed(
            title="💾 RAM Set",
            description=f"**OS:** {os_name}\n**RAM:** {spec['ram']} GB\n\nClick **Continue** to set Storage (💿).",
        ),
        view=NextStepView(spec, "storage"),
        ephemeral=True,
    )


async def start_cpu_step(interaction: discord.Interaction, spec):
    os_name = config.os_names.get(spec["os"], spec["os"].title())
    await interaction.response.send_message(
        embed=brand_embed(
            title="💿 Storage Set",
            description=f"**OS:** {os_name}\n**RAM:** {spec['ram']} GB\n**Storage:** {spec['disk']} GB\n\nClick **Continue** to set CPU (⚙️).",
        ),
        view=NextStepView(spec, "cpu"),
        ephemeral=True,
    )


async def show_confirmation(interaction: discord.Interaction, spec):
    valid, reason = vpslib.check_spec(
        spec["os"], int(spec["cpu"]), int(spec["ram"]), int(spec["disk"])
    )
    if not valid:
        await interaction.response.send_message(
            embed=brand_embed(title="Invalid Configuration", description=reason, color=discord.Color.red()),
            ephemeral=True,
        )
        return
    embed = spec_embed(interaction, spec)
    embed.description += "\n**Ready to deploy?**"
    await interaction.response.send_message(embed=embed, view=ConfirmView(spec), ephemeral=True)


async def deploy_vps(interaction: discord.Interaction, spec):
    user_id = interaction.user.id

    if db.is_banned(user_id):
        await interaction.response.edit_message(
            embed=brand_embed(title="Banned", description="You are banned from creating VPS instances.", color=discord.Color.red()),
            view=None,
        )
        return

    state = vpslib.allocation_state()
    owned = len(db.list_user_vps(user_id))
    if owned >= state["max_vps_per_user"]:
        await interaction.response.edit_message(
            embed=brand_embed(
                title="Limit Reached",
                description=f"You have reached the limit of {state['max_vps_per_user']} VPS instances.",
                color=discord.Color.red(),
            ),
            view=None,
        )
        return

    wait = rate_limited(user_id)
    if wait:
        await interaction.response.edit_message(
            embed=brand_embed(
                title="Slow down",
                description=f"Please wait {wait}s between deployments.",
                color=discord.Color.red(),
            ),
            view=None,
        )
        return

    lock = user_lock(user_id)
    if lock.locked():
        await interaction.response.edit_message(
            embed=brand_embed(
                title="Busy",
                description="You already have a deployment in progress.",
                color=discord.Color.red(),
            ),
            view=None,
        )
        return

    valid, reason = vpslib.check_spec(
        spec["os"], int(spec["cpu"]), int(spec["ram"]), int(spec["disk"])
    )
    if not valid:
        await interaction.response.edit_message(
            embed=brand_embed(title="Invalid Configuration", description=reason, color=discord.Color.red()),
            view=None,
        )
        return

    await interaction.response.defer(ephemeral=True)
    vps = db.create_vps_record(
        user_id, spec["os"], int(spec["cpu"]), int(spec["ram"]), int(spec["disk"])
    )
    progress = Progress(DEPLOY_STEPS)

    async def update_embed(progress_obj):
        embed = brand_embed(
            title="🚀 Deploying VPS",
            description=f"VPS ID: {vps['vps_id']}\n\n" + progress_obj.render(),
        )
        await interaction.edit_original_response(embed=embed, view=None)

    await update_embed(progress)

    async def worker():
        async with global_lock():
            async with lock:
                try:
                    tmate_session, ip_address = await _run_deployment(
                        interaction, spec, vps, update_embed, progress
                    )
                    os_name = config.os_names.get(spec["os"], spec["os"].title())
                    emu_note = ""
                    if vpslib.acceleration() == "tcg":
                        emu_note = (
                            "\n⚠️ **Software emulation mode** - this VPS runs via QEMU TCG "
                            "(no /dev/kvm), so it will be slower than a hardware-accelerated VPS.\n"
                        )
                    success = brand_embed(
                        title="✅ VPS Successfully Created",
                        description=(
                            f"VPS ID: **{vps['vps_id']}**\n"
                            f"OS: {os_name}\n"
                            f"CPU: {spec['cpu']} Cores\n"
                            f"RAM: {spec['ram']} GB\n"
                            f"Storage: {spec['disk']} GB\n"
                            f"Status: 🟢 Online\n"
                            f"{emu_note}"
                        ),
                        color=discord.Color.green(),
                    )
                    if tmate_session:
                        success.add_field(
                            name="🔐 SSH",
                            value=f"```\n{tmate_session}\n```\n⚠️ Keep this connection private.",
                            inline=False,
                        )
                    else:
                        success.add_field(
                            name="🔐 SSH",
                            value=f"`ssh {config.ssh_user}@{ip_address}` (tmate disabled)",
                            inline=False,
                        )
                    await interaction.edit_original_response(embed=success, view=None)
                    try:
                        await interaction.user.send(
                            embed=brand_embed(
                                title=f"{config.bot_name} - VPS Access",
                                description=f"Your VPS **{vps['vps_id']}** is online.",
                                color=discord.Color.green(),
                            ).add_field(
                                name="🔐 SSH",
                                value=f"```\n{tmate_session}\n```\n⚠️ Keep this connection private.",
                                inline=False,
                            )
                        )
                    except discord.Forbidden:
                        logger.warning("Cannot DM user %s", user_id)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Deploy worker error for %s: %s", vps["vps_id"], exc)
                    await interaction.edit_original_response(
                        embed=brand_embed(
                            title="❌ Deployment Failed",
                            description=f"VPS ID: {vps['vps_id']}\n\n{exc}\n\nAll created resources were cleaned up.",
                            color=discord.Color.red(),
                        ),
                        view=None,
                    )

    asyncio.create_task(worker())


async def show_my_vps(interaction: discord.Interaction):
    vps_list = db.list_user_vps(interaction.user.id)
    if not vps_list:
        await interaction.response.edit_message(
            embed=brand_embed(
                title="📦 Your VPS",
                description="You have no VPS instances yet. Use **Create VPS** to deploy one.",
            ),
            view=HomeView(),
        )
        return
    embed = brand_embed(title="📦 Your VPS", color=discord.Color.blue())
    for v in vps_list[:25]:
        status = "🟢 Online" if v["status"] == "running" else "🔴 Offline"
        os_name = config.os_names.get(v["os"], v["os"].title())
        embed.add_field(
            name=f"{v['vps_id']} - {status}",
            value=(
                f"{os_name}\n"
                f"{v['ram']} GB RAM\n"
                f"{v['cpu']} CPU\n"
                f"{v['disk']} GB Storage"
            ),
            inline=True,
        )
    await interaction.response.edit_message(embed=embed, view=HomeView())


async def show_manage(interaction: discord.Interaction):
    vps_list = db.list_user_vps(interaction.user.id)
    if not vps_list:
        await interaction.response.edit_message(
            embed=brand_embed(
                title="⚙️ Manage VPS",
                description="You have no VPS instances yet.",
            ),
            view=HomeView(),
        )
        return
    await interaction.response.edit_message(
        embed=brand_embed(title="⚙️ Manage VPS", description="Select a VPS to manage."),
        view=VpsSelectView(vps_list),
    )


async def show_manage_actions(interaction: discord.Interaction, vps_id):
    vps = db.get_vps_by_vps_id(vps_id)
    if not vps:
        await interaction.response.edit_message(
            embed=brand_embed(title="Error", description="VPS no longer exists.", color=discord.Color.red()),
            view=HomeView(),
        )
        return
    os_name = config.os_names.get(vps["os"], vps["os"].title())
    status = "🟢 Online" if vps["status"] == "running" else "🔴 Offline"
    embed = brand_embed(
        title=f"⚙️ {vps['vps_id']}",
        description=(
            f"OS: {os_name}\n"
            f"CPU: {vps['cpu']} Cores\n"
            f"RAM: {vps['ram']} GB\n"
            f"Storage: {vps['disk']} GB\n"
            f"Status: {status}"
        ),
    )
    await interaction.response.edit_message(embed=embed, view=ManageActionsView(vps_id))


async def run_management_action(interaction, vps, action):
    """Execute a management action for a VPS (user or admin)."""
    vm_name = vps["vm_name"]
    embed = None
    try:
        if action == "start":
            ok = await vpslib.start_vm(vm_name)
            db.set_vps_status(vps["vps_id"], "running" if ok else "stopped")
            embed = _action_result("Start", ok)
        elif action == "stop":
            ok = await vpslib.stop_vm(vm_name)
            db.set_vps_status(vps["vps_id"], "stopped" if ok else "running")
            embed = _action_result("Stop", ok)
        elif action == "restart":
            ok = await vpslib.restart_vm(vm_name)
            db.set_vps_status(vps["vps_id"], "running" if ok else "stopped")
            embed = _action_result("Restart", ok)
        elif action == "delete":
            await interaction.followup.send(
                embed=brand_embed(
                    title="⚠️ Confirm Delete",
                    description=f"This will permanently delete {vps['vps_id']} and its disk.",
                    color=discord.Color.red(),
                ),
                view=ConfirmActionView(vps["vps_id"], "delete"),
                ephemeral=True,
            )
            return
        elif action == "reinstall":
            await interaction.followup.send(
                embed=brand_embed(
                    title="⚠️ Confirm Reinstall",
                    description=f"This will reinstall {vps['vps_id']} with the same resources.",
                    color=discord.Color.red(),
                ),
                view=ConfirmActionView(vps["vps_id"], "reinstall"),
                ephemeral=True,
            )
            return
        elif action == "ssh":
            await regen_ssh(interaction, vps)
            return
    except vpslib.VpsError as exc:
        embed = brand_embed(title=f"{action.title()} Failed", description=str(exc), color=discord.Color.red())

    if embed:
        await interaction.followup.send(embed=embed, ephemeral=True)


async def _confirm_delete(interaction, vps):
    await interaction.response.defer(ephemeral=True)
    await vpslib.delete_vm(vps["vm_name"])
    vpslib.remove_vps_files(vps)
    db.delete_vps_record(vps["vps_id"])
    db.log_audit(interaction.user.id, "vps_deleted", vps["vps_id"])
    await interaction.followup.send(
        embed=brand_embed(
            title="🗑️ VPS Deleted",
            description=f"{vps['vps_id']} has been permanently deleted.",
            color=discord.Color.green(),
        ),
        ephemeral=True,
    )


async def _confirm_reinstall(interaction, vps):
    await interaction.response.defer(ephemeral=True)
    progress = Progress(DEPLOY_STEPS)
    embed = brand_embed(
        title="♻️ Reinstalling VPS",
        description=f"VPS ID: {vps['vps_id']}\n\n" + progress.render(),
    )
    await interaction.edit_original_response(embed=embed, view=None)

    async def update_embed(progress_obj):
        embed.description = f"VPS ID: {vps['vps_id']}\n\n" + progress_obj.render()
        await interaction.edit_original_response(embed=embed)

    try:
        await vpslib.delete_vm(vps["vm_name"])
        vpslib.remove_vps_files(vps)
        db.update_vps(vps["vps_id"], status="deploying", tmate_session=None, ip_address=None)
        pubkey = await asyncio.to_thread(vpslib.ensure_ssh_key)
        fresh = db.get_vps_by_vps_id(vps["vps_id"])
        disk_path, seed_path, instance_dir = await vpslib.create_vm(
            fresh, pubkey, progress=progress.mark
        )
        db.update_vps(vps["vps_id"], disk_path=disk_path, seed_path=seed_path, instance_dir=instance_dir)
        await update_embed(progress)
        ip_address = await vpslib.wait_for_ip(vps["vm_name"], timeout=180, progress=progress.mark)
        db.update_vps(vps["vps_id"], ip_address=ip_address)
        await update_embed(progress)
        await tmate_lib.wait_for_ssh(ip_address, timeout=config.boot_timeout)
        await tmate_lib.wait_for_cloud_init(ip_address, timeout=config.boot_timeout)
        await progress.mark("Installing tmate")
        await update_embed(progress)
        session = None
        if config.tmate_enabled:
            await tmate_lib.install_tmate(ip_address, timeout=300)
            session = await tmate_lib.start_tmate(ip_address, timeout=240)
        await progress.mark("Generating SSH access")
        await update_embed(progress)
        db.update_vps(vps["vps_id"], status="running", tmate_session=session or None)
        db.log_audit(interaction.user.id, "vps_reinstalled", vps["vps_id"])
        result = brand_embed(
            title="✅ VPS Reinstalled",
            description=f"{vps['vps_id']} is back online.",
            color=discord.Color.green(),
        )
        if session:
            result.add_field(name="🔐 SSH", value=f"```\n{session}\n```", inline=False)
        await interaction.edit_original_response(embed=result)
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
        tail = _serial_tail(vps)
        if tail:
            detail += "\n\n--- serial log (tail) ---\n" + tail
        logger.error("Reinstall failed for %s: %s", vps["vps_id"], detail)
        await vpslib.cleanup_failed(vps)
        await interaction.edit_original_response(
            embed=brand_embed(
                title="❌ Reinstall Failed",
                description=detail,
                color=discord.Color.red(),
            )
        )


def _action_result(action, ok):
    past = {
        "start": "started",
        "stop": "stopped",
        "restart": "restarted",
        "kill": "killed",
        "delete": "deleted",
        "shutdown": "shut down",
    }.get(action.lower(), action.lower() + "ed")
    if ok:
        return brand_embed(
            title=f"{action.title()} Successful",
            description=f"VPS {past} successfully.",
            color=discord.Color.green(),
        )
    return brand_embed(title=f"{action} Failed", description="The operation did not complete.", color=discord.Color.red())


async def regen_ssh(interaction, vps):
    """Regenerate / resend the tmate connection for a VPS."""
    if vps["status"] != "running" or not vps.get("ip_address"):
        await interaction.followup.send(
            embed=brand_embed(
                title="SSH Unavailable",
                description="The VPS must be running to generate SSH access.",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )
        return
    ip_address = vps["ip_address"]
    session = vps.get("tmate_session")
    if not session and config.tmate_enabled:
        try:
            await tmate_lib.wait_for_ssh(ip_address, timeout=60)
            await tmate_lib.install_tmate(ip_address, timeout=240)
            session = await tmate_lib.start_tmate(ip_address, timeout=180)
            db.update_vps(vps["vps_id"], tmate_session=session)
        except tmate_lib.TmateError as exc:
            await interaction.followup.send(
                embed=brand_embed(title="SSH Generation Failed", description=str(exc), color=discord.Color.red()),
                ephemeral=True,
            )
            return
    conn = session or f"ssh {config.ssh_user}@{ip_address}"
    embed = brand_embed(title="🔐 SSH Access")
    embed.add_field(name="SSH", value=f"```\n{conn}\n```\n⚠️ Keep this connection private.", inline=False)
    try:
        await interaction.user.send(embed=embed)
        await interaction.followup.send(
            embed=brand_embed(title="Sent", description="SSH access sent to your DMs.", color=discord.Color.green()),
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.followup.send(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------
# Admin displays
# --------------------------------------------------------------------------
async def show_all_vps(interaction: discord.Interaction):
    all_vps = db.get_all_vps()
    if not all_vps:
        await interaction.response.edit_message(
            embed=brand_embed(title="📋 All VPS", description="No VPS instances exist.", color=discord.Color.blue()),
            view=AdminView(),
        )
        return
    embed = brand_embed(title="📋 All VPS", color=discord.Color.blue())
    for v in all_vps[:15]:
        status = "🟢" if v["status"] == "running" else "🔴"
        owner = db.get_user(v["discord_user_id"])
        name = owner["username"] if owner else str(v["discord_user_id"])
        embed.add_field(
            name=f"{status} {v['vps_id']} - {name}",
            value=(
                f"OS: {config.os_names.get(v['os'], v['os'])} | "
                f"{v['ram']} GB | {v['cpu']} CPU | {v['disk']} GB\n"
                f"Status: {v['status']}"
            ),
            inline=False,
        )
    embed.set_footer(text=f"{config.bot_footer} | Showing first {min(15, len(all_vps))} of {len(all_vps)}")
    await interaction.response.edit_message(embed=embed, view=AdminView())


def build_host_resources_embed():
    state = vpslib.allocation_state()
    ram_over = "🟢 Enabled" if state["ram_overcommit"] else "🔴 Disabled"
    cpu_over = "🟢 Enabled" if state["cpu_overcommit"] else "🔴 Disabled"
    embed = brand_embed(title="🖥️ Host Resources", color=discord.Color.blue())
    embed.add_field(
        name="RAM",
        value=(
            f"Physical RAM: {state['physical_ram_gb']:.1f} GB\n"
            f"Allocated VPS RAM: {state['allocated_ram_gb']:.1f} GB\n"
            f"Maximum Allocation: {state['max_ram_gb']:.1f} GB\n"
            f"RAM Overcommit: {ram_over}"
        ),
        inline=False,
    )
    embed.add_field(
        name="CPU",
        value=(
            f"Physical CPU: {state['physical_cpu']} Cores\n"
            f"Allocated VPS CPU: {state['allocated_cpu']} vCPU\n"
            f"Maximum Allocation: {state['max_cpu']} vCPU\n"
            f"CPU Overcommit: {cpu_over}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Storage",
        value=(
            f"Physical Storage: {state['physical_storage_gb']:.1f} GB\n"
            f"Free Storage: {state['free_storage_gb']:.1f} GB\n"
            f"Allocated VPS Storage: {state['allocated_disk_gb']} GB"
        ),
        inline=False,
    )
    embed.add_field(
        name="Virtualization",
        value=(
            f"Acceleration: {'🟢 KVM' if state['acceleration'] == 'kvm' else '🟡 Software emulation (TCG)'}\n"
            f"Mode: {'hardware-accelerated' if state['acceleration'] == 'kvm' else 'QEMU software emulation (slower)'}"
        ),
        inline=False,
    )
    counts = db.vps_counts()
    embed.add_field(
        name="VPS",
        value=f"Created: {counts['total']}\nRunning: {counts['running']}\nStopped: {counts['stopped']}",
        inline=False,
    )
    return embed


async def show_host_resources(interaction: discord.Interaction):
    embed = build_host_resources_embed()
    await interaction.response.edit_message(embed=embed, view=AdminView())


async def show_settings(interaction: discord.Interaction):
    state = vpslib.allocation_state()
    embed = brand_embed(title="⚙️ Admin Settings", color=discord.Color.blue())
    embed.add_field(name="VPS Creation", value="🟢 Enabled" if state["creation_enabled"] else "🔴 Disabled")
    embed.add_field(name="RAM Overcommit", value="🟢 Enabled" if state["ram_overcommit"] else "🔴 Disabled")
    embed.add_field(name="CPU Overcommit", value="🟢 Enabled" if state["cpu_overcommit"] else "🔴 Disabled")
    embed.add_field(name="Max Allocation RAM", value=f"{state['max_ram_gb']:.1f} GB")
    embed.add_field(name="Max Allocation CPU", value=f"{state['max_cpu']} vCPU")
    embed.add_field(name="Max Disk per VPS", value=f"{int(state['max_disk_per_vps'])} GB")
    embed.add_field(name="Max VPS per User", value=state["max_vps_per_user"])
    embed.description = "Use `/admin config <key> <value>` to change a limit.\nKeys: `creation_enabled`, `ram_overcommit`, `cpu_overcommit`, `max_allocated_ram`, `max_allocated_cpu`, `max_disk_per_vps`, `max_vps_per_user`."
    await interaction.response.edit_message(embed=embed, view=AdminView())


async def show_audit(interaction: discord.Interaction):
    entries = db.recent_audit(15)
    if not entries:
        await interaction.response.edit_message(
            embed=brand_embed(title="📝 Audit Log", description="No audit entries yet.", color=discord.Color.blue()),
            view=AdminView(),
        )
        return
    embed = brand_embed(title="📝 Audit Log", color=discord.Color.blue())
    for e in entries:
        embed.add_field(
            name=f"{e['created_at']} - {e['action']}",
            value=f"User: {e['discord_user_id']}\n{e['details'] or ''}"[:200],
            inline=False,
        )
    await interaction.response.edit_message(embed=embed, view=AdminView())


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
@bot.tree.command(name="vps", description="Open the VPS management panel")
async def vps_panel(interaction: discord.Interaction):
    db.add_user(interaction.user.id, str(interaction.user))
    embed = brand_embed(
        title=f"{config.bot_emoji} {config.bot_name} VPS",
        description="Create and manage your VPS directly from Discord.",
    )
    if config.bot_website:
        embed.description += f"\n\n🔗 {config.bot_website}"
    if config.bot_support:
        embed.description += f"\n🛟 Support: {config.bot_support}"
    await interaction.response.send_message(embed=embed, view=HomeView(), ephemeral=True)


@bot.tree.command(name="ping", description="Check the bot latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=brand_embed(title="🏓 Pong!", description=f"Latency: {round(bot.latency * 1000)} ms", color=discord.Color.green()),
        ephemeral=True,
    )


@bot.tree.command(name="about", description="About this bot")
async def about(interaction: discord.Interaction):
    embed = brand_embed(
        title=f"About {config.bot_name}",
        description=(
            f"**{config.bot_name}** is a white-label VPS deployment bot.\n\n"
            "Every VPS is a **real KVM/QEMU virtual machine** created locally on the host with libvirt, "
            "configured with cloud-init and accessed through a private tmate SSH session."
        ),
    )
    embed.add_field(name="Status", value="🟢 Online", inline=True)
    embed.add_field(name="Framework", value="Python • discord.py", inline=True)
    accel = "KVM/QEMU • libvirt" if vpslib.acceleration() == "kvm" else "QEMU (software emulation) • libvirt"
    embed.add_field(name="Virtualization", value=accel, inline=True)
    if config.bot_website:
        embed.add_field(name="Website", value=config.bot_website, inline=True)
    if config.bot_support:
        embed.add_field(name="Support", value=config.bot_support, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- Admin command group ---------------------------------------------------
admin_group = app_commands.Group(name="admin", description="Admin commands")


@admin_group.command(name="panel", description="Open the admin panel")
@app_commands.guild_only()
async def admin_panel(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(
            embed=brand_embed(title="Denied", description="This command is restricted to admins.", color=discord.Color.red()),
            ephemeral=True,
        )
        return
    counts = db.vps_counts()
    state = vpslib.allocation_state()
    issues = [msg for ok, msg in vpslib.check_environment() if not ok]
    embed = brand_embed(
        title=f"🛠️ {config.bot_name} VPS Admin",
        description=(
            f"VPS Created: {counts['total']}\n"
            f"VPS Running: {counts['running']}\n"
            f"VPS Stopped: {counts['stopped']}\n\n"
            f"Physical CPU: {state['physical_cpu']} Cores\n"
            f"Allocated CPU: {state['allocated_cpu']} vCPU\n\n"
            f"Physical RAM: {state['physical_ram_gb']:.1f} GB\n"
            f"Allocated RAM: {state['allocated_ram_gb']:.1f} GB\n"
            f"Maximum Allocation: {state['max_ram_gb']:.1f} GB\n\n"
            f"RAM Overcommit: {'🟢 Enabled' if state['ram_overcommit'] else '🔴 Disabled'}\n"
            f"CPU Overcommit: {'🟢 Enabled' if state['cpu_overcommit'] else '🔴 Disabled'}"
        ),
        color=discord.Color.blue(),
    )
    if issues:
        embed.add_field(name="⚠️ Host Checks", value="\n".join(f"❌ {i}" for i in issues), inline=False)
    await interaction.response.send_message(embed=embed, view=AdminView(), ephemeral=True)


@admin_group.command(name="config", description="Change a runtime setting")
@app_commands.describe(key="Setting key", value="New value")
@app_commands.guild_only()
async def admin_config(interaction: discord.Interaction, key: str, value: str):
    if not is_admin(interaction):
        await interaction.response.send_message(
            embed=brand_embed(title="Denied", description="This command is restricted to admins.", color=discord.Color.red()),
            ephemeral=True,
        )
        return
    allowed = {
        "creation_enabled", "ram_overcommit", "cpu_overcommit",
        "max_allocated_ram", "max_allocated_cpu", "max_disk_per_vps",
        "max_vps_per_user",
    }
    key = key.strip().lower()
    if key not in allowed:
        await interaction.response.send_message(
            embed=brand_embed(title="Invalid Key", description="Allowed keys: " + ", ".join(sorted(allowed)), color=discord.Color.red()),
            ephemeral=True,
        )
        return
    db.set_setting(key, value)
    db.log_audit(interaction.user.id, "config_change", f"{key}={value}")
    await interaction.response.send_message(
        embed=brand_embed(
            title="Setting Updated",
            description=f"`{key}` is now `{value}`.",
            color=discord.Color.green(),
        ),
        ephemeral=True,
    )


vps_group = app_commands.Group(name="vps", description="Admin VPS operations", parent=admin_group)


async def _require_admin(interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(
            embed=brand_embed(title="Denied", description="This command is restricted to admins.", color=discord.Color.red()),
            ephemeral=True,
        )
        return False
    return True


@vps_group.command(name="list", description="List all VPS")
@app_commands.guild_only()
async def admin_vps_list(interaction: discord.Interaction):
    if not await _require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    all_vps = db.get_all_vps()
    if not all_vps:
        await interaction.followup.send(
            embed=brand_embed(title="📋 All VPS", description="No VPS instances exist.", color=discord.Color.blue()),
            ephemeral=True,
        )
        return
    embed = brand_embed(title="📋 All VPS", color=discord.Color.blue())
    for v in all_vps[:15]:
        status = "🟢" if v["status"] == "running" else "🔴"
        owner = db.get_user(v["discord_user_id"])
        name = owner["username"] if owner else str(v["discord_user_id"])
        embed.add_field(
            name=f"{status} {v['vps_id']} - {name}",
            value=(
                f"OS: {config.os_names.get(v['os'], v['os'])} | "
                f"{v['ram']} GB | {v['cpu']} CPU | {v['disk']} GB\n"
                f"Status: {v['status']} | IP: {v.get('ip_address') or 'N/A'}"
            ),
            inline=False,
        )
    embed.set_footer(text=f"{config.bot_footer} | Showing first {min(15, len(all_vps))} of {len(all_vps)}")
    await interaction.followup.send(embed=embed, ephemeral=True)


@vps_group.command(name="info", description="Show details for a VPS")
@app_commands.describe(vps_id="VPS ID, e.g. VPS-0001")
@app_commands.guild_only()
async def admin_vps_info(interaction: discord.Interaction, vps_id: str):
    if not await _require_admin(interaction):
        return
    vps = resolve_vps(vps_id)
    if not vps:
        await interaction.response.send_message(
            embed=brand_embed(title="Not Found", description=f"No VPS found for `{vps_id}`.", color=discord.Color.red()),
            ephemeral=True,
        )
        return
    real_status = vpslib.status_of(vps["vm_name"])
    owner = db.get_user(vps["discord_user_id"])
    owner_name = owner["username"] if owner else str(vps["discord_user_id"])
    embed = brand_embed(title=f"{vps['vps_id']} - {vps['vm_name']}", color=discord.Color.blue())
    embed.add_field(name="Owner", value=owner_name, inline=True)
    embed.add_field(name="OS", value=config.os_names.get(vps["os"], vps["os"]), inline=True)
    embed.add_field(name="Status", value=f"{real_status}", inline=True)
    embed.add_field(name="CPU", value=f"{vps['cpu']} cores", inline=True)
    embed.add_field(name="RAM", value=f"{vps['ram']} GB", inline=True)
    embed.add_field(name="Disk", value=f"{vps['disk']} GB", inline=True)
    embed.add_field(name="IP Address", value=vps.get("ip_address") or "N/A", inline=True)
    embed.add_field(name="Created", value=vps["created_at"], inline=True)
    embed.add_field(name="VM UUID", value=f"`{vps['vm_uuid']}`", inline=False)
    if vps.get("tmate_session"):
        embed.add_field(name="SSH", value=f"```\n{vps['tmate_session']}\n```", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@vps_group.command(name="start", description="Start a VPS")
@app_commands.describe(vps_id="VPS ID, e.g. VPS-0001")
@app_commands.guild_only()
async def admin_vps_start(interaction: discord.Interaction, vps_id: str):
    await admin_vps_operate(interaction, vps_id, "start")


@vps_group.command(name="stop", description="Stop a VPS")
@app_commands.describe(vps_id="VPS ID, e.g. VPS-0001")
@app_commands.guild_only()
async def admin_vps_stop(interaction: discord.Interaction, vps_id: str):
    await admin_vps_operate(interaction, vps_id, "stop")


@vps_group.command(name="restart", description="Restart a VPS")
@app_commands.describe(vps_id="VPS ID, e.g. VPS-0001")
@app_commands.guild_only()
async def admin_vps_restart(interaction: discord.Interaction, vps_id: str):
    await admin_vps_operate(interaction, vps_id, "restart")


@vps_group.command(name="kill", description="Force stop a VPS")
@app_commands.describe(vps_id="VPS ID, e.g. VPS-0001")
@app_commands.guild_only()
async def admin_vps_kill(interaction: discord.Interaction, vps_id: str):
    await admin_vps_operate(interaction, vps_id, "kill")


@vps_group.command(name="delete", description="Permanently delete a VPS")
@app_commands.describe(vps_id="VPS ID, e.g. VPS-0001")
@app_commands.guild_only()
async def admin_vps_delete(interaction: discord.Interaction, vps_id: str):
    await admin_vps_operate(interaction, vps_id, "delete")


@vps_group.command(name="reinstall", description="Reinstall a VPS")
@app_commands.describe(vps_id="VPS ID, e.g. VPS-0001")
@app_commands.guild_only()
async def admin_vps_reinstall(interaction: discord.Interaction, vps_id: str):
    await admin_vps_operate(interaction, vps_id, "reinstall")


@vps_group.command(name="resources", description="Show host resources and overcommit state")
@app_commands.guild_only()
async def admin_vps_resources(interaction: discord.Interaction):
    if not await _require_admin(interaction):
        return
    await interaction.response.send_message(
        embed=build_host_resources_embed(), ephemeral=True
    )


async def admin_vps_operate(interaction: discord.Interaction, vps_id: str, action: str):
    if not await _require_admin(interaction):
        return
    vps = resolve_vps(vps_id)
    if not vps:
        await interaction.response.send_message(
            embed=brand_embed(title="Not Found", description=f"No VPS found for `{vps_id}`.", color=discord.Color.red()),
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    try:
        if action == "delete":
            await vpslib.delete_vm(vps["vm_name"])
            vpslib.remove_vps_files(vps)
            db.delete_vps_record(vps["vps_id"])
            db.log_audit(interaction.user.id, "admin_vps_delete", vps["vps_id"])
            embed = _action_result("Delete", True)
        elif action == "reinstall":
            # Reuse the confirm flow logic via a direct deployment.
            db.log_audit(interaction.user.id, "admin_vps_reinstall", vps["vps_id"])
            await _admin_reinstall(interaction, vps)
            return
        elif action == "kill":
            ok = await vpslib.kill_vm(vps["vm_name"])
            db.set_vps_status(vps["vps_id"], "stopped" if ok else vps["status"])
            db.log_audit(interaction.user.id, "admin_vps_kill", vps["vps_id"])
            embed = _action_result("Kill", ok)
        elif action == "start":
            ok = await vpslib.start_vm(vps["vm_name"])
            db.set_vps_status(vps["vps_id"], "running" if ok else "stopped")
            db.log_audit(interaction.user.id, "admin_vps_start", vps["vps_id"])
            embed = _action_result("Start", ok)
        elif action == "stop":
            ok = await vpslib.stop_vm(vps["vm_name"])
            db.set_vps_status(vps["vps_id"], "stopped" if ok else "running")
            db.log_audit(interaction.user.id, "admin_vps_stop", vps["vps_id"])
            embed = _action_result("Stop", ok)
        elif action == "restart":
            ok = await vpslib.restart_vm(vps["vm_name"])
            db.set_vps_status(vps["vps_id"], "running" if ok else "stopped")
            db.log_audit(interaction.user.id, "admin_vps_restart", vps["vps_id"])
            embed = _action_result("Restart", ok)
        else:
            embed = brand_embed(title="Unknown Action", color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
    except vpslib.VpsError as exc:
        await interaction.followup.send(
            embed=brand_embed(title=f"{action.title()} Failed", description=str(exc), color=discord.Color.red()),
            ephemeral=True,
        )


async def _admin_reinstall(interaction, vps):
    progress = Progress(DEPLOY_STEPS)
    embed = brand_embed(
        title="♻️ Reinstalling VPS",
        description=f"VPS ID: {vps['vps_id']}\n\n" + progress.render(),
    )
    await interaction.edit_original_response(embed=embed)

    async def update_embed(progress_obj):
        embed.description = f"VPS ID: {vps['vps_id']}\n\n" + progress_obj.render()
        await interaction.edit_original_response(embed=embed)

    try:
        await vpslib.delete_vm(vps["vm_name"])
        vpslib.remove_vps_files(vps)
        db.update_vps(vps["vps_id"], status="deploying", tmate_session=None, ip_address=None)
        pubkey = await asyncio.to_thread(vpslib.ensure_ssh_key)
        fresh = db.get_vps_by_vps_id(vps["vps_id"])
        disk_path, seed_path, instance_dir = await vpslib.create_vm(fresh, pubkey, progress=progress.mark)
        db.update_vps(vps["vps_id"], disk_path=disk_path, seed_path=seed_path, instance_dir=instance_dir)
        await update_embed(progress)
        ip_address = await vpslib.wait_for_ip(vps["vm_name"], timeout=180, progress=progress.mark)
        db.update_vps(vps["vps_id"], ip_address=ip_address)
        await update_embed(progress)
        await tmate_lib.wait_for_ssh(ip_address, timeout=config.boot_timeout)
        await tmate_lib.wait_for_cloud_init(ip_address, timeout=config.boot_timeout)
        await progress.mark("Installing tmate")
        await update_embed(progress)
        session = None
        if config.tmate_enabled:
            await tmate_lib.install_tmate(ip_address, timeout=300)
            session = await tmate_lib.start_tmate(ip_address, timeout=240)
        await progress.mark("Generating SSH access")
        await update_embed(progress)
        db.update_vps(vps["vps_id"], status="running", tmate_session=session or None)
        result = brand_embed(
            title="✅ VPS Reinstalled",
            description=f"{vps['vps_id']} is back online.",
            color=discord.Color.green(),
        )
        if session:
            result.add_field(name="🔐 SSH", value=f"```\n{session}\n```", inline=False)
        await interaction.edit_original_response(embed=result)
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
        tail = _serial_tail(vps)
        if tail:
            detail += "\n\n--- serial log (tail) ---\n" + tail
        logger.error("Admin reinstall failed for %s: %s", vps["vps_id"], detail)
        await vpslib.cleanup_failed(vps)
        await interaction.edit_original_response(
            embed=brand_embed(title="❌ Reinstall Failed", description=detail, color=discord.Color.red())
        )


bot.tree.add_command(admin_group)


# --------------------------------------------------------------------------
# Background tasks
# --------------------------------------------------------------------------
@tasks.loop(seconds=15)
async def presence_loop():
    status = config.bot_status
    activity_type = config.bot_activity_type
    if activity_type == "playing":
        activity = discord.Game(name=status)
    elif activity_type == "listening":
        activity = discord.Activity(type=discord.ActivityType.listening, name=status)
    elif activity_type == "streaming":
        activity = discord.Streaming(name=status, url=config.bot_stream_url)
    elif activity_type == "custom":
        activity = discord.CustomActivity(name=status)
    else:  # watching default
        activity = discord.Activity(type=discord.ActivityType.watching, name=status)
    await bot.change_presence(activity=activity)


@tasks.loop(minutes=5)
async def sync_statuses():
    try:
        for vps in db.get_all_vps():
            real = vpslib.status_of(vps["vm_name"])
            if real != vps["status"] and vps["status"] not in ("deploying", "error"):
                db.set_vps_status(vps["vps_id"], real)
                logger.info("Synced status of %s to %s", vps["vps_id"], real)
    except Exception as exc:  # noqa: BLE001
        logger.error("Status sync failed: %s", exc)


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------
@bot.event
async def on_ready():
    presence_loop.start()
    sync_statuses.start()
    logger.info("Bot ready: %s", bot.user)
    issues = [msg for ok, msg in vpslib.check_environment() if not ok]
    if issues:
        logger.warning("Host checks failed:")
        for issue in issues:
            logger.warning("  - %s", issue)
    if not vpslib.host_kvm_available():
        if config.allow_software_emulation:
            logger.warning("/dev/kvm does not exist - running in QEMU software emulation (TCG) mode. VMs are real but slower.")
        else:
            logger.warning("/dev/kvm does not exist - KVM acceleration unavailable and software emulation is disabled!")
    try:
        synced = await bot.tree.sync()
        logger.info("Synced %d commands", len(synced))
    except Exception as exc:  # noqa: BLE001
        logger.error("Command sync failed: %s", exc)


def main():
    if not config.token:
        logger.error("DISCORD_TOKEN is not set in .env")
        sys.exit(1)
    vpslib.ensure_ssh_key()
    bot.run(config.token)


if __name__ == "__main__":
    main()
