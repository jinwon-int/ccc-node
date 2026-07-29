"""Stream state/control mixin for ProjectChatHandler."""

import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from telegram_bot.core.agent_session_registry import (
    AgentSessionRegistry,
    IdleSessionCandidate,
    StreamKey,
)
from telegram_bot.utils.secure_fs import (
    _atomic_write_bytes,
    _secure_existing_state_file,
    ensure_private_directory,
)
from telegram_bot.utils.session_resource_guard import process_tree_rss_mb

logger = logging.getLogger(__name__)


class FollowupQueueCorruptionError(RuntimeError):
    """Raised when the durable follow-up queue cannot be trusted."""


@dataclass(frozen=True, slots=True)
class QueuedFollowup:
    """One durable Telegram update waiting for its conversation turn."""

    item_id: int
    conversation_key: str
    handler: str
    update_json: str
    enqueued_at: float


class PersistentFollowupQueue:
    """Small owner-only, atomically-written FIFO for follow-up updates.

    The cap is enforced independently for every normalized conversation key.
    Mutations reload the file while holding a process-local lock, then replace
    it atomically. A malformed file is never overwritten: callers must fail
    closed so an operator can recover the original evidence.
    """

    _VERSION = 1
    _HANDLERS = frozenset({"document", "photo", "sticker", "text", "voice"})

    def __init__(self, path: Path, *, per_chat_cap: int) -> None:
        if per_chat_cap < 1:
            raise ValueError("follow-up queue cap must be at least 1")
        self.path = Path(path)
        self.per_chat_cap = int(per_chat_cap)
        self._lock = threading.Lock()

    @staticmethod
    def _empty_data() -> dict[str, Any]:
        return {"version": PersistentFollowupQueue._VERSION, "next_id": 1, "items": []}

    def initialize(self) -> None:
        """Validate storage without discarding an existing queue."""

        ensure_private_directory(self.path.parent)
        _secure_existing_state_file(self.path)
        with self._lock:
            self._read_data()

    def _read_data(self) -> dict[str, Any]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return self._empty_data()
        except OSError as exc:
            raise FollowupQueueCorruptionError(
                "durable follow-up queue cannot be read"
            ) from exc

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FollowupQueueCorruptionError(
                "durable follow-up queue is not valid JSON"
            ) from exc
        if (
            not isinstance(parsed, dict)
            or parsed.get("version") != self._VERSION
            or not isinstance(parsed.get("next_id"), int)
            or parsed["next_id"] < 1
            or not isinstance(parsed.get("items"), list)
        ):
            raise FollowupQueueCorruptionError(
                "durable follow-up queue has an invalid schema"
            )

        seen_ids: set[int] = set()
        previous_id = 0
        for item in parsed["items"]:
            if not isinstance(item, dict):
                raise FollowupQueueCorruptionError(
                    "durable follow-up queue item is invalid"
                )
            item_id = item.get("id")
            conversation_key = item.get("conversation_key")
            handler = item.get("handler")
            update_json = item.get("update_json")
            enqueued_at = item.get("enqueued_at")
            if (
                not isinstance(item_id, int)
                or item_id < 1
                or item_id in seen_ids
                or item_id <= previous_id
                or not isinstance(conversation_key, str)
                or not conversation_key
                or handler not in self._HANDLERS
                or not isinstance(update_json, str)
                or not update_json
                or not isinstance(enqueued_at, (int, float))
            ):
                raise FollowupQueueCorruptionError(
                    "durable follow-up queue item is invalid"
                )
            try:
                decoded_update = json.loads(update_json)
            except json.JSONDecodeError as exc:
                raise FollowupQueueCorruptionError(
                    "durable follow-up queue update is invalid"
                ) from exc
            if not isinstance(decoded_update, dict):
                raise FollowupQueueCorruptionError(
                    "durable follow-up queue update is invalid"
                )
            seen_ids.add(item_id)
            previous_id = item_id

        if seen_ids and parsed["next_id"] <= max(seen_ids):
            raise FollowupQueueCorruptionError(
                "durable follow-up queue sequence is invalid"
            )
        return parsed

    def _write_data(self, data: dict[str, Any]) -> None:
        payload = json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        _atomic_write_bytes(self.path, payload)

    @staticmethod
    def _item_from_data(item: dict[str, Any]) -> QueuedFollowup:
        return QueuedFollowup(
            item_id=int(item["id"]),
            conversation_key=str(item["conversation_key"]),
            handler=str(item["handler"]),
            update_json=str(item["update_json"]),
            enqueued_at=float(item["enqueued_at"]),
        )

    def enqueue(
        self,
        *,
        conversation_key: str,
        handler: str,
        update_json: str,
        enqueued_at: float,
    ) -> tuple[QueuedFollowup | None, int]:
        """Persist one item and return ``(item, position)``.

        ``item`` is ``None`` when the per-chat cap is already reached; no file
        mutation occurs in that case.
        """

        if handler not in self._HANDLERS:
            raise ValueError(f"unsupported follow-up handler: {handler}")
        decoded_update = json.loads(update_json)
        if not isinstance(decoded_update, dict):
            raise ValueError("serialized Telegram update must be a JSON object")

        with self._lock:
            data = self._read_data()
            matching = [
                item
                for item in data["items"]
                if item["conversation_key"] == conversation_key
            ]
            if len(matching) >= self.per_chat_cap:
                return None, len(matching)
            item_data = {
                "id": data["next_id"],
                "conversation_key": conversation_key,
                "handler": handler,
                "update_json": update_json,
                "enqueued_at": float(enqueued_at),
            }
            data["next_id"] += 1
            data["items"].append(item_data)
            self._write_data(data)
            return self._item_from_data(item_data), len(matching) + 1

    def peek(self, conversation_key: str) -> QueuedFollowup | None:
        """Return the oldest queued item for one conversation."""

        with self._lock:
            data = self._read_data()
            for item in data["items"]:
                if item["conversation_key"] == conversation_key:
                    return self._item_from_data(item)
        return None

    def acknowledge(self, item_id: int) -> bool:
        """Remove exactly one successfully processed item."""

        with self._lock:
            data = self._read_data()
            remaining = [item for item in data["items"] if item["id"] != item_id]
            if len(remaining) == len(data["items"]):
                return False
            data["items"] = remaining
            self._write_data(data)
            return True

    def depth(self, conversation_key: str) -> int:
        with self._lock:
            data = self._read_data()
            return sum(
                item["conversation_key"] == conversation_key
                for item in data["items"]
            )

    def conversation_keys(self) -> tuple[str, ...]:
        """Return keys in first-arrival order."""

        with self._lock:
            data = self._read_data()
            return tuple(
                dict.fromkeys(item["conversation_key"] for item in data["items"])
            )

    def clear(self, conversation_key: str) -> int:
        """Remove all queued items for an explicit /stop request."""

        with self._lock:
            data = self._read_data()
            remaining = [
                item
                for item in data["items"]
                if item["conversation_key"] != conversation_key
            ]
            cleared = len(data["items"]) - len(remaining)
            if cleared:
                data["items"] = remaining
                self._write_data(data)
            return cleared


