"""Matrix projection of the per-conversation external-wait status (#2088).

Stage 2 of #2081: the same ONE status message per conversation route that the
Telegram projection (:mod:`telegram_bot.core.bot_wait_status`) keeps, built on
the same channel-neutral renderer, planner, flag and id store
(:mod:`telegram_bot.core.external_wait_status`). Only the three channel
operations differ:

* **send** goes through the durable outbox (``enqueue_notice``). The outbox
  is one ``seq``-ordered queue, so a status queued after the turn's reply row
  is ready is delivered after that reply by construction, with the same
  device pinning, room gate, retry and crash-safe part acknowledgement as
  every reply. The row id is stored as ``outbox:<row id>`` until the sender
  records the Matrix event id the row became (``jobs.sent_event``); the
  transport's ``delivered`` hook then swaps it in and re-plans the route.
* **edit** is a direct ``m.replace`` of that event (``edit_notice``), like the
  progress bubble's edits; the stored text hash only advances on success, so
  a failed edit is retried by the next sync.
* **delete** is a redaction (``redact_notice``).

Triggers: the transport's ``turn_closed`` hook (every turn, including the
external-wait resume and continuation self-jobs, after the reply row is
queued), every terminal transition in ``ExternalWaitMonitor``
(``status_syncer``), ``/cancelwait``, and a bounded reconcile after start.

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
    records_for_route,
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
#: Upper bound for one direct edit or redaction.
IO_TIMEOUT_S = 30.0
#: Transport capabilities the projection needs; without them it stays silent
#: rather than posting messages it could never edit or remove.
_REQUIRED_TRANSPORT = ("enqueue_notice", "notice_event", "edit_notice", "redact_notice")

StatusSyncer = Callable[[int, int], Awaitable[None]]


def _capable(transport: Any) -> bool:
    return transport is not None and all(
        callable(getattr(transport, name, None)) for name in _REQUIRED_TRANSPORT
    )


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
    async def _sync_external_wait_status(self, user_id: int, chat_id: int) -> None:
        """Bring the route's status message in line with the registry (fail-open)."""
        if not status_enabled():
            return
        try:
            async with self._wait_status_lock():
                await self._wait_status_sync_route(int(user_id), int(chat_id))
        except Exception as exc:
            logger.debug(
                "Matrix external-wait status sync failed: route=%s:%s error=%s",
                user_id,
                chat_id,
                type(exc).__name__,
            )

    async def _wait_status_sync_route(self, user_id: int, chat_id: int) -> None:
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
        records = records_for_route(self._external_wait_registry().records(), user_id, chat_id)
        action, text = plan_status_update(records, stored, time.time())
        if action == "noop":
            return
        room = self.room_for_chat(chat_id)
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
                edited = await transport.edit_notice(room, event_id, text)
            if edited:
                store.put(user_id, chat_id, message_id=event_id, text_hash=text_hash_of(text))
                return
            # Refused for good (event gone): forget it and post a fresh one
            # only while something is still monitored, as on Telegram.
            store.pop(user_id, chat_id)
            fresh_action, fresh = plan_status_update(records, None, time.time())
            if fresh_action == "send" and fresh is not None:
                self._wait_status_send(transport, store, user_id, chat_id, room, fresh)
        elif action == "delete" and stored is not None:
            async with asyncio.timeout(IO_TIMEOUT_S):
                await transport.redact_notice(room, str(stored.get("message_id")))
            # Redacted, or already gone / not redactable: nothing left to clean.
            store.pop(user_id, chat_id)

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
            store.put(user_id, chat_id, message_id=event_id, text_hash=text_hash)
            return {**stored, "message_id": event_id}, False
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
        row = transport.enqueue_notice(room, text, key=key)
        if not isinstance(row, str) or not row:
            logger.debug("Matrix external-wait status queued without a row id; not stored")
            return
        store.put(user_id, chat_id, message_id=PENDING_PREFIX + row, text_hash=text_hash_of(text))

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
            await self._sync_external_wait_status(user_id, chat_id)
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


__all__ = ["IO_TIMEOUT_S", "MatrixWaitStatusMixin", "PENDING_PREFIX", "StatusSyncer"]
