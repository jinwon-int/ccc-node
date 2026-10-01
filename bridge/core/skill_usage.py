"""Bounded, body-free logger shared by native bridge skill-read adapters."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
import logging
import os
from pathlib import Path
import re
import signal
import stat

logger = logging.getLogger(__name__)
_MAX_CHILDREN = 4
_TIMEOUT_SECONDS = 4.0
_ENV_KEYS = {
    "HOME", "PATH", "CCC_CLAUDE_DIR", "CCC_SKILL_USAGE_LOGGER",
    "CCC_MEMORY_AUDIENCE_SCOPED", "CCC_STATE_DIR",
}
_SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class SkillUsageSink:
    """Bounded best-effort adapter to the installed common logger.

    Scoped audiences write only below their own state root. Invalid scope
    configuration disables logging rather than falling back to the owner ledger.
    Neither raw paths, commands, tool output nor conversation IDs enter stdin.
    """

    def __init__(self, environment: Mapping[str, str] | None = None, *, runtime: str = "codex") -> None:
        if runtime not in {"codex", "danso"}:
            raise ValueError("unsupported skill usage runtime")
        self._runtime = runtime
        env = dict(os.environ if environment is None else environment)
        self._base_environment = {key: value for key, value in env.items() if key in _ENV_KEYS}
        home = Path(env.get("HOME") or str(Path.home()))
        claude = Path(env.get("CCC_CLAUDE_DIR") or str(home / ".claude"))
        self._logger = Path(env.get("CCC_SKILL_USAGE_LOGGER") or str(claude / "hooks/skill-usage-log.sh"))
        self._enabled = True
        if env.get("CCC_MEMORY_AUDIENCE_SCOPED") == "1":
            state = Path(env.get("CCC_STATE_DIR") or ".")
            if not state.is_absolute() or state.name != "state":
                self._enabled = False
            else:
                claude = state.parent
        self._environment = {
            "HOME": str(home),
            "PATH": env.get("PATH") or os.defpath,
            "CCC_CLAUDE_DIR": str(claude),
            "CCC_SKILL_USAGE_RUNTIME": self._runtime,
        }
        self._tasks: set[asyncio.Task[None]] = set()

    def for_session(self, overlay: Mapping[str, str] | None) -> SkillUsageSink:
        if not overlay:
            return self
        sink = SkillUsageSink({
            **self._base_environment,
            **{key: value for key, value in overlay.items() if key in _ENV_KEYS},
        }, runtime=self._runtime)
        # One concurrency budget and shutdown drain across all session routes.
        sink._tasks = self._tasks
        return sink

    def record(self, skill: str) -> None:
        if not isinstance(skill, str) or not _SKILL_NAME.fullmatch(skill):
            return
        if not self._enabled or len(self._tasks) >= _MAX_CHILDREN:
            return
        task = asyncio.create_task(self._write(skill))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def _write(self, skill: str) -> None:
        process: asyncio.subprocess.Process | None = None
        try:
            info = self._logger.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                return
            payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": f"/skills/{skill}/SKILL.md"}}).encode()
            async with asyncio.timeout(_TIMEOUT_SECONDS):
                process = await asyncio.create_subprocess_exec(
                    "bash", str(self._logger), env=self._environment,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
                )
                await process.communicate(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Skill usage write unavailable")
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