class ProjectChatStateMixin:
    _agent_interrupt_timeout_seconds: float
    _agent_runtime: Any
    _agent_runtime_closed: bool
    _agent_runtime_recycle_pending: bool
    _agent_session_attachments: int
    _agent_session_registry: AgentSessionRegistry
    _config: Any
    _session_guard_evictions: int
    _session_guard_last_tree_rss_mb: float
    _session_guard_lock: asyncio.Lock
    _session_guard_runtime_recycles: int

    def _stream_key(self, user_id: int, chat_id: int) -> StreamKey:
        """Implemented by the ProjectChatHandler composition root."""

        raise NotImplementedError

    def session_resource_snapshot(self) -> dict[str, int | float]:
        """Return body-free counters for health.json and fleet diagnostics."""

        metrics = self._agent_session_registry.metrics()
        return {
            "resident_sessions": metrics.resident_sessions,
            "active_sessions": metrics.active_sessions,
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
            metrics = self._agent_session_registry.metrics()
            idle = self._agent_session_registry.idle_lru_candidates()

            ttl = int(getattr(self._config, "session_idle_ttl_seconds", 4 * 60 * 60))
            candidates: list[IdleSessionCandidate] = [
                candidate
                for candidate in idle
                if current - candidate.last_used_at >= ttl
            ]

            maximum = int(getattr(self._config, "max_resident_sessions", 2))
            projected = metrics.resident_sessions - len(candidates)
            if projected > maximum:
                for candidate in idle:
                    if candidate in candidates:
                        continue
                    candidates.append(candidate)
                    projected -= 1
                    if projected <= maximum:
                        break

            rss_mb = await asyncio.to_thread(process_tree_rss_mb)
            self._session_guard_last_tree_rss_mb = rss_mb
            rss_limit = int(
                getattr(self._config, "session_tree_rss_limit_mb", 1024)
            )
            memory_high = rss_limit > 0 and rss_mb >= rss_limit
            if memory_high and metrics.active_sessions == 0:
                for candidate in idle:
                    if candidate not in candidates:
                        candidates.append(candidate)

            evicted = 0
            for candidate in candidates:
                entry = self._agent_session_registry.drop_idle_cached_if_same(
                    candidate
                )
                if entry is None:
                    continue
                await self._close_agent_session(entry.session)
                evicted += 1
            if evicted:
                self._session_guard_evictions += evicted
                if getattr(self._config, "agent_provider", "claude") == "codex":
                    self._agent_runtime_recycle_pending = True
                resident = (
                    self._agent_session_registry.metrics().resident_sessions
                )
                logger.info(
                    "Session resource guard evicted %d idle session(s); "
                    "resident=%d rss_mb=%.1f",
                    evicted,
                    resident,
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
                and self._agent_session_registry.metrics().active_sessions == 0
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
        if self._agent_session_registry.metrics().active_sessions == 0:
            await self._recycle_agent_runtime_locked("attachment-cap")

    async def _recycle_agent_runtime_locked(self, reason: str) -> bool:
        """Restart a shared idle runtime while preserving durable thread ids."""

        if self._agent_session_registry.metrics().active_sessions:
            self._agent_runtime_recycle_pending = True
            return False
        recycle = getattr(self._agent_runtime, "recycle", None)
        if not callable(recycle):
            logger.warning("Session resource guard cannot recycle this agent runtime")
            return False

        # Cached CodexSession wrappers belong to the old app-server connection.
        # The bot/session store still owns their durable thread ids, so the next
        # message reconstructs a wrapper through thread/resume.
        if self._agent_session_registry.clear_cached_if_idle() is None:
            self._agent_runtime_recycle_pending = True
            return False
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

    def is_agent_approval_active(
        self, user_id: int, chat_id: int, generation: int
    ) -> bool:
        key = self._stream_key(user_id, chat_id)
        return (
            not self._agent_runtime_closed
            and self._agent_session_registry.approval_is_active(key, generation)
        )

    def invalidate_agent_approvals(
        self, user_id: int, chat_id: Optional[int] = None
    ) -> None:
        self._agent_session_registry.invalidate_approvals(
            self._agent_keys_for_user(user_id, chat_id)
        )

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
        # The resource guard also serializes session attachment. Holding it
        # through interrupt prevents a stale handle for turn A from reaching a
        # newly registered turn B that reused the same session wrapper.
        async with self._session_guard_lock:
            keys = self._agent_keys_for_user(user_id, chat_id)
            handles = self._agent_session_registry.active_handles_for_keys(keys)
            self._agent_session_registry.invalidate_approvals(keys)
            current = tuple(
                owned
                for handle in handles
                if (
                    owned := self._agent_session_registry.active_handle_if_same(
                        handle.token
                    )
                )
                is not None
            )
            await asyncio.gather(
                *(
                    self._interrupt_agent_session(handle.session)
                    for handle in current
                )
            )
            for handle in handles:
                self._agent_session_registry.deactivate_if_same(handle.token)
            return bool(handles)

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

    def busy_for_seconds(
        self,
        user_id: int,
        chat_id: int,
        now: float,
    ) -> float | None:
        started_at = self._agent_session_registry.active_started_at(
            self._stream_key(user_id, chat_id)
        )
        if started_at is None:
            return None
        return max(0.0, float(now) - started_at)

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

    async def _drop_agent_session(
        self,
        key: StreamKey,
        session: Any = None,
    ) -> None:
        """Evict and close the cached agent-session entry for ``key``.

        With ``session`` given, evict only while the cached entry still wraps
        that exact session object, so a concurrent turn's fresh entry is never
        removed by a stale turn's cleanup. Provider sessions without a
        conversation-local ``close`` method (currently Codex) remain a cheap
        wrapper over their shared runtime.
        """
        cached = self._agent_session_registry.get_cached(key)
        if cached is None:
            return
        if session is not None and cached.entry.session is not session:
            return
        entry = self._agent_session_registry.drop_cached_if_same(
            key,
            cached.token,
        )
        if entry is not None:
            await self._close_agent_session(entry.session)

    async def _close_agent_session(self, session: Any) -> None:
        close = getattr(session, "close", None)
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

    def _agent_keys_for_user(
        self,
        user_id: int,
        chat_id: Optional[int] = None,
    ) -> tuple[StreamKey, ...]:
        if chat_id is not None:
            return (self._stream_key(user_id, chat_id),)
        return self._agent_session_registry.keys_for_user(user_id)

    def _active_agent_sessions_for_user(
        self,
        user_id: int,
        chat_id: Optional[int] = None,
    ) -> list[Any]:
        keys = self._agent_keys_for_user(user_id, chat_id)
        return [
            handle.session
            for handle in self._agent_session_registry.active_handles_for_keys(keys)
        ]

    def has_live_agent_session(self, user_id: int, chat_id: int) -> bool:
        """Typed ownership seam used by dead-session wakeup."""

        return self._agent_session_registry.has_live_owner(
            self._stream_key(user_id, chat_id)
        )

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
        async with self._session_guard_lock:
            if self._agent_runtime is None or self._agent_runtime_closed:
                return
            self._agent_runtime_closed = True
            handles = self._agent_session_registry.prepare_close()
            current = tuple(
                owned
                for handle in handles
                if (
                    owned := self._agent_session_registry.active_handle_if_same(
                        handle.token
                    )
                )
                is not None
            )
            await asyncio.gather(
                *(
                    self._interrupt_agent_session(handle.session)
                    for handle in current
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
