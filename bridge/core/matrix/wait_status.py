"""Matrix projection of the per-conversation external-wait status (#2088).

Stage 2 of #2081: the same ONE status message per conversation route that the
Telegram projection (:mod:`telegram_bot.core.bot_wait_status`) keeps, built on
the same channel-neutral renderer, planner, flag and id store
(:mod:`telegram_bot.core.external_wait_status`). Only the three channel
operations differ:

* **send** goes through the durable outbox (``enqueue_notice``) as an
  ``m.notice`` (:data:`STATUS_MSGTYPE`): a bot status line clients should not
  ping or notify on, unlike an ``m.text`` reply. The outbox
  is one ``seq``-ordered queue, so a status queued after the turn's reply row
  is ready is delivered after that reply by construction, with the same
  device pinning, room gate, retry and crash-safe part acknowledgement as
  every reply. The row id is stored as ``outbox:<row id>`` until the sender
  records the Matrix event id the row became (``jobs.sent_event``); the
  transport's ``delivered`` hook then swaps it in and re-plans the route.
* **edit** is a direct ``m.replace`` of that event (``edit_notice_event``),
  like the progress bubble's edits, and stays an ``m.notice`` (fallback and
  ``m.new_content``); the stored text hash only advances on success, so
  a failed edit is retried by the next sync. The edit event's id is kept as
  the entry's ``tail`` so the next placement check recognises it.
* **move** (#2288) is a redaction of the current event followed by a fresh
  outbox send of the same text, so the status lands at the room's bottom
  again — the "⏳ Working" bubble's rule (edit while newest, repost when
  buried) applied to the status line. An edit keeps the original timeline
  position, so once the user's next message and the reply have landed the
  status would otherwise stay buried under them for good.
* **delete** is a redaction (``redact_notice``).

Placement (``buried``) is decided from the transport's ``last_room_event``
(newest timeline event per room, fed by ``/sync`` — the bot's own echoes
included): the status is buried when that event is neither the stored
message nor its latest edit. Two guards keep this from looping: the
``delivered`` hook never moves (it runs before the echo of the row it just
delivered can arrive, so the fresh status would look buried), and a status
posted less than :data:`MOVE_MIN_INTERVAL_S` ago is left where it is.

Triggers: the transport's ``turn_closed`` hook (every turn, including the
external-wait resume and continuation self-jobs, after the reply row is
queued), every terminal transition in ``ExternalWaitMonitor``
(``status_syncer``), ``/cancelwait``, and a bounded reconcile after start
(``last_room_event`` is empty right after start, so a reconcile never moves).

Fail-open everywhere and body-free logs (route ids and action kinds only).
Syncs are serialized per bot so the monitor and a closing turn cannot both
post a first message for the same route.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple

from telegram_bot.core.external_wait_status import (
    MAX_RECONCILE_ROUTES,
    ExternalWaitStatusStore,
    default_status_store_path,
    plan_status_update,
    reconcile_routes,
    merge_background_records,
    records_for_route,
    route_background_snapshot,
    status_enabled,
    text_hash_of,
)

if TYPE_CHECKING:
    from telegram_bot.core.external_wait import ExternalWaitRegistry

logger = logging.getLogger(__name__)

#: ``message_id`` prefix while the status notice is still an undelivered
#: outbox row (Matrix event ids start with ``$``, so the two never collide).
PENDING_PREFIX = "outbox:"
#: Outbox row ids of queued notices (``MatrixStore.notice``).
_NOTICE_ROW_PREFIX = "$notice-"
#: Message type of the status message and its edits: a notice, not a reply.
STATUS_MSGTYPE = "m.notice"
#: Upper bound for one direct edit or redaction.
IO_TIMEOUT_S = 30.0
#: A status posted (sent or moved) less recently than this may be moved again;
#: a younger one is left alone even when it looks buried, so a sync that runs
#: between the delivery of a fresh status and its ``/sync`` echo cannot
#: redact-and-repost it a second time (#2288).
MOVE_MIN_INTERVAL_S = 10.0
#: Transport capabilities the projection needs; without them it stays silent
#: rather than posting messages it could never edit or remove.
_REQUIRED_TRANSPORT = ("enqueue_notice", "notice_event", "edit_notice", "redact_notice")

StatusSyncer = Callable[[int, int], Awaitable[None]]


def _capable(transport: Any) -> bool:
    return transport is not None and all(
        callable(getattr(transport, name, None)) for name in _REQUIRED_TRANSPORT
    )


def _posted_epoch(stored: Mapping[str, Any]) -> float:
    """When the stored status was last posted; entries from before #2288 fall back to ``updated_epoch``."""
    try:
        return float(stored.get("posted_epoch") or stored.get("updated_epoch") or 0.0)
    except (TypeError, ValueError):
        return 0.0


