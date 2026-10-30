"""「한데」 projection of the external-wait status (#2590 A).

Matrix keeps ONE status message per conversation route and edits, moves and
redacts it (:mod:`telegram_bot.core.matrix.wait_status`, #2088). The hande port
has no edit and no delete, so this projection rides the status-v1 line instead
(family-messenger#560): the transport sends the rendered ``⏳ Waiting for
results …`` text as a ``notice`` frame with ``kind:"status"``, the bot posts it
as a ``status-<uuid>`` message, and the apps show it as the sender's transient
status line — at the bottom of the room and in the room-list preview, never in
history, unread or push.

Because that line is transient on the app side (dropped on the sender's next
normal message, and 5 minutes after its last update), the projection re-sends
instead of editing:

* on ``turn_closed`` (after the turn's reply went out — the reply cleared the
  line on the apps) while a wait on that route is still monitored;
* when the rendered text changes (a monitor transition: one of several waits
  finished, the rest are still watched);
* periodically, every ``wait_status_interval_s`` (default 120 s, at most
  240 s), so the line outlives the apps' 5-minute expiry;

and stops as soon as nothing on the route is monitored: the resume turn's reply
announces the result, so no "finished" line is sent. While a turn runs in the
same room nothing is sent — that turn's own ``⏳ Working`` line owns the slot.

Shared with Matrix: the registry, the route selection and the renderer
(:mod:`telegram_bot.core.external_wait_status`), and the ``CCC_EXTERNAL_WAIT_STATUS``
flag. Hande-only: the ``wait_status_frames`` config knob (default off — an older
bot without ``kind:"status"`` would post every re-send as a plain notice) and
``notice_frames``, which it needs as well. With either off nothing changes.

Fail-open everywhere; logs carry route ids and action kinds, never the text.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional, Tuple

from telegram_bot.core.external_wait import STATE_MONITORING, conversation_key_of
from telegram_bot.core.external_wait_status import (
    MAX_RECONCILE_ROUTES,
    reconcile_routes,
    records_for_route,
    render_wait_status,
    status_enabled,
    text_hash_of,
)

if TYPE_CHECKING:
    from telegram_bot.core.external_wait import ExternalWaitRegistry

logger = logging.getLogger(__name__)

#: How often the re-send loop wakes up; a route is re-sent only once its
#: interval has passed, so this only bounds the lateness of a re-send.
TICK_S = 15.0


class HandeWaitStatusMixin:
    """External-wait status for ``HandeBot``; placed ahead of ``MatrixBot`` in the MRO.

    Overrides the Matrix mixin's per-route sync, its delivery hook and its
    one-shot start reconcile (which becomes the re-send loop). The host
    provides ``_transport``, ``room_for_chat()``, ``_external_wait_registry()``
    and ``load_config()``.
    """

    _transport: Any
    #: route key -> (last send epoch, text hash). In memory only: after a
    #: restart the first loop tick simply sends the current line again.
    _hande_wait_sent: Optional[Dict[str, Tuple[float, str]]] = None

    if TYPE_CHECKING:

        def room_for_chat(self, chat_id: int) -> str | None: ...

        def _external_wait_registry(self) -> ExternalWaitRegistry: ...

        def load_config(self) -> Mapping[str, Any]: ...

        async def _sync_external_wait_status(
            self, user_id: int, chat_id: int, *, allow_move: bool = True
        ) -> None: ...

    def _hande_wait_state(self) -> Dict[str, Tuple[float, str]]:
        state = self._hande_wait_sent
        if state is None:
            state = self._hande_wait_sent = {}
        return state

    def _hande_wait_transport(self) -> Any:
        """The transport when it can send status lines now, else ``None``."""

        transport = self._transport
        if transport is None or not getattr(transport, "wait_status_ready", False):
            return None
        if not callable(getattr(transport, "send_status_notice", None)):
            return None
        return transport

    def _hande_wait_interval(self, transport: Any) -> float:
        try:
            return float(getattr(transport, "wait_status_interval_s", 0) or 120.0)
        except (TypeError, ValueError):
            return 120.0

    # -- overrides of MatrixWaitStatusMixin ---------------------------------------

    async def _wait_status_sync_route(
        self, user_id: int, chat_id: int, *, allow_move: bool = True
    ) -> None:
        """Send the route's current line when due (called under the mixin's lock)."""

        del allow_move  # no timeline placement on hande: a re-send is the move
        transport = self._hande_wait_transport()
        if transport is None:
            return
        key = conversation_key_of(user_id, chat_id)
        state = self._hande_wait_state()
        records = records_for_route(self._external_wait_registry().records(), user_id, chat_id)
        if not any(rec.get("state") == STATE_MONITORING for rec in records):
            # Nothing watched any more: the resume turn's reply carries the
            # result, and the apps drop the old line on that reply.
            state.pop(key, None)
            return
        now = time.time()
        text = render_wait_status(records, now)
        if text is None:
            state.pop(key, None)
            return
        room = self.room_for_chat(chat_id)
        if room is None:
            logger.debug("hande external-wait status: no room for route=%s", key)
            return
        if transport.active_room() == room:
            # A turn runs here: its own ``⏳ Working`` line owns the slot, and
            # ``turn_closed`` re-sends the wait line once it ends.
            return
        text_hash = text_hash_of(text)
        last = state.get(key)
        if last is not None and last[1] == text_hash and now - last[0] < self._hande_wait_interval(transport):
            return
        if await transport.send_status_notice(room, text):
            state[key] = (now, text_hash)
            logger.debug("hande external-wait status sent: route=%s", key)

    async def _wait_status_turn_closed(self, job: Mapping[str, Any]) -> None:
        """The turn's reply cleared the line on the apps: re-send it now if still watched."""

        if not status_enabled() or self._hande_wait_transport() is None:
            return
        try:
            user_id, chat_id = self._control_identity(job)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.debug("hande external-wait status: no route for turn: %s", type(exc).__name__)
            return
        self._hande_wait_state().pop(conversation_key_of(user_id, chat_id), None)
        await self._sync_external_wait_status(user_id, chat_id)

    async def _wait_status_delivered(self, job: Mapping[str, Any]) -> None:
        """No outbox rows on hande: nothing to adopt."""

        del job

    async def _reconcile_external_wait_status_on_start(self) -> None:
        """The re-send loop (a cancel-on-stop background leg, as on Matrix).

        Returns at once when the projection is off, so a node without the
        knobs runs exactly as before.
        """

        if not status_enabled():
            return
        try:
            config = self.load_config()
        except Exception:
            return
        if not (config.get("wait_status_frames") and config.get("notice_frames")):
            return
        while True:
            await self._hande_wait_status_tick()
            await asyncio.sleep(TICK_S)

    async def _hande_wait_status_tick(self) -> None:
        """One pass over the routes with a monitored wait (bounded, fail-open)."""

        if self._hande_wait_transport() is None:
            return
        try:
            routes = reconcile_routes({}, self._external_wait_registry().records())
        except Exception as exc:
            logger.debug("hande external-wait status tick skipped: %s", type(exc).__name__)
            return
        live = set()
        for user_id, chat_id in routes[:MAX_RECONCILE_ROUTES]:
            live.add(conversation_key_of(user_id, chat_id))
            await self._sync_external_wait_status(user_id, chat_id)
        # Forget routes whose waits were removed from the registry entirely.
        state = self._hande_wait_state()
        for key in [k for k in state if k not in live]:
            state.pop(key, None)


__all__ = ["HandeWaitStatusMixin", "TICK_S"]
