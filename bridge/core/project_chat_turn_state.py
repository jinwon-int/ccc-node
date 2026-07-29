"""Typed, I/O-free state for one provider-neutral agent turn."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TypeAlias

from telegram_bot.core.agent_runtime import (
    AgentEvent,
    ApprovalRequestEvent,
    CompletionEvent,
    ErrorEvent,
    MessageCompletedEvent,
    ReasoningDeltaEvent,
    ResultEvent,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
)
from telegram_bot.core.heartbeat import tool_label


@dataclass(frozen=True, slots=True)
class TextDeltaTransition:
    event: TextDeltaEvent


@dataclass(frozen=True, slots=True)
class MessageCompletedTransition:
    event: MessageCompletedEvent


@dataclass(frozen=True, slots=True)
class ToolStartedTransition:
    event: ToolStartedEvent
    current_tool_label: str | None


@dataclass(frozen=True, slots=True)
class ToolCompletedTransition:
    event: ToolCompletedEvent
    current_tool_label: str | None


@dataclass(frozen=True, slots=True)
class ErrorTransition:
    event: ErrorEvent


@dataclass(frozen=True, slots=True)
class ResultTransition:
    event: ResultEvent


IgnoredEvent: TypeAlias = ReasoningDeltaEvent | ApprovalRequestEvent | CompletionEvent


@dataclass(frozen=True, slots=True)
class IgnoredTransition:
    event: IgnoredEvent


TurnEventTransition: TypeAlias = (
    TextDeltaTransition
    | MessageCompletedTransition
    | ToolStartedTransition
    | ToolCompletedTransition
    | ErrorTransition
    | ResultTransition
    | IgnoredTransition
)


@dataclass(slots=True)
class TurnEventState:
    """Own only the mutable event-routing facts for one turn.

    Runtime/session I/O, request lifecycle authority, output buffering,
    metering, and finalization deliberately remain with the caller.
    """

    approval_pending: bool = False
    approval_pending_since: float | None = None
    admitted: bool = False
    attempt_recorded: bool = False
    terminal_error: ErrorEvent | None = None
    terminal_result_text: str | None = None
    active_tools: dict[str, str] = field(default_factory=dict)
    active_tool_ids: set[str] = field(default_factory=set)

    @property
    def busy_depth(self) -> int:
        """Return the number of distinct active provider tool calls."""

        return len(self.active_tool_ids)

    @property
    def current_tool_label(self) -> str | None:
        return list(self.active_tools.values())[-1] if self.active_tools else None

    @property
    def needs_attempt_recording(self) -> bool:
        return not self.attempt_recorded

    def mark_admitted(self) -> None:
        """Observe admission after RequestLifecycle accepted the event."""

        self.admitted = True

    def mark_attempt_recorded(self) -> None:
        """Observe successful external request-attempt metering."""

        self.attempt_recorded = True

    def observe(
        self,
        event: AgentEvent,
        *,
        observed_at: float | None = None,
    ) -> TurnEventTransition:
        """Apply one normalized event and return its typed routing transition."""

        was_approval_pending = self.approval_pending
        approval_pending = isinstance(event, ApprovalRequestEvent)
        self.approval_pending = approval_pending
        if approval_pending:
            if not was_approval_pending:
                self.approval_pending_since = observed_at
        else:
            self.approval_pending_since = None

        if isinstance(event, TextDeltaEvent):
            return TextDeltaTransition(event)
        if isinstance(event, MessageCompletedEvent):
            return MessageCompletedTransition(event)
        if isinstance(event, ToolStartedEvent):
            self.active_tool_ids.add(event.tool_call_id)
            label = tool_label(event.tool_name, dict(event.arguments))
            if label is not None:
                self.active_tools[event.tool_call_id] = label
            return ToolStartedTransition(event, label)
        if isinstance(event, ToolCompletedEvent):
            if event.tool_call_id in self.active_tool_ids:
                self.active_tool_ids.remove(event.tool_call_id)
                self.active_tools.pop(event.tool_call_id, None)
            return ToolCompletedTransition(event, self.current_tool_label)
        if isinstance(event, ErrorEvent):
            self.terminal_error = event
            return ErrorTransition(event)
        if isinstance(event, ResultEvent):
            # #775: keep the provider's final result text (when the payload
            # carries one, e.g. the Claude adapter's `message.result`) so an
            # empty normal completion can recover it instead of reporting a
            # placeholder success. Payloads are frozen mappings.
            payload = event.result
            if isinstance(payload, Mapping):
                candidate = payload.get("result")
                if isinstance(candidate, str) and candidate:
                    self.terminal_result_text = candidate
            return ResultTransition(event)
        if isinstance(
            event,
            (ReasoningDeltaEvent, ApprovalRequestEvent, CompletionEvent),
        ):
            return IgnoredTransition(event)
        raise TypeError(f"unsupported agent event: {type(event).__name__}")
