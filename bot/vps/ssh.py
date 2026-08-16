"""tmate / SSH session provisioning.

The bot installs tmate inside the container, starts a *detached* tmate session
in the background and captures the public ``ssh ...`` connection string. The
session is left running, so the SSH connection stays valid for as long as the
container is up. The string is the only credential a user needs, and it is
only ever delivered via direct message.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("vpsbot.ssh")


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
        """Start a background tmate session and return ``(ssh_line, error)``.

        The session stays alive after this returns so the delivered SSH string
        keeps working for the life of the container.
        """
        sock = "/tmp/tmate.sock"

        # Kill any stale tmate (and its tmux server) so a brand-new relay session
        # and a new ssh string are minted; the previous session is auto-deleted.
        bootstrap = (
            "pkill -x tmate; pkill -x tmux; sleep 1; rm -f {sock}; "
            "setsid tmate -S {sock} new-session -d -s main >/dev/null 2>&1 &"
        ).format(sock=sock)
        result = await self.lxd.exec(instance, ["bash", "-c", bootstrap], timeout=20.0)
        if not result.ok:
            return None, result.message or "Failed to start tmate."

        # Poll for the connection string; tmate prints it once it has joined
        # the relay, which normally takes a few seconds.
        session_timeout = max(10, self.settings.get_int("ssh.session_timeout", 45))
        deadline = asyncio.get_event_loop().time() + session_timeout
        cmd = ["bash", "-c", f"tmate -S {sock} display -p '#{{tmate_ssh}}' 2>/dev/null"]
        while asyncio.get_event_loop().time() < deadline:
            check = await self.lxd.exec(instance, cmd, timeout=20.0)
            line = (check.stdout or "").strip()
            if line.startswith("ssh "):
                return line, ""
            await asyncio.sleep(1)
        return None, "Timed out waiting for an SSH session."
