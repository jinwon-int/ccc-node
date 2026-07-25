"""Typed, I/O-free state for one provider-neutral agent turn."""

from __future__ import annotations

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

    busy_depth: int = 0
    approval_pending: bool = False
    admitted: bool = False
    attempt_recorded: bool = False
    terminal_error: ErrorEvent | None = None
    active_tools: dict[str, str] = field(default_factory=dict)

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

    def observe(self, event: AgentEvent) -> TurnEventTransition:
        """Apply one normalized event and return its typed routing transition."""

        self.approval_pending = isinstance(event, ApprovalRequestEvent)

        if isinstance(event, TextDeltaEvent):
            return TextDeltaTransition(event)
        if isinstance(event, MessageCompletedEvent):
            return MessageCompletedTransition(event)
        if isinstance(event, ToolStartedEvent):
            self.busy_depth += 1
            label = tool_label(event.tool_name, dict(event.arguments))
            if label is not None:
                self.active_tools[event.tool_call_id] = label
            return ToolStartedTransition(event, label)
        if isinstance(event, ToolCompletedEvent):
            self.busy_depth = max(0, self.busy_depth - 1)
            self.active_tools.pop(event.tool_call_id, None)
            return ToolCompletedTransition(event, self.current_tool_label)
        if isinstance(event, ErrorEvent):
            self.terminal_error = event
            return ErrorTransition(event)
        if isinstance(event, ResultEvent):
            return ResultTransition(event)
        if isinstance(
            event,
            (ReasoningDeltaEvent, ApprovalRequestEvent, CompletionEvent),
        ):
            return IgnoredTransition(event)
        raise TypeError(f"unsupported agent event: {type(event).__name__}")
