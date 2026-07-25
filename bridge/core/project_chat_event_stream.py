"""Typed bounded reads for provider-neutral project-chat event streams (#346)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Generic, TypeVar


EventT = TypeVar("EventT")


class EventWaitTimeout(TimeoutError):
    """A bounded event read expired without cancelling the provider read."""


class TimeoutPreservingEventReader(Generic[EventT]):
    """Own at most one bounded ``__anext__`` task for a single consumer.

    ``asyncio.wait_for`` cancels the awaited operation on timeout. Provider
    turns need the opposite order: interrupt the provider owner first, then
    cancel its pending event read. This reader raises :class:`EventWaitTimeout`
    while retaining the exact task so the caller can enforce that ordering.

    Unbounded reads and iterator closure deliberately stay with the caller.
    Concurrent calls to :meth:`read` are unsupported and fail explicitly.
    """

    __slots__ = ("_events", "_pending", "_waiting")

    def __init__(self, events: AsyncIterator[EventT]) -> None:
        self._events = events
        self._pending: asyncio.Task[EventT] | None = None
        self._waiting = False

    async def _read_next(self) -> EventT:
        return await self._events.__anext__()

    async def read(self, timeout_seconds: float) -> EventT:
        """Read one event without cancelling it when the timeout expires."""

        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self._waiting:
            raise RuntimeError("concurrent event reads are not supported")

        self._waiting = True
        pending = self._pending
        if pending is None:
            pending = asyncio.create_task(self._read_next())
            self._pending = pending
        try:
            try:
                done, _ = await asyncio.wait((pending,), timeout=timeout_seconds)
            except asyncio.CancelledError:
                await self.cancel_pending()
                raise
            if pending not in done:
                raise EventWaitTimeout
            self._pending = None
            return pending.result()
        finally:
            self._waiting = False

    async def cancel_pending(self) -> None:
        """Cancel and reap the retained read, if any."""

        pending = self._pending
        if pending is None:
            return
        pending.cancel()
        interrupted = await self._drain_pending(pending)
        if self._pending is pending:
            self._pending = None
        if interrupted:
            raise asyncio.CancelledError

    @staticmethod
    async def _drain_pending(pending: asyncio.Task[EventT]) -> bool:
        """Finish reaping even if the caller is cancelled more than once."""

        current = asyncio.current_task()
        cancellation_count = current.cancelling() if current is not None else 0
        interrupted = False
        while True:
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                new_count = current.cancelling() if current is not None else 0
                if new_count > cancellation_count:
                    interrupted = True
                    cancellation_count = new_count
                if pending.done():
                    break
            except BaseException:
                break
            else:
                break
        return interrupted


__all__ = ["EventWaitTimeout", "TimeoutPreservingEventReader"]
