import asyncio
import logging
from typing import Any, Optional, Protocol

import telegram.error

from telegram_bot.core.bot_ports import HeartbeatConfigPort, RuntimeDataConfigPort
from telegram_bot.utils.heartbeat_store import (
    discard_heartbeat,
    record_heartbeat,
    store_path_for,
)

logger = logging.getLogger(__name__)


class _StatusConfigPort(RuntimeDataConfigPort, HeartbeatConfigPort, Protocol):
    """Config slices this mixin reads (#1509): ``bot_data_dir`` and the heartbeat store fields."""


class BotStatusMixin:
    _config: _StatusConfigPort

    def _heartbeat_store_path(self):
        """Resolve the heartbeat id registry path, or None when unavailable."""
        return store_path_for(
            getattr(self._config, "bot_data_dir", None),
            getattr(self._config, "heartbeat_store_path", None),
        )

    def _make_status_callback(self, bot: Any, chat_id: int):
        """Build a fail-open replace/delete callback for task heartbeat messages."""
        store_path = self._heartbeat_store_path()

        pending_delete: Optional[int] = None
        current_id: Optional[int] = None
        lock = asyncio.Lock()

        async def delete_status(message_id: int) -> None:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except telegram.error.BadRequest as exc:
                if "message to delete not found" not in str(exc).lower():
                    raise
            if store_path is not None:
                discard_heartbeat(store_path, chat_id, message_id)

        async def status_callback(text: Optional[str], message_id: Optional[int] = None) -> Optional[int]:
            nonlocal pending_delete, current_id
            async with lock:
                message_id = current_id if current_id is not None else message_id
                current_id = message_id
                try:
                    # Retry one stale predecessor before sending another status.
                    # A Telegram delete failure must not grow a trail of messages.
                    if pending_delete is not None:
                        await delete_status(pending_delete)
                        pending_delete = None
                    if text is None:
                        if message_id is not None:
                            if getattr(self._config, "heartbeat_delete_on_done", True):
                                await delete_status(message_id)
                            elif store_path is not None:
                                discard_heartbeat(store_path, chat_id, message_id)
                        current_id = None
                        return None
                    # Editing cannot move a Telegram message to the bottom. Send
                    # its replacement silently before deleting the old status so
                    # a failed send leaves the existing heartbeat visible.
                    sent = await bot.send_message(
                        chat_id=chat_id, text=text, disable_notification=True,
                    )
                    value = getattr(sent, "message_id", None)
                    if type(value) is not int:
                        return message_id
                    if store_path is not None:
                        record_heartbeat(store_path, chat_id, value)
                    pending_delete = message_id if message_id != value else None
                    current_id = message_id = value
                    if pending_delete is not None:
                        await delete_status(pending_delete)
                        pending_delete = None
                    return message_id
                except Exception as exc:
                    logger.warning("Heartbeat status callback failed: %s", type(exc).__name__)
                    return message_id

        return status_callback
