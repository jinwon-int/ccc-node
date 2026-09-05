"""Regression tests for #1479 (bug items 1 and 2).

1. Fire-and-forget ``asyncio.create_task`` calls must keep a strong reference
   to the spawned task (the loop only holds weak references, so an untracked
   task may be garbage-collected mid-flight). Each spawner keeps a
   per-instance set that the task leaves once it is done.
2. ``ClaudeRuntime._session_locks`` must not grow forever: forgetting a
   session prunes its lock unless the lock is still held or shared with
   another live session of the same id.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from telegram_bot.core.async_completion_event import NormalizedAsyncCompletionEvent
from telegram_bot.core.async_completion_journal import AsyncCompletionJournal
from telegram_bot.core.claude_runtime import ClaudeRuntime, ClaudeSession
from telegram_bot.core.crush_runtime import CrushEvent, CrushRuntime
from telegram_bot.core.external_wait import ExternalWaitRegistry
from telegram_bot.core.project_chat import ProjectChatHandler
from telegram_bot.core.webhook_nudge import WebhookNudgeServer


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was not reached in time")
        await asyncio.sleep(0.001)


# -- crush: permission handler tasks -----------------------------------------


class _GatedCrushClient:
    """Only the surface ``_handle_permission`` touches; ``permission_reply`` blocks."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.replies: list[tuple[str, str]] = []
        self.fail = False

    async def permission_reply(self, workspace_id: str, payload: Any, decision: str) -> None:
        self.started.set()
        await self.gate.wait()
        if self.fail:
            raise RuntimeError("reply transport down")
        self.replies.append((workspace_id, decision))

    async def close(self) -> None:
        return None


def _permission_event(request_id: str = "perm-1") -> CrushEvent:
    # No active turn for this session id: _decide_permission fails closed to
    # DENY without waiting, so the task's lifetime is bounded by the client.
    return CrushEvent(
        kind="permission_request",
        change="created",
        workspace_id="ws-1",
        session_id="sess-1",
        payload={"id": request_id, "action": "bash", "description": "ls"},
    )


@pytest.mark.anyio
async def test_crush_permission_task_is_tracked_until_done() -> None:
    client = _GatedCrushClient()
    runtime = CrushRuntime(client_factory=lambda: client)

    runtime._spawn_permission(_permission_event())
    await asyncio.wait_for(client.started.wait(), timeout=2.0)
    assert len(runtime._permission_tasks) == 1
    (task,) = runtime._permission_tasks
    assert not task.done()

    client.gate.set()
    await asyncio.wait_for(task, timeout=2.0)
    await _wait_until(lambda: not runtime._permission_tasks)
    assert client.replies == [("ws-1", "deny")]


@pytest.mark.anyio
async def test_crush_permission_reply_failure_is_logged_with_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _GatedCrushClient()
    client.fail = True
    client.gate.set()
    runtime = CrushRuntime(client_factory=lambda: client)

    with caplog.at_level(logging.WARNING, logger="telegram_bot.core.crush_runtime"):
        runtime._spawn_permission(_permission_event("perm-42"))
        await _wait_until(lambda: not runtime._permission_tasks)

    messages = [record.getMessage() for record in caplog.records]
    assert any("perm-42" in message for message in messages), messages


@pytest.mark.anyio
async def test_crush_close_cancels_outstanding_permission_tasks() -> None:
    client = _GatedCrushClient()
    runtime = CrushRuntime(client_factory=lambda: client)

    runtime._spawn_permission(_permission_event())
    await asyncio.wait_for(client.started.wait(), timeout=2.0)
    (task,) = runtime._permission_tasks

    await runtime.close()
    assert task.cancelled()
    assert not runtime._permission_tasks


# -- webhook nudge: per-connection tasks --------------------------------------


@pytest.mark.anyio
async def test_webhook_nudge_connection_task_is_tracked_until_done(tmp_path: Path) -> None:
    registry = ExternalWaitRegistry(tmp_path / "registry.json")
    server = WebhookNudgeServer(registry, secret="test-secret", port=0)
    gate = asyncio.Event()
    started = asyncio.Event()
    served: list[socket.socket] = []

    async def gated_serve(conn: socket.socket) -> None:
        served.append(conn)
        started.set()
        await gate.wait()

    server._serve_accepted = gated_serve
    left, right = socket.socketpair()
    try:
        server._spawn_serve(right)
        await asyncio.wait_for(started.wait(), timeout=2.0)
        assert len(server._connection_tasks) == 1
        (task,) = server._connection_tasks
        assert not task.done()

        gate.set()
        await asyncio.wait_for(task, timeout=2.0)
        await _wait_until(lambda: not server._connection_tasks)
        assert served == [right]
    finally:
        left.close()
        right.close()


