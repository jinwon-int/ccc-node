"""Cloudflare 403 block page is a transient edge block (#2808).

2026-11-13: three nodes failed turns within 30 minutes on
``Failed to authenticate. API Error: 403 Attention Required! | Cloudflare``.
The bridge classified it ``retryable=False`` and an automatic follow-up turn
ended with "did not finish (error)"; the next turn minutes later succeeded.
The block page is retried once (after a short delay) when the turn produced
nothing yet; a plain 403, or a turn that already reached a tool, is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from telegram_bot.core import project_chat_process
from telegram_bot.core.agent_runtime import CompletionEvent, ErrorEvent, ToolStartedEvent
from telegram_bot.core.sdk_text import is_edge_block_message

from test_project_chat_empty_completion import (
    FakeRuntime,
    _claude_result,
    _enable_retries,
    _handler,
    _SequencedSession,
)

BLOCK = "Failed to authenticate. API Error: 403 Attention Required! | Cloudflare"
PLAIN_403 = "Failed to authenticate. API Error: 403 Forbidden"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(project_chat_process, "_EDGE_BLOCK_RETRY_DELAY_SECONDS", 0.0)


def test_classifier_matches_only_the_block_page() -> None:
    assert is_edge_block_message(BLOCK)
    assert is_edge_block_message("api error: 403 attention required!\n| cloudflare ray id 1")
    assert not is_edge_block_message(PLAIN_403)
    assert not is_edge_block_message("API Error: 503 Attention Required! | Cloudflare")
    assert not is_edge_block_message("Attention Required! | Cloudflare")  # no 403
    assert not is_edge_block_message("")
    assert not is_edge_block_message(None)


@pytest.mark.anyio
async def test_edge_block_before_any_output_is_retried(tmp_path: Path) -> None:
    session = _SequencedSession(
        "claude-edge",
        [
            [ErrorEvent("claude_turn_failed", BLOCK)],
            [_claude_result("CI is green, continuing.", "claude-edge"), CompletionEvent("end_turn")],
        ],
    )
    handler = _handler(tmp_path, FakeRuntime(session))
    _enable_retries(handler)

    response = await handler.process_message("[external_event: ci green]", 7, 70)

    assert response.success is True
    assert response.content == "CI is green, continuing."
    assert session.calls == ["[external_event: ci green]"] * 2


@pytest.mark.anyio
async def test_edge_block_retry_exhaustion_is_labelled(tmp_path: Path) -> None:
    session = _SequencedSession(
        "claude-edge2",
        [[ErrorEvent("claude_turn_failed", BLOCK)], [ErrorEvent("claude_turn_failed", BLOCK)]],
    )
    handler = _handler(tmp_path, FakeRuntime(session))
    _enable_retries(handler)

    response = await handler.process_message("hello", 7, 70)

    assert response.success is False
    assert response.failure_class == "edge-block"
    assert "(temporary edge block)" in response.content
    assert len(session.calls) == 2  # bounded: one retry only


@pytest.mark.anyio
async def test_plain_403_is_not_retried(tmp_path: Path) -> None:
    session = _SequencedSession(
        "claude-auth",
        [[ErrorEvent("claude_turn_failed", PLAIN_403)], [_claude_result("x", "claude-auth")]],
    )
    handler = _handler(tmp_path, FakeRuntime(session))
    _enable_retries(handler)

    response = await handler.process_message("hello", 7, 70)

    assert response.success is False
    assert response.failure_class is None
    assert "(temporary edge block)" not in response.content
    assert session.calls == ["hello"]


@pytest.mark.anyio
async def test_edge_block_after_a_tool_started_is_not_replayed(tmp_path: Path) -> None:
    # A tool may have had a side effect; replaying the turn could repeat it.
    session = _SequencedSession(
        "claude-tool",
        [
            [
                ToolStartedEvent("tool-1", "command", {"command": "true"}),
                ErrorEvent("claude_turn_failed", BLOCK),
            ],
            [_claude_result("x", "claude-tool")],
        ],
    )
    handler = _handler(tmp_path, FakeRuntime(session))
    _enable_retries(handler)

    response = await handler.process_message("hello", 7, 70)

    assert response.success is False
    assert response.failure_class is None
    assert "(temporary edge block)" in response.content  # still labelled for the user
    assert session.calls == ["hello"]


@pytest.mark.anyio
async def test_edge_block_without_retry_budget_is_single_attempt(tmp_path: Path) -> None:
    session = _SequencedSession(
        "claude-nobudget",
        [[ErrorEvent("claude_turn_failed", BLOCK)], [_claude_result("x", "claude-nobudget")]],
    )
    handler = _handler(tmp_path, FakeRuntime(session))  # retries off (SimpleNamespace default)

    response = await handler.process_message("hello", 7, 70)

    assert response.success is False
    assert response.failure_class == "edge-block"
    assert session.calls == ["hello"]
