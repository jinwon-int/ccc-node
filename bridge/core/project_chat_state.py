"""Stream state/control mixin for ProjectChatHandler."""

# mypy: disable-error-code="attr-defined"

import asyncio
import logging
from typing import List, Optional

from telegram_bot.core.project_chat_types import _UserStreamState
from telegram_bot.core.sdk_text import TASK_TERMINATED_NOTICE

logger = logging.getLogger(__name__)


class ProjectChatStateMixin:
    def _require_runtime(self):
        if self._agent_runtime is None or self._agent_runtime_closed:
            raise RuntimeError("Agent runtime is unavailable")
        return self._agent_runtime

    def _require_session_browser(self):
        runtime = self._require_runtime()
        if not getattr(runtime, "supports_session_browsing", False):
            raise RuntimeError("Session browsing is unavailable for this provider")
        return runtime

    async def list_runtime_sessions(self, *, limit: int = 10):
        """List sessions through the active provider runtime."""

        return await self._require_session_browser().list_sessions(limit=limit)

    async def read_runtime_session(self, session_id: str, *, limit: int = 5):
        """Read user-visible history through the active provider runtime."""

        return await self._require_session_browser().read_session(session_id, limit=limit)

    async def list_runtime_models(self):
        """List models through the active provider runtime."""

        return await self._require_runtime().list_models()

    async def stop(self, user_id: int, chat_id: Optional[int] = None) -> bool:
        """Stop active stream(s) for a user and fail pending requests.

        The default notice is upgraded to a specific reason by
        ``_disconnect_stream_state`` when a recent SDK/stream error (usage limit,
        auth, network) is what actually caused the stop.
        """
        if self._agent_runtime is not None:
            sessions = self._active_agent_sessions_for_user(user_id, chat_id)
            await asyncio.gather(
                *(self._interrupt_agent_session(session) for session in sessions)
            )
            for key in self._agent_keys_for_user(user_id, chat_id):
                if self._agent_active_sessions.get(key) in sessions:
                    self._agent_active_sessions.pop(key, None)
                    self._agent_started_at.pop(key, None)
            return bool(sessions)
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
        if self._agent_runtime is not None:
            return len(self._active_agent_sessions_for_user(user_id, chat_id))
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
        if self._agent_runtime is not None:
            await self.stop(user_id, chat_id)
            for key in self._agent_keys_for_user(user_id, chat_id):
                self._agent_sessions.pop(key, None)
                self._agent_session_models.pop(key, None)
            return
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

    def _agent_keys_for_user(self, user_id: int, chat_id: Optional[int] = None):
        if chat_id is not None:
            return [self._stream_key(user_id, chat_id)]
        return [key for key in self._agent_sessions if key[0] == user_id]

    def _active_agent_sessions_for_user(self, user_id: int, chat_id: Optional[int] = None):
        keys = self._agent_keys_for_user(user_id, chat_id)
        return [self._agent_active_sessions[key] for key in keys if key in self._agent_active_sessions]

    async def _interrupt_agent_session(self, session) -> None:
        try:
            await asyncio.wait_for(
                session.interrupt(), timeout=self._agent_interrupt_timeout_seconds
            )
        except TimeoutError:
            logger.warning(
                "Agent session interrupt timed out after %.1fs",
                self._agent_interrupt_timeout_seconds,
            )
        except Exception:
            logger.exception("Failed to interrupt agent session")

    async def close(self) -> None:
        """Interrupt active Codex turns and close its shared runtime once."""
        if self._agent_runtime is None or self._agent_runtime_closed:
            return
        self._agent_runtime_closed = True
        await asyncio.gather(
            *(
                self._interrupt_agent_session(session)
                for session in tuple(self._agent_active_sessions.values())
            )
        )
        close = getattr(self._agent_runtime, "close", None)
        if close is not None:
            try:
                await asyncio.wait_for(close(), timeout=10.0)
            except TimeoutError:
                logger.warning("Agent runtime close timed out after 10s")
            except Exception:
                logger.exception("Agent runtime close failed")
