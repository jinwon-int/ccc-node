"""Telegram projection of the per-conversation external-wait status (#2081).

One silent (``disable_notification``) message per conversation route shows
the waits the bridge is still watching; it is edited as waits finish and
deleted once nothing recent is left. The decision logic lives in
:mod:`telegram_bot.core.external_wait_status` (pure, channel-neutral); this
mixin only reads the registry, plans, and performs the send/edit/delete.

Fail-open everywhere: a status hiccup must never break the turn, the wake, or
the command that triggered the sync. Logs are body-free (route ids and
action kinds only).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, cast

import telegram.error

from telegram_bot.core.bot_ports import RuntimeDataConfigPort
from telegram_bot.core.external_wait import (
    ExternalWaitRegistry,
    default_registry_path,
)
from telegram_bot.core.external_wait_status import (
    MAX_RECONCILE_ROUTES,
    STATUS_ENV_FLAG,
    ExternalWaitStatusStore,
    default_status_store_path,
    plan_status_update,
    reconcile_routes,
    records_for_route,
    status_enabled,
    text_hash_of,
)
from telegram_bot.utils.tg_errors import is_not_modified
from telegram_bot.utils.tg_robust import send_with_retry

logger = logging.getLogger(__name__)

_GONE_FRAGMENTS = (
    "message to edit not found",
    "message to delete not found",
    "message can't be edited",
    "message can't be deleted",
    "message_id_invalid",
    "chat not found",
)


def _message_gone(error: BaseException) -> bool:
    """True when Telegram says the stored message no longer exists/is editable."""
    text = str(error).lower()
    return any(fragment in text for fragment in _GONE_FRAGMENTS)


StatusSyncer = Callable[[int, int], Awaitable[None]]


async def refresh_wait_status(bot: Any, user_id: int, chat_id: int) -> None:
    """Refresh one route's status after a reply, when the bot composes the sync (#2088).

    The command/callback mixins run turns outside the main message path and
    do not inherit :class:`BotWaitStatusMixin` themselves; test doubles of
    those mixins do not carry it at all. Always fail-open.
    """
    sync_status = getattr(bot, "_sync_external_wait_status", None)
    if not callable(sync_status):
        return
    try:
        await sync_status(int(user_id), int(chat_id))
    except Exception as exc:
        logger.debug("External-wait status refresh failed: %s", type(exc).__name__)


class BotWaitStatusMixin:
    _config: RuntimeDataConfigPort
    application: Any

    # -- wiring helpers ----------------------------------------------------------
    def _external_wait_status_enabled(self) -> bool:
        return status_enabled()

    def _external_wait_status_home(self) -> Path:
        data_dir = getattr(self._config, "bot_data_dir", None) or (
            Path(self._config.project_root) / ".telegram_bot"
        )
        return Path(data_dir) / "external-wait"

    def _external_wait_status_store(self) -> ExternalWaitStatusStore:
        return ExternalWaitStatusStore(default_status_store_path(self._external_wait_status_home()))

    def _external_wait_status_registry(self) -> ExternalWaitRegistry:
        return ExternalWaitRegistry(default_registry_path(self._external_wait_status_home()))

    def _external_wait_status_syncer(self) -> Optional[StatusSyncer]:
        """The monitor/lifecycle hook, or None when the projection is off."""
        if not self._external_wait_status_enabled():
            return None
        return self._sync_external_wait_status

    # -- the one entry point -----------------------------------------------------
    # ccc-side-effect: telegram.external_wait_status
    async def _sync_external_wait_status(self, user_id: int, chat_id: int) -> None:
        """Bring the route's status message in line with the registry (fail-open)."""
        if not self._external_wait_status_enabled():
            return
        try:
            registry = self._external_wait_status_registry()
            store = self._external_wait_status_store()
            records = records_for_route(registry.records(), user_id, chat_id)
            stored = store.get(user_id, chat_id)
            action, text = plan_status_update(records, stored, time.time())
            if action == "noop":
                return
            logger.debug(
                "External-wait status %s: route=%s:%s", action, int(user_id), int(chat_id)
            )
            if action == "send":
                await self._wait_status_send(store, user_id, chat_id, cast(str, text))
            elif action == "edit":
                await self._wait_status_edit(
                    store, user_id, chat_id, cast(Dict[str, Any], stored), cast(str, text)
                )
            else:
                await self._wait_status_delete(store, user_id, chat_id, cast(Dict[str, Any], stored))
        except Exception as exc:
            logger.debug(
                "External-wait status sync failed: route=%s:%s error=%s",
                user_id,
                chat_id,
                type(exc).__name__,
            )

    async def _reconcile_external_wait_status_on_start(self) -> None:
        """Restart durability: refresh stored messages, finalize or drop stale ones.

        Routes come from both the id store (messages a previous run left
        behind) and the registry's monitoring records (a message that was
        never sent, e.g. the run died between register and turn end).
        Bounded and fail-open; nothing here can delay startup on an outage
        beyond one bounded Telegram call per route.
        """
        if not self._external_wait_status_enabled():
            return
        try:
            routes = reconcile_routes(
                self._external_wait_status_store().entries(),
                self._external_wait_status_registry().records(),
            )
        except Exception as exc:
            logger.debug("External-wait status reconcile skipped: %s", type(exc).__name__)
            return
        if not routes:
            return
        selected = routes[:MAX_RECONCILE_ROUTES]
        for user_id, chat_id in selected:
            await self._sync_external_wait_status(user_id, chat_id)
        logger.info("External-wait status reconciled %d route(s) after restart", len(selected))

    # -- Telegram primitives -----------------------------------------------------
    async def _wait_status_send(
        self, store: ExternalWaitStatusStore, user_id: int, chat_id: int, text: str
    ) -> None:
        bot = self.application.bot
        sent = await send_with_retry(
            lambda: bot.send_message(
                chat_id=int(chat_id), text=text, disable_notification=True
            ),
            name="wait-status",
        )
        message_id = getattr(sent, "message_id", None)
        if message_id is None:
            logger.debug("External-wait status send returned no message id; not stored")
            return
        store.put(user_id, chat_id, message_id=message_id, text_hash=text_hash_of(text))

    async def _wait_status_edit(
        self,
        store: ExternalWaitStatusStore,
        user_id: int,
        chat_id: int,
        stored: Dict[str, Any],
        text: str,
    ) -> None:
        bot = self.application.bot
        message_id = stored.get("message_id")
        try:
            await send_with_retry(
                lambda: bot.edit_message_text(
                    chat_id=int(chat_id), message_id=message_id, text=text
                ),
                name="wait-status",
            )
        except telegram.error.BadRequest as exc:
            if is_not_modified(exc):
                store.put(user_id, chat_id, message_id=message_id, text_hash=text_hash_of(text))
                return
            if not _message_gone(exc):
                raise
            # The owner (or Telegram's 48h edit window) removed it: forget the
            # id and post a fresh one only while something is still monitored.
            store.pop(user_id, chat_id)
            logger.debug("External-wait status message gone; re-sending: route=%s:%s", user_id, chat_id)
            records = records_for_route(self._external_wait_status_registry().records(), user_id, chat_id)
            action, fresh = plan_status_update(records, None, time.time())
            if action == "send" and fresh:
                await self._wait_status_send(store, user_id, chat_id, fresh)
            return
        store.put(user_id, chat_id, message_id=message_id, text_hash=text_hash_of(text))

    async def _wait_status_delete(
        self, store: ExternalWaitStatusStore, user_id: int, chat_id: int, stored: Dict[str, Any]
    ) -> None:
        bot = self.application.bot
        message_id = stored.get("message_id")
        try:
            await send_with_retry(
                lambda: bot.delete_message(chat_id=int(chat_id), message_id=message_id),
                name="wait-status",
            )
        except telegram.error.BadRequest:
            # Already gone or no longer deletable: nothing left to clean.
            pass
        store.pop(user_id, chat_id)


__all__ = [
    "BotWaitStatusMixin",
    "MAX_RECONCILE_ROUTES",
    "STATUS_ENV_FLAG",
    "StatusSyncer",
    "refresh_wait_status",
]
