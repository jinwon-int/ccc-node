"""Handler-side wiring tests for the #646 async-completion journal.

Covers the composition-root contract: the journal is built fail-open under
the project root, the runtime observer seam is registered, and an unowned
completion observation produces exactly one durable body-free record and at
most one owner notice per identity.
"""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from telegram_bot.core.async_completion_journal import AsyncCompletionJournal
from telegram_bot.core.project_chat import ProjectChatHandler


class _FakeRuntime:
    """Runtime double exposing only the #646 observer seam."""

    def __init__(self) -> None:
        self.listener = None
        self.supports_session_browsing = True

    def set_unowned_completion_listener(self, listener) -> None:
        self.listener = listener


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        agent_provider="codex",
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
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_handler_registers_listener_and_defers_filesystem(tmp_path: Path) -> None:
    runtime = _FakeRuntime()
    handler = ProjectChatHandler(settings=_settings(tmp_path), agent_runtime=runtime)

    assert callable(runtime.listener)
    assert isinstance(handler._async_completion_journal, AsyncCompletionJournal)
    # Deferred-initialization invariant: construction never touches disk.
    assert not (tmp_path / ".telegram_bot" / "async-completions").exists()

    handler._observe_unowned_codex_completion("thread-orphan", "turn-x")
    # The failed-closed observation (no resident route) still initializes the
    # journal root; the root being created is fine after composition.
    assert (tmp_path / ".telegram_bot" / "async-completions").is_dir()


def test_handler_fail_open_without_journal_support(tmp_path: Path) -> None:
    runtime = _FakeRuntime()
    handler = ProjectChatHandler(settings=_settings(tmp_path), agent_runtime=runtime)
    # Simulate a broken journal after construction; the boundary must keep
    # the conversation path working and simply drop durable evidence.
    handler._async_completion_journal = None
    handler._observe_unowned_codex_completion("thread-1", "turn-1")


def test_observation_is_body_free_and_exactly_once(tmp_path: Path) -> None:
    runtime = _FakeRuntime()
    handler = ProjectChatHandler(settings=_settings(tmp_path), agent_runtime=runtime)
    journal = handler._async_completion_journal
    assert journal is not None

    # No resident session for the thread: fail-closed, nothing recorded.
    handler._observe_unowned_codex_completion("thread-orphan", "turn-x")
    assert journal.counts().get("queued", 0) == 0

    # Bind a resident session for the route so attribution can succeed.
    from telegram_bot.core.project_chat_types import AgentSessionEntry

    class _Session:
        session_id = "thread-known"

    handler._agent_session_registry.put_cached(
        (7, 42), AgentSessionEntry(session=_Session())
    )
    # Sessions become resident only after a first turn, so the route's
    # lifecycle generation is ≥ 1 in every real observation; seed it directly
    # to mirror that post-turn state without driving a full turn here.
    handler._agent_session_registry._generation_high_water[(7, 42)] = 4

    handler._observe_unowned_codex_completion("thread-known", "turn-detached")
    handler._observe_unowned_codex_completion("thread-known", "turn-detached")

    counts = journal.counts()
    assert counts.get("queued", 0) == 1
    records = journal.list_records()
    assert len(records) == 1
    record = records[0]
    assert record.provider == "codex"
    assert record.session_generation == 4
    assert record.noticed_at is not None

    # Durable storage never carries the raw provider identifiers.
    record_id = journal.record_id_for(record.idempotency_key)
    payload = (journal.root / f"{record_id}.json").read_text(encoding="utf-8")
    assert "thread-known" not in payload
    assert "turn-detached" not in payload
