"""Correlate native read calls/results without retaining paths or tool bodies."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import PurePosixPath
import re

from .skill_usage import SkillUsageSink

_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_RANGE = re.compile(r"\[read: lines [0-9]+-[0-9]+ of [0-9]+; (?:EOF|next offset [0-9]+)\]\n")
_TOOLS = {"read", "write", "edit", "bash"}
_MAX_CALLS = 256
_MAX_BATCH = 64


def _candidate(arguments: dict, cwd: str) -> tuple[str | None, bool]:
    path = arguments.get("path")
    if not isinstance(path, str) or not path or len(path) > 4096 or "\x00" in path:
        return None, False
    parts = PurePosixPath(path).parts
    if ".." in parts:
        return None, False
    parts = (PurePosixPath(cwd) / path).parts
    if len(parts) < 3 or parts[-1] != "SKILL.md" or not _NAME.fullmatch(parts[-2]):
        return None, False
    if parts[-3] != "skills" and not (len(parts) >= 4 and parts[-4:-2] == ("skills", ".system")):
        return None, False
    for key in ("offset", "limit"):
        if key in arguments and (type(arguments[key]) is not int or arguments[key] < 1):
            return None, False
    return parts[-2], "offset" in arguments or "limit" in arguments


def _has_body(message: dict, ranged: bool) -> bool:
    if message.get("isError") is not False:
        return False
    content = message.get("content")
    # Native read emits exactly one text block; do not infer from attachments.
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        return False
    block = content[0]
    body = block.get("text")
    if block.get("type") != "text" or not isinstance(body, str):
        return False
    if ranged:
        header = _RANGE.match(body)
        if header is None:
            return False
        body = body[header.end():]
    return bool(body.strip())


@dataclass
class _Call:
    ident: str
    tool: str
    skill: str | None
    ranged: bool
    results: int = 0
    has_body: bool = False


class DansoSkillReads:
    """One bounded observer per native process, including task continuations.

    Requires the native assistant call, sequential start, matching successful
    result with actual content, and successful settlement. Missing or ambiguous
    correlation disables capture for this run, never the conversation.
    """

    def __init__(self, sink: SkillUsageSink, cwd: str):
        self.sink, self.cwd = sink, cwd
        self.pending: deque[_Call] = deque()
        self.seen: set[str] = set()
        self.active: _Call | None = None
        self.disabled = False

    def disable(self) -> None:
        self.disabled = True
        self.pending.clear()
        self.active = None
        self.seen.clear()

    def observe(self, record: dict) -> None:
        if self.disabled:
            return
        if record.get("type") == "danso_progress":
            self._progress(record)
        elif record.get("type") == "message":
            message = record["message"]
            if message.get("role") == "assistant" and message.get("stopReason") == "toolUse":
                self._calls(message)
            elif message.get("role") == "toolResult":
                self._result(message)

    def _calls(self, message: dict) -> None:
        calls = [b for b in message["content"] if b.get("type") == "toolCall"]
        if self.pending or self.active or len(calls) > _MAX_BATCH or len(self.seen) + len(calls) > _MAX_CALLS:
            self.disable()
            return
        for block in calls:
            ident, tool = block["id"], block["name"]
            if len(ident) > 512 or len(tool) > 128 or ident in self.seen:
                self.disable()
                return
            self.seen.add(ident)
            skill, ranged = _candidate(block["arguments"], self.cwd) if tool == "read" else (None, False)
            self.pending.append(_Call(ident, tool, skill, ranged))

    def _result(self, message: dict) -> None:
        call = self.active
        if call is None or message.get("toolCallId") != call.ident or message.get("toolName") != call.tool:
            self.disable()
            return
        call.results += 1
        call.has_body = _has_body(message, call.ranged)

    def _progress(self, record: dict) -> None:
        if record["phase"] == "started":
            if self.active is not None or not self.pending:
                self.disable()
                return
            self.active = self.pending.popleft()
            expected = self.active.tool if self.active.tool in _TOOLS else "other"
            if record["tool"] != expected:
                self.disable()
        else:
            call, self.active = self.active, None
            if (call is not None and call.skill and call.results == 1 and call.has_body
                    and record["success"] is True):
                self.sink.record(call.skill)
