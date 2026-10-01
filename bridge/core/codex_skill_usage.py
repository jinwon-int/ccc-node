"""Conservative, body-free Codex skill-read telemetry for the shared ledger.

Only paired successful command reads qualify. Unknown actions, shell control
flow, redirects and missing exit status are deliberately unmeasured: command
classification is advisory and a shell's final status cannot prove every read.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
import re
import shlex

from .agent_runtime import AgentEvent, ToolCompletedEvent, ToolStartedEvent
from .skill_usage import SkillUsageSink

_NAME = re.compile(r"(?:^|/)skills/(?:\.system/)?([a-z0-9][a-z0-9-]{0,63})/SKILL\.md$")
_MAX_ITEMS = 256


def _read_words(command: object) -> list[str]:
    if not isinstance(command, str) or not command or len(command) > 16384:
        return []
    if any(char in command for char in ("$", "`", "\n", "\r")):
        return []
    try:
        words = shlex.split(command)
        if len(words) == 3 and words[0] in {"bash", "sh", "/bin/bash", "/bin/sh"} and words[1] in {"-c", "-lc"}:
            command = words[2]
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:
        return []
    if any(word and all(c in ";&|<>()" for c in word) for word in words):
        return []
    return words


def _head_tail_operands(args: list[str]) -> list[str]:
    # Exclude verbose/zero-byte reads and headers from multi-file invocations.
    index = 0
    while index < len(args) and args[index].startswith("-"):
        word = args[index]
        index += 1
        if word == "--":
            break
        if word == "-q":
            continue
        if word in {"-n", "-c"}:
            if index == len(args) or not re.fullmatch(r"[1-9][0-9]{0,8}", args[index]):
                return []
            index += 1
        elif not re.fullmatch(r"-(?:n|c)?[1-9][0-9]{0,8}", word):
            return []
    operands = args[index:]
    return operands if len(operands) == 1 else []


def _command_operands(command: object) -> list[str]:
    words = _read_words(command)
    if not words:
        return []
    program = words[0]
    if program not in {f"{prefix}{name}" for prefix in ("", "/bin/", "/usr/bin/") for name in ("cat", "head", "tail", "sed")}:
        return []
    args = words[1:]
    name = Path(program).name
    if name in {"head", "tail"}:
        return _head_tail_operands(args)
    if name == "sed":
        if args[:1] == ["-n"]:
            args = args[1:]
        if args[:1] == ["-e"]:
            args = args[1:]
        if not args or not re.fullmatch(r"[1-9][0-9]{0,8}(?:,[1-9][0-9]{0,8})?p", args[0]):
            return []
        args = args[1:]
        if args[:1] == ["--"]:
            args = args[1:]
        return args if len(args) == 1 and not args[0].startswith("-") else []
    while args and args[0].startswith("-"):
        flag, args = args[0], args[1:]
        if flag == "--":
            break
        if not re.fullmatch(r"-[benstuvAET]+", flag):
            return []
    return args if args and all(arg != "-" and not arg.startswith("-") for arg in args) else []


def _absolute_path(path: object, cwd: object) -> str | None:
    if not isinstance(path, str) or len(path) > 4096 or not path:
        return None
    if not path.startswith("/"):
        if not isinstance(cwd, str) or not cwd.startswith("/"):
            return None
        path = str(PurePosixPath(cwd) / path)
    return None if ".." in PurePosixPath(path).parts else str(PurePosixPath(path))


def _read_skills(item: Mapping[str, object]) -> frozenset[str]:
    actions = item.get("commandActions")
    if not isinstance(actions, (list, tuple)) or not 1 <= len(actions) <= 64:
        return frozenset()
    operands = _command_operands(item.get("command"))
    paths = {_absolute_path(path, item.get("cwd")) for path in operands}
    skills: set[str] = set()
    for action in actions:
        if not isinstance(action, Mapping) or action.get("type") != "read":
            return frozenset()
        path = _absolute_path(action.get("path"), item.get("cwd"))
        if path is not None and path in paths:
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
