"""Stream state/control mixin for ProjectChatHandler."""

import asyncio
import bisect
import json
import logging
import threading
import time
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
from telegram_bot.utils.session_resource_guard import (
    host_memory_pressure,
    process_tree_rss_mb,
)

logger = logging.getLogger(__name__)


class FollowupQueueCorruptionError(RuntimeError):
    """Raised when the durable follow-up queue cannot be trusted."""


class FollowupNotificationCapacityError(RuntimeError):
    """Raised when retaining another failure receipt would exceed its cap."""


@dataclass(frozen=True, slots=True)
class QueuedFollowup:
    """One durable Telegram update waiting for its conversation turn."""

    item_id: int
    conversation_key: str
    handler: str
    update_json: str
    enqueued_at: float
    retry_count: int


@dataclass(frozen=True, slots=True)
class FollowupQueueClearResult:
    """Counts removed by an explicit conversation-scoped purge."""

    queued_items: int
    failure_notifications: int

    @property
    def total(self) -> int:
        return self.queued_items + self.failure_notifications


@dataclass(frozen=True, slots=True)
class FollowupQueueMetrics:
    """Body-free process-lifetime counters for queue safety events."""

    failure_notification_evictions: int


class PersistentFollowupQueue:
    """Small owner-only, atomically-written FIFO for follow-up updates.

    The cap is enforced independently for every normalized conversation key.
    Mutations reload the file while holding a process-local lock, then replace
    it atomically. A malformed file is never overwritten: callers must fail
    closed so an operator can recover the original evidence.
    """

    _VERSION = 1
    _HANDLERS = frozenset({"document", "photo", "sticker", "text", "voice"})

    _UNSORTED_BACKUP_SUFFIX = ".pre-796-unsorted"

    def __init__(
        self,
        path: Path,
        *,
        per_chat_cap: int,
        failure_notification_cap: int = 32,
    ) -> None:
        if per_chat_cap < 1:
            raise ValueError("follow-up queue cap must be at least 1")
        if failure_notification_cap < 1:
            raise ValueError("failure-notification cap must be at least 1")
        self.path = Path(path)
        self.per_chat_cap = int(per_chat_cap)
        self.failure_notification_cap = int(failure_notification_cap)
        self._lock = threading.Lock()
        self._failure_notification_evictions = 0

    @staticmethod
    def _empty_data() -> dict[str, Any]:
        return {
            "version": PersistentFollowupQueue._VERSION,
            "next_id": 1,
            "items": [],
            "failure_notifications": [],
        }

    def initialize(self) -> None:
        """Validate storage and narrowly repair pre-#796 notice ordering.

        The old writer could append otherwise-valid failure notifications out
        of ID order. That one known shape is sorted only after the exact source
        bytes are preserved in an owner-only sidecar. Every other malformed
        shape remains fail-closed.
        """

        ensure_private_directory(self.path.parent)
        _secure_existing_state_file(self.path)
        _secure_existing_state_file(self.migration_backup_path)
        with self._lock:
            raw = self._read_raw()
            if raw is None:
                return
            try:
                self._decode_data(raw)
                return
            except FollowupQueueCorruptionError as exc:
                strict_failure = exc
                try:
                    parsed = self._decode_data(
                        raw,
                        allow_unsorted_failure_notifications=True,
                    )
                except FollowupQueueCorruptionError:
                    raise strict_failure

            notifications = parsed["failure_notifications"]
            notification_ids = [item["id"] for item in notifications]
            if notification_ids == sorted(notification_ids):
                raise strict_failure

            backup_path = self.migration_backup_path
            try:
                preserved = backup_path.read_bytes()
            except FileNotFoundError:
                _atomic_write_bytes(backup_path, raw)
            except OSError as exc:
                raise FollowupQueueCorruptionError(
                    "pre-#796 follow-up queue backup cannot be read"
                ) from exc
            else:
                if preserved != raw:
                    raise FollowupQueueCorruptionError(
                        "pre-#796 follow-up queue backup already contains "
                        "different evidence"
                    )

            notifications.sort(key=lambda item: item["id"])
            self._write_data(parsed)
            logger.warning(
                "Migrated %s out-of-order durable follow-up failure "
                "notification(s); exact source preserved at %s",
                len(notifications),
                backup_path,
            )

    @property
    def migration_backup_path(self) -> Path:
        return self.path.with_name(self.path.name + self._UNSORTED_BACKUP_SUFFIX)

    def _read_raw(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise FollowupQueueCorruptionError(
                "durable follow-up queue cannot be read"
            ) from exc

    def _decode_data(
        self,
        raw: bytes,
        *,
        allow_unsorted_failure_notifications: bool = False,
    ) -> dict[str, Any]:
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
            or not isinstance(parsed.get("failure_notifications", []), list)
        ):
            raise FollowupQueueCorruptionError(
                "durable follow-up queue has an invalid schema"
            )

        failure_notifications = parsed.setdefault("failure_notifications", [])
        all_ids: set[int] = set()
        for collection in (parsed["items"], failure_notifications):
            allow_unsorted = (
                allow_unsorted_failure_notifications
                and collection is failure_notifications
            )
            previous_id = 0
            collection_ids: set[int] = set()
            for item in collection:
                if not isinstance(item, dict):
                    raise FollowupQueueCorruptionError(
                        "durable follow-up queue item is invalid"
                    )
                item_id = item.get("id")
                conversation_key = item.get("conversation_key")
                handler = item.get("handler")
                update_json = item.get("update_json")
                enqueued_at = item.get("enqueued_at")
                retry_count = item.get("retry_count", 0)
                if (
                    not isinstance(item_id, int)
                    or item_id < 1
                    or item_id in collection_ids
                    or (not allow_unsorted and item_id <= previous_id)
                    or not isinstance(conversation_key, str)
                    or not conversation_key
                    or handler not in self._HANDLERS
                    or not isinstance(update_json, str)
                    or not update_json
                    or not isinstance(enqueued_at, (int, float))
                    or not isinstance(retry_count, int)
                    or isinstance(retry_count, bool)
                    or retry_count < 0
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
                collection_ids.add(item_id)
                all_ids.add(item_id)
                previous_id = item_id
        if all_ids and parsed["next_id"] <= max(all_ids):
            raise FollowupQueueCorruptionError(
                "durable follow-up queue sequence is invalid"
            )
        return parsed

    def _read_data(self) -> dict[str, Any]:
        raw = self._read_raw()
        if raw is None:
            return self._empty_data()
        return self._decode_data(raw)

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
            retry_count=int(item.get("retry_count", 0)),
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
                "retry_count": 0,
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

    def stage_failure_notification(self, item_id: int) -> QueuedFollowup | None:
        """Durably retain the outcome notice before discarding one item.

        At the separate notice cap, the newest staged candidate is the only
        eviction victim. It is rejected before commit, so the matching queued
        item remains the durable source of truth and no older promised receipt
        is displaced. Automatic cap enforcement never destroys a committed
        promised receipt; only an explicit user purge may do that.
        """

        with self._lock:
            data = self._read_data()
            existing = next(
                (
                    item
                    for item in data["failure_notifications"]
                    if item["id"] == item_id
                ),
                None,
            )
            if existing is not None:
                return self._item_from_data(existing)
            failed = next(
                (item for item in data["items"] if item["id"] == item_id),
                None,
            )
            if failed is None:
                return None
            retained_for_conversation = sum(
                item["conversation_key"] == failed["conversation_key"]
                for item in data["failure_notifications"]
            )
            if retained_for_conversation >= self.failure_notification_cap:
                self._failure_notification_evictions += 1
                logger.warning(
                    "Durable follow-up failure-notification cap reached for "
                    "%s; evicted newest staged candidate %s before commit "
                    "(cap=%s, failure_notification_evictions=%s); matching "
                    "queued item retained",
                    failed["conversation_key"],
                    failed["id"],
                    self.failure_notification_cap,
                    self._failure_notification_evictions,
                )
                raise FollowupNotificationCapacityError(
                    "failure-notification cap reached; queued item retained"
                )
            notification = dict(failed)
            # Notices are staged in discard order, but _read_data requires each
            # collection to be strictly id-ascending. Appending would therefore
            # write a file that fails our own validator as soon as two
            # conversations discard out of enqueue order, corrupting the shared
            # queue for every chat. Insert in id order instead.
            retained = data["failure_notifications"]
            position = bisect.bisect_left(
                [item["id"] for item in retained], notification["id"]
            )
            retained.insert(position, notification)
            self._write_data(data)
            return self._item_from_data(notification)

    def peek_failure_notification(
        self, conversation_key: str
    ) -> QueuedFollowup | None:
        """Return the oldest deliverable discard notice for one conversation.

        A notice staged before a crash is not deliverable until its matching
        queue item has actually been removed.
        """

        with self._lock:
            data = self._read_data()
            queued_ids = {item["id"] for item in data["items"]}
            for item in data["failure_notifications"]:
                if (
                    item["conversation_key"] == conversation_key
                    and item["id"] not in queued_ids
                ):
                    return self._item_from_data(item)
        return None

    def acknowledge_failure_notification(self, item_id: int) -> bool:
        """Remove one outcome notice after Telegram accepted it."""

        with self._lock:
            data = self._read_data()
            remaining = [
                item
                for item in data["failure_notifications"]
                if item["id"] != item_id
            ]
            if len(remaining) == len(data["failure_notifications"]):
                return False
            data["failure_notifications"] = remaining
            self._write_data(data)
            return True

    def record_failure(self, item_id: int) -> QueuedFollowup | None:
        """Durably increment one item's retry counter."""

        with self._lock:
            data = self._read_data()
            failed = next(
                (item for item in data["items"] if item["id"] == item_id),
                None,
            )
            if failed is None:
                return None
            failed["retry_count"] = int(failed.get("retry_count", 0)) + 1
            self._write_data(data)
            return self._item_from_data(failed)

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
                dict.fromkeys(
                    item["conversation_key"]
                    for item in sorted(
                        data["items"] + data["failure_notifications"],
                        key=lambda candidate: candidate["id"],
                    )
                )
            )

    def failure_notification_depth(self, conversation_key: str) -> int:
        """Count retained outcome notices for one conversation."""

        with self._lock:
            data = self._read_data()
            return sum(
                item["conversation_key"] == conversation_key
                for item in data["failure_notifications"]
            )

    def metrics(self) -> FollowupQueueMetrics:
        """Return body-free queue safety counters."""

        with self._lock:
            return FollowupQueueMetrics(
                failure_notification_evictions=(
                    self._failure_notification_evictions
                )
            )

    def clear(self, conversation_key: str) -> FollowupQueueClearResult:
        """Purge queued work and retained outcomes for an explicit /stop.

        A user-authorized stop is the sole path permitted to destroy committed
        promised receipts, so its return value reports those separately.
        """

        with self._lock:
            data = self._read_data()
            remaining_items = [
                item
                for item in data["items"]
                if item["conversation_key"] != conversation_key
            ]
            remaining_notifications = [
                item
                for item in data["failure_notifications"]
                if item["conversation_key"] != conversation_key
            ]
            cleared = FollowupQueueClearResult(
                queued_items=len(data["items"]) - len(remaining_items),
                failure_notifications=(
                    len(data["failure_notifications"])
                    - len(remaining_notifications)
                ),
            )
            if cleared.total:
                data["items"] = remaining_items
                data["failure_notifications"] = remaining_notifications
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
    _session_guard_busy_deferrals: int
    _session_guard_last_busy_deferred: int
    _session_guard_last_tree_rss_mb: float
    _session_guard_lock: asyncio.Lock
    _session_guard_runtime_recycles: int

    #: Finished background tasks are remembered this long for the status line.
    _BACKGROUND_FINISHED_RETENTION_SECONDS = 30 * 60
    _BACKGROUND_FINISHED_MAX = 8
    _background_finished: dict[StreamKey, list[float]]

    def _remember_background_finish(self, key: StreamKey, epoch: float) -> None:
        """Keep a bounded, body-free list of recent terminal frames per route (#2605)."""

        store: dict[StreamKey, list[float]] | None = getattr(self, "_background_finished", None)
        if store is None:
            store = self._background_finished = {}
        cutoff = float(epoch) - self._BACKGROUND_FINISHED_RETENTION_SECONDS
        kept = [value for value in store.get(key, ()) if value >= cutoff]
        kept.append(float(epoch))
        store[key] = kept[-self._BACKGROUND_FINISHED_MAX :]

    def background_workload_for_route(
        self, user_id: int, chat_id: int
    ) -> Optional[tuple[int, float, list[float]]]:
        """``(running, oldest_age_seconds, finished_epochs)`` for one route (#2605).

        Read from the route's resident provider session (the same tracker the
        session guard consults) plus this handler's finish memory. ``None``
        when nothing is running and nothing finished recently, so callers
        render exactly as before. Body-free and read-only; a probe that raises
        counts as nothing to show.
        """

        key = self._stream_key(user_id, chat_id)
        running, oldest_age = 0, 0.0
        handle = self._agent_session_registry.get_cached(key)
        session = handle.entry.session if handle is not None else None
        snapshot = getattr(session, "background_workload_snapshot", None)
        if callable(snapshot):
            try:
                count, age = snapshot(asyncio.get_running_loop().time())
                running, oldest_age = max(0, int(count)), max(0.0, float(age))
            except Exception:
                logger.debug("Background workload probe failed: route=%s:%s", user_id, chat_id)
                running, oldest_age = 0, 0.0
        store = getattr(self, "_background_finished", None) or {}
        cutoff = time.time() - self._BACKGROUND_FINISHED_RETENTION_SECONDS
        finished = [value for value in store.get(key, ()) if value >= cutoff]
        if running == 0 and not finished:
            return None
        return running, oldest_age, finished

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
            "busy_deferrals": self._session_guard_busy_deferrals,
            "runtime_recycles": self._session_guard_runtime_recycles,
            "codex_attachments": self._agent_session_attachments,
        }

    # A run-in-background task older than this no longer pins its session:
    # a forgotten server or `tail -f` must not keep a CLI resident forever.
    _BACKGROUND_BUSY_MAX_AGE_SECONDS = 24 * 60 * 60

    @classmethod
    def _provider_session_busy(cls, session: object, now: float) -> bool:
        """Return True while a provider session owns work the bridge did not submit.

        The registry marks only bridge-submitted turns active. A Claude CLI
        that continues on its own after a ``<task-notification>`` (an
        autonomous turn), or that still owns run-in-background tasks, is not
        idle: closing it SIGKILLs the running tool and destroys the work
        (#2291, #1277). Both seams are optional so other providers keep the
        old behaviour. A probe that raises counts as busy: never evict on
        doubt; the next guard tick asks again.
        """

        alive = getattr(session, "provider_reader_alive", None)
        if callable(alive):
            try:
                if not alive():
                    # A dead reader can never clear its own work state; the
                    # session is reclaimable like before #2291.
                    return False
            except Exception:
                logger.debug(
                    "Provider liveness probe failed: provider=%s",
                    type(session).__name__,
                )
        inflight = getattr(session, "autonomous_turn_inflight", None)
        if callable(inflight):
            try:
                if inflight():
                    return True
            except Exception:
                logger.debug(
                    "Autonomous-turn probe failed: provider=%s",
                    type(session).__name__,
                )
                return True
        recent = getattr(session, "recent_background_task_count", None)
        if callable(recent):
            try:
                return int(recent(now, cls._BACKGROUND_BUSY_MAX_AGE_SECONDS)) > 0
            except Exception:
                logger.debug(
                    "Recent background task probe failed: provider=%s",
                    type(session).__name__,
                )
                return True
        snapshot = getattr(session, "background_workload_snapshot", None)
        if callable(snapshot):
            try:
                count, oldest_age = snapshot(now)
                if (
                    int(count) > 0
                    and float(oldest_age) < cls._BACKGROUND_BUSY_MAX_AGE_SECONDS
                ):
                    return True
            except Exception:
                logger.debug(
                    "Background workload probe failed: provider=%s",
                    type(session).__name__,
                )
                return True
        return False

    def _spare_busy_candidates(
        self, idle: tuple[IdleSessionCandidate, ...], now: float
    ) -> tuple[list[IdleSessionCandidate], list[IdleSessionCandidate]]:
        """Split idle candidates into evictable and busy ones.

        Done before TTL/LRU/RSS selection so a busy session never occupies a
        slot the LRU cap would otherwise fill with a genuinely idle one.
        """

        free: list[IdleSessionCandidate] = []
        busy: list[IdleSessionCandidate] = []
        for candidate in idle:
            entry = self._agent_session_registry.peek_idle_cached_if_same(candidate)
            if entry is not None and self._provider_session_busy(entry.session, now):
                busy.append(candidate)
            else:
                free.append(candidate)
        return free, busy

    @staticmethod
    def _count_pressured_busy(
        busy: list[IdleSessionCandidate],
        now: float,
        ttl: int,
        under_pressure: bool,
    ) -> int:
        """Count busy sessions the guard would have evicted were they idle.

        ``busy_deferrals`` must mean "work spared from eviction", not "a busy
        session existed during a quiet tick": TTL expiry, or cap/memory
        pressure that applies to every idle session.
        """

        if under_pressure:
            return len(busy)
        return sum(1 for candidate in busy if now - candidate.last_used_at >= ttl)

    def _any_resident_provider_busy(self, now: float) -> bool:
        return any(
            self._provider_session_busy(entry.session, now)
            for entry in self._agent_session_registry.resident_entries_snapshot()
        )

    async def _evict_guard_candidates(
        self,
        candidates: list[IdleSessionCandidate],
        now: float,
        rss_mb: float,
        deferred: int = 0,
        reasons: Optional[list[str]] = None,
        evicted_reasons: Optional[dict[str, int]] = None,
    ) -> int:
        """Close idle candidates, sparing those whose provider still owns work.

        Idle to the registry is not idle to the provider: an autonomous turn
        or a run-in-background task may be running (#2291). Peek and drop
        are synchronous with no await between them, so the busy answer holds
        for the drop it guards.

        ``reasons`` runs parallel to ``candidates`` (``ttl`` / ``lru`` /
        ``rss``); each actual eviction is tallied into ``evicted_reasons``.
        """

        evicted = 0
        for index, candidate in enumerate(candidates):
            peeked = self._agent_session_registry.peek_idle_cached_if_same(candidate)
            if peeked is not None and self._provider_session_busy(
                peeked.session, now
            ):
                deferred += 1
                continue
            entry = self._agent_session_registry.drop_idle_cached_if_same(candidate)
            if entry is None:
                continue
            await self._close_agent_session(entry.session)
            evicted += 1
            if evicted_reasons is not None:
                reason = (
                    reasons[index]
                    if reasons is not None and index < len(reasons)
                    else "unknown"
                )
                evicted_reasons[reason] = evicted_reasons.get(reason, 0) + 1
        self._session_guard_busy_deferrals += deferred
        if deferred != self._session_guard_last_busy_deferred:
            # Log on change only: a long autonomous turn would otherwise
            # repeat this line on every guard tick.
            self._session_guard_last_busy_deferred = deferred
            if deferred:
                logger.info(
                    "Session resource guard deferred %d session(s) with "
                    "in-flight provider work (autonomous turn or background "
                    "task); rss_mb=%.1f",
                    deferred,
                    rss_mb,
                )
        return evicted

    async def enforce_session_resource_limits(
        self, *, now: float | None = None
    ) -> dict[str, int | float]:
        """Close least-recently-used idle sessions and recycle idle Codex.

        The provider's durable session id lives in ``sessions.json`` and is not
        changed here. A later message therefore resumes the same conversation.
        Active turns are never evicted or used as a runtime-recycle boundary.

        Eviction branches: ``ttl`` (idle past the TTL), ``lru`` (resident cap)
        and ``rss``. The RSS branch needs both the tree RSS watermark and real
        host pressure (``MemAvailable`` below ``max(512 MiB, 15% MemTotal)``,
        #2298); if ``MemAvailable`` is unreadable the watermark alone decides,
        as before.
        """

        if not bool(getattr(self._config, "session_guard_enabled", False)):
            return self.session_resource_snapshot()
        current = asyncio.get_running_loop().time() if now is None else float(now)
        async with self._session_guard_lock:
            metrics = self._agent_session_registry.metrics()
            idle, busy = self._spare_busy_candidates(
                self._agent_session_registry.idle_lru_candidates(), current
            )

            ttl = int(getattr(self._config, "session_idle_ttl_seconds", 4 * 60 * 60))
            candidates: list[IdleSessionCandidate] = [
                candidate
                for candidate in idle
                if current - candidate.last_used_at >= ttl
            ]
            reasons: list[str] = ["ttl"] * len(candidates)

            maximum = int(getattr(self._config, "max_resident_sessions", 2))
            projected = metrics.resident_sessions - len(candidates)
            if projected > maximum:
                for candidate in idle:
                    if candidate in candidates:
                        continue
                    candidates.append(candidate)
                    reasons.append("lru")
                    projected -= 1
                    if projected <= maximum:
                        break

            rss_mb = await asyncio.to_thread(process_tree_rss_mb)
            self._session_guard_last_tree_rss_mb = rss_mb
            rss_limit = int(
                getattr(self._config, "session_tree_rss_limit_mb", 1024)
            )
            memory_high = rss_limit > 0 and rss_mb >= rss_limit
            mem_available_mb: float | None = None
            if memory_high:
                # The watermark is necessary, not sufficient (#2298): an idle
                # tree on a small host sits at the floor while the host has
                # plenty available. Unknown MemAvailable keeps the old rule.
                host_pressured, mem_available_mb = await asyncio.to_thread(
                    host_memory_pressure
                )
                memory_high = host_pressured is not False
            if memory_high and metrics.active_sessions == 0:
                for candidate in idle:
                    if candidate not in candidates:
                        candidates.append(candidate)
                        reasons.append("rss")

            deferred = self._count_pressured_busy(
                busy,
                current,
                ttl,
                metrics.resident_sessions > maximum
                or (memory_high and metrics.active_sessions == 0),
            )
            evicted_reasons: dict[str, int] = {}
            evicted = await self._evict_guard_candidates(
                candidates,
                current,
                rss_mb,
                deferred=deferred,
                reasons=reasons,
                evicted_reasons=evicted_reasons,
            )
            if evicted:
                self._session_guard_evictions += evicted
                if getattr(self._config, "agent_provider", "claude") == "codex":
                    self._agent_runtime_recycle_pending = True
                resident = (
                    self._agent_session_registry.metrics().resident_sessions
                )
                logger.info(
                    "Session resource guard evicted %d idle session(s); "
                    "reason=%s resident=%d rss_mb=%.1f mem_available_mb=%s",
                    evicted,
                    ",".join(
                        f"{name}:{count}"
                        for name, count in sorted(evicted_reasons.items())
                    ),
                    resident,
                    rss_mb,
                    "n/a"
                    if mem_available_mb is None
                    else f"{mem_available_mb:.1f}",
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
                and not self._any_resident_provider_busy(current)
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

        if self._agent_session_registry.metrics().active_sessions or (
            self._any_resident_provider_busy(asyncio.get_running_loop().time())
        ):
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

    async def request_danso_task_pause(self, user_id: int, chat_id: int) -> str:
        """Request a graceful pause from the exact active Danso session.

        The worker validates that its native parent has emitted the bounded
        ready checkpoint before sending SIGUSR1.  This method never targets a
        process group and never starts a new process.
        """
        async with self._session_guard_lock:
            key = self._stream_key(user_id, chat_id)
            handles = self._agent_session_registry.active_handles_for_keys((key,))
            if not handles:
                return "not_active"
            handle = self._agent_session_registry.active_handle_if_same(handles[0].token)
            if handle is None:
                return "not_active"
            request_pause = getattr(handle.session, "request_task_pause", None)
            if not callable(request_pause):
                return "unsupported"
            try:
                return "requested" if request_pause() else "not_ready"
            except (OSError, ProcessLookupError):
                return "not_ready"

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
