"""Stream state/control mixin for ProjectChatHandler."""

# mypy: disable-error-code="attr-defined"

import logging
from typing import List, Optional

from telegram_bot.core.project_chat_types import _UserStreamState
from telegram_bot.core.sdk_text import TASK_TERMINATED_NOTICE

logger = logging.getLogger(__name__)


class ProjectChatStateMixin:
    async def stop(self, user_id: int, chat_id: Optional[int] = None) -> bool:
        """Stop active stream(s) for a user and fail pending requests.

        The default notice is upgraded to a specific reason by
        ``_disconnect_stream_state`` when a recent SDK/stream error (usage limit,
        auth, network) is what actually caused the stop.
        """
        return await self._disconnect_user_stream(
            user_id, chat_id=chat_id, cancel_message=TASK_TERMINATED_NOTICE
        )
    def _states_for_user(self, user_id: int, chat_id: Optional[int] = None) -> List[_UserStreamState]:
        if chat_id is not None:
            state = self._streams.get(self._stream_key(user_id, chat_id))
            return [state] if state else []
        return [
            state
            for key, state in self._streams.items()
            if (key[0] if isinstance(key, tuple) else key) == user_id
        ]
    async def cancel_user_streaming(self, user_id: int, chat_id: Optional[int] = None) -> bool:
        """Cancel streaming drafts for one Telegram conversation, or all user conversations."""
        states = self._states_for_user(user_id, chat_id)
        if not states:
            return False

        cancelled = False
        for state in states:
            if not state.pending:
                continue
            for req in state.pending:
                if req.streaming_handler:
                    try:
                        await req.streaming_handler.cancel()
                        cancelled = True
                    except Exception as e:
                        logger.error(f"Failed to cancel streaming for user {user_id}: {e}")

        return cancelled
    def inflight_count(self, user_id: int, chat_id: Optional[int] = None) -> int:
        return sum(len(state.pending) for state in self._states_for_user(user_id, chat_id))
    def is_user_busy(self, user_id: int, chat_id: Optional[int] = None) -> bool:
        return self.inflight_count(user_id, chat_id) > 0
    async def clear_user_stream(self, user_id: int, chat_id: Optional[int] = None) -> None:
        """Clear active stream(s) for a user to force a new SDK connection (used by /revert).

        Cancels pending request futures first (revert relies on cancellation
        semantics, not a 'terminated' result), then delegates to the full
        teardown which cancels AND awaits the reader/typing tasks with timeouts
        and awaits ``client.disconnect()``. The old implementation fire-and-forgot
        ``asyncio.create_task(close_fn())`` — an unreferenced task that could be
        garbage-collected mid-flight, and ``close`` often did not exist on the
        client (the real method is ``disconnect``), leaking the SDK subprocess.
        """
        for state in self._states_for_user(user_id, chat_id):
            for req in list(state.pending):
                if req.future and not req.future.done():
                    req.future.cancel()
        await self._disconnect_user_stream(user_id, chat_id)
        logger.info(f"Cleared stream for user {user_id} chat {chat_id or '*'}")
    def clear_pending_permissions(self, user_id: int, chat_id: Optional[int] = None) -> None:
        """Clear pending permission futures for a user."""
        for state in self._states_for_user(user_id, chat_id):
            for req in list(state.pending):
                if req.future and not req.future.done():
                    req.future.cancel()
            logger.info(f"Cleared pending permissions for user {user_id} chat {chat_id or '*'}")
