"""Conservative, body-free Codex skill-read telemetry for the shared ledger.

Only paired successful command reads qualify. Unknown actions, shell control
flow, redirects and missing exit status are deliberately unmeasured: command
classification is advisory and a shell's final status cannot prove every read.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import stat

from .agent_runtime import AgentEvent, ToolCompletedEvent, ToolStartedEvent

logger = logging.getLogger(__name__)
_NAME = re.compile(r"(?:^|/)skills/(?:\.system/)?([a-z0-9][a-z0-9-]{0,63})/SKILL\.md$")
_MAX_ITEMS = 256
_MAX_CHILDREN = 4
_TIMEOUT_SECONDS = 4.0
_ENV_KEYS = {
    "HOME", "PATH", "CCC_CLAUDE_DIR", "CCC_SKILL_USAGE_LOGGER",
    "CCC_MEMORY_AUDIENCE_SCOPED", "CCC_STATE_DIR",
}


def _simple_read(command: object) -> bool:
    if not isinstance(command, str) or not command or len(command) > 16384:
        return False
    if any(char in command for char in ("$", "`", "\n", "\r")):
        return False
    try:
        words = shlex.split(command)
        if len(words) == 3 and Path(words[0]).name in {"bash", "sh"} and words[1] in {"-c", "-lc"}:
            command = words[2]
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:
        return False
    return bool(
        words
        and Path(words[0]).name in {"cat", "head", "tail", "sed"}
        and not any(word and all(c in ";&|<>()" for c in word) for word in words)
        and not any(word in {"--help", "--version"} for word in words)
    )


def _read_skills(item: Mapping[str, object]) -> frozenset[str]:
    actions = item.get("commandActions")
    if not isinstance(actions, (list, tuple)) or not 1 <= len(actions) <= 64:
        return frozenset()
    if not _simple_read(item.get("command")):
        return frozenset()
    skills: set[str] = set()
    for action in actions:
        if not isinstance(action, Mapping) or action.get("type") != "read":
            return frozenset()
        path = action.get("path")
        if not isinstance(path, str) or len(path) > 4096:
            continue
        if not path.startswith("/"):
            cwd = item.get("cwd")
            if not isinstance(cwd, str) or not cwd.startswith("/"):
                continue
            path = str(PurePosixPath(cwd) / path)
        if ".." in PurePosixPath(path).parts:
            continue
        match = _NAME.search(path)
        if match:
            skills.add(match[1])
    return frozenset(skills)


class CodexSkillReads:
    """One live turn; never accepts history, orphan completions, or replay IDs."""

    def __init__(self, sink: SkillUsageSink) -> None:
        self._sink = sink
        self._pending: dict[str, frozenset[str]] = {}
        self._seen: set[str] = set()

    def observe(self, event: AgentEvent) -> None:
        if isinstance(event, ToolStartedEvent) and event.tool_name == "commandExecution":
            key = event.tool_call_id
            if key in self._seen or len(self._seen) >= _MAX_ITEMS:
                return
            skills = _read_skills(event.arguments)
            if skills:
                self._seen.add(key)
                self._pending[key] = skills
        elif isinstance(event, ToolCompletedEvent) and event.tool_name == "commandExecution":
            expected = self._pending.pop(event.tool_call_id, frozenset())
            item = event.result
            if not expected or not event.success or not isinstance(item, Mapping):
                return
            if item.get("status") != "completed" or type(item.get("exitCode")) is not int or item.get("exitCode") != 0:
                return
            output = item.get("aggregatedOutput")
            if not isinstance(output, str) or not output.strip():
                return
            for skill in sorted(expected & _read_skills(item)):
                self._sink.record(skill)


class SkillUsageSink:
    """Bounded best-effort adapter to the installed common logger.

    Scoped audiences write only below their own state root. Invalid scope
    configuration disables logging rather than falling back to the owner ledger.
    Neither raw paths, commands, tool output nor conversation IDs enter stdin.
    """

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
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
            "CCC_SKILL_USAGE_RUNTIME": "codex",
        }
        self._tasks: set[asyncio.Task[None]] = set()

    def for_session(self, overlay: Mapping[str, str] | None) -> SkillUsageSink:
        if not overlay:
            return self
        sink = SkillUsageSink({
            **self._base_environment,
            **{key: value for key, value in overlay.items() if key in _ENV_KEYS},
        })
        # One concurrency budget and shutdown drain across all session routes.
        sink._tasks = self._tasks
        return sink

    def record(self, skill: str) -> None:
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
            logger.debug("Codex skill usage write unavailable")
        finally:
            if process is not None and process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
