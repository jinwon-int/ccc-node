"""Stream state/control mixin for ProjectChatHandler."""

# mypy: disable-error-code="attr-defined,has-type"

import asyncio
import logging
from typing import Optional

from telegram_bot.utils.session_resource_guard import process_tree_rss_mb

logger = logging.getLogger(__name__)


class ProjectChatStateMixin:
    def session_resource_snapshot(self) -> dict[str, int | float]:
        """Return body-free counters for health.json and fleet diagnostics."""

        return {
            "resident_sessions": len(self._agent_sessions),
            "active_sessions": len(self._agent_active_sessions),
            "tree_rss_mb": self._session_guard_last_tree_rss_mb,
            "evictions": self._session_guard_evictions,
            "runtime_recycles": self._session_guard_runtime_recycles,
            "codex_attachments": self._agent_session_attachments,
        }

    async def enforce_session_resource_limits(
        self, *, now: float | None = None
    ) -> dict[str, int | float]:
        """Close least-recently-used idle sessions and recycle idle Codex.

        The provider's durable session id lives in ``sessions.json`` and is not
        changed here. A later message therefore resumes the same conversation.
        Active turns are never evicted or used as a runtime-recycle boundary.
        """

        if not bool(getattr(self._config, "session_guard_enabled", False)):
            return self.session_resource_snapshot()
        current = asyncio.get_running_loop().time() if now is None else float(now)
        async with self._session_guard_lock:
            active_keys = set(self._agent_active_sessions)
            idle = [
                (key, entry)
                for key, entry in self._agent_sessions.items()
                if key not in active_keys
            ]
            idle.sort(key=lambda item: float(getattr(item[1], "last_used_at", 0.0)))

            ttl = int(getattr(self._config, "session_idle_ttl_seconds", 4 * 60 * 60))
            candidates: list[object] = [
                key
                for key, entry in idle
                if current - float(getattr(entry, "last_used_at", 0.0)) >= ttl
            ]

            maximum = int(getattr(self._config, "max_resident_sessions", 2))
            projected = len(self._agent_sessions) - len(candidates)
            if projected > maximum:
                for key, _entry in idle:
                    if key in candidates:
                        continue
                    candidates.append(key)
                    projected -= 1
                    if projected <= maximum:
                        break

            rss_mb = await asyncio.to_thread(process_tree_rss_mb)
            self._session_guard_last_tree_rss_mb = rss_mb
            rss_limit = int(
                getattr(self._config, "session_tree_rss_limit_mb", 1024)
            )
            memory_high = rss_limit > 0 and rss_mb >= rss_limit
            if memory_high and not active_keys:
                for key, _entry in idle:
                    if key not in candidates:
                        candidates.append(key)

            evicted = 0
            for key in candidates:
                if key in self._agent_active_sessions:
                    continue
                before = len(self._agent_sessions)
                await self._drop_agent_session(key)
                evicted += int(len(self._agent_sessions) < before)
            if evicted:
                self._session_guard_evictions += evicted
                if getattr(self._config, "agent_provider", "claude") == "codex":
                    self._agent_runtime_recycle_pending = True
                logger.info(
                    "Session resource guard evicted %d idle session(s); "
                    "resident=%d rss_mb=%.1f",
                    evicted,
                    len(self._agent_sessions),
                    rss_mb,
                )

            attachment_limit = int(
                getattr(self._config, "codex_max_session_attachments", 2)
            )
            attachment_high = (
                attachment_limit > 0
                and self._agent_session_attachments >= attachment_limit
            )
            if (
                getattr(self._config, "agent_provider", "claude") == "codex"
                and not self._agent_active_sessions
                and (
                    memory_high
                    or attachment_high
                    or self._agent_runtime_recycle_pending
                )
            ):
                reason = (
                    "rss-high"
                    if memory_high
                    else "attachment-cap"
                    if attachment_high
                    else "idle-eviction"
                )
                await self._recycle_agent_runtime_locked(reason)
            return self.session_resource_snapshot()

    async def _prepare_agent_session_start_locked(self) -> None:
        """Recycle before a new Codex attachment would exceed the hard cap."""

        if not bool(getattr(self._config, "session_guard_enabled", False)):
            return
        if getattr(self._config, "agent_provider", "claude") != "codex":
            return
        limit = int(getattr(self._config, "codex_max_session_attachments", 2))
        if limit <= 0 or self._agent_session_attachments < limit:
            return
        self._agent_runtime_recycle_pending = True
        if not self._agent_active_sessions:
            await self._recycle_agent_runtime_locked("attachment-cap")

    async def _recycle_agent_runtime_locked(self, reason: str) -> bool:
        """Restart a shared idle runtime while preserving durable thread ids."""

        if self._agent_active_sessions:
            self._agent_runtime_recycle_pending = True
            return False
        recycle = getattr(self._agent_runtime, "recycle", None)
        if not callable(recycle):
            logger.warning("Session resource guard cannot recycle this agent runtime")
            return False

        # Cached CodexSession wrappers belong to the old app-server connection.
        # The bot/session store still owns their durable thread ids, so the next
        # message reconstructs a wrapper through thread/resume.
        self._agent_sessions.clear()
        try:
            recycled = await asyncio.wait_for(recycle(), timeout=15.0)
        except TimeoutError:
            logger.warning("Agent runtime recycle timed out after 15s")
            return False
        except Exception:
            logger.exception("Agent runtime recycle failed")
            return False
        if not recycled:
            return False
        self._agent_session_attachments = 0
        self._agent_runtime_recycle_pending = False
        self._session_guard_runtime_recycles += 1
        logger.info("Session resource guard recycled idle runtime reason=%s", reason)
        return True

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
            await self._drop_agent_session(key)

    def clear_pending_permissions(self, user_id: int, chat_id: Optional[int] = None) -> None:
        """Compatibility no-op retained for the bot layer (#584 slice C-2).

        Legacy per-request permission futures lived on the removed direct SDK
        path's pending FIFO; runtime-path approvals are invalidated through
        ``invalidate_agent_approvals`` generations instead.
        """
        del user_id, chat_id

    async def _drop_agent_session(self, key, session=None) -> None:
        """Evict and close the cached agent-session entry for ``key``.

        With ``session`` given, evict only while the cached entry still wraps
        that exact session object, so a concurrent turn's fresh entry is never
        removed by a stale turn's cleanup. Provider sessions without a
        conversation-local ``close`` method (currently Codex) remain a cheap
        wrapper over their shared runtime.
        """
        entry = self._agent_sessions.get(key)
        if entry is None:
            return
        if session is not None and entry.session is not session:
            return
        self._agent_sessions.pop(key, None)
        close = getattr(entry.session, "close", None)
        if not callable(close):
            return
        try:
            await asyncio.wait_for(
                close(), timeout=self._agent_interrupt_timeout_seconds
            )
        except TimeoutError:
            logger.warning(
                "Agent session close timed out after %.1fs",
                self._agent_interrupt_timeout_seconds,
            )
        except Exception:
            logger.exception("Agent session close failed")

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
