"""Typed, I/O-free state for one provider-neutral agent turn."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TypeAlias

from telegram_bot.core.agent_runtime import (
    AgentEvent,
    ApprovalRequestEvent,
    CompletionEvent,
    DelegatedTaskLifecycleEvent,
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


@dataclass(frozen=True, slots=True)
class DelegatedTaskLifecycleTransition:
    event: DelegatedTaskLifecycleEvent


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
    | DelegatedTaskLifecycleTransition
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
    delegated_tasks_active: int = 0
    delegated_tasks_oldest_started_at: float | None = None
    delegated_tasks_last_activity_at: float | None = None
    terminal_stall_started_at: float | None = None
    terminal_stall_deferral_recorded: bool = False

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

    def _observe_approval_state(
        self,
        event: AgentEvent,
        *,
        observed_at: float | None,
    ) -> None:
        """Update approval state unless the event is observation-only."""

        if isinstance(event, DelegatedTaskLifecycleEvent):
            return
        was_approval_pending = self.approval_pending
        approval_pending = isinstance(event, ApprovalRequestEvent)
        self.approval_pending = approval_pending
        if approval_pending:
            if not was_approval_pending:
                self.approval_pending_since = observed_at
        else:
            self.approval_pending_since = None

    def _observe_delegated_lifecycle(
        self,
        event: DelegatedTaskLifecycleEvent,
        *,
        observed_at: float | None,
    ) -> DelegatedTaskLifecycleTransition:
        was_delegated = self.delegated_tasks_active > 0
        self.delegated_tasks_active = event.active_count
        self.delegated_tasks_last_activity_at = observed_at
        if event.active_count <= 0:
            self.delegated_tasks_oldest_started_at = None
            if was_delegated:
                # A full fresh ordinary grace begins only after the last
                # delegated task settles. Time spent delegated never consumes
                # the missing-terminal budget.
                self.terminal_stall_started_at = observed_at
            return DelegatedTaskLifecycleTransition(event)
        self.terminal_stall_started_at = None
        if observed_at is not None and event.oldest_age_seconds is not None:
            self.delegated_tasks_oldest_started_at = max(
                0.0,
                observed_at - event.oldest_age_seconds,
            )
        else:
            self.delegated_tasks_oldest_started_at = observed_at
        return DelegatedTaskLifecycleTransition(event)

    def observe(
        self,
        event: AgentEvent,
        *,
        observed_at: float | None = None,
    ) -> TurnEventTransition:
        """Apply one normalized event and return its typed routing transition."""

        # Delegated lifecycle frames are observation-only and can arrive while
        # a provider approval callback remains outstanding. They must not clear
        # that approval lease or replace its deadline.
        self._observe_approval_state(event, observed_at=observed_at)

        if not isinstance(event, DelegatedTaskLifecycleEvent):
            if self.delegated_tasks_active <= 0:
                # Preserve the existing stream-silence contract explicitly:
                # every accepted non-delegated event re-arms the ordinary
                # missing-terminal grace from this observation.
                self.terminal_stall_started_at = observed_at

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
        if isinstance(event, DelegatedTaskLifecycleEvent):
            return self._observe_delegated_lifecycle(event, observed_at=observed_at)
        if isinstance(
            event,
            (ReasoningDeltaEvent, ApprovalRequestEvent, CompletionEvent),
        ):
            return IgnoredTransition(event)
        raise TypeError(f"unsupported agent event: {type(event).__name__}")
