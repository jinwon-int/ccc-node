"""Translate Claude SDK frames into events for one active turn."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import re
from typing import TYPE_CHECKING, cast

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from .agent_runtime import (
    CompletionEvent,
    ErrorEvent,
    JsonValue,
    MessageCompletedEvent,
    ReasoningDeltaEvent,
    ResultEvent,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
)
from .sdk_text import _extract_stream_text_delta

if TYPE_CHECKING:
    from .claude_runtime import _ActiveTurn


INTERRUPTED_ERROR_CODE = "interrupted"

_SNAKE_CASE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
_RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})


class ClaudeSessionTurnEventsMixin:
    """Route active-turn Claude frames into provider-neutral events."""

    _active_turn: _ActiveTurn | None
    _observe_background_task_result: Callable[[JsonValue], None]

    @staticmethod
    def _route_stream_event(active: _ActiveTurn, message: StreamEvent) -> None:
        # Only top-level assistant text streams; nested subagent deltas carry
        # parent_tool_use_id and must not pollute the turn's answer text.
        if message.parent_tool_use_id is not None:
            return
        delta = _extract_stream_text_delta(message.event)
        if delta:
            active.streamed_current_message = True
            active.emitted_text = True
            active.queue.put_nowait(TextDeltaEvent(delta))

    def _route_assistant_message(self, active: _ActiveTurn, message: AssistantMessage) -> None:
        for block in message.content:
            if isinstance(block, TextBlock):
                # Whole-block fallback only when token deltas did not already
                # stream this message; otherwise the text would be doubled.
                if block.text and not active.streamed_current_message:
                    active.emitted_text = True
                    active.queue.put_nowait(TextDeltaEvent(block.text))
            elif isinstance(block, ThinkingBlock):
                if block.thinking:
                    active.queue.put_nowait(ReasoningDeltaEvent(block.thinking))
            elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
                # A tool in the same assistant message proves any preceding
                # user-visible text was interim; close that message first.
                self._flush_message_boundary(active)
                active.open_tools[block.id] = block.name
                active.queue.put_nowait(
                    ToolStartedEvent(
                        block.id,
                        block.name,
                        cast(Mapping[str, JsonValue], block.input),
                    )
                )
            elif isinstance(block, ServerToolResultBlock):
                self._emit_tool_completed(
                    active,
                    block.tool_use_id,
                    cast(JsonValue, block.content),
                    success=True,
                )
        # Each completed SDK assistant message is a message boundary.
        self._flush_message_boundary(active)
        active.streamed_current_message = False

    def _route_tool_results(self, active: _ActiveTurn, message: UserMessage) -> None:
        content = message.content
        if not isinstance(content, list):
            return
        for block in content:
            if isinstance(block, ToolResultBlock):
                self._emit_tool_completed(
                    active,
                    block.tool_use_id,
                    cast(JsonValue, block.content),
                    success=block.is_error is not True,
                )

    def _emit_tool_completed(
        self,
        active: _ActiveTurn,
        tool_call_id: str,
        result: JsonValue,
        *,
        success: bool,
    ) -> None:
        # Pair by call id: a completion without a recorded start (or a second
        # completion for the same id) never reaches the stream.
        tool_name = active.open_tools.pop(tool_call_id, None)
        if tool_name is None:
            return
        if tool_name == "Bash" and success:
            self._observe_background_task_result(result)
        active.queue.put_nowait(
            ToolCompletedEvent(tool_call_id, tool_name, result, success)
        )

    @staticmethod
    def _flush_message_boundary(active: _ActiveTurn) -> None:
        if active.emitted_text:
            active.queue.put_nowait(MessageCompletedEvent())
            active.emitted_text = False

    def _complete_turn(self, active: _ActiveTurn, message: ResultMessage) -> None:
        if active.interrupt_requested:
            active.queue.put_nowait(
                ErrorEvent(INTERRUPTED_ERROR_CODE, "Claude turn was interrupted")
            )
        elif message.is_error:
            # Include diagnostic fields (subtype/api_error_status/terminal_reason)
            # in the user-facing message when result is empty, per #901.
            text = (message.result or "").strip()
            if not text:
                parts = ["Claude turn failed"]
                if message.subtype and message.subtype != "success":
                    parts.append(f"(subtype: {message.subtype})")
                if message.api_error_status:
                    parts.append(f"(HTTP status: {message.api_error_status})")
                if message.terminal_reason:
                    parts.append(f"(reason: {message.terminal_reason})")
                text = " ".join(parts)
            active.queue.put_nowait(
                ErrorEvent(
                    self._error_code(message.subtype),
                    text,
                    retryable=message.api_error_status in _RETRYABLE_HTTP_STATUSES,
                )
            )
        else:
            self._flush_message_boundary(active)
            active.queue.put_nowait(self._result_event(message))
            active.queue.put_nowait(CompletionEvent(message.stop_reason or "end_turn"))
        active.finished = True

    @staticmethod
    def _error_code(subtype: str) -> str:
        if subtype and subtype != "success" and _SNAKE_CASE_CODE.match(subtype):
            return subtype
        return "claude_turn_failed"

    @staticmethod
    def _result_event(message: ResultMessage) -> ResultEvent:
        payload: dict[str, JsonValue] = {
            "subtype": message.subtype,
            "result": message.result,
            "session_id": message.session_id,
            "duration_ms": message.duration_ms,
            "num_turns": message.num_turns,
            "total_cost_usd": message.total_cost_usd,
            "usage": cast(JsonValue, message.usage),
        }
        try:
            return ResultEvent(payload)
        except (TypeError, ValueError):
            # Never let a non-JSON usage payload swallow the terminal event.
            return ResultEvent({"subtype": message.subtype, "result": message.result})

    def _fail_active_turn(self, code: str, message: str) -> None:
        active = self._active_turn
        if active is None or active.finished:
            return
        active.queue.put_nowait(ErrorEvent(code, message or "Claude connection failed"))
        active.finished = True
