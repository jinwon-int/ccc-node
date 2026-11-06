"""Body-free ``tool`` spans derived from the Claude SDK frame stream (#2684).

The bridge already observes every raw SDK frame through the session's
``set_sdk_frame_observer`` seam (``ClaudeSessionFrameRouting._route_message``
calls it before any routing, for turn and between-turns flows alike, including
subagent frames that carry ``parent_tool_use_id``). :class:`ToolSpanTracker`
pairs each assistant ``ToolUseBlock`` (id, name, observed time) with the
``ToolResultBlock`` of the same ``tool_use_id`` in a later user message and
writes one ``kind="tool"`` span per completed call into the slice-1
:class:`~telegram_bot.core.span_ledger.SpanLedger`.

Why the stream and not a ``PostToolUse`` hook: a hook runs as a separate
process that knows neither the bridge project's spans directory nor the
conversation, and adding one means changing every node's ``settings.json``.
The stream is already in-process, per conversation, and costs nothing extra.

What is measured: ``duration_ms`` is **bridge-observed** latency — the
monotonic time between the frame carrying the tool use and the frame carrying
its result. It includes SDK/CLI transport and any approval wait, not only the
tool's own execution. ``start_ts`` is the wall clock when the use was seen.

Body-free: only the tool *name* is read from a use block (and only when it is
identifier-shaped, :data:`_TOOL_NAME_RE`), only ``tool_use_id``/``is_error``
from a result block. Tool input, tool output, paths and command text are never
read into the span; the raw call id is used only as an in-memory pairing key
and never stored. The ledger's allowlist sanitizer runs on top of that.

Frames are matched by SDK class name rather than ``isinstance``: several test
modules install spec-less ``claude_agent_sdk`` stubs, and importing the block
classes here would make this module depend on which import generation won.
``ServerToolUseBlock``/``ServerToolResultBlock`` (web search, advisor, …) are
deliberately not tool spans: they execute inside the API call and arrive in
one assistant message, so their observed latency would always be ~0.

Lifecycle and bounds:

* At most :data:`MAX_PENDING_TOOL_CALLS` calls are in flight per tracker (one
  per bridge process); inserting past the bound evicts the oldest entry.
* A terminal ``ResultMessage`` for a conversation drops that conversation's
  unmatched top-level calls (counted in :attr:`dropped_unmatched`). The CLI
  pairs every tool use with a result before it ends a turn — an interrupted
  call gets an ``is_error`` result — so an unmatched use at turn end is an
  anomaly, not a measurable timeout; it is dropped rather than recorded as
  ``status=timeout``. Turns that end *without* a ``ResultMessage`` (stall,
  crash, transport loss) are a later slice; their pending calls are cleared by
  :meth:`reset` when the bridge drops the session, or age out via the bound.
* Subagent calls (``parent_tool_use_id`` set) are kept across that turn end: a
  background subagent can still be running when the parent's result arrives.
  They are spans of their own (``ccc.tool.scope=subagent``); the parent's
  ``Task`` use/result pair is one ``main`` span. Each call id is counted at
  most once, so nothing is double counted.
* Every public method swallows exceptions; a broken tracker never reaches the
  frame routing that serves the turn.
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Hashable, Optional, Protocol

from telegram_bot.core.span_ledger import conversation_ref

logger = logging.getLogger(__name__)

#: ``gen_ai.operation.name`` for one tool call (OTel GenAI well-known value).
OPERATION_EXECUTE_TOOL = "execute_tool"
#: ``error.type`` of a call whose result block has ``is_error=True``.
TOOL_ERROR_TYPE = "tool_error"
#: In-flight tool calls kept per tracker (one tracker per bridge process).
MAX_PENDING_TOOL_CALLS = 256
#: Conversations whose last seen session id is remembered for the fallback ref.
_MAX_SESSION_REFS = 128

SCOPE_MAIN = "main"
SCOPE_SUBAGENT = "subagent"

#: Tool names as the Messages API allows them (``^[a-zA-Z0-9_-]{1,128}$``);
#: MCP names (``mcp__server__tool``) fit. Stricter than the ledger's generic
#: identifier shape, so a path- or URL-like "name" never passes.
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")

_ASSISTANT_MESSAGE = "AssistantMessage"
_USER_MESSAGE = "UserMessage"
_RESULT_MESSAGE = "ResultMessage"
_TOOL_USE_BLOCK = "ToolUseBlock"
_TOOL_RESULT_BLOCK = "ToolResultBlock"


class _Ledger(Protocol):
    @property
    def enabled(self) -> bool: ...

    def record(self, span: Any) -> bool: ...


@dataclass(frozen=True)
class _PendingCall:
    scope: Hashable
    name: Optional[str]
    start_ts: float
    start_mono: float
    conversation: Optional[str]
    nested: bool


def _type_name(value: object) -> str:
    return type(value).__name__


def _tool_name(value: object) -> Optional[str]:
    if isinstance(value, str) and _TOOL_NAME_RE.match(value):
        return value
    return None


def _blocks(message: object) -> list[Any]:
    content = getattr(message, "content", None)
    return content if isinstance(content, list) else []


class ToolSpanTracker:
    """Pair SDK tool use/result blocks and record one ``tool`` span per call."""

    def __init__(
        self,
        ledger: _Ledger,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        max_pending: int = MAX_PENDING_TOOL_CALLS,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._monotonic = monotonic
        self._max_pending = max(1, int(max_pending))
        self._pending: OrderedDict[str, _PendingCall] = OrderedDict()
        self._session_refs: OrderedDict[Hashable, str] = OrderedDict()
        self.dropped_unmatched = 0
        self.evicted = 0

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def observe(self, scope: Hashable, message: object) -> int:
        """Feed one raw SDK frame; return the number of spans written. Never raises."""

        try:
            if not getattr(self._ledger, "enabled", False):
                return 0
            kind = _type_name(message)
            if kind == _ASSISTANT_MESSAGE:
                self._remember_session(scope, getattr(message, "session_id", None))
                self._observe_uses(scope, message)
                return 0
            if kind == _USER_MESSAGE:
                return self._observe_results(message)
            if kind == _RESULT_MESSAGE:
                self._remember_session(scope, getattr(message, "session_id", None))
                self.end_turn(scope)
            return 0
        except Exception:
            logger.debug("Tool span observe failed", exc_info=True)
            return 0

    def end_turn(self, scope: Hashable) -> int:
        """Drop ``scope``'s unmatched top-level calls; return how many. Never raises."""

        try:
            stale = [
                call_id
                for call_id, call in self._pending.items()
                if call.scope == scope and not call.nested
            ]
            for call_id in stale:
                del self._pending[call_id]
            self.dropped_unmatched += len(stale)
            if stale:
                logger.debug("Dropped %d unmatched tool call(s) at turn end", len(stale))
            return len(stale)
        except Exception:
            logger.debug("Tool span turn-end cleanup failed", exc_info=True)
            return 0

    def reset(self, scope: Hashable) -> int:
        """Forget every pending call of ``scope`` (session dropped). Never raises."""

        try:
            stale = [call_id for call_id, call in self._pending.items() if call.scope == scope]
            for call_id in stale:
                del self._pending[call_id]
            self._session_refs.pop(scope, None)
            return len(stale)
        except Exception:
            logger.debug("Tool span reset failed", exc_info=True)
            return 0

    def _remember_session(self, scope: Hashable, session_id: object) -> None:
        ref = conversation_ref(session_id) if isinstance(session_id, str) else None
        if ref is None:
            return
        self._session_refs[scope] = ref
        self._session_refs.move_to_end(scope)
        while len(self._session_refs) > _MAX_SESSION_REFS:
            self._session_refs.popitem(last=False)

    def _observe_uses(self, scope: Hashable, message: object) -> None:
        nested = getattr(message, "parent_tool_use_id", None) is not None
        for block in _blocks(message):
            if _type_name(block) != _TOOL_USE_BLOCK:
                continue
            call_id = getattr(block, "id", None)
            if not isinstance(call_id, str) or not call_id or call_id in self._pending:
                continue
            # Only the name is read from the use block; input stays untouched.
            call = _PendingCall(
                scope=scope,
                name=_tool_name(getattr(block, "name", None)),
                start_ts=float(self._clock()),
                start_mono=float(self._monotonic()),
                conversation=self._session_refs.get(scope),
                nested=nested,
            )
            self._pending[call_id] = call
            while len(self._pending) > self._max_pending:
                self._pending.popitem(last=False)
                self.evicted += 1

    def _observe_results(self, message: object) -> int:
        written = 0
        for block in _blocks(message):
            if _type_name(block) != _TOOL_RESULT_BLOCK:
                continue
            call_id = getattr(block, "tool_use_id", None)
            if not isinstance(call_id, str):
                continue
            call = self._pending.pop(call_id, None)
            if call is None:
                continue
            # Only is_error is read from the result block; content stays untouched.
            is_error = getattr(block, "is_error", None) is True
            if self._record(call, is_error=is_error):
                written += 1
        return written

    def _record(self, call: _PendingCall, *, is_error: bool) -> bool:
        try:
            elapsed = max(0.0, float(self._monotonic()) - call.start_mono)
            span: dict[str, Any] = {
                "kind": "tool",
                "status": "error" if is_error else "ok",
                "gen_ai.operation.name": OPERATION_EXECUTE_TOOL,
                "gen_ai.tool.name": call.name,
                "gen_ai.conversation.id": call.conversation,
                "ccc.tool.scope": SCOPE_SUBAGENT if call.nested else SCOPE_MAIN,
                "start_ts": call.start_ts,
                "duration_ms": int(round(elapsed * 1000.0)),
            }
            if is_error:
                span["error.type"] = TOOL_ERROR_TYPE
            return bool(self._ledger.record(span))
        except Exception:
            logger.debug("Tool span record failed", exc_info=True)
            return False


__all__ = [
    "MAX_PENDING_TOOL_CALLS",
    "OPERATION_EXECUTE_TOOL",
    "SCOPE_MAIN",
    "SCOPE_SUBAGENT",
    "TOOL_ERROR_TYPE",
    "ToolSpanTracker",
]