class MatrixWaitStatusMixin:
    """External-wait status for ``MatrixBot`` (route = ``MatrixIdMap`` ints).

    The host provides ``_transport``, ``_data_dir()``, ``room_for_chat()``,
    ``_external_wait_registry()`` and ``_control_identity()``.
    """

    _transport: Any
    _wait_status_lock_obj: Optional[asyncio.Lock] = None

    if TYPE_CHECKING:

        def _data_dir(self) -> Path: ...

        def room_for_chat(self, chat_id: int) -> str | None: ...

        def _external_wait_registry(self) -> ExternalWaitRegistry: ...

        def _control_identity(self, job: Mapping[str, Any]) -> tuple[int, int]: ...

    # -- wiring helpers ----------------------------------------------------------
    def _external_wait_status_store(self) -> ExternalWaitStatusStore:
        return ExternalWaitStatusStore(default_status_store_path(self._data_dir() / "external-wait"))

    def _external_wait_status_syncer(self) -> Optional[StatusSyncer]:
        """The monitor hook, or None when the projection is off."""
        if not status_enabled():
            return None
        return self._sync_external_wait_status

    def _wait_status_lock(self) -> asyncio.Lock:
        lock = self._wait_status_lock_obj
        if lock is None:
            lock = self._wait_status_lock_obj = asyncio.Lock()
        return lock

    # -- the one entry point -----------------------------------------------------
    # ccc-side-effect: matrix.external_wait_status
    async def _sync_external_wait_status(
        self, user_id: int, chat_id: int, *, allow_move: bool = True
    ) -> None:
        """Bring the route's status message in line with the registry (fail-open).

        ``allow_move=False`` (the ``delivered`` hook) adopts ids and edits but
        never reposts: see the module notes on the echo race (#2288).
        """
        if not status_enabled():
            return
        try:
            async with self._wait_status_lock():
                await self._wait_status_sync_route(int(user_id), int(chat_id), allow_move=allow_move)
        except Exception as exc:
            logger.debug(
                "Matrix external-wait status sync failed: route=%s:%s error=%s",
                user_id,
                chat_id,
                type(exc).__name__,
            )

    async def _wait_status_sync_route(self, user_id: int, chat_id: int, *, allow_move: bool = True) -> None:
        transport = self._transport
        if not _capable(transport):
            return
        store = self._external_wait_status_store()
        stored = store.get(user_id, chat_id)
        if stored is not None:
            stored, pending = self._wait_status_resolve(transport, store, user_id, chat_id, stored)
            if pending:
                # Still queued (behind the reply, or in a muted room): there is
                # no event to edit or redact yet. The transport's delivery
                # hook re-runs this sync once the row is out.
                return
        now = time.time()
        records = merge_background_records(
            records_for_route(self._external_wait_registry().records(), user_id, chat_id),
            route_background_snapshot(getattr(self, "_project_chat", None), user_id, chat_id),
            user_id,
            chat_id,
            now,
        )
        room = self.room_for_chat(chat_id)
        buried = (
            allow_move
            and stored is not None
            and room is not None
            and self._wait_status_buried(transport, room, stored, now)
        )
        action, text = plan_status_update(records, stored, now, buried=buried)
        if action == "noop":
            return
        if room is None:
            logger.debug("Matrix external-wait status: no room for route=%s:%s", user_id, chat_id)
            if action == "delete":
                store.pop(user_id, chat_id)
            return
        logger.debug("Matrix external-wait status %s: route=%s:%s", action, user_id, chat_id)
        if action == "send" and text is not None:
            self._wait_status_send(transport, store, user_id, chat_id, room, text)
        elif action == "edit" and stored is not None and text is not None:
            event_id = str(stored.get("message_id"))
            async with asyncio.timeout(IO_TIMEOUT_S):
                edited, tail = await self._wait_status_edit(transport, room, event_id, text)
            if edited:
                store.put(
                    user_id,
                    chat_id,
                    message_id=event_id,
                    text_hash=text_hash_of(text),
                    tail=tail,
                    posted_epoch=_posted_epoch(stored),
                )
                return
            # Refused for good (event gone): forget it and post a fresh one
            # only while something is still monitored, as on Telegram.
            store.pop(user_id, chat_id)
            fresh_action, fresh = plan_status_update(records, None, time.time())
            if fresh_action == "send" and fresh is not None:
                self._wait_status_send(transport, store, user_id, chat_id, room, fresh)
        elif action == "move" and stored is not None and text is not None:
            # Buried by later events: take the old one down and post the
            # current text again through the outbox, so it lands after the
            # reply that buried it. A redaction that fails for good (event
            # already gone) changes nothing; a transient failure raises and
            # leaves the stored entry for the next sync.
            async with asyncio.timeout(IO_TIMEOUT_S):
                await transport.redact_notice(room, str(stored.get("message_id")))
            store.pop(user_id, chat_id)
            self._wait_status_send(transport, store, user_id, chat_id, room, text)
        elif action == "delete" and stored is not None:
            async with asyncio.timeout(IO_TIMEOUT_S):
                await transport.redact_notice(room, str(stored.get("message_id")))
            # Redacted, or already gone / not redactable: nothing left to clean.
            store.pop(user_id, chat_id)

    @staticmethod
    def _wait_status_buried(transport: Any, room: str, stored: Mapping[str, Any], now: float) -> bool:
        """Whether newer room events hide the stored status (#2288).

        Unknown newest event (nothing synced yet, or a transport without the
        map) reads as not buried; so does a status posted within
        :data:`MOVE_MIN_INTERVAL_S`.
        """
        latest = (getattr(transport, "last_room_event", None) or {}).get(room)
        if not isinstance(latest, str) or not latest:
            return False
        if now - _posted_epoch(stored) < MOVE_MIN_INTERVAL_S:
            return False
        own = {str(stored.get("message_id") or ""), str(stored.get("tail") or "")}
        return latest not in own

    @staticmethod
    async def _wait_status_edit(
        transport: Any, room: str, event_id: str, text: str
    ) -> Tuple[bool, Optional[str]]:
        """``(edited, edit event id)``; the id is ``None`` on a transport without ``edit_notice_event``."""
        edit_event = getattr(transport, "edit_notice_event", None)
        if callable(edit_event):
            tail = await edit_event(room, event_id, text, msgtype=STATUS_MSGTYPE)
            return tail is not None, (str(tail) if tail else None)
        edited = await transport.edit_notice(room, event_id, text, msgtype=STATUS_MSGTYPE)
        return bool(edited), None

    def _wait_status_resolve(
        self,
        transport: Any,
        store: ExternalWaitStatusStore,
        user_id: int,
        chat_id: int,
        stored: Dict[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """``(stored entry, still pending)`` with a delivered outbox row resolved to its event id."""
        message_id = str(stored.get("message_id") or "")
        if not message_id.startswith(PENDING_PREFIX):
            return stored, False
        state, event_id = transport.notice_event(message_id[len(PENDING_PREFIX):])
        if state == "pending":
            return stored, True
        if state == "sent" and event_id:
            text_hash = str(stored.get("text_hash") or "")
            posted = _posted_epoch(stored)
            store.put(user_id, chat_id, message_id=event_id, text_hash=text_hash, posted_epoch=posted)
            return {**stored, "message_id": event_id, "posted_epoch": posted}, False
        # The row is unknown or produced no event (every part rejected):
        # there is nothing to edit, so plan as if no message existed.
        store.pop(user_id, chat_id)
        return None, False

    def _wait_status_send(
        self,
        transport: Any,
        store: ExternalWaitStatusStore,
        user_id: int,
        chat_id: int,
        room: str,
        text: str,
    ) -> None:
        # Notice rows are permanent and keyed: a fresh key per status message
        # (a reused key with different text is an identity conflict).
        key = f"wait-status-{time.time_ns()}-{secrets.token_hex(4)}"
        row = transport.enqueue_notice(room, text, key=key, msgtype=STATUS_MSGTYPE)
        if not isinstance(row, str) or not row:
            logger.debug("Matrix external-wait status queued without a row id; not stored")
            return
        store.put(
            user_id,
            chat_id,
            message_id=PENDING_PREFIX + row,
            text_hash=text_hash_of(text),
            posted_epoch=time.time(),
        )

    # -- transport hooks (via MatrixTurnRunner) ------------------------------------
    async def _wait_status_turn_closed(self, job: Mapping[str, Any]) -> None:
        """A turn ended and its reply (if any) is queued: refresh that route."""
        if not status_enabled():
            return
        try:
            user_id, chat_id = self._control_identity(job)
        except Exception as exc:
            logger.debug("Matrix external-wait status: no route for turn: %s", type(exc).__name__)
            return
        await self._sync_external_wait_status(user_id, chat_id)

    async def _wait_status_delivered(self, job: Mapping[str, Any]) -> None:
        """An outbox row went out: if it is a tracked status, adopt its event id."""
        row = str(job.get("event_id") or "")
        if not row.startswith(_NOTICE_ROW_PREFIX) or not status_enabled():
            return
        target = PENDING_PREFIX + row
        for entry in self._external_wait_status_store().entries().values():
            if entry.get("message_id") != target:
                continue
            try:
                user_id, chat_id = int(entry["user_id"]), int(entry["chat_id"])
            except (KeyError, TypeError, ValueError):
                return
            # Adopt the id and apply any pending change, but never move here:
            # the row's own /sync echo has not arrived yet, so the fresh
            # status would read as buried and be reposted again (#2288).
            await self._sync_external_wait_status(user_id, chat_id, allow_move=False)
            return

    async def _reconcile_external_wait_status_on_start(self) -> None:
        """Restart durability: adopt delivered ids, refresh, finalize or drop stale messages.

        Bounded (``MAX_RECONCILE_ROUTES``) and fail-open; runs as a background
        leg so a slow homeserver never delays serving.
        """
        if not status_enabled():
            return
        try:
            routes = reconcile_routes(
                self._external_wait_status_store().entries(),
                self._external_wait_registry().records(),
            )
        except Exception as exc:
            logger.debug("Matrix external-wait status reconcile skipped: %s", type(exc).__name__)
            return
        selected = routes[:MAX_RECONCILE_ROUTES]
        for user_id, chat_id in selected:
            await self._sync_external_wait_status(user_id, chat_id)
        if selected:
            logger.info("Matrix external-wait status reconciled %d route(s) after start", len(selected))


__all__ = ["IO_TIMEOUT_S", "MatrixWaitStatusMixin", "PENDING_PREFIX", "STATUS_MSGTYPE", "StatusSyncer"]
