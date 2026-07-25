"""Unit tests for bounded provider-event reads (#346)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from telegram_bot.core.project_chat_event_stream import (
    EventWaitTimeout,
    TimeoutPreservingEventReader,
)


_END = object()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ControlledIterator(AsyncIterator[str]):
    def __init__(self) -> None:
        self.items: asyncio.Queue[str | BaseException | object] = asyncio.Queue()
        self.read_calls = 0
        self.active_reads = 0
        self.max_active_reads = 0
        self.cancelled_reads = 0

    def __aiter__(self) -> ControlledIterator:
        return self

    async def __anext__(self) -> str:
        self.read_calls += 1
        self.active_reads += 1
        self.max_active_reads = max(self.max_active_reads, self.active_reads)
        try:
            try:
                item = await self.items.get()
            except asyncio.CancelledError:
                self.cancelled_reads += 1
                raise
        finally:
            self.active_reads -= 1
        if item is _END:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, str)
        return item


class DelayedCancellationIterator(ControlledIterator):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.order: list[str] = []

    async def __anext__(self) -> str:
        self.read_calls += 1
        self.active_reads += 1
        self.max_active_reads = max(self.max_active_reads, self.active_reads)
        try:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.order.append("pending-cancel-start")
                self.cleanup_started.set()
                await self.cleanup_release.wait()
                self.cancelled_reads += 1
                self.order.append("pending-cancel-done")
                raise
        finally:
            self.active_reads -= 1


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.anyio
async def test_timeout_retains_one_pending_read_across_retries() -> None:
    events = ControlledIterator()
    reader = TimeoutPreservingEventReader(events)

    with pytest.raises(EventWaitTimeout):
        await reader.read(0.001)
    with pytest.raises(EventWaitTimeout):
        await reader.read(0.001)

    assert events.read_calls == 1
    assert events.active_reads == 1
    assert events.max_active_reads == 1
    assert events.cancelled_reads == 0

    await reader.cancel_pending()
    assert events.active_reads == 0
    assert events.cancelled_reads == 1


@pytest.mark.anyio
async def test_late_event_is_returned_once_from_the_retained_read() -> None:
    events = ControlledIterator()
    reader = TimeoutPreservingEventReader(events)

    with pytest.raises(EventWaitTimeout):
        await reader.read(0.001)
    events.items.put_nowait("late")

    assert await reader.read(1.0) == "late"
    assert events.read_calls == 1

    events.items.put_nowait("next")
    assert await reader.read(1.0) == "next"
    assert events.read_calls == 2


@pytest.mark.anyio
async def test_terminal_and_error_results_clear_the_pending_read() -> None:
    events = ControlledIterator()
    reader = TimeoutPreservingEventReader(events)

    events.items.put_nowait(RuntimeError("broken stream"))
    with pytest.raises(RuntimeError, match="broken stream"):
        await reader.read(1.0)

    events.items.put_nowait("recovered")
    assert await reader.read(1.0) == "recovered"

    events.items.put_nowait(_END)
    with pytest.raises(StopAsyncIteration):
        await reader.read(1.0)

    assert events.read_calls == 3
    await reader.cancel_pending()
    assert events.cancelled_reads == 0


@pytest.mark.anyio
async def test_consumer_cancellation_cancels_and_reaps_the_pending_read() -> None:
    events = ControlledIterator()
    reader = TimeoutPreservingEventReader(events)
    consumer = asyncio.create_task(reader.read(60.0))
    await _wait_until(lambda: events.active_reads == 1)

    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert events.active_reads == 0
    assert events.cancelled_reads == 1
    await reader.cancel_pending()
    assert events.cancelled_reads == 1


@pytest.mark.anyio
async def test_repeated_consumer_cancellation_cannot_overtake_pending_drain() -> None:
    events = DelayedCancellationIterator()
    reader = TimeoutPreservingEventReader(events)

    async def consume_and_close() -> None:
        try:
            await reader.read(60.0)
        finally:
            await reader.cancel_pending()
            assert events.active_reads == 0
            events.order.append("aclose")

    consumer = asyncio.create_task(consume_and_close())
    await _wait_until(lambda: events.active_reads == 1)
    consumer.cancel()
    await events.cleanup_started.wait()

    consumer.cancel()
    await asyncio.sleep(0)
    assert "aclose" not in events.order

    events.cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert events.order == [
        "pending-cancel-start",
        "pending-cancel-done",
        "aclose",
    ]
    assert events.active_reads == 0
    assert events.cancelled_reads == 1


@pytest.mark.anyio
async def test_concurrent_reads_are_rejected_without_starting_another_anext() -> None:
    events = ControlledIterator()
    reader = TimeoutPreservingEventReader(events)
    first = asyncio.create_task(reader.read(60.0))
    await _wait_until(lambda: events.active_reads == 1)

    with pytest.raises(RuntimeError, match="concurrent event reads"):
        await reader.read(0.01)
    assert events.read_calls == 1

    await reader.cancel_pending()
    with pytest.raises(asyncio.CancelledError):
        await first


@pytest.mark.anyio
async def test_cancel_pending_is_idempotent_and_reader_has_no_close_authority() -> None:
    events = ControlledIterator()
    reader = TimeoutPreservingEventReader(events)

    with pytest.raises(EventWaitTimeout):
        await reader.read(0.001)
    await reader.cancel_pending()
    await reader.cancel_pending()

    assert events.cancelled_reads == 1
    assert not hasattr(reader, "close")


@pytest.mark.anyio
async def test_bounded_read_requires_a_positive_timeout() -> None:
    reader = TimeoutPreservingEventReader(ControlledIterator())

    with pytest.raises(ValueError, match="must be positive"):
        await reader.read(0)
    with pytest.raises(ValueError, match="must be positive"):
        await reader.read(-1)
