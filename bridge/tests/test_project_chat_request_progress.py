"""Direct tests for the typed ProjectChat request-progress coordinator."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from telegram_bot.core.project_chat_request_progress import (
    RequestProgressCoordinator,
    RequestProgressHandle,
)
from telegram_bot.core.project_chat_process import _finalize_request_progress
from telegram_bot.core.project_chat_types import _PendingRequest
from telegram_bot.core.request_lifecycle import RequestPhase


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class EffectRecorder:
    def __init__(self, *, cleaned: bool = True) -> None:
        self.cleaned = cleaned
        self.order: list[str] = []
        self.duration_args: tuple[str | None, int, bool] | None = None
        self.finish_args: tuple[str, bool] | None = None

    def ledger_create(
        self,
        user_id: int,
        chat_id: int,
        *,
        initial_state: str,
    ) -> str:
        self.order.append(f"ledger-create:{user_id}:{chat_id}:{initial_state}")
        return "task-1"

    async def progress_loop(self, request: _PendingRequest) -> None:
        self.order.append("progress-start")
        await request.future
        self.order.append("progress-stop")

    async def cleanup_heartbeat(self, request: _PendingRequest) -> bool:
        assert request.task_id == "task-1"
        self.order.append("heartbeat-cleanup")
        return self.cleaned

    def append_duration_log(
        self,
        request: _PendingRequest,
        *,
        session_id: str | None,
        duration_ms: int,
        success: bool,
    ) -> None:
        assert request.task_id == "task-1"
        self.order.append("duration")
        self.duration_args = (session_id, duration_ms, success)

    def ledger_finish(
        self,
        request: _PendingRequest,
        state: str,
        *,
        cleanup_done: bool,
    ) -> None:
        assert request.task_id == "task-1"
        self.order.append("ledger-finish")
        self.finish_args = (state, cleanup_done)


def _coordinator(
    recorder: EffectRecorder,
    *,
    progress_loop: Callable[[_PendingRequest], Awaitable[None]] | None = None,
    append_duration_log=None,
    ledger_finish=None,
    reap_timeout_seconds: float = 0.1,
) -> RequestProgressCoordinator:
    return RequestProgressCoordinator(
        ledger_create=recorder.ledger_create,
        progress_loop=progress_loop or recorder.progress_loop,
        cleanup_heartbeat=recorder.cleanup_heartbeat,
        append_duration_log=append_duration_log or recorder.append_duration_log,
        ledger_finish=ledger_finish or recorder.ledger_finish,
        reap_timeout_seconds=reap_timeout_seconds,
    )


def _start(coordinator: RequestProgressCoordinator) -> RequestProgressHandle:
    return coordinator.start(
        user_id=7,
        chat_id=70,
        model="test-model",
        requested_session_id="requested-session",
        typing_callback=None,
        status_callback=None,
        streaming_handler=None,
        usage_mode="autonomous",
    )


@pytest.mark.anyio
async def test_start_and_finalize_preserve_effect_order_and_values() -> None:
    recorder = EffectRecorder(cleaned=False)
    coordinator = _coordinator(recorder)
    handle = _start(coordinator)
    await asyncio.sleep(0)

    await coordinator.finalize(
        handle,
        terminal_outcome=RequestPhase.COMPLETED,
        session_id="resolved-session",
        duration_ms=123,
    )

    assert handle.request.user_id == 7
    assert handle.request.chat_id == 70
    assert handle.request.model == "test-model"
    assert handle.request.requested_session_id == "requested-session"
    assert handle.request.usage_mode == "autonomous"
    assert handle.request.task_id == "task-1"
    assert handle.request.started_at > 0
    assert handle.request.future.done()
    assert handle.task.done()
    assert recorder.order == [
        "ledger-create:7:70:waiting-for-turn",
        "progress-start",
        "progress-stop",
        "heartbeat-cleanup",
        "duration",
        "ledger-finish",
    ]
    assert recorder.duration_args == ("resolved-session", 123, True)
    assert recorder.finish_args == ("completed", False)


@pytest.mark.anyio
async def test_already_completed_future_is_not_set_twice() -> None:
    recorder = EffectRecorder()
    coordinator = _coordinator(recorder)
    handle = _start(coordinator)
    handle.request.future.set_result(None)

    await coordinator.finalize(
        handle,
        terminal_outcome=RequestPhase.FAILED,
        session_id=None,
        duration_ms=-5,
    )

    assert recorder.duration_args == (None, 0, False)
    assert recorder.finish_args == ("failed", True)


@pytest.mark.anyio
async def test_independently_cancelled_progress_task_is_reaped_then_finalized() -> None:
    recorder = EffectRecorder()
    coordinator = _coordinator(recorder)
    handle = _start(coordinator)
    handle.request.future.cancel()

    await coordinator.finalize(
        handle,
        terminal_outcome=RequestPhase.CANCELED,
        session_id="session",
        duration_ms=1,
    )

    assert handle.task.cancelled()
    assert recorder.finish_args == ("canceled", True)


@pytest.mark.anyio
async def test_stalled_progress_task_is_cancelled_and_gathered_before_cleanup() -> None:
    recorder = EffectRecorder()
    cancelled = asyncio.Event()

    async def stalled(_request: _PendingRequest) -> None:
        recorder.order.append("progress-start")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            recorder.order.append("progress-cancel")
            cancelled.set()
            raise

    coordinator = _coordinator(
        recorder,
        progress_loop=stalled,
        reap_timeout_seconds=0.001,
    )
    handle = _start(coordinator)
    await asyncio.sleep(0)

    await coordinator.finalize(
        handle,
        terminal_outcome=RequestPhase.TIMEOUT,
        session_id=None,
        duration_ms=2,
    )

    assert cancelled.is_set()
    assert handle.task.cancelled()
    assert recorder.order.index("progress-cancel") < recorder.order.index("heartbeat-cleanup")
    assert recorder.finish_args == ("timeout", True)


@pytest.mark.anyio
async def test_duplicate_finalize_fails_closed_without_repeating_effects() -> None:
    recorder = EffectRecorder()
    coordinator = _coordinator(recorder)
    handle = _start(coordinator)
    await coordinator.finalize(
        handle,
        terminal_outcome=RequestPhase.INTERRUPTED,
        session_id=None,
        duration_ms=3,
    )
    first_order = list(recorder.order)

    with pytest.raises(
        RuntimeError,
        match="request progress finalization already started",
    ):
        await coordinator.finalize(
            handle,
            terminal_outcome=RequestPhase.INTERRUPTED,
            session_id=None,
            duration_ms=3,
        )

    assert recorder.order == first_order


@pytest.mark.anyio
async def test_process_finalizer_claims_fallback_and_resolves_live_session() -> None:
    recorder = EffectRecorder()
    coordinator = _coordinator(recorder)
    handle = _start(coordinator)
    handle.request.started_at = 10.0

    class Session:
        session_id = "live-session"

    await _finalize_request_progress(
        coordinator=coordinator,
        handle=handle,
        session=Session(),
        requested_session_id="requested-session",
        finished_at=10.125,
    )

    assert handle.request.lifecycle.terminal_outcome is RequestPhase.FAILED
    assert handle.request.lifecycle.terminal_cause == "finalizer-fallback"
    assert recorder.duration_args == ("live-session", 125, False)


@pytest.mark.anyio
async def test_process_finalizer_uses_requested_session_fallback_exactly_once() -> None:
    recorder = EffectRecorder()
    coordinator = _coordinator(recorder)
    handle = _start(coordinator)
    handle.request.started_at = 10.0
    handle.request.lifecycle.try_terminal(
        RequestPhase.INTERRUPTED,
        cause="restart",
    )

    class UnreadableSession:
        @property
        def session_id(self) -> str:
            raise RuntimeError("session id unavailable")

    await _finalize_request_progress(
        coordinator=coordinator,
        handle=handle,
        session=UnreadableSession(),
        requested_session_id="requested-session",
        finished_at=11.0,
    )
    first_order = list(recorder.order)

    await _finalize_request_progress(
        coordinator=coordinator,
        handle=handle,
        session=None,
        requested_session_id="different-session",
        finished_at=12.0,
    )

    assert recorder.duration_args == ("requested-session", 1000, False)
    assert recorder.finish_args == ("interrupted", True)
    assert recorder.order == first_order


@pytest.mark.anyio
async def test_duration_failure_propagates_before_ledger_finish() -> None:
    recorder = EffectRecorder()

    def fail_duration(*_args, **_kwargs) -> None:
        recorder.order.append("duration-failed")
        raise OSError("duration unavailable")

    coordinator = _coordinator(recorder, append_duration_log=fail_duration)
    handle = _start(coordinator)

    with pytest.raises(OSError, match="duration unavailable"):
        await coordinator.finalize(
            handle,
            terminal_outcome=RequestPhase.FAILED,
            session_id=None,
            duration_ms=4,
        )

    assert "heartbeat-cleanup" in recorder.order
    assert "duration-failed" in recorder.order
    assert "ledger-finish" not in recorder.order


@pytest.mark.anyio
async def test_ledger_failure_propagates_after_duration() -> None:
    recorder = EffectRecorder()

    def fail_ledger(*_args, **_kwargs) -> None:
        recorder.order.append("ledger-failed")
        raise OSError("ledger unavailable")

    coordinator = _coordinator(recorder, ledger_finish=fail_ledger)
    handle = _start(coordinator)

    with pytest.raises(OSError, match="ledger unavailable"):
        await coordinator.finalize(
            handle,
            terminal_outcome=RequestPhase.FAILED,
            session_id=None,
            duration_ms=5,
        )

    assert recorder.order.index("duration") < recorder.order.index("ledger-failed")


@pytest.mark.anyio
async def test_caller_cancellation_propagates_and_cancels_progress_task() -> None:
    recorder = EffectRecorder()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def stalled(_request: _PendingRequest) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    coordinator = _coordinator(
        recorder,
        progress_loop=stalled,
        reap_timeout_seconds=60.0,
    )
    handle = _start(coordinator)
    finalizer = asyncio.create_task(
        coordinator.finalize(
            handle,
            terminal_outcome=RequestPhase.CANCELED,
            session_id=None,
            duration_ms=6,
        )
    )
    await started.wait()

    finalizer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await finalizer

    assert cancelled.is_set()
    assert handle.task.cancelled()
    assert "heartbeat-cleanup" not in recorder.order


def test_nonpositive_reap_timeout_is_rejected() -> None:
    recorder = EffectRecorder()
    with pytest.raises(ValueError, match="must be positive"):
        _coordinator(recorder, reap_timeout_seconds=0)
