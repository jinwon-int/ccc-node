import logging
from typing import Any, Optional

import telegram.error

from telegram_bot.utils.heartbeat_store import (
    discard_heartbeat,
    record_heartbeat,
    store_path_for,
)

logger = logging.getLogger(__name__)


class BotStatusMixin:
    def _heartbeat_store_path(self):
        """Resolve the heartbeat id registry path, or None when unavailable."""
        return store_path_for(
            getattr(self._config, "bot_data_dir", None),
            getattr(self._config, "heartbeat_store_path", None),
        )

    def _make_status_callback(self, bot: Any, chat_id: int):
        """Build a fail-open send/edit/delete callback for task heartbeat messages."""
        store_path = self._heartbeat_store_path()

        async def status_callback(text: Optional[str], message_id: Optional[int] = None) -> Optional[int]:
            try:
                if text is None:
                    if message_id is not None and getattr(self._config, "heartbeat_delete_on_done", True):
                        await bot.delete_message(chat_id=chat_id, message_id=message_id)
                    # Only reached when the delete above didn't raise: the message
                    # is gone, so drop it from the startup-sweep registry. A failed
                    # delete falls through to the except and stays registered, so
                    # the next startup retries it.
                    if message_id is not None and store_path is not None:
                        discard_heartbeat(store_path, chat_id, message_id)
                    return None
                if message_id is not None:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=text,
                        )
                    except telegram.error.BadRequest as exc:
                        if "message is not modified" not in str(exc).lower():
                            raise
                    return message_id
                sent = await bot.send_message(chat_id=chat_id, text=text)
                value = getattr(sent, "message_id", None)
                value = value if isinstance(value, int) else None
                # Register the freshly created heartbeat so a bridge restart that
                # kills this request mid-flight can still delete the message on
                # its next startup instead of leaving it frozen on "⏳ Working".
                if value is not None and store_path is not None:
                    record_heartbeat(store_path, chat_id, value)
                return value
            except Exception as exc:
                logger.warning("Heartbeat status callback failed: %s", type(exc).__name__)
                return message_id

        return status_callback
