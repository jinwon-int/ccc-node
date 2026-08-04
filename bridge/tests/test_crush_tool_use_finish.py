"""Regression tests: crush finish reason "tool_use" is an intermediate step
boundary, not a turn outcome (#926 follow-up).

bangtong 2026-08-04: every crush turn in which kimi/k3 called a tool died as
"Processing failed: tool_use". crush maps the provider's tool_calls finish to
FinishReasonToolUse with EMPTY message/details (``AddFinish(reason, "", "")``),
and the bridge's ``_complete_turn`` only whitelisted end_turn/stop/... — the
unknown reason fell into the error branch, which then echoed the bare reason
string as the failure message. Pure-text turns (finish end_turn) were the only
ones that survived, which is why canary5b passed while every real task failed.

The fix: ``tool_use`` is an intermediate step boundary — the agent loop runs
the tools and calls the model again — so it must not complete the turn early
and must not fail it. Permission denials already arrive as end_turn (crush
maps StopTurn tool results to end_turn), so waiting is correct.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from telegram_bot.core.agent_runtime import (
    CompletionEvent,
    ErrorEvent,
    ResultEvent,
    deny_approval,
)
from telegram_bot.core.crush_runtime import CrushRuntime, _ActiveTurn


def _active() -> _ActiveTurn:
    return _ActiveTurn(asyncio.Queue(), deny_approval)


def _drain(queue: asyncio.Queue) -> list[object]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


class TestToolUseFinishIsIntermediate:
    def test_tool_use_does_not_fail_or_finish(self):
        active = _active()
        CrushRuntime._complete_turn(active, active, {"reason": "tool_use", "message": "", "details": ""})
        assert active.finished is False
        assert _drain(active.queue) == []

    def test_tool_use_keeps_collecting_then_end_turn_completes(self):
        active = _active()
        active.collected_text.append("working… ")
        CrushRuntime._complete_turn(active, active, {"reason": "tool_use"})
        assert active.finished is False
        active.collected_text.append("done")
        CrushRuntime._complete_turn(active, active, {"reason": "end_turn"})
        events = _drain(active.queue)
        assert active.finished is True
        assert any(isinstance(e, ResultEvent) for e in events)
        assert any(isinstance(e, CompletionEvent) for e in events)
        assert not any(isinstance(e, ErrorEvent) for e in events)

    def test_unknown_reason_still_fails_with_detail(self):
        active = _active()
        CrushRuntime._complete_turn(active, active, {"reason": "content_filter", "message": "", "details": ""})
        events = _drain(active.queue)
        assert active.finished is True
        errors = [e for e in events if isinstance(e, ErrorEvent)]
        assert errors and errors[0].code == "crush_turn_failed"
        assert errors[0].message == "content_filter"

    def test_cancel_reason_interrupts(self):
        active = _active()
        CrushRuntime._complete_turn(active, active, {"reason": "canceled"})
        events = _drain(active.queue)
        assert active.finished is True
        errors = [e for e in events if isinstance(e, ErrorEvent)]
        assert errors and errors[0].code == "interrupted"
