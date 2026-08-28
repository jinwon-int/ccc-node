"""Environment conformance for the #646 async-completion flow.

The issue requires fake-app-server tests under root Linux, non-root Linux,
and Termux. The async-completion contract is deliberately environment-
independent — the journal is project-root scoped with owner-only file
semantics, the sender is an injected seam, and no code path branches on
privilege or platform — so this suite pins the identical observable outcome
under all three profiles, driving the real ``CodexRuntime`` notification
path (scripted fake app-server client) through handler, journal, and the
delivery/reclaim seams.

These are conformance tests, not unit tests: each scenario asserts the full
observable contract (exactly-once send, journal state, body-free durable
storage, owner-only file mode) rather than one component's behavior.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import os
import stat
import tempfile
from typing import Any, Mapping
import unittest
from unittest.mock import patch

from test_async_completion_wiring import _settings
from test_codex_runtime import FakeClient

from telegram_bot.core import project_chat as project_chat_module
from telegram_bot.core.codex_app_server import CodexNotification
from telegram_bot.core.codex_runtime import CodexRuntime
from telegram_bot.core.project_chat import ProjectChatHandler
from telegram_bot.core.usage_meter import MODE_AUTONOMOUS, MODE_INTERACTIVE

# (name, running_as_root, termux_env)
_PROFILES: tuple[tuple[str, bool, Mapping[str, str]], ...] = (
    ("root-linux", True, {}),
    ("non-root-linux", False, {}),
    ("termux", False, {"TERMUX_VERSION": "0.118", "ANDROID_API_LEVEL": "34"}),
)


class _DurableCapableRuntime(CodexRuntime):
    """Real CodexRuntime with the capability declaration flipped (#646).

    The production adapter stays degraded (``detached_ownership_unavailable``);
    this subclass exists to exercise the promotion path a runtime with a
    negotiated durable-delivery contract would take.
    """

    @staticmethod
    def async_completion_capability() -> Any:
        from telegram_bot.core.agent_runtime import AsyncCompletionCapability

        return AsyncCompletionCapability(
            provider="codex",
            state="supported",
            protocol_version="1",
            notification_method="turn/completed",
            recovery_method="thread/read",
            ownership_scope="detached_task",
            supports_durable_delivery=True,
            reason_code="conformance_contract",
        )


class AsyncCompletionEnvironmentConformanceTests(unittest.IsolatedAsyncioTestCase):
    """The #646 flow behaves identically under root/non-root/Termux."""

    def setUp(self) -> None:
        self.sent: list[tuple[int, int, str]] = []

        async def sender(user_id: int, chat_id: int, text: str) -> bool:
            self.sent.append((user_id, chat_id, text))
            return True

        self.sender = sender

    def _build_runtime(self) -> _DurableCapableRuntime:
        """Fresh runtime per scenario: tombstones and turns must not leak."""

        def factory(handler) -> FakeClient:
            client = FakeClient(handler)
            client.thread_start_result = {"thread": {"id": "thread-known"}}
            return client

        return _DurableCapableRuntime(client_factory=factory)

    def _bind_resident_session(self, handler: ProjectChatHandler) -> None:
        from telegram_bot.core.project_chat_types import AgentSessionEntry

        class _Session:
            session_id = "thread-known"

        handler._agent_session_registry.put_cached(
            (7, 42), AgentSessionEntry(session=_Session())
        )
        handler._agent_session_registry._generation_high_water[(7, 42)] = 4

    def _completed_notification(self, body: str) -> CodexNotification:
        return CodexNotification(
            "turn/completed",
            {
                "threadId": "thread-known",
                "turn": {
                    "id": "turn-detached",
                    "status": "completed",
                    "itemsView": "full",
                    "items": [
                        {"id": "m1", "type": "agentMessage", "text": body}
                    ],
                },
            },
        )

    async def _drain(self, ticks: int = 30) -> None:
        for _ in range(ticks):
            await asyncio.sleep(0)

    async def test_delivery_flow_is_identical_across_environments(self) -> None:

        for name, is_root, termux_env in _PROFILES:
            with self.subTest(profile=name):
                self.sent.clear()
                runtime = self._build_runtime()
                try:
                    # Each profile is its own deployment: a fresh project root
                    # so journal identities never collide across profiles.
                    with tempfile.TemporaryDirectory() as profile_root:
                        await self._delivery_scenario(
                            name,
                            is_root,
                            termux_env,
                            runtime,
                            Path(profile_root),
                        )
                finally:
                    await runtime.close()

    async def _delivery_scenario(
        self,
        name: str,
        is_root: bool,
        termux_env: Mapping[str, str],
        runtime: _DurableCapableRuntime,
        profile_root: Path,
    ) -> None:
        from telegram_bot.core.agent_runtime import SessionRequest
        from telegram_bot.core.project_chat import ProjectChatHandler

        with (
            patch.object(
                project_chat_module, "running_as_root", return_value=is_root
            ),
            patch.dict(os.environ, dict(termux_env)),
        ):
            handler = ProjectChatHandler(
                settings=_settings(profile_root), agent_runtime=runtime
            )
        handler.set_async_completion_sender(self.sender)
        self._bind_resident_session(handler)

        # The fake app-server owns the thread only after the bridge resumed
        # it — the real admission path.
        await runtime.start_or_resume(
            SessionRequest(
                working_directory=str(profile_root), session_id="thread-known"
            )
        )

        # Real notification path: the fake app-server reports an unowned
        # completed turn on a live thread.
        runtime._route_notification(self._completed_notification(f"body-{name}"))
        await self._drain()

        # Exactly one conversation delivery with the bounded body.
        self.assertEqual(len(self.sent), 1, name)
        self.assertEqual(self.sent[0][0], 7, name)
        self.assertEqual(self.sent[0][1], 42, name)
        self.assertIn(f"body-{name}", self.sent[0][2], name)

        # Journal: exactly one delivered, route-bound record.
        journal = handler._async_completion_journal
        assert journal is not None
        records = journal.list_records()
        self.assertEqual(len(records), 1, name)
        self.assertEqual(records[0].state, "delivered", name)
        self.assertEqual(records[0].conversation_route_id, "7:42", name)

        # Durable storage never carries raw provider identifiers or bodies.
        record_id = journal.record_id_for(records[0].idempotency_key)
        payload = (journal.root / f"{record_id}.json").read_text(encoding="utf-8")
        self.assertNotIn("thread-known", payload, name)
        self.assertNotIn("turn-detached", payload, name)
        self.assertNotIn(f"body-{name}", payload, name)

        # Owner-only file semantics hold in every environment.
        mode = stat.S_IMODE((journal.root / f"{record_id}.json").stat().st_mode)
        self.assertEqual(mode, 0o600, name)

        # A duplicate notification cannot double-send.
        runtime._route_notification(self._completed_notification(f"body-{name}"))
        await self._drain()
        self.assertEqual(len(self.sent), 1, name)

    async def test_restart_reclaim_is_identical_across_environments(self) -> None:
        for name, is_root, termux_env in _PROFILES:
            with self.subTest(profile=name):
                self.sent.clear()
                runtime = self._build_runtime()
                try:
                    with tempfile.TemporaryDirectory() as profile_root:
                        await self._reclaim_scenario(
                            name,
                            is_root,
                            termux_env,
                            runtime,
                            Path(profile_root),
                        )
                finally:
                    await runtime.close()

    async def _reclaim_scenario(
        self,
        name: str,
        is_root: bool,
        termux_env: Mapping[str, str],
        runtime: _DurableCapableRuntime,
        profile_root: Path,
    ) -> None:
        from telegram_bot.core.async_completion_event import (
            NormalizedAsyncCompletionEvent,
        )
        from telegram_bot.core.project_chat import ProjectChatHandler

        with (
            patch.object(
                project_chat_module, "running_as_root", return_value=is_root
            ),
            patch.dict(os.environ, dict(termux_env)),
        ):
            handler = ProjectChatHandler(
                settings=_settings(profile_root), agent_runtime=runtime
            )
        handler.set_async_completion_sender(self.sender)
        journal = handler._async_completion_journal
        assert journal is not None
        journal.initialize()

        # Pre-restart state: one route-bound queued record, exactly as a
        # previous process under the declaring capability left it.
        event = NormalizedAsyncCompletionEvent(
            provider="codex",
            thread_id="thread-lost",
            conversation_route_id="7:42",
            session_generation=4,
            turn_id="turn-lost",
        )
        self.assertTrue(journal.observe(event, deliverable=True))

        # Autonomous turns never consume the user's reclaim window.
        await handler._maybe_reclaim_async_completions(7, 42, MODE_AUTONOMOUS)
        self.assertEqual(self.sent, [], name)

        # The next interactive user turn reclaims body-free, once.
        await handler._maybe_reclaim_async_completions(7, 42, MODE_INTERACTIVE)
        self.assertEqual(len(self.sent), 1, name)
        self.assertIn("undelivered background completion", self.sent[0][2], name)
        record = journal.get(event.idempotency_key)
        assert record is not None
        self.assertEqual(record.state, "delivered", name)
        self.assertEqual(record.last_error_code, "body_free_reclaim", name)

        # The lost thread id never reached durable storage.
        record_id = journal.record_id_for(event.idempotency_key)
        payload = (journal.root / f"{record_id}.json").read_text(encoding="utf-8")
        self.assertNotIn("thread-lost", payload, name)
        self.assertNotIn("turn-lost", payload, name)


if __name__ == "__main__":
    unittest.main()
