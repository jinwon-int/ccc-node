"""Typed, event-loop-confined ownership for ProjectChat agent sessions.

The registry is deliberately synchronous: every mutation is one non-awaiting
compare-and-set transition.  Provider calls, interruption, and session closing
remain the responsibility of ``ProjectChatHandler``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

from telegram_bot.core.project_chat_types import AgentSessionEntry


StreamKey: TypeAlias = tuple[int, int]


@dataclass(frozen=True, slots=True)
class CacheToken:
    """Opaque identity for one cached session slot."""

    sequence: int


@dataclass(frozen=True, slots=True)
class ActiveToken:
    """Opaque ownership token for one active turn."""

    key: StreamKey
    generation: int


@dataclass(frozen=True, slots=True)
class CachedSessionHandle:
    """Internal typed access to one exact cached entry."""

    entry: AgentSessionEntry
    token: CacheToken


@dataclass(frozen=True, slots=True)
class ActiveSessionHandle:
    """Internal interruption handle; never exposed in health output."""

    token: ActiveToken
    session: Any


@dataclass(frozen=True, slots=True)
class IdleSessionCandidate:
    """Body-free LRU candidate whose token must be rechecked at removal."""

    key: StreamKey
    token: CacheToken
    last_used_at: float


@dataclass(frozen=True, slots=True)
class AgentSessionMetrics:
    """Body-free numeric projection for health and workload reporting."""

    resident_sessions: int
    active_sessions: int
    waiting_for_turn: int
    oldest_started_at: float | None


@dataclass(slots=True)
class _CachedSlot:
    entry: AgentSessionEntry
    token: CacheToken


@dataclass(slots=True)
class _ActiveTurn:
    session: Any
    token: ActiveToken
    approval_generation: int | None
    started_at: float
    waiting_for_turn: bool = True


@dataclass(slots=True)
class _SessionRecord:
    cached: _CachedSlot | None = None
    active: _ActiveTurn | None = None


class AgentSessionRegistry:
    """Sole owner of cached and active ProjectChat session state.

    Generation high-water values intentionally outlive live records.  They are
    the lightweight equivalent of the previous ``_agent_generation_counters``
    map and prevent a stale Telegram approval generation from being reused
    after cache eviction, active cleanup, or Codex runtime recycling.
    """

    def __init__(self) -> None:
        self._records: dict[StreamKey, _SessionRecord] = {}
        self._generation_high_water: dict[StreamKey, int] = {}
        self._cache_sequence = 0

    def _record(self, key: StreamKey) -> _SessionRecord:
        record = self._records.get(key)
        if record is None:
            record = _SessionRecord()
            self._records[key] = record
        return record

    def _prune(self, key: StreamKey) -> None:
        record = self._records.get(key)
        if record is not None and record.cached is None and record.active is None:
            self._records.pop(key, None)

    def get_cached(self, key: StreamKey) -> CachedSessionHandle | None:
        record = self._records.get(key)
        slot = record.cached if record is not None else None
        if slot is None:
            return None
        return CachedSessionHandle(slot.entry, slot.token)

    def put_cached(
        self,
        key: StreamKey,
        entry: AgentSessionEntry,
    ) -> CachedSessionHandle:
        self._cache_sequence += 1
        token = CacheToken(self._cache_sequence)
        self._record(key).cached = _CachedSlot(entry, token)
        return CachedSessionHandle(entry, token)

    def touch_cached_if_same(
        self,
        key: StreamKey,
        token: CacheToken,
        now: float,
    ) -> bool:
        record = self._records.get(key)
        slot = record.cached if record is not None else None
        if record is None or slot is None or slot.token != token:
            return False
        slot.entry.last_used_at = max(float(slot.entry.last_used_at), float(now))
        return True

    def drop_cached_if_same(
        self,
        key: StreamKey,
        token: CacheToken,
    ) -> AgentSessionEntry | None:
        record = self._records.get(key)
        slot = record.cached if record is not None else None
        if record is None or slot is None or slot.token != token:
            return None
        record.cached = None
        self._prune(key)
        return slot.entry

    def idle_lru_candidates(self) -> tuple[IdleSessionCandidate, ...]:
        candidates = [
            IdleSessionCandidate(
                key=key,
                token=record.cached.token,
                last_used_at=float(record.cached.entry.last_used_at),
            )
            for key, record in self._records.items()
            if record.cached is not None and record.active is None
        ]
        return tuple(sorted(candidates, key=lambda candidate: candidate.last_used_at))

    def drop_idle_cached_if_same(
        self,
        candidate: IdleSessionCandidate,
    ) -> AgentSessionEntry | None:
        record = self._records.get(candidate.key)
        slot = record.cached if record is not None else None
        if (
            record is None
            or record.active is not None
            or slot is None
            or slot.token != candidate.token
        ):
            return None
        record.cached = None
        self._prune(candidate.key)
        return slot.entry

    def clear_cached_if_idle(self) -> tuple[AgentSessionEntry, ...] | None:
        """Drop every cached wrapper only when no turn is currently active."""

        if any(record.active is not None for record in self._records.values()):
            return None
        entries: list[AgentSessionEntry] = []
        for key, record in tuple(self._records.items()):
            if record.cached is not None:
                entries.append(record.cached.entry)
                record.cached = None
            self._prune(key)
        return tuple(entries)

    def register_active(
        self,
        key: StreamKey,
        session: Any,
        *,
        started_at: float,
    ) -> ActiveToken:
        record = self._record(key)
        if record.active is not None:
            raise RuntimeError("agent turn is already active for this conversation")
        generation = self._generation_high_water.get(key, 0) + 1
        self._generation_high_water[key] = generation
        token = ActiveToken(key, generation)
        record.active = _ActiveTurn(
            session=session,
            token=token,
            approval_generation=generation,
            started_at=float(started_at),
        )
        return token

    def approval_is_active(self, key: StreamKey, generation: int) -> bool:
        record = self._records.get(key)
        active = record.active if record is not None else None
        return bool(
            active is not None
            and active.token.key == key
            and active.approval_generation == generation
        )

    def invalidate_approvals(self, keys: tuple[StreamKey, ...]) -> None:
        for key in keys:
            generation = self._generation_high_water.get(key, 0) + 1
            self._generation_high_water[key] = generation
            record = self._records.get(key)
            if record is not None and record.active is not None:
                record.active.approval_generation = None

    def admit_if_same(self, token: ActiveToken) -> bool:
        record = self._records.get(token.key)
        active = record.active if record is not None else None
        if record is None or active is None or active.token != token:
            return False
        active.waiting_for_turn = False
        return True

    def active_handle_if_same(
        self,
        token: ActiveToken,
    ) -> ActiveSessionHandle | None:
        record = self._records.get(token.key)
        active = record.active if record is not None else None
        if active is None or active.token != token:
            return None
        return ActiveSessionHandle(active.token, active.session)

    def active_started_at(self, key: StreamKey) -> float | None:
        """Return the monotonic start time for one active conversation turn."""

        record = self._records.get(key)
        active = record.active if record is not None else None
        return active.started_at if active is not None else None

    def deactivate_if_same(
        self,
        token: ActiveToken,
        *,
        touch_at: float | None = None,
    ) -> bool:
        record = self._records.get(token.key)
        active = record.active if record is not None else None
        if record is None or active is None or active.token != token:
            return False
        if (
            touch_at is not None
            and record.cached is not None
            and record.cached.entry.session is active.session
        ):
            record.cached.entry.last_used_at = max(
                float(record.cached.entry.last_used_at),
                float(touch_at),
            )
        record.active = None
        self._prune(token.key)
        return True

    def keys_for_user(
        self,
        user_id: int,
        chat_id: int | None = None,
    ) -> tuple[StreamKey, ...]:
        if chat_id is not None:
            key = (user_id, chat_id)
            return (key,) if self.has_live_owner(key) else ()
        return tuple(
            key
            for key, record in self._records.items()
            if key[0] == user_id
            and (record.cached is not None or record.active is not None)
        )

    def active_handles_for_keys(
        self,
        keys: tuple[StreamKey, ...],
    ) -> tuple[ActiveSessionHandle, ...]:
        handles: list[ActiveSessionHandle] = []
        for key in keys:
            record = self._records.get(key)
            active = record.active if record is not None else None
            if active is not None:
                handles.append(ActiveSessionHandle(active.token, active.session))
        return tuple(handles)

    def active_handles_snapshot(self) -> tuple[ActiveSessionHandle, ...]:
        return self.active_handles_for_keys(tuple(self._records))

    def prepare_close(self) -> tuple[ActiveSessionHandle, ...]:
        """Deny approvals/waiting projections and snapshot active handles."""

        handles: list[ActiveSessionHandle] = []
        for record in self._records.values():
            active = record.active
            if active is None:
                continue
            active.approval_generation = None
            active.waiting_for_turn = False
            handles.append(ActiveSessionHandle(active.token, active.session))
        return tuple(handles)

    def has_live_owner(self, key: StreamKey) -> bool:
        record = self._records.get(key)
        return bool(
            record is not None
            and (record.cached is not None or record.active is not None)
        )

    def metrics(self) -> AgentSessionMetrics:
        resident = 0
        active_count = 0
        waiting = 0
        oldest: float | None = None
        for record in self._records.values():
            resident += int(record.cached is not None)
            active = record.active
            if active is None:
                continue
            active_count += 1
            waiting += int(active.waiting_for_turn)
            if oldest is None or active.started_at < oldest:
                oldest = active.started_at
        return AgentSessionMetrics(
            resident_sessions=resident,
            active_sessions=active_count,
            waiting_for_turn=waiting,
            oldest_started_at=oldest,
        )

    def generation_high_water(self, key: StreamKey) -> int:
        """Return the body-free monotonic value for tests and diagnostics."""

        return self._generation_high_water.get(key, 0)


__all__ = [
    "ActiveSessionHandle",
    "ActiveToken",
    "AgentSessionMetrics",
    "AgentSessionRegistry",
    "CacheToken",
    "CachedSessionHandle",
    "IdleSessionCandidate",
    "StreamKey",
]
