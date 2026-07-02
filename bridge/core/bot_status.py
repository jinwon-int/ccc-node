import logging
from typing import Any, Optional

import telegram.error

from telegram_bot.utils.config import config

logger = logging.getLogger(__name__)


class BotStatusMixin:
    def _make_status_callback(self, bot: Any, chat_id: int):
        """Build a fail-open send/edit/delete callback for task heartbeat messages."""

        async def status_callback(text: Optional[str], message_id: Optional[int] = None) -> Optional[int]:
            try:
                if text is None:
                    if message_id is not None and getattr(config, "heartbeat_delete_on_done", True):
                        await bot.delete_message(chat_id=chat_id, message_id=message_id)
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
                return value if isinstance(value, int) else None
            except Exception as exc:
                logger.warning("Heartbeat status callback failed: %s", type(exc).__name__)
                return message_id

        return status_callback
