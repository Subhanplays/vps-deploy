"""SSH + tmate integration.

The bot connects to a freshly deployed VM over SSH (using the key it
generated), makes sure tmate is installed, starts a tmate session and reads
back the public connection string. That string is the ONLY way a user
reaches their VM - every user only ever sees their own session.
"""

import asyncio
import time

from config import config


class TmateError(Exception):
    """Raised when SSH/tmate provisioning fails."""


async def run_ssh(host, command, timeout=60, user=None):
    """Run a single predefined command inside the VM via ssh."""
    user = user or config.ssh_user
    cmd = [
        "ssh",
        "-i", config.ssh_priv_key,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
        f"{user}@{host}",
        command,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise TmateError("SSH command timed out")
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def wait_for_ssh(host, timeout=180, user=None):
    """Wait until the VM answers on SSH (cloud-init finished)."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            code, out, err = await run_ssh(host, "true", timeout=15, user=user)
            if code == 0:
                return True
        except (TmateError, OSError):
            pass
        await asyncio.sleep(5)
    raise TmateError("Timed out waiting for SSH to become available.")


async def wait_for_cloud_init(host, timeout=300, user=None):
    """Wait for cloud-init to finish first-boot provisioning."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            code, out, err = await run_ssh(
                host,
                "cloud-init status --wait >/dev/null 2>&1; "
                "test -f /var/lib/cloud/instance/boot-finished && echo READY",
                timeout=45,
                user=user,
            )
            if "READY" in out:
                return True
        except (TmateError, OSError):
            pass
        await asyncio.sleep(5)
    # Not fatal: package installation is retried later by install_tmate.
    return False


async def install_tmate(host, timeout=300, user=None):
    """Install tmate (idempotent) and report readiness."""
    if not config.tmate_enabled:
        return False
    command = (
        "command -v tmate >/dev/null 2>&1 || "
        "(apt-get update -qq && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tmate)"
    )
    code, out, err = await run_ssh(host, command, timeout=timeout, user=user)
    if code != 0:
        raise TmateError(f"Failed to install tmate inside the VM: {err[-400:]}")
    return True


async def start_tmate(host, timeout=240, user=None):
    """Start tmate and return the public SSH + web connection strings."""
    if not config.tmate_enabled:
        return None
    user = user or config.ssh_user
    command = (
        "tmate -S /tmp/vpsbot-tmate.sock new-session -d 2>/dev/null; "
        "tmate -S /tmp/vpsbot-tmate.sock wait tmate-ready 2>/dev/null; "
        "echo SSH_LINE_BEGIN; "
        "tmate -S /tmp/vpsbot-tmate.sock display -p '#{tmate_ssh}' 2>/dev/null; "
        "echo SSH_LINE_END; "
        "tmate -S /tmp/vpsbot-tmate.sock display -p '#{tmate_web}' 2>/dev/null"
    )
    last_error = "unknown"
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            code, out, err = await run_ssh(host, command, timeout=60, user=user)
        except TmateError as exc:
            last_error = str(exc)
            await asyncio.sleep(5)
            continue
        if code == 0 and out:
            match = _parse_ssh(out)
            if match:
                return match
        last_error = (err or out or "").strip()[-300:] or last_error
        await asyncio.sleep(5)
    raise TmateError(f"tmate did not produce a session: {last_error}")


def _parse_ssh(output):
    """Extract 'ssh ...@...' from tmate output. Returns string or None."""
    between = False
    for line in output.splitlines():
        line = line.strip()
        if line == "SSH_LINE_BEGIN":
            between = True
            continue
        if line == "SSH_LINE_END":
            between = False
            continue
        if between and line.startswith("ssh "):
            return line
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("ssh ") and "@" in line and "tmate" in line:
            return line
    return None
