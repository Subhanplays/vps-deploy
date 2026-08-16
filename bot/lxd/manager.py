"""Low-level ``lxc`` operations for LXD system containers.

All commands run as argument lists through :mod:`asyncio.create_subprocess_exec`
- never ``shell=True``, so untrusted user input can never be injected into a
shell. Every call returns a :class:`LxdResult` instead of raising, so UI code
can render friendly errors.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass


@dataclass
class LxdResult:
    ok: bool
    code: int
    stdout: str
    stderr: str

    @property
    def message(self) -> str:
        return (self.stderr or self.stdout or "").strip()[-400:]


class LxdError(Exception):
    """Raised for LXD failures that must reach the user as friendly errors."""


class LxdManager:
    def __init__(self, app=None):
        self.app = app
        self._binary = shutil.which("lxc") or "lxc"

    # ------------------------------------------------------------------
    # Low level
    # ------------------------------------------------------------------
    async def _run(self, args: list[str], timeout: float = 60.0) -> LxdResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return LxdResult(False, -1, "", "LXD executable (lxc) not found on this host.")
        except Exception as exc:  # noqa: BLE001
            return LxdResult(False, -1, "", f"Failed to start lxc: {exc}")
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return LxdResult(False, -1, "", "LXD operation timed out.")
        return LxdResult(
            ok=proc.returncode == 0,
            code=proc.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace").strip(),
            stderr=stderr.decode("utf-8", errors="replace").strip(),
        )

    def available(self) -> bool:
        return shutil.which("lxc") is not None

    # ------------------------------------------------------------------
    # Instance lifecycle
    # ------------------------------------------------------------------
    async def launch(self, args: list[str], timeout: float = 300.0) -> LxdResult:
        """Run a pre-built ``lxc launch ...`` command (see lxd.instances)."""
        return await self._run(args, timeout=timeout)

    async def start(self, name: str) -> LxdResult:
        return await self._run(["start", name], timeout=90.0)

    async def stop(self, name: str) -> LxdResult:
        result = await self._run(["stop", name, "--timeout", "30"], timeout=60.0)
        if not result.ok:
            # Force-stop fallback so a stuck instance cannot block the bot.
            await self._run(["stop", name, "--force"], timeout=30.0)
        return result

    async def restart(self, name: str) -> LxdResult:
        return await self._run(["restart", name, "--timeout", "30"], timeout=90.0)

    async def delete(self, name: str, *, force: bool = True) -> LxdResult:
        args = ["delete", "--force", name] if force else ["delete", name]
        return await self._run(args, timeout=90.0)

    async def exec(self, name: str, cmd: list[str], timeout: float = 120.0) -> LxdResult:
        return await self._run(["exec", name, "--", *cmd], timeout=timeout)

    async def exec_stream(self, name: str, cmd: list[str]) -> tuple[asyncio.subprocess.Process | None, LxdResult | None]:
        """Start a streaming exec; returns the live process for line capture."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                "exec",
                name,
                "--",
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            return proc, None
        except Exception as exc:  # noqa: BLE001
            return None, LxdResult(False, -1, "", str(exc))

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    async def list_instances(self) -> list[dict]:
        result = await self._run(["list", "--format", "json"], timeout=30.0)
        if not result.ok:
            return []
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    async def instance_info(self, name: str) -> dict:
        result = await self._run(["info", name, "--format", "json"], timeout=30.0)
        if not result.ok:
            return {}
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    async def state(self, name: str) -> str | None:
        """Return the raw LXD state string (running/stopped/...) or None."""
        for item in await self.list_instances():
            if item.get("name") == name:
                status = item.get("status") or (item.get("state") or {}).get("status", "")
                return str(status).lower() if status else None
        return None

    async def exists(self, name: str) -> bool:
        return await self.state(name) is not None

    async def storage_pool(self, pool: str = "default") -> dict:
        result = await self._run(["storage", "show", pool, "--format", "json"], timeout=30.0)
        if not result.ok:
            return {}
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}
