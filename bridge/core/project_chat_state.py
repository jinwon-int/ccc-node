"""Stream state/control mixin for ProjectChatHandler."""

# mypy: disable-error-code="attr-defined,has-type"

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class ProjectChatStateMixin:
    def _next_agent_generation(self, key) -> int:
        generation = self._agent_generation_counters.get(key, 0) + 1
        self._agent_generation_counters[key] = generation
        return generation

    def is_agent_approval_active(
        self, user_id: int, chat_id: int, generation: int
    ) -> bool:
        key = self._stream_key(user_id, chat_id)
        return (
            not self._agent_runtime_closed
            and self._agent_active_generations.get(key) == generation
            and key in self._agent_active_sessions
        )

    def invalidate_agent_approvals(
        self, user_id: int, chat_id: Optional[int] = None
    ) -> None:
        for key in self._agent_keys_for_user(user_id, chat_id):
            self._next_agent_generation(key)
            self._agent_active_generations.pop(key, None)

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
        """Interrupt the active agent session(s) for a user/conversation."""
        sessions = self._active_agent_sessions_for_user(user_id, chat_id)
        self.invalidate_agent_approvals(user_id, chat_id)
        await asyncio.gather(
            *(self._interrupt_agent_session(session) for session in sessions)
        )
        for key in self._agent_keys_for_user(user_id, chat_id):
            if self._agent_active_sessions.get(key) in sessions:
                self._agent_active_sessions.pop(key, None)
                self._agent_started_at.pop(key, None)
                self._agent_waiting_for_turn.discard(key)
        return bool(sessions)

    async def cancel_user_streaming(self, user_id: int, chat_id: Optional[int] = None) -> bool:
        """Compatibility no-op retained for the bot layer (#584 slice C-2).

        On the runtime path streaming drafts are cancelled by the turn's own
        cancellation handling (``_cancel_agent_streaming`` when the awaiting
        task is cancelled); there is no request FIFO to sweep here anymore.
        """
        del user_id, chat_id
        return False

    def inflight_count(self, user_id: int, chat_id: Optional[int] = None) -> int:
        return len(self._active_agent_sessions_for_user(user_id, chat_id))

    def is_user_busy(self, user_id: int, chat_id: Optional[int] = None) -> bool:
        return self.inflight_count(user_id, chat_id) > 0

    async def clear_user_stream(self, user_id: int, chat_id: Optional[int] = None) -> None:
        """Interrupt and evict agent session(s) so the next turn starts fresh
        (used by /revert)."""
        await self.stop(user_id, chat_id)
        for key in self._agent_keys_for_user(user_id, chat_id):
            self._drop_agent_session(key)

    def clear_pending_permissions(self, user_id: int, chat_id: Optional[int] = None) -> None:
        """Compatibility no-op retained for the bot layer (#584 slice C-2).

        Legacy per-request permission futures lived on the removed direct SDK
        path's pending FIFO; runtime-path approvals are invalidated through
        ``invalidate_agent_approvals`` generations instead.
        """
        del user_id, chat_id

    def _drop_agent_session(self, key, session=None) -> None:
        """Evict the cached agent-session entry for ``key``.

        With ``session`` given, evict only while the cached entry still wraps
        that exact session object, so a concurrent turn's fresh entry is never
        removed by a stale turn's cleanup.
        """
        entry = self._agent_sessions.get(key)
        if entry is None:
            return
        if session is not None and entry.session is not session:
            return
        self._agent_sessions.pop(key, None)

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
        self._agent_active_generations.clear()
        self._agent_waiting_for_turn.clear()
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
