"""Regression coverage for empty normal completions (#775, #1128).

A provider turn that ends with ResultEvent + CompletionEvent but no
user-visible text events must not surface "(No response)" as a successful
answer: the terminal result text is recovered when the payload carries it
(event-loss class), and otherwise the turn is classified as a typed,
retryable failure (truly-empty class). Streaming, interim delivery, and the
terminal-stall/unsolicited paths keep their exactly-once semantics.

#1128 splits that truly-empty class once more. When a follower is queued
behind the turn, the provider answered both messages at once and this
request is simply the half without text — an explained outcome that must
not carry retry guidance, since resending only enqueues another message.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core.agent_runtime import (
    AgentEvent,
    ApprovalHandler,
    CompletionEvent,
    MessageCompletedEvent,
    ResultEvent,
    SessionRequest,
    TextDeltaEvent,
    ToolStartedEvent,
    deny_approval,
)
from telegram_bot.core.project_chat import ProjectChatHandler


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _settings(tmp_path: Path, provider: str) -> SimpleNamespace:
    return SimpleNamespace(
        agent_provider=provider,
        project_root=tmp_path,
        execution_profile="strict-project",
        bash_policy="disabled",
        allowed_user_ids=[7],
        require_allowlist=True,
        claude_cli_path=None,
        claude_settings_path=tmp_path / "claude" / "settings.json",
        enable_streaming=False,
        enable_partial_streaming=False,
        bot_data_dir=None,
        task_ledger_path=None,
        session_guard_enabled=False,
    )


class FakeSession:
    def __init__(self, session_id: str, events: list[AgentEvent]) -> None:
        self.session_id = session_id
        self.events = events

    def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        async def stream() -> AsyncIterator[AgentEvent]:
            for event in self.events:
                yield event

        return stream()

    async def interrupt(self) -> None:
        return None


class FakeRuntime:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.supports_session_browsing = False

    async def start_or_resume(self, request: SessionRequest) -> FakeSession:
        return self.session

    async def close(self) -> None:
        return None

    async def recycle(self) -> bool:
        return True


def _handler(
    tmp_path: Path,
    runtime: FakeRuntime,
    *,
    provider: str = "claude",
) -> ProjectChatHandler:
    handler = ProjectChatHandler(
        settings=_settings(tmp_path, provider),
        agent_runtime=runtime,
    )
    handler._task_ledger_cache = False
    return handler


def _claude_result(text: str, session_id: str) -> ResultEvent:
    return ResultEvent(
        {
            "subtype": "success",
            "result": text,
            "session_id": session_id,
            "duration_ms": 5,
            "num_turns": 1,
            "total_cost_usd": 0.0,
            "usage": {},
        }
    )


@pytest.mark.anyio
async def test_empty_completion_recovers_terminal_result_text(tmp_path: Path) -> None:
    # ClaudeRuntime shape: ResultEvent preserves message.result even when no
    # TextDeltaEvent ever produced user-visible text (#775 acceptance 1+2).
    session = FakeSession(
        "claude-empty",
        [
            _claude_result("The real final answer.", "claude-empty"),
            CompletionEvent("end_turn"),
        ],
    )
    handler = _handler(tmp_path, FakeRuntime(session))

    response = await handler.process_message("hello", 7, 70)

    assert response.success is True
    assert response.content == "The real final answer."
    assert response.streamed is False


@pytest.mark.anyio
async def test_empty_completion_whitespace_result_is_typed_failure(tmp_path: Path) -> None:
    # Result text that cleans to empty is the truly-empty class: a typed,
    # retryable failure — never a "(No response)" success (#775 acceptance 3).
    session = FakeSession(
        "claude-blank",
        [
            _claude_result("   \n  ", "claude-blank"),
            CompletionEvent("end_turn"),
        ],
    )
    handler = _handler(tmp_path, FakeRuntime(session))

    response = await handler.process_message("hello", 7, 70)

    assert response.success is False
    assert response.error
    assert "(No response)" not in response.content
    assert "retry" in response.content.lower()


@pytest.mark.anyio
async def test_empty_completion_codex_payload_is_typed_failure(tmp_path: Path) -> None:
    # Codex shape: the ResultEvent payload is the raw turn object, which
    # carries no safe final user text (#775 acceptance 5).
    session = FakeSession(
        "codex-empty",
        [
            ResultEvent({"id": "turn-1", "status": "completed"}),
            CompletionEvent("end_turn"),
        ],
    )
    handler = _handler(tmp_path, FakeRuntime(session), provider="codex")

    response = await handler.process_message("hello", 7, 70)

    assert response.success is False
    assert response.error
    assert "(No response)" not in response.content


@pytest.mark.anyio
async def test_visible_answer_is_not_duplicated_by_result_text(tmp_path: Path) -> None:
    # Exactly-once: deltas that already produced the visible answer win; the
    # identical terminal result text must not be appended a second time.
    session = FakeSession(
        "claude-visible",
        [
            TextDeltaEvent("The answer."),
            _claude_result("The answer.", "claude-visible"),
            CompletionEvent("end_turn"),
        ],
    )
    handler = _handler(tmp_path, FakeRuntime(session))

    response = await handler.process_message("hello", 7, 70)

    assert response.success is True
    assert response.content == "The answer."


@pytest.mark.anyio
async def test_delivered_interim_with_empty_final_is_not_a_placeholder(
    tmp_path: Path,
) -> None:
    # Exactly-once: an interim bubble that already reached the user is the
    # answer; the empty final must add neither a placeholder nor a duplicate.
    delivered: list[str] = []

    async def deliver_interim(content: str) -> None:
        delivered.append(content)

    session = FakeSession(
        "claude-interim",
        [
            TextDeltaEvent("Working on it."),
            MessageCompletedEvent(),
            ToolStartedEvent("tool-1", "command", {"command": "true"}),
            _claude_result("", "claude-interim"),
            CompletionEvent("end_turn"),
        ],
    )
    handler = _handler(tmp_path, FakeRuntime(session))

    response = await handler.process_message(
        "go",
        7,
        70,
        interim_message_callback=deliver_interim,
    )

    assert delivered == ["Working on it."]
    assert response.success is True
    assert response.content == ""
    assert response.streamed is True


@pytest.mark.anyio
async def test_empty_completion_with_follower_is_coalesced_not_a_retry(
    tmp_path: Path,
) -> None:
    # #1128: two messages sent within about a second reach the provider as one
    # turn, so one request carries the answer and the other ends empty. The
    # empty half must not tell the user to retry — a resend just enqueues
    # another message and splits the next turn the same way.
    session = FakeSession(
        "claude-coalesced",
        [
            _claude_result("", "claude-coalesced"),
            CompletionEvent("end_turn"),
        ],
    )
    handler = _handler(tmp_path, FakeRuntime(session))
    # A follower registered before this turn finishes is exactly the state a
    # second Telegram message produces while the first still holds the lock.
    handler._enter_conversation_queue(handler._stream_key(7, 70))

    response = await handler.process_message("hello", 7, 70)

    assert response.success is False
    assert response.error == "coalesced_turn"
    assert "retry" not in response.content.lower()
    assert "together" in response.content.lower()
    assert "(No response)" not in response.content


@pytest.mark.anyio
async def test_empty_completion_without_follower_keeps_the_typed_failure(
    tmp_path: Path,
) -> None:
    # The #1128 branch must stay narrow: with nobody queued behind the turn an
    # empty answer is still unexplained, so #775's retryable failure stands.
    session = FakeSession(
        "claude-lonely",
        [
            _claude_result("", "claude-lonely"),
            CompletionEvent("end_turn"),
        ],
    )
    handler = _handler(tmp_path, FakeRuntime(session))

    response = await handler.process_message("hello", 7, 70)

    assert response.success is False
    assert response.error == "Agent finished without a visible answer"
    assert "retry" in response.content.lower()


@pytest.mark.anyio
async def test_conversation_queue_count_is_released_after_a_turn(
    tmp_path: Path,
) -> None:
    # The counter backs a user-visible branch, so a leak would silently turn
    # every later empty completion on this chat into a "coalesced" claim.
    session = FakeSession(
        "claude-clean",
        [
            _claude_result("done", "claude-clean"),
            CompletionEvent("end_turn"),
        ],
    )
    handler = _handler(tmp_path, FakeRuntime(session))
    key = handler._stream_key(7, 70)

    response = await handler.process_message("hello", 7, 70)

    assert response.success is True
    assert handler._conversation_followers(key) == 0
    assert key not in handler._conversation_pending
