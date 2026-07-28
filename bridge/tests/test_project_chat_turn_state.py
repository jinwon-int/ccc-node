"""Unit tests for the I/O-free provider-neutral turn event state."""

from typing import cast

import pytest

from telegram_bot.core import project_chat_turn_state as turn_state_module
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
from telegram_bot.core.project_chat_turn_state import (
    ErrorTransition,
    IgnoredTransition,
    MessageCompletedTransition,
    ResultTransition,
    TextDeltaTransition,
    ToolCompletedTransition,
    ToolStartedTransition,
    TurnEventState,
)


def test_turn_state_starts_without_lifecycle_or_io_authority() -> None:
    state = TurnEventState()

    assert state.busy_depth == 0
    assert state.approval_pending is False
    assert state.admitted is False
    assert state.needs_attempt_recording is True
    assert state.terminal_error is None
    assert state.terminal_result_text is None
    assert state.active_tools == {}
    assert state.active_tool_ids == set()
    assert state.current_tool_label is None


def test_admission_and_attempt_observations_are_idempotent() -> None:
    state = TurnEventState()

    state.mark_admitted()
    state.mark_admitted()
    state.mark_attempt_recorded()
    state.mark_attempt_recorded()

    assert state.admitted is True
    assert state.needs_attempt_recording is False


def test_text_and_message_transitions_preserve_event_identity() -> None:
    state = TurnEventState()
    text = TextDeltaEvent("hello")
    completed = MessageCompletedEvent()

    text_transition = state.observe(text)
    completed_transition = state.observe(completed)

    assert isinstance(text_transition, TextDeltaTransition)
    assert text_transition.event is text
    assert isinstance(completed_transition, MessageCompletedTransition)
    assert completed_transition.event is completed


def test_approval_pending_tracks_only_the_latest_observed_event() -> None:
    state = TurnEventState()
    approval = ApprovalRequestEvent(
        "approval-1",
        "Bash",
        {"command": "true"},
        "run a command",
    )

    transition = state.observe(approval)

    assert isinstance(transition, IgnoredTransition)
    assert transition.event is approval
    assert state.approval_pending is True

    reasoning = ReasoningDeltaEvent("private")
    transition = state.observe(reasoning)

    assert isinstance(transition, IgnoredTransition)
    assert transition.event is reasoning
    assert state.approval_pending is False


def test_nested_tools_keep_balance_and_restore_previous_label() -> None:
    state = TurnEventState()
    first = ToolStartedEvent("tool-1", "Bash", {"command": "echo first"})
    second = ToolStartedEvent("tool-2", "Read", {"file_path": "/tmp/second"})

    first_transition = state.observe(first)
    second_transition = state.observe(second)

    assert isinstance(first_transition, ToolStartedTransition)
    assert first_transition.current_tool_label == "Bash: echo first"
    assert isinstance(second_transition, ToolStartedTransition)
    assert second_transition.current_tool_label == "Read: /tmp/second"
    assert state.busy_depth == 2
    assert state.current_tool_label == "Read: /tmp/second"

    second_done = state.observe(
        ToolCompletedEvent("tool-2", "Read", result=None, success=True)
    )

    assert isinstance(second_done, ToolCompletedTransition)
    assert second_done.current_tool_label == "Bash: echo first"
    assert state.busy_depth == 1

    first_done = state.observe(
        ToolCompletedEvent("tool-1", "Bash", result=None, success=True)
    )

    assert isinstance(first_done, ToolCompletedTransition)
    assert first_done.current_tool_label is None
    assert state.busy_depth == 0
    assert state.active_tools == {}


def test_duplicate_or_unknown_tool_completion_never_makes_depth_negative() -> None:
    state = TurnEventState()
    completed = ToolCompletedEvent("missing", "Bash", result=None, success=False)

    first = state.observe(completed)
    second = state.observe(completed)

    assert isinstance(first, ToolCompletedTransition)
    assert isinstance(second, ToolCompletedTransition)
    assert state.busy_depth == 0
    assert state.active_tools == {}


