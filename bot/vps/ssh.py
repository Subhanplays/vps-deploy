"""tmate / SSH session provisioning.

The bot installs tmate inside the container, launches a foreground session
and captures the public ``ssh ...`` connection string. That string is the only
credential a user needs, and it is only ever delivered via direct message.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("vpsbot.ssh")

_SSH_MARKER = "ssh session:"


class SSHError(Exception):
    """Raised when SSH/tmate provisioning fails."""


class SSHManager:
    def __init__(self, app):
        self.app = app
        self.lxd = app.lxd
        self.settings = app.settings

    def _install_command(self) -> str:
        packages = " ".join(self.settings.get_list("ssh.packages") or ["tmate"])
        return (
            "command -v tmate >/dev/null 2>&1 || "
            "(apt-get update -qq && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
            f"{packages})"
        )

    async def ensure_installed(self, instance: str) -> tuple[bool, str]:
        timeout = self.settings.get_int("ssh.install_timeout", 240)
        attempts = max(1, self.settings.get_int("ssh.install_attempts", 2))
        cmd = ["bash", "-c", self._install_command()]
        last = None
        for attempt in range(attempts):
            result = await self.lxd.exec(instance, cmd, timeout=timeout)
            if result.ok:
                return True, ""
            last = result
            if attempt < attempts - 1:
                # DNS/DHCP is sometimes still settling right after launch.
                await asyncio.sleep(5)
        return False, last.message or "Failed to install tmate."

    async def start_session(self, instance: str) -> tuple[str | None, str]:
        """Launch tmate and return ``(ssh_line, error_message)``."""
        proc, err = await self.lxd.exec_stream(instance, ["tmate", "-F"])
        if err is not None:
            return None, err.message or "Failed to launch tmate."
        if proc is None:
            return None, "Failed to launch tmate."
        try:
            ssh_line = await self._capture_session(proc)
        finally:
            self._terminate(proc)
        if not ssh_line:
            return None, "Timed out waiting for an SSH session."
        return ssh_line, ""

    async def _capture_session(self, proc: asyncio.subprocess.Process) -> str | None:
        session_timeout = self.settings.get_int("ssh.session_timeout", 45)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + session_timeout
        streams = [s for s in (proc.stdout, proc.stderr) if s is not None]
        while loop.time() < deadline:
            if proc.returncode is not None:
                break
            for stream in streams:
                try:
                    line = await asyncio.wait_for(stream.readline(), timeout=min(2.0, max(0.1, deadline - loop.time())))
                except asyncio.TimeoutError:
                    continue
                if not line:
                    continue
                text = line.decode("utf-8", errors="replace").strip()
                if _SSH_MARKER in text.lower():
                    return text.split(_SSH_MARKER)[-1].strip()
            await asyncio.sleep(0.05)
        return None

    def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