@pytest.mark.anyio
async def test_webhook_nudge_close_cancels_outstanding_connection_tasks(
    tmp_path: Path,
) -> None:
    registry = ExternalWaitRegistry(tmp_path / "registry.json")
    server = WebhookNudgeServer(registry, secret="test-secret", port=0)
    started = asyncio.Event()

    async def hanging_serve(conn: socket.socket) -> None:
        started.set()
        await asyncio.Event().wait()

    server._serve_accepted = hanging_serve
    left, right = socket.socketpair()
    try:
        server._spawn_serve(right)
        await asyncio.wait_for(started.wait(), timeout=2.0)
        (task,) = server._connection_tasks

        await server.close()
        assert task.cancelled()
        assert not server._connection_tasks
    finally:
        left.close()
        right.close()


# -- project chat: durable completion delivery tasks --------------------------


def _chat_settings(tmp_path: Path) -> SimpleNamespace:
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
        task_ledger_path=None,
        session_guard_enabled=False,
    )


class _GatedCoordinator:
    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.calls: list[str] = []

    async def deliver(self, idempotency_key: str, **_: Any) -> bool:
        self.calls.append(idempotency_key)
        self.started.set()
        await self.gate.wait()
        return True


@pytest.mark.anyio
async def test_durable_codex_delivery_task_is_tracked_until_done(tmp_path: Path) -> None:
    handler = ProjectChatHandler(
        settings=_chat_settings(tmp_path), agent_runtime=SimpleNamespace()
    )
    journal = AsyncCompletionJournal(tmp_path / "journal")
    journal.initialize()
    coordinator = _GatedCoordinator()
    handler._init_async_completion_journal = lambda: journal
    handler._delivery_for = lambda _journal: coordinator
    handler._agent_session_registry = SimpleNamespace(
        find_route_by_session_id=lambda _thread_id: (7, 7),
        generation_high_water=lambda _key: 1,
    )

    handler._observe_durable_codex_completion("thread-1", "turn-1", "done")
    await asyncio.wait_for(coordinator.started.wait(), timeout=2.0)
    assert len(handler._async_completion_tasks) == 1
    (task,) = handler._async_completion_tasks
    assert not task.done()

    coordinator.gate.set()
    await asyncio.wait_for(task, timeout=2.0)
    await _wait_until(lambda: not handler._async_completion_tasks)
    expected_key = NormalizedAsyncCompletionEvent(
        provider="codex",
        thread_id="thread-1",
        conversation_route_id="7",
        session_generation=1,
        turn_id="turn-1",
    ).idempotency_key
    assert coordinator.calls == [expected_key]


# -- claude runtime: session lock pruning --------------------------------------


def _claude_runtime() -> ClaudeRuntime:
    def factory(_options: Any) -> Any:
        raise AssertionError("SDK client factory must not be called")

    return ClaudeRuntime(sdk_client_factory=factory)


@pytest.mark.anyio
async def test_forget_session_prunes_unheld_session_lock() -> None:
    runtime = _claude_runtime()
    session = ClaudeSession(runtime, "sid-1")
    session._turn_lock = runtime._session_lock("sid-1")
    runtime._sessions.append(session)
    assert "sid-1" in runtime._session_locks

    runtime._forget_session(session)

    assert "sid-1" not in runtime._session_locks
    assert session not in runtime._sessions


@pytest.mark.anyio
async def test_forget_session_keeps_held_lock_until_next_forget() -> None:
    runtime = _claude_runtime()
    session = ClaudeSession(runtime, "sid-1")
    lock = runtime._session_lock("sid-1")
    session._turn_lock = lock
    runtime._sessions.append(session)

    async with lock:
        runtime._forget_session(session)
        assert runtime._session_locks.get("sid-1") is lock

    # Once released, the next forget of any session with that id prunes it.
    later = ClaudeSession(runtime, "sid-1")
    runtime._forget_session(later)
    assert "sid-1" not in runtime._session_locks


@pytest.mark.anyio
async def test_forget_session_keeps_lock_shared_with_live_session() -> None:
    runtime = _claude_runtime()
    owner = ClaudeSession(runtime, "sid-1")
    waiter = ClaudeSession(runtime, "sid-1")
    lock = runtime._session_lock("sid-1")
    owner._turn_lock = waiter._turn_lock = lock
    runtime._sessions.extend([owner, waiter])

    runtime._forget_session(owner)
    assert runtime._session_locks.get("sid-1") is lock

    runtime._forget_session(waiter)
    assert "sid-1" not in runtime._session_locks


@pytest.mark.anyio
async def test_session_lock_map_does_not_grow_across_new_sessions() -> None:
    runtime = _claude_runtime()
    for index in range(25):
        session = ClaudeSession(runtime, f"sid-{index}")
        session._turn_lock = runtime._session_lock(f"sid-{index}")
        runtime._sessions.append(session)
        runtime._forget_session(session)
    assert runtime._session_locks == {}
