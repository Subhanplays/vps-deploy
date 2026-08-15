"""Low-level Docker operations.

All commands run as argument lists through :mod:`asyncio.create_subprocess_exec`
- never ``shell=True``, so untrusted user input can never be injected into a
shell. Every call returns a :class:`DockerResult` instead of raising, so UI
code can render friendly errors.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass


@dataclass
class DockerResult:
    ok: bool
    code: int
    stdout: str
    stderr: str

    @property
    def message(self) -> str:
        return (self.stderr or self.stdout or "").strip()[-400:]


class DockerError(Exception):
    """Raised for Docker failures that must reach the user as friendly errors."""


class DockerManager:
    def __init__(self, app=None):
        self.app = app
        self._binary = shutil.which("docker") or "docker"

    # ------------------------------------------------------------------
    # Low level
    # ------------------------------------------------------------------
    async def _run(self, args: list[str], timeout: float = 60.0) -> DockerResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return DockerResult(False, -1, "", "Docker executable not found on this host.")
        except Exception as exc:  # noqa: BLE001
            return DockerResult(False, -1, "", f"Failed to start docker: {exc}")
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return DockerResult(False, -1, "", "Docker operation timed out.")
        return DockerResult(
            ok=proc.returncode == 0,
            code=proc.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace").strip(),
            stderr=stderr.decode("utf-8", errors="replace").strip(),
        )

    def available(self) -> bool:
        return shutil.which("docker") is not None

    # ------------------------------------------------------------------
    # Container lifecycle
    # ------------------------------------------------------------------
    async def run_container(self, args: list[str], timeout: float = 120.0) -> DockerResult:
        return await self._run(args, timeout=timeout)

    async def start(self, container_id: str) -> DockerResult:
        return await self._run(["start", container_id], timeout=60.0)

    async def stop(self, container_id: str) -> DockerResult:
        result = await self._run(["stop", "--time", "30", container_id], timeout=60.0)
        if not result.ok:
            # Hard-kill fallback so a stuck container cannot hold the bot hostage.
            await self._run(["kill", container_id], timeout=30.0)
        return result

    async def restart(self, container_id: str) -> DockerResult:
        return await self._run(["restart", "--time", "30", container_id], timeout=90.0)

    async def remove(self, container_id: str, *, force: bool = True) -> DockerResult:
        args = ["rm", "-f" if force else "", container_id] if force else ["rm", container_id]
        return await self._run(args, timeout=60.0)

    async def exec(self, container_id: str, cmd: list[str], timeout: float = 120.0) -> DockerResult:
        return await self._run(["exec", container_id, *cmd], timeout=timeout)

    async def exec_stream(self, container_id: str, cmd: list[str]) -> tuple[asyncio.subprocess.Process | None, DockerResult | None]:
        """Start a streaming exec; returns the live process for line capture."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                "exec",
                container_id,
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            return proc, None
        except Exception as exc:  # noqa: BLE001
            return None, DockerResult(False, -1, "", str(exc))

    async def state(self, container_id: str) -> str | None:
        """Return the raw Docker container state (running/stopped/...) or None."""
        result = await self._run(
            ["inspect", "--format", "{{.State.Status}}", container_id], timeout=30.0
        )
        if not result.ok:
            return None
        return result.stdout

    async def started_at(self, container_id: str) -> str | None:
        result = await self._run(
            ["inspect", "--format", "{{.State.StartedAt}}", container_id], timeout=30.0
        )
        if result.ok and result.stdout and result.stdout != "<no value>":
            return result.stdout
        return None

    async def container_exists(self, container_id: str) -> bool:
        result = await self._run(
            ["inspect", "--format", "{{.Id}}", container_id], timeout=30.0
        )
        return result.ok

    async def pull_image(self, image: str) -> DockerResult:
        return await self._run(["pull", image], timeout=600.0)

    async def image_exists(self, image: str) -> bool:
        result = await self._run(
            ["image", "inspect", "--format", "{{.Id}}", image], timeout=30.0
        )
        return result.ok
