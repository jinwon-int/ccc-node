"""#2161: progress-update thinking blocks become user-visible interim text.

On Claude Fable 5.x / Mythos 5.x / Opus 5.5 the narration written between
tool calls arrives as a non-empty ``thinking`` block right before the
``tool_use`` it introduces, not as a ``text`` block. The adapter must route
those blocks through the text path (own message boundary, so the next tool
start delivers them as an interim bubble) while every other model keeps its
thinking private.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_agent_sdk import AssistantMessage, TextBlock, ThinkingBlock, ToolUseBlock

from telegram_bot.core.agent_runtime import (
    MessageCompletedEvent,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolStartedEvent,
)
from telegram_bot.core.claude_runtime import _ActiveTurn
from telegram_bot.core.claude_session_turn_events import (
    PROGRESS_INTERRUPTED_SENTINEL,
    PROGRESS_THINKING_MODEL_PREFIXES,
    ClaudeSessionTurnEventsMixin,
    is_progress_thinking_model,
    progress_thinking_model_prefixes,
)

FABLE = "claude-fable-5-1"
NOTE = "브리지 로그를 찾았습니다. 이제 상태 라인을 확인합니다."


class _Host(ClaudeSessionTurnEventsMixin):
    """Bare adapter host: the mixin only reads ``_settings`` (optional)."""

    def __init__(self, settings: object | None = None) -> None:
        self._settings = settings
        self._active_turn = None
        self._observe_background_task_result = lambda result: None


def _drain(active: _ActiveTurn) -> list[object]:
    events: list[object] = []
    while not active.queue.empty():
        events.append(active.queue.get_nowait())
    return events


def _route(host: _Host, model: str, blocks: list[object]) -> list[object]:
    active = _ActiveTurn(queue=asyncio.Queue(), approval_handler=None, generation=1)
    host._route_assistant_message(active, AssistantMessage(content=blocks, model=model))
    return _drain(active)


def _tool() -> ToolUseBlock:
    return ToolUseBlock(id="toolu-1", name="Bash", input={"command": "pwd"})


def test_progress_note_on_fable_becomes_its_own_text_message() -> None:
    events = _route(_Host(), FABLE, [ThinkingBlock(thinking=NOTE, signature="sig"), _tool()])
    assert [type(e) for e in events] == [TextDeltaEvent, MessageCompletedEvent, ToolStartedEvent]
    assert events[0].text == NOTE


def test_empty_progress_block_emits_nothing() -> None:
    # Reasoning stays an empty block under display "omitted"/"updates".
    events = _route(_Host(), FABLE, [ThinkingBlock(thinking="", signature="sig"), _tool()])
    assert [type(e) for e in events] == [ToolStartedEvent]


def test_interrupted_sentinel_is_not_narrated() -> None:
    # The stand-in text stays on the private reasoning channel, never as text.
    events = _route(
        _Host(), FABLE, [ThinkingBlock(thinking=PROGRESS_INTERRUPTED_SENTINEL, signature="sig")]
    )
    assert [type(e) for e in events] == [ReasoningDeltaEvent]


def test_other_models_keep_thinking_private() -> None:
    for model in ("claude-opus-5", "claude-opus-4-8", "claude-sonnet-5", "claude-conformance-model"):
        events = _route(_Host(), model, [ThinkingBlock(thinking=NOTE, signature="sig"), _tool()])
        assert [type(e) for e in events] == [ReasoningDeltaEvent, ToolStartedEvent], model


def test_kill_switch_keeps_thinking_private() -> None:
    host = _Host(SimpleNamespace(claude_progress_thinking=False))
    events = _route(host, FABLE, [ThinkingBlock(thinking=NOTE, signature="sig"), _tool()])
    assert [type(e) for e in events] == [ReasoningDeltaEvent, ToolStartedEvent]


def test_model_prefix_list_is_configurable() -> None:
    host = _Host(
        SimpleNamespace(claude_progress_thinking=True, claude_progress_thinking_models="claude-opus-5")
    )
    events = _route(host, "claude-opus-5", [ThinkingBlock(thinking=NOTE, signature="sig"), _tool()])
    assert [type(e) for e in events] == [TextDeltaEvent, MessageCompletedEvent, ToolStartedEvent]
    # And the default list no longer applies once overridden.
    events = _route(host, FABLE, [ThinkingBlock(thinking=NOTE, signature="sig"), _tool()])
    assert [type(e) for e in events] == [ReasoningDeltaEvent, ToolStartedEvent]


def test_streamed_text_closes_before_the_progress_note() -> None:
    # Token deltas streamed "hi" for this SDK message; the whole-block
    # TextBlock is deduped, then the note closes the streamed text first and
    # becomes a second message of its own.
    host = _Host()
    active = _ActiveTurn(queue=asyncio.Queue(), approval_handler=None, generation=1)
    active.streamed_current_message = True
    active.emitted_text = True
    host._route_assistant_message(
        active,
        AssistantMessage(
            content=[TextBlock(text="hi"), ThinkingBlock(thinking=NOTE, signature="sig"), _tool()],
            model=FABLE,
        ),
    )
    events = _drain(active)
    assert [type(e) for e in events] == [
        MessageCompletedEvent,  # closes the streamed "hi"
        TextDeltaEvent,
        MessageCompletedEvent,
        ToolStartedEvent,
    ]
    assert events[1].text == NOTE
    assert active.streamed_current_message is False


def test_prefix_helpers() -> None:
    assert progress_thinking_model_prefixes(None) == PROGRESS_THINKING_MODEL_PREFIXES
    assert progress_thinking_model_prefixes("  ") == PROGRESS_THINKING_MODEL_PREFIXES
    assert progress_thinking_model_prefixes(" claude-opus-5 , Claude-Fable-5 ") == (
        "claude-opus-5",
        "claude-fable-5",
    )
    prefixes = PROGRESS_THINKING_MODEL_PREFIXES
    assert is_progress_thinking_model("claude-fable-5-1", prefixes)
    assert is_progress_thinking_model("claude-mythos-5-1", prefixes)
    assert is_progress_thinking_model("claude-opus-5-5", prefixes)
    assert not is_progress_thinking_model("claude-opus-5", prefixes)
    assert not is_progress_thinking_model("", prefixes)
    assert not is_progress_thinking_model(None, prefixes)