def test_unknown_completion_cannot_release_a_different_active_tool() -> None:
    state = TurnEventState()
    state.observe(ToolStartedEvent("tool-1", "Bash", {"command": "sleep 1"}))

    state.observe(
        ToolCompletedEvent("unknown", "Bash", result=None, success=False)
    )

    assert state.busy_depth == 1
    assert state.active_tool_ids == {"tool-1"}
    assert state.current_tool_label == "Bash: sleep 1"

    state.observe(
        ToolCompletedEvent("tool-1", "Bash", result=None, success=True)
    )

    assert state.busy_depth == 0
    assert state.active_tool_ids == set()


def test_duplicate_start_counts_one_active_call_id() -> None:
    state = TurnEventState()
    started = ToolStartedEvent("tool-1", "Bash", {"command": "echo duplicate"})

    state.observe(started)
    state.observe(started)

    assert state.busy_depth == 1
    assert state.active_tool_ids == {"tool-1"}
    assert state.active_tools == {"tool-1": "Bash: echo duplicate"}

    state.observe(
        ToolCompletedEvent("tool-1", "Bash", result=None, success=True)
    )

    assert state.busy_depth == 0
    assert state.active_tool_ids == set()
    assert state.active_tools == {}


def test_tool_without_label_still_balances_by_call_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(turn_state_module, "tool_label", lambda _name, _input: None)
    state = TurnEventState()

    started = state.observe(ToolStartedEvent("tool-1", "Custom", {}))

    assert isinstance(started, ToolStartedTransition)
    assert started.current_tool_label is None
    assert state.busy_depth == 1
    assert state.active_tool_ids == {"tool-1"}
    assert state.active_tools == {}

    state.observe(
        ToolCompletedEvent("tool-1", "Custom", result=None, success=True)
    )

    assert state.busy_depth == 0
    assert state.active_tool_ids == set()


def test_error_projection_preserves_current_latest_error_behavior() -> None:
    state = TurnEventState()
    first = ErrorEvent("first", "first error")
    second = ErrorEvent("second", "second error")

    first_transition = state.observe(first)
    second_transition = state.observe(second)

    assert isinstance(first_transition, ErrorTransition)
    assert first_transition.event is first
    assert isinstance(second_transition, ErrorTransition)
    assert second_transition.event is second
    assert state.terminal_error is second


def test_result_and_ignored_events_remain_typed_and_body_preserving() -> None:
    state = TurnEventState()
    result = ResultEvent({"ok": True})
    completion = CompletionEvent("end_turn")

    result_transition = state.observe(result)
    completion_transition = state.observe(completion)

    assert isinstance(result_transition, ResultTransition)
    assert result_transition.event is result
    assert isinstance(completion_transition, IgnoredTransition)
    assert completion_transition.event is completion


def test_result_event_captures_terminal_result_text() -> None:
    # #775: the Claude adapter payload carries message.result under "result";
    # the frozen (MappingProxy) payload must still yield the text.
    state = TurnEventState()

    state.observe(ResultEvent({"subtype": "success", "result": "final text"}))

    assert state.terminal_result_text == "final text"


def test_result_event_without_safe_text_leaves_terminal_result_empty() -> None:
    state = TurnEventState()

    state.observe(ResultEvent({"id": "turn-1", "status": "completed"}))
    assert state.terminal_result_text is None
    state.observe(ResultEvent({"result": ""}))
    assert state.terminal_result_text is None
    state.observe(ResultEvent({"result": 42}))
    assert state.terminal_result_text is None


def test_unknown_runtime_event_fails_closed() -> None:
    state = TurnEventState()

    with pytest.raises(TypeError, match="unsupported agent event"):
        state.observe(cast(AgentEvent, object()))
