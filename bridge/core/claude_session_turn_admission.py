"""Admit and abort one Claude session turn."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, cast

from .agent_runtime import (
    AgentEvent,
    ApprovalHandler,
    CompletionEvent,
    ErrorEvent,
    deny_approval,
)

if TYPE_CHECKING:
    from .claude_runtime import ClaudeRuntime, ClaudeSession, SdkClient, _ActiveTurn


class ClaudeSessionTurnAdmissionMixin:
    """Own send/interrupt/abort for one Claude session turn."""

    _active_turn: _ActiveTurn | None
    _client: SdkClient | None
    _closed: bool
    _runtime: ClaudeRuntime
    _turn_generation: int
    _turn_lock: asyncio.Lock | None
    _unsolicited_discard: bool
    _begin_close: Callable[[], asyncio.Task[None] | None]

    if TYPE_CHECKING:
        @property
        def session_id(self) -> str: ...

    def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        async def events() -> AsyncIterator[AgentEvent]:
            # Late import: ``_ActiveTurn`` lives on the composed runtime module
            # and importing it at load time would cycle through this mixin.
            from .claude_runtime import _ActiveTurn

            client = self._client
            lock = self._turn_lock
            if client is None or lock is None:
                raise RuntimeError("Claude session is not started")
            async with lock:
                # A resumed waiter can capture its client before blocking on a
                # shared lock. Re-check after admission so _begin_close() can
                # seal it synchronously before the old owner releases (#625).
                if self._closed:
                    raise RuntimeError("Claude session is closed")
                self._runtime._turn_owners[self.session_id] = cast(
                    "ClaudeSession", self
                )
                self._turn_generation += 1
                active = _ActiveTurn(
                    asyncio.Queue(),
                    approval_handler,
                    self._turn_generation,
                )
                self._active_turn = active
                queried = False
                try:
                    await client.query(message)
                    queried = True
                    while True:
                        event = await active.queue.get()
                        yield event
                        if isinstance(event, (CompletionEvent, ErrorEvent)):
                            return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    yield ErrorEvent(
                        code="claude_runtime_error",
                        message=str(exc) or "Claude runtime request failed",
                    )
                finally:
                    if queried and not active.finished:
                        # Abandoned before its terminal frame (stall release,
                        # timeout, cancellation) while the provider turn may
                        # still be running: its late frames must be swallowed
                        # by the between-turns listener, not re-delivered as
                        # an unsolicited message.
                        self._unsolicited_discard = True
                    active.finished = True
                    if self._active_turn is active:
                        self._active_turn = None
                    if self._runtime._turn_owners.get(self.session_id) is self:
                        self._runtime._turn_owners.pop(self.session_id, None)

        return events()

    async def interrupt(self) -> None:
        owner = self._runtime._turn_owners.get(self.session_id)
        if owner is not None and owner is not self:
            await owner.interrupt()
            return
        active = self._active_turn
        client = self._client
        if active is None or active.finished or client is None:
            return
        active.interrupt_requested = True
        await client.interrupt()

    async def abort_stalled_turn(self) -> None:
        """Close the real lock owner and rotate its poisoned admission lock.

        A second session resumed with the same id shares ``_turn_lock``. If
        the first session lost its terminal frame, interrupting the waiter
        alone cannot release that lock and every recreated session would join
        the same dead queue. Closing the recorded owner terminates its reader;
        rotating only that session id lets the next clean session proceed
        while the abandoned generator unwinds on the old lock (#625).
        """

        session_id = self.session_id
        owner = self._runtime._turn_owners.get(session_id) or self
        owner_lock = owner._turn_lock
        # Preserve #625's waiter-before-owner safety without making the
        # waiter's full graceful disconnect a prerequisite for owner cleanup.
        # _begin_close() has no await: the waiter is sealed before the owner
        # can release the old lock, and send_turn re-checks that seal after
        # admission. The SDK cleanup tasks may then progress concurrently.
        close_tasks: list[asyncio.Task[None]] = []
        waiter_close = self._begin_close()
        if waiter_close is not None:
            close_tasks.append(waiter_close)
        if owner is not self:
            owner_close = owner._begin_close()
            if owner_close is not None:
                close_tasks.append(owner_close)
        try:
            await asyncio.gather(*(asyncio.shield(task) for task in close_tasks))
        finally:
            # The project-chat abort guard is deliberately shorter than the
            # SDK's worst-case graceful-close window. Rotate ownership even if
            # that guard expires; each close's shielded cleanup task keeps
            # running and remains joinable by a later close().
            if self._runtime._session_locks.get(session_id) is owner_lock:
                self._runtime._session_locks[session_id] = asyncio.Lock()
            if self._runtime._turn_owners.get(session_id) is owner:
                self._runtime._turn_owners.pop(session_id, None)
