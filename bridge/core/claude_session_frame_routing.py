"""Route Claude SDK frames for active and between-turn session work."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
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


class ClaudeSessionFrameRoutingMixin:
    """Own the Claude SDK reader and route frames by turn ownership."""

    _active_turn: _ActiveTurn | None
    _closed: bool
    _session_id: str | None
    _session_ready: asyncio.Event
    _unsolicited_discard: bool
    _unsolicited_handler: Callable[[str, str | None], Awaitable[None]] | None
    _unsolicited_inflight: bool
    _unsolicited_texts: list[str]
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
            if not self._closed:
                self._fail_active_turn(
                    "claude_connection_failed",
                    stream_failure or "Claude connection closed",
                )
            # A closed stream can never announce an id; unblock _start.
            self._session_ready.set()

    async def _route_message(self, message: Message) -> None:
        self._observe_sdk_frame(message)
        self._observe_session_id(message)
        self._observe_background_task_notifications(message)
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
            # in between must not steal the autonomous turn's result.
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
                self._unsolicited_texts.clear()
                logger.warning(
                    "Swallowed late Claude ResultMessage after an abandoned turn: "
                    "session=%s",
                    message.session_id,
                )
            return
        if isinstance(message, StreamEvent):
            # The first token delta establishes turn ownership even though
            # unsolicited partials are intentionally not delivered.
            self._unsolicited_inflight = True
            return
        if isinstance(message, AssistantMessage):
            self._unsolicited_inflight = True
            self._unsolicited_texts.extend(
                block.text for block in message.content if isinstance(block, TextBlock)
            )
            return
        if not isinstance(message, ResultMessage):
            return
        raw = message.result or "\n".join(self._unsolicited_texts)
        self._unsolicited_texts.clear()
        self._unsolicited_inflight = False
        handler = self._unsolicited_handler
        if handler is None:
            logger.warning(
                "Dropping unsolicited Claude result without a registered handler: "
                "session=%s",
                message.session_id,
            )
            return
        try:
            await handler(raw, message.session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Fail-open: a broken delivery route must never take down the
            # reader task that also serves in-turn frames.
            logger.exception("Unsolicited Claude delivery handler failed")
