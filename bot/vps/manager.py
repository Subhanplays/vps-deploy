"""VPS lifecycle: creation, management, reinstalls, SSH and status sync.

This module is pure business logic - it knows nothing about Discord. The
command/UI layer calls these methods and renders results.
"""

from __future__ import annotations

import asyncio
import logging
import random

import database.models as dbm
from lxd.instances import InstanceSpec, create_instance
from config.settings import sanitize_name

logger = logging.getLogger("vpsbot.vps")


class VpsError(Exception):
    """Business-logic error with a user-safe message."""


class VPSManager:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.settings = app.settings
        self.lxd = app.lxd
        self.stats = app.stats
        self.ssh = app.ssh
        self.resources = app.resources
        self.audit = app.audit

    # ------------------------------------------------------------------
    # Naming
    # ------------------------------------------------------------------
    def generate_name(self) -> str:
        cfg = self.settings.get("name_generator", {})
        prefixes = cfg.get("prefixes") or ["vps"]
        sep = cfg.get("separator", "-")
        digits = int(cfg.get("random_digits", 4) or 4)
        prefix = random.choice(prefixes)
        number = random.randint(10 ** (digits - 1), 10 ** digits - 1)
        return f"{prefix}{sep}{number}"

    def container_name_for(self, name: str) -> str:
        prefix = self.settings.get_str("lxd.container_prefix", "vps")
        return sanitize_name(f"{prefix}-{name}")

    def hostname_for(self, name: str) -> str:
        prefix = self.settings.get_str("vps.hostname_prefix", "node")
        return sanitize_name(f"{prefix}-{name}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _image_map(self, os_key: str) -> dict | None:
        return self.settings.image_by_key(os_key)

    def _spec(self, *, os_key: str, name: str, ram: float, cpu: float, disk: float) -> InstanceSpec:
        image = self._image_map(os_key)
        cfg = self.settings.get("lxd", {})
        return InstanceSpec(
            image=image["image"],
            name=self.container_name_for(name),
            hostname=self.hostname_for(name),
            ram_gb=ram,
            cpu=cpu,
            disk_gb=disk,
            autostart=cfg.get("autostart", True),
            storage_quota_enabled=cfg.get("storage_quota", True),
            storage_pool=cfg.get("storage_pool", "default"),
            profiles=cfg.get("profiles", ["default"]),
            security_privileged=cfg.get("security_privileged", False),
        )

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    async def create(
        self,
        *,
        user_id: int,
        username: str,
        os_key: str,
        ram: float,
        cpu: float,
        disk: float,
        name: str | None = None,
        on_progress=None,
    ) -> dict:
        """Create and provision a VPS. Returns a result dict."""
        dbm.add_user(self.db, user_id, username)

        async def progress(stage: str):
            if on_progress:
                try:
                    await on_progress(stage)
                except Exception:  # noqa: BLE001
                    pass

        image = self._image_map(os_key)
        if image is None:
            return {"ok": False, "error": f"Unknown operating system: {os_key}"}

        ok, msg = self.resources.validate_spec(ram=ram, cpu=cpu, disk=disk)
        if not ok:
            return {"ok": False, "error": msg}

        ok, msg = self.resources.check_limits(user_id=user_id)
        if not ok:
            return {"ok": False, "error": msg}

        ok, rem = self.resources.check_cooldown(user_id=user_id)
        if not ok:
            return {"ok": False, "error": f"Please wait {rem}s before creating another VPS."}

        if self.settings.is_banned(user_id):
            return {"ok": False, "error": "You are banned from creating VPS instances."}

        ok, msg = await self.resources.check_host(ram=ram, cpu=cpu, disk=disk)
        if not ok:
            return {"ok": False, "error": msg}

        final_name = (sanitize_name(name) if name else self.generate_name())
        if not final_name:
            final_name = self.generate_name()

        vps_id = dbm.add_vps(
            self.db,
            user_id=user_id,
            name=final_name,
            os_key=os_key,
            image=image["image"],
            hostname=self.hostname_for(final_name),
            ram=ram,
            cpu=cpu,
            disk=disk,
            status="creating",
        )
        self.audit.log_vps_created(user_id, final_name, os_key, ram, cpu, disk, status="creating")
        container_id = None

        try:
            await progress("Creating instance…")
            instance_name, disk_enforced, error = await create_instance(
                self.lxd, self._spec(os_key=os_key, name=final_name, ram=ram, cpu=cpu, disk=disk)
            )
            if not instance_name:
                raise VpsError(error or "LXD failed to create the instance.")

            # Track the instance so _abort_creation can delete it if any
            # later provisioning step fails.
            container_id = instance_name
            dbm.update_vps_container(self.db, vps_id, instance_name, instance_name)

            # Let cloud-init/DHCP finish before installing packages.
            await asyncio.sleep(self.settings.get_int("lxd.ready_wait", 5))

            await progress("Installing packages…")
            ok, err = await self.ssh.ensure_installed(instance_name)
            if not ok:
                raise VpsError(err or "Failed to install packages.")

            await progress("Establishing SSH session…")
            await asyncio.sleep(self.settings.get_int("ssh.wait_after_install", 10))
            ssh_line, err = await self.ssh.start_session(instance_name)
            if not ssh_line:
                raise VpsError(err or "Failed to establish an SSH session.")

            dbm.update_vps_ssh(self.db, vps_id, ssh_line)
            dbm.update_vps_status(self.db, vps_id, "running")
            self.resources.record_creation(user_id=user_id)
            self.audit.log_vps_created(user_id, final_name, os_key, ram, cpu, disk, status="running")

            return {
                "ok": True,
                "vps_id": vps_id,
                "name": final_name,
                "os_key": os_key,
                "ram": ram,
                "cpu": cpu,
                "disk": disk,
                "ssh": ssh_line,
                "disk_enforced": disk_enforced,
                "error": "",
            }
        except VpsError as exc:
            await self._abort_creation(vps_id, container_id, str(exc))
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.exception("VPS creation failed")
            await self._abort_creation(vps_id, container_id, f"Internal error: {exc}")
            return {"ok": False, "error": "An internal error occurred during provisioning."}

    async def _abort_creation(self, vps_id: str, container_id: str | None, error: str) -> None:
        if container_id:
            await self.lxd.delete(container_id, force=True)
        dbm.update_vps_error(self.db, vps_id, error)
        dbm.update_vps_status(self.db, vps_id, "error")
        self.audit.log_vps_failed(vps_id, error)

    # ------------------------------------------------------------------
    # Ownership + lookup
    # ------------------------------------------------------------------
    def owned(self, vps_id: str, user_id: int) -> dict | None:
        return dbm.get_vps(self.db, vps_id, user_id)

    def admin_get(self, vps_id: str) -> dict | None:
        return dbm.get_vps(self.db, vps_id)

    # ------------------------------------------------------------------
    # Lifecycle actions
    # ------------------------------------------------------------------
    async def start(self, vps_id: str, user_id: int, *, by_admin: bool = False) -> tuple[bool, str]:
        vps = self.owned(vps_id, user_id)
        if not vps:
            return False, "VPS not found."
        if vps["suspended"] and not by_admin:
            return False, "This VPS is suspended by an administrator. Contact support."
        if vps["status"] in ("creating", "reinstalling"):
            return False, "This VPS is still provisioning. Please wait."

        dbm.update_vps_status(self.db, vps_id, "starting")
        self.audit.log_action(vps_id, user_id, "vps_start")
        result = await self.lxd.start(vps["container_id"])
        dbm.update_vps_status(self.db, vps_id, "running" if result.ok else "error")
        if not result.ok:
            dbm.update_vps_error(self.db, vps_id, result.message)
            return False, "Failed to start the VPS."
        return True, "VPS started."

    async def stop(self, vps_id: str, user_id: int) -> tuple[bool, str]:
        vps = self.owned(vps_id, user_id)
        if not vps:
            return False, "VPS not found."
        dbm.update_vps_status(self.db, vps_id, "stopping")
        self.audit.log_action(vps_id, user_id, "vps_stop")
        result = await self.lxd.stop(vps["container_id"])
        dbm.update_vps_status(self.db, vps_id, "stopped" if result.ok else "error")
        if not result.ok:
            dbm.update_vps_error(self.db, vps_id, result.message)
            return False, "Failed to stop the VPS."
        return True, "VPS stopped."

    async def restart(self, vps_id: str, user_id: int, *, by_admin: bool = False) -> tuple[bool, str]:
        vps = self.owned(vps_id, user_id)
        if not vps:
            return False, "VPS not found."
        if vps["suspended"] and not by_admin:
            return False, "This VPS is suspended by an administrator. Contact support."
        if vps["status"] in ("creating", "reinstalling"):
            return False, "This VPS is still provisioning. Please wait."
        dbm.update_vps_status(self.db, vps_id, "starting")
        self.audit.log_action(vps_id, user_id, "vps_restart")
        result = await self.lxd.restart(vps["container_id"])
        dbm.update_vps_status(self.db, vps_id, "running" if result.ok else "error")
        if not result.ok:
            dbm.update_vps_error(self.db, vps_id, result.message)
            return False, "Failed to restart the VPS."
        return True, "VPS restarted."

    async def delete(self, vps_id: str, user_id: int) -> tuple[bool, str]:
        vps = self.owned(vps_id, user_id)
        if not vps:
            return False, "VPS not found."
        await self.lxd.stop(vps["container_id"])
        await asyncio.sleep(1)
        await self.lxd.delete(vps["container_id"], force=True)
        dbm.delete_vps(self.db, vps_id)
        self.audit.log_vps_deleted(vps_id, user_id, vps["name"])
        return True, "VPS deleted."

    async def reinstall(self, vps_id: str, user_id: int, os_key: str) -> tuple[bool, str]:
        vps = self.owned(vps_id, user_id)
        if not vps:
            return False, "VPS not found."
        image = self._image_map(os_key)
        if image is None:
            return False, f"Unknown operating system: {os_key}"

        old_container = vps["container_id"]
        dbm.update_vps_status(self.db, vps_id, "reinstalling")
        self.audit.log_action(vps_id, user_id, "vps_reinstall", f"-> {os_key}")

        if old_container:
            await self.lxd.stop(old_container)
            await asyncio.sleep(1)
            await self.lxd.delete(old_container, force=True)
            dbm.update_vps_container(self.db, vps_id, "", "")

        instance_name, disk_enforced, error = await create_instance(
            self.lxd, self._spec(
                os_key=os_key,
                name=vps["name"],
                ram=vps["ram"],
                cpu=vps["cpu"],
                disk=vps["disk"],
            )
        )
        if not instance_name:
            dbm.update_vps_status(self.db, vps_id, "error")
            dbm.update_vps_error(self.db, vps_id, error or "Reinstall failed at instance creation.")
            return False, error or "Failed to recreate the instance."

        dbm.update_vps_container(self.db, vps_id, instance_name, instance_name)
        dbm.update_vps_ssh(self.db, vps_id, None)
        dbm.update_vps_status(self.db, vps_id, "reinstalling")

        ok, err = await self.ssh.ensure_installed(instance_name)
        if not ok:
            await self.lxd.delete(instance_name, force=True)
            dbm.update_vps_status(self.db, vps_id, "error")
            dbm.update_vps_error(self.db, vps_id, err or "Failed to install packages.")
            return False, err or "Failed to install packages."

        await asyncio.sleep(self.settings.get_int("ssh.wait_after_install", 10))
        ssh_line, err = await self.ssh.start_session(instance_name)
        if not ssh_line:
            await self.lxd.delete(instance_name, force=True)
            dbm.update_vps_status(self.db, vps_id, "error")
            dbm.update_vps_error(self.db, vps_id, err or "Failed to establish SSH.")
            return False, err or "Failed to establish an SSH session."

        dbm.update_vps_ssh(self.db, vps_id, ssh_line)
        dbm.update_vps_status(self.db, vps_id, "running")
        return True, "VPS reinstalled."

    async def regenerate_ssh(self, vps_id: str, user_id: int) -> tuple[bool, str, str]:
        vps = self.owned(vps_id, user_id)
        if not vps:
            return False, "VPS not found.", ""
        if vps["status"] != "running":
            return False, "The VPS must be running to generate an SSH session.", ""
        ssh_line, err = await self.ssh.start_session(vps["container_id"])
        if not ssh_line:
            return False, err or "Failed to generate an SSH session.", ""
        dbm.update_vps_ssh(self.db, vps_id, ssh_line)
        self.audit.log_action(vps_id, user_id, "ssh_regenerate")
        return True, "New SSH session generated.", ssh_line

    # ------------------------------------------------------------------
    # Status reconciliation
    # ------------------------------------------------------------------
    async def sync_statuses(self) -> int:
        changed = 0
        vps_list = dbm.get_all_vps(self.db)
        for vps in vps_list:
            if vps["status"] in ("creating", "starting", "stopping", "reinstalling"):
                continue
            if not vps["container_id"]:
                continue
            state = await self.lxd.state(vps["container_id"])
            if state is None:
                state = "missing"
            mapped = "running" if state == "running" else "stopped"
            if state == "missing" and vps["status"] != "stopped":
                mapped = "stopped"
            if mapped != vps["status"]:
                dbm.update_vps_status(self.db, vps["id"], mapped)
                self.audit.log_status_change(vps["id"], vps["status"], mapped)
                changed += 1
        return changed

    # ------------------------------------------------------------------
    # Stats snapshot for embeds
    # ------------------------------------------------------------------
    async def info_snapshot(self, vps: dict) -> dict:
        snapshot = dict(vps)
        snapshot["stats"] = {"cpu": "N/A", "mem": "N/A", "net": "N/A"}
        snapshot["uptime"] = "Not running"
        if vps["container_id"]:
            if vps["status"] == "running":
                snapshot["stats"] = await self.stats.container_stats(vps["container_id"])
                snapshot["uptime"] = await self.stats.uptime(vps["container_id"])
            else:
                snapshot["uptime"] = "Not running"
        return snapshot
