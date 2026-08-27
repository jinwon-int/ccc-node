"""Handler-side wiring tests for the #646 async-completion journal.

Covers the composition-root contract: the journal is built fail-open under
the project root, the runtime observer seam is registered, and an unowned
completion observation produces exactly one durable body-free record and at
most one owner notice per identity.  The slice-2 classes cover conversation
delivery for capability-declared (durable) runtimes.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest


from telegram_bot.core.async_completion_journal import AsyncCompletionJournal
from telegram_bot.core.project_chat import ProjectChatHandler


class _FakeRuntime:
    """Runtime double exposing only the #646 observer seam."""

    def __init__(self) -> None:
        self.listener = None
        self.durable_listener = None
        self.supports_session_browsing = True

    def set_unowned_completion_listener(self, listener) -> None:
        self.listener = listener

    def set_durable_completion_listener(self, listener) -> None:
        self.durable_listener = listener


class _DurableCapableRuntime(_FakeRuntime):
    """Runtime double declaring durable delivery (#646 slice 2)."""

    def async_completion_capability(self):
        from telegram_bot.core.agent_runtime import AsyncCompletionCapability

        return AsyncCompletionCapability(
            provider="codex",
            state="supported",
            protocol_version="1",
            notification_method="turn/completed",
            recovery_method="thread/read",
            ownership_scope="detached_task",
            supports_durable_delivery=True,
            reason_code="test_contract",
        )


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


def _bind_resident_session(
    handler: ProjectChatHandler, key=(7, 42), generation=4
) -> None:
    from telegram_bot.core.project_chat_types import AgentSessionEntry

    class _Session:
        session_id = "thread-known"

    handler._agent_session_registry.put_cached(
        key, AgentSessionEntry(session=_Session())
    )
    # Sessions become resident only after a first turn, so the route's
    # lifecycle generation is >= 1 in every real observation; seed it
    # directly to mirror that post-turn state without driving a full turn.
    handler._agent_session_registry._generation_high_water[key] = generation


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
    _bind_resident_session(handler)

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


class TestDurableDeliveryWiring(unittest.IsolatedAsyncioTestCase):
    """Slice-2 conversation delivery wiring (capability-declared runtimes)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _handler(self, runtime) -> ProjectChatHandler:
        return ProjectChatHandler(
            settings=_settings(self.root), agent_runtime=runtime
        )

    async def _drain(self, ticks: int = 30) -> None:
        for _ in range(ticks):
            await asyncio.sleep(0)

    async def test_durable_observation_delivers_to_conversation_once(self) -> None:
        runtime = _DurableCapableRuntime()
        handler = self._handler(runtime)
        sent: list[tuple[int, int, str]] = []

        async def sender(user_id, chat_id, text):
            sent.append((user_id, chat_id, text))
            return True

        handler.set_async_completion_sender(sender)
        _bind_resident_session(handler)
        assert callable(runtime.durable_listener)

        runtime.durable_listener("thread-known", "turn-detached", "hello body")
        await self._drain()

        assert len(sent) == 1
        assert sent[0][0] == 7 and sent[0][1] == 42
        assert "hello body" in sent[0][2]
        journal = handler._async_completion_journal
        assert journal is not None
        records = journal.list_records()
        assert len(records) == 1
        assert records[0].state == "delivered"
        # The route binding is the only raw identifier in durable storage.
        record_id = journal.record_id_for(records[0].idempotency_key)
        payload = (journal.root / f"{record_id}.json").read_text(encoding="utf-8")
        assert "thread-known" not in payload
        assert "turn-detached" not in payload

    async def test_ten_duplicate_observations_send_once(self) -> None:
        runtime = _DurableCapableRuntime()
        handler = self._handler(runtime)
        sends = 0

        async def sender(user_id, chat_id, text):
            nonlocal sends
            sends += 1
            return True

        handler.set_async_completion_sender(sender)
        _bind_resident_session(handler)

        for _ in range(10):
            runtime.durable_listener("thread-known", "turn-detached", "body")
        await self._drain(60)

        assert sends == 1

    async def test_generation_rotation_drops_without_delivery(self) -> None:
        runtime = _DurableCapableRuntime()
        handler = self._handler(runtime)
        sends = 0

        async def sender(user_id, chat_id, text):
            nonlocal sends
            sends += 1
            return True

        handler.set_async_completion_sender(sender)
        _bind_resident_session(handler, generation=4)

        runtime.durable_listener("thread-known", "turn-detached", "stale body")
        # The conversation rotates sessions before the delivery task runs.
        handler._agent_session_registry._generation_high_water[(7, 42)] = 9
        await self._drain()

        assert sends == 0
        journal = handler._async_completion_journal
        assert journal is not None
        records = journal.list_records()
        assert len(records) == 1
        assert records[0].state == "terminal_failed"
        assert records[0].last_error_code == "generation_mismatch"

    async def test_without_sender_falls_back_to_owner_notice(self) -> None:
        runtime = _DurableCapableRuntime()
        handler = self._handler(runtime)
        _bind_resident_session(handler)
        # No sender wired: the capability alone must not deliver.

        runtime.durable_listener("thread-known", "turn-detached", "body")
        await self._drain(5)

        journal = handler._async_completion_journal
        assert journal is not None
        records = journal.list_records()
        assert len(records) == 1
        assert records[0].state == "queued"
        assert records[0].conversation_route_id is None
        assert records[0].noticed_at is not None

    async def test_send_failure_ends_terminal_with_owner_notice(self) -> None:
        runtime = _DurableCapableRuntime()
        handler = self._handler(runtime)
        spool: list[str] = []

        async def sender(user_id, chat_id, text):
            return False

        handler.set_async_completion_sender(sender)
        handler._write_owner_notice_spool = (  # type: ignore[method-assign]
            lambda event, message, dedup: spool.append(dedup)
        )
        _bind_resident_session(handler, generation=4)

        runtime.durable_listener("thread-known", "turn-detached", "body")
        # Default backoff is 1s between attempts; poll past the full retry
        # window (3 attempts x 1s backoff).
        journal = handler._async_completion_journal
        assert journal is not None
        for _ in range(140):
            await asyncio.sleep(0.05)
            records = journal.list_records()
            if records and records[0].state == "terminal_failed":
                break

        records = journal.list_records()
        assert len(records) == 1
        assert records[0].state == "terminal_failed"
        assert len(spool) == 1

    async def test_degraded_runtime_ignores_durable_listener_path(self) -> None:
        runtime = _FakeRuntime()
        handler = self._handler(runtime)
        _bind_resident_session(handler)
        # The degraded runtime has no durable seam at all; the slice-1
        # observer still records evidence-only records.

        handler._observe_unowned_codex_completion("thread-known", "turn-detached")

        journal = handler._async_completion_journal
        assert journal is not None
        records = journal.list_records()
        assert len(records) == 1
        assert records[0].conversation_route_id is None


if __name__ == "__main__":
    unittest.main()
