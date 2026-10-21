"""Route Claude SDK frames for active and between-turn session work."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import os
from typing import TYPE_CHECKING

from claude_agent_sdk import (
    AssistantMessage,
    Message,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    UserMessage,
)

from .agent_runtime import DelegatedTaskLifecycleEvent

if TYPE_CHECKING:
    from .claude_runtime import SdkClient, _ActiveTurn


# Preserve the established body-free trace channel after this pure move.
logger = logging.getLogger("telegram_bot.core.claude_runtime")

# How long a ResultMessage that closed an autonomous turn is held when a user
# turn was submitted during that turn (#2440). Claude Code queues a prompt
# that arrives mid-turn *inside the CLI* and merges it into the running turn
# ("The user sent a new message while you were working"), so one terminal
# ResultMessage ends both. The bridge cannot tell that merged result from an
# autonomous result that a separate user turn will follow, so it waits this
# long for the user turn's own first frame: a frame arrives → two-result
# shape, the held result is delivered on the autonomous route as before; the
# grace expires with no frame → the CLI coalesced them, the held result
# completes the user turn. Either way the user turn can no longer wait
# forever for a frame that never comes.
MERGED_TURN_GRACE_ENV = "CCC_CLAUDE_MERGED_TURN_GRACE_SECONDS"
DEFAULT_MERGED_TURN_GRACE_SECONDS = 20.0
MIN_MERGED_TURN_GRACE_SECONDS = 1.0
MAX_MERGED_TURN_GRACE_SECONDS = 300.0


def merged_turn_grace_seconds(
    environ: "os._Environ[str] | dict[str, str] | None" = None,
) -> float:
    """Resolve the merged-turn grace from the environment, clamped and fail-safe."""

    source = os.environ if environ is None else environ
    raw = (source.get(MERGED_TURN_GRACE_ENV) or "").strip()
    if not raw:
        return DEFAULT_MERGED_TURN_GRACE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MERGED_TURN_GRACE_SECONDS
    if value != value:  # NaN
        return DEFAULT_MERGED_TURN_GRACE_SECONDS
    return min(MAX_MERGED_TURN_GRACE_SECONDS, max(MIN_MERGED_TURN_GRACE_SECONDS, value))


@dataclass(slots=True)
class _HeldResult:
    """A ResultMessage parked while the bridge learns whether it is shared."""

    message: ResultMessage
    active: _ActiveTurn
    raw: str
    timer: asyncio.Task[None] | None = None


class ClaudeSessionFrameRoutingMixin:
    """Own the Claude SDK reader and route frames by turn ownership."""

    _active_turn: _ActiveTurn | None
    _closed: bool
    _reader_task: asyncio.Task[None] | None
    _session_id: str | None
    _session_ready: asyncio.Event
    _unsolicited_discard: bool
    _unsolicited_handler: Callable[[str, str | None], Awaitable[None]] | None
    _unsolicited_inflight: bool
    _unsolicited_started_at: float | None
    _unsolicited_texts: list[str]
    _unsolicited_lifecycle: Callable[[str, str | None], Awaitable[None]] | None
    _unsolicited_route_open: bool
    _held_result: _HeldResult | None
    _merged_turn_grace_seconds: float
    _observe_sdk_frame: Callable[[Message], None]
    _observe_background_task_notifications: Callable[[Message], None]
    _observe_result_deferring_task: Callable[
        [_ActiveTurn, Message], DelegatedTaskLifecycleEvent | None
    ]
    _route_stream_event: Callable[[_ActiveTurn, StreamEvent], None]
    _route_assistant_message: Callable[[_ActiveTurn, AssistantMessage], None]
    _route_tool_results: Callable[[_ActiveTurn, UserMessage], None]
    _complete_turn: Callable[[_ActiveTurn, ResultMessage], None]
    _fail_active_turn: Callable[[str, str], None]

    def provider_reader_alive(self) -> bool:
        """Return True while this session can still receive provider frames.

        A closed session, or one whose reader task has ended (stream end,
        transport or parse failure), will never see the terminal frame that
        clears its autonomous-turn or background-task state. The session guard
        must not treat such a session as busy forever (#2291).
        """

        reader = self._reader_task
        return not self._closed and reader is not None and not reader.done()

    def autonomous_turn_inflight(self) -> bool:
        """Return True while the CLI runs a turn the bridge did not submit.

        After a run-in-background task finishes, Claude Code injects its
        ``<task-notification>`` and continues on its own. The registry only
        marks bridge-submitted (``send_turn``) turns active, so without this
        seam the session resource guard treats such a session as idle and
        closing it SIGKILLs the running tool (#2291). The discard window of
        an abandoned ``send_turn`` counts too: the CLI is still producing
        that turn's frames. Body-free and read-only.
        """

        if not self.provider_reader_alive():
            return False
        return bool(self._unsolicited_inflight or self._unsolicited_discard)

    def autonomous_workload_snapshot(self, now: float) -> tuple[int, float]:
        """Return ``(count, age_seconds)`` for the autonomous turn window.

        The restart-gating counterpart of :meth:`autonomous_turn_inflight`
        (#2305): the registry-based workload snapshot only counts
        bridge-submitted turns, so without this a task-notification
        continuation published ``turn_occupancy: idle`` while the CLI was
        still running tools. ``now`` must come from the event-loop clock,
        like every other workload snapshot. The age is 0.0 when the window
        has no recorded start (an abandoned ``send_turn``'s discard window).
        Body-free and read-only.
        """

        if not self.autonomous_turn_inflight():
            return 0, 0.0
        started = self._unsolicited_started_at
        if started is None:
            return 1, 0.0
        return 1, max(0.0, float(now) - started)

    async def _read_frames(self, client: SdkClient) -> None:
        stream_failure: str | None = None
        try:
            async for message in client.receive_messages():
                try:
                    await self._route_message(message)
                except (TypeError, ValueError):
                    # One malformed frame must not take the connection down.
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive transport guard
            stream_failure = str(exc) or "Claude connection failed"
        finally:
            # A held result can never be resolved by a frame once the reader
            # is gone. Stop its timer before the active turn is failed so the
            # turn sees exactly one terminal event, and surface the parked
            # answer on the autonomous route rather than losing it with the
            # stream (a sealed session has already dropped it in _begin_close).
            held = self._take_held_result()
            if not self._closed:
                self._fail_active_turn(
                    "claude_connection_failed",
                    stream_failure or "Claude connection closed",
                )
                if held is not None:
                    await self._deliver_unsolicited(held.raw, held.message.session_id)
            # A closed stream can never announce an id; unblock _start.
            self._session_ready.set()

    async def _route_message(self, message: Message) -> None:
        self._observe_sdk_frame(message)
        self._observe_session_id(message)
        self._observe_background_task_notifications(message)
        if self._held_result is not None and isinstance(
            message, (StreamEvent, AssistantMessage, ResultMessage)
        ):
            # A turn-bearing frame after the held result: the CLI did run the
            # user turn separately, so the held result was the autonomous
            # turn's own (#2440). Deliver it on the autonomous route first,
            # then route this frame to the user turn as usual.
            await self._release_held_result_to_handler()
        active = self._active_turn
        if active is not None and not active.finished:
            delegated_event = self._observe_result_deferring_task(active, message)
            if delegated_event is not None:
                active.queue.put_nowait(delegated_event)
        if self._unsolicited_inflight or active is None or active.finished:
            # Same ownership rule as the direct reader loop
            # (``unsolicited_inflight or not state.pending``): a turn-bearing
            # frame that arrived without an active ``send_turn`` keeps every
            # frame through its terminal ResultMessage — a user turn submitted
            # in between must not steal the autonomous turn's result. The one
            # exception is the terminal frame itself when such a user turn is
            # pending: see _hold_result_for_pending_turn (#2440).
            await self._handle_unsolicited_frame(message)
            return
        if isinstance(message, StreamEvent):
            self._route_stream_event(active, message)
        elif isinstance(message, AssistantMessage):
            if message.parent_tool_use_id is None:
                self._route_assistant_message(active, message)
        elif isinstance(message, UserMessage):
            if message.parent_tool_use_id is None:
                self._route_tool_results(active, message)
        elif isinstance(message, ResultMessage):
            if active.result_deferring_tasks:
                # Body-free observability: task count is enough to explain why
                # the apparent terminal frame did not revoke approval routing;
                # emit at most once for the whole run.
                if not active.completion_deferral_observed:
                    active.completion_deferral_observed = True
                    logger.info(
                        "Deferring Claude run completion while %d delegated task(s) remain",
                        len(active.result_deferring_tasks),
                    )
            else:
                self._complete_turn(active, message)

    def _observe_session_id(self, message: Message) -> None:
        if self._session_id is not None:
            return
        candidate: object
        if isinstance(message, SystemMessage):
            candidate = message.data.get("session_id")
        else:
            candidate = getattr(message, "session_id", None)
        if isinstance(candidate, str) and candidate:
            self._session_id = candidate
            self._session_ready.set()

    async def _handle_unsolicited_frame(self, message: Message) -> None:
        """Consume one between-turns ("unsolicited") SDK frame.

        Assistant text is buffered until its terminal ResultMessage so the
        registered handler receives one complete message per autonomous turn,
        not one per SDK frame. StreamEvent partials only establish ownership;
        they are never delivered (no live draft exists for an unsolicited
        turn). Without a registered handler the terminal frame is dropped —
        the adapter's pre-seam behavior.
        """

        if self._unsolicited_discard:
            # Late frames of an abandoned send_turn: swallow everything
            # through the abandoned turn's terminal ResultMessage so its
            # answer cannot deliver twice.
            if isinstance(message, ResultMessage):
                self._unsolicited_discard = False
                self._unsolicited_inflight = False
                self._unsolicited_started_at = None
                self._unsolicited_texts.clear()
                await self._notify_unsolicited_lifecycle("end", message.session_id)
                logger.warning(
                    "Swallowed late Claude ResultMessage after an abandoned turn: "
                    "session=%s",
                    message.session_id,
                )
            return
        if isinstance(message, StreamEvent):
            # The first token delta establishes turn ownership even though
            # unsolicited partials are intentionally not delivered.
            await self._begin_unsolicited_turn(message)
            return
        if isinstance(message, AssistantMessage):
            await self._begin_unsolicited_turn(message)
            self._unsolicited_texts.extend(
                block.text for block in message.content if isinstance(block, TextBlock)
            )
            return
        if not isinstance(message, ResultMessage):
            return
        raw = message.result or "\n".join(self._unsolicited_texts)
        self._unsolicited_texts.clear()
        self._unsolicited_inflight = False
        self._unsolicited_started_at = None
        active = self._active_turn
        if active is not None and not active.finished:
            # A user turn was submitted while this autonomous turn ran. The
            # CLI may have merged it into the turn this frame just ended, in
            # which case this is the user turn's result too and no other frame
            # will ever come for it (#2440). Park the result until the next
            # frame or the grace decides which shape this is.
            await self._notify_unsolicited_lifecycle("end", message.session_id)
            self._hold_result_for_pending_turn(active, message, raw)
            return
        try:
            await self._deliver_unsolicited(raw, message.session_id)
        finally:
            await self._notify_unsolicited_lifecycle("end", message.session_id)

    async def _deliver_unsolicited(self, raw: str, session_id: str | None) -> None:
        handler = self._unsolicited_handler
        if handler is None:
            logger.warning(
                "Dropping unsolicited Claude result without a registered handler: "
                "session=%s",
                session_id,
            )
            return
        try:
            await handler(raw, session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Fail-open: a broken delivery route must never take down the
            # reader task that also serves in-turn frames.
            logger.exception("Unsolicited Claude delivery handler failed")

    # -- shared-result hold (#2440) ------------------------------------------

    def _hold_result_for_pending_turn(
        self, active: _ActiveTurn, message: ResultMessage, raw: str
    ) -> None:
        self._cancel_held_result()
        # Keep the autonomous text the handler would have received so the
        # two-result shape delivers exactly what it delivered before.
        held = _HeldResult(message=message, active=active, raw=raw)
        grace = self._merged_turn_grace_seconds

        async def expire() -> None:
            try:
                await asyncio.sleep(grace)
            except asyncio.CancelledError:
                raise
            if self._held_result is not held:
                return
            self._held_result = None
            if held.active.finished or self._closed:
                # The waiter gave up (stall release, cancellation, close)
                # before the grace ran out: nothing is left to complete, but
                # the answer must still reach the room.
                await self._deliver_unsolicited(held.raw, held.message.session_id)
                return
            if held.active.result_deferring_tasks:
                # Delegated work registered on the user turn is still running:
                # this was an intermediate result, and the run-ending frame
                # that follows it routes to the turn normally now that the
                # autonomous window is closed. Completing here would revoke
                # the turn's approval route early.
                logger.info(
                    "Parked autonomous result released without completing the "
                    "user turn: %d delegated task(s) still running (session=%s)",
                    len(held.active.result_deferring_tasks),
                    held.message.session_id,
                )
                return
            logger.warning(
                "Completing the pending user turn with the autonomous turn's "
                "result: no frame followed it within %.0fs, so the CLI merged "
                "the user prompt into that turn (session=%s)",
                grace,
                held.message.session_id,
            )
            self._complete_turn(held.active, held.message)

        held.timer = asyncio.get_running_loop().create_task(expire())
        self._held_result = held

    async def _release_held_result_to_handler(self) -> None:
        held = self._held_result
        if held is None:
            return
        self._cancel_held_result()
        await self._deliver_unsolicited(held.raw, held.message.session_id)

    def _take_held_result(self) -> _HeldResult | None:
        """Detach the parked result and stop its timer; the caller owns it."""

        held = self._held_result
        if held is None:
            return None
        self._held_result = None
        timer = held.timer
        if timer is not None and not timer.done():
            timer.cancel()
        return held

    def _cancel_held_result(self) -> None:
        self._take_held_result()

    async def _begin_unsolicited_turn(self, message: Message) -> None:
        """Mark autonomous-turn ownership; announce its start exactly once."""

        if self._unsolicited_inflight:
            return
        self._unsolicited_inflight = True
        self._unsolicited_started_at = asyncio.get_running_loop().time()
        session_id = getattr(message, "session_id", None) or self._session_id
        await self._notify_unsolicited_lifecycle("start", session_id)

    async def _notify_unsolicited_lifecycle(self, phase: str, session_id: str | None) -> None:
        """Fail-open start/end seam for the autonomous turn window (#2122).

        Only an "end" that follows a delivered "start" is forwarded, so an
        abandoned turn's swallowed result never clears a route this window
        did not publish.
        """

        lifecycle = self._unsolicited_lifecycle
        if phase == "start":
            if lifecycle is None or self._unsolicited_route_open:
                return
            self._unsolicited_route_open = True
        else:
            if not self._unsolicited_route_open:
                return
            self._unsolicited_route_open = False
            if lifecycle is None:
                return
        try:
            await lifecycle(phase, session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unsolicited Claude lifecycle handler failed: phase=%s", phase)
