"""Hermetic signal/drain contract tests for bridge service restarts."""

from __future__ import annotations

import asyncio
import signal

import pytest

from telegram_bot.core.bot_lifecycle import BotLifecycleMixin
from telegram_bot.core.task_queue import UserTaskQueue


class _ProjectWorkload:
    def __init__(self, count: int) -> None:
        self.count = count
        self.draining = False

    def begin_drain(self) -> bool:
        first = not self.draining
        self.draining = True
        return first

    def workload_snapshot(self, now: float) -> tuple[int, float]:
        return self.count, 12.0 if self.count else 0.0


class _DrainSubject(BotLifecycleMixin):
    pass


def _subject(count: int, *, window: float = 0.2) -> _DrainSubject:
    subject = _DrainSubject()
    subject._project_chat = _ProjectWorkload(count)
    subject._tasks = UserTaskQueue()
    subject._SHUTDOWN_DRAIN_SECONDS = window
    subject._SHUTDOWN_DRAIN_POLL_SECONDS = 0.001
    subject._shutdown_drain_task = None
    return subject


@pytest.mark.anyio
async def test_busy_signal_drains_running_work_before_stop() -> None:
    subject = _subject(1)
    stop_event = asyncio.Event()

    subject._request_shutdown_drain(stop_event, signal.SIGTERM)
    await asyncio.sleep(0.01)
    assert subject._project_chat.draining is True
    assert stop_event.is_set() is False

    subject._project_chat.count = 0
    await asyncio.wait_for(stop_event.wait(), timeout=0.2)


@pytest.mark.anyio
async def test_signal_preserves_accepted_background_task_until_it_finishes() -> None:
    subject = _subject(0)
    stop_event = asyncio.Event()
    started = asyncio.Event()
    release = asyncio.Event()

    async def tracked_work() -> None:
        started.set()
        await release.wait()

    async def overflow() -> None:
        raise AssertionError("unexpected overflow")

    assert await subject._tasks.enqueue("chat", tracked_work, overflow) is True
    await started.wait()
    subject._request_shutdown_drain(stop_event, signal.SIGTERM)
    await asyncio.sleep(0.01)
    assert stop_event.is_set() is False

    release.set()
    await asyncio.wait_for(stop_event.wait(), timeout=0.2)


@pytest.mark.anyio
async def test_idle_signal_stops_without_waiting_for_window() -> None:
    subject = _subject(0, window=60.0)
    stop_event = asyncio.Event()

    subject._request_shutdown_drain(stop_event, signal.SIGTERM)
    await asyncio.wait_for(stop_event.wait(), timeout=0.1)


@pytest.mark.anyio
async def test_busy_signal_forces_teardown_at_timeout_boundary() -> None:
    subject = _subject(1, window=0.02)
    stop_event = asyncio.Event()
    started = asyncio.get_running_loop().time()

    subject._request_shutdown_drain(stop_event, signal.SIGTERM)
    await asyncio.wait_for(stop_event.wait(), timeout=0.2)

    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed >= 0.015
    assert elapsed < 0.15


@pytest.mark.anyio
async def test_repeated_signal_is_explicit_force_during_busy_drain() -> None:
    subject = _subject(1, window=60.0)
    stop_event = asyncio.Event()

    subject._request_shutdown_drain(stop_event, signal.SIGTERM)
    await asyncio.sleep(0)
    assert stop_event.is_set() is False
    subject._request_shutdown_drain(stop_event, signal.SIGTERM)

    assert stop_event.is_set() is True
    assert subject._project_chat.count == 1
