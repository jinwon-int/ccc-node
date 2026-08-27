"""Durable, body-free journal for out-of-turn async completions (#646).

One journal record per :class:`NormalizedAsyncCompletionEvent` identity.  The
record stores the state machine and bounded bookkeeping only — thread, turn,
task, and conversation-route identifiers never reach durable storage in raw
form because they are already folded into the event's body-free idempotency
key (``async-completion:<identity hash>``).  A restart therefore recovers
counts and states without ever being able to misdeliver a result to a
conversation.

State machine (all transitions validated fail-closed)::

    queued → claimed → delivered | retryable_failed | terminal_failed
    claimed → queued            (stale-claim recovery only)
    retryable_failed → claimed  (bounded retry attempts)

Owner-only filesystem semantics are inherited from
:class:`telegram_bot.memory.journal_core.JsonJournalCore`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Literal, Optional

from .async_completion_event import (
    ASYNC_COMPLETION_SCHEMA_VERSION,
    NormalizedAsyncCompletionEvent,
)

try:
    from telegram_bot.utils.secure_fs import _fsync_directory
except ModuleNotFoundError:  # Standalone hook install beside ccc_secure_fs.py.
    from ccc_secure_fs import _fsync_directory

from telegram_bot.memory.journal_core import JsonJournalCore

ASYNC_COMPLETION_JOURNAL_SCHEMA_VERSION = 1
_DEFAULT_MAX_RECORDS = 256
_DEFAULT_STALE_CLAIM_SECONDS = 300.0
_MAX_ATTEMPTS = (1 << 16) - 1

AsyncCompletionRecordState = Literal[
    "queued", "claimed", "delivered", "retryable_failed", "terminal_failed"
]

_QUEUED: AsyncCompletionRecordState = "queued"
_CLAIMED: AsyncCompletionRecordState = "claimed"
_DELIVERED: AsyncCompletionRecordState = "delivered"
_RETRYABLE: AsyncCompletionRecordState = "retryable_failed"
_TERMINAL: AsyncCompletionRecordState = "terminal_failed"

# Which states each transition may depart from.  ``claimed → queued`` is
# handled by recover_stale_claimed(), not by mark_*().
_TRANSITIONS: dict[AsyncCompletionRecordState, frozenset[str]] = {
    _CLAIMED: frozenset({_QUEUED, _RETRYABLE}),
    _DELIVERED: frozenset({_CLAIMED}),
    _RETRYABLE: frozenset({_CLAIMED}),
    _TERMINAL: frozenset({_CLAIMED, _RETRYABLE}),
}
_TERMINAL_STATES = frozenset({_DELIVERED, _TERMINAL})

_SAFE_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _normalize_time(value).isoformat()


def _normalize_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("journal timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("journal timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class AsyncCompletionRecord:
    """Body-free durable state for one normalized completion identity."""

    idempotency_key: str
    provider: str
    state: AsyncCompletionRecordState
    session_generation: int
    created_at: datetime
    updated_at: datetime
    attempts: int = 0
    noticed_at: Optional[datetime] = None
    last_error_code: Optional[str] = None

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL_STATES


Clock = Callable[[], datetime]


class AsyncCompletionJournal(JsonJournalCore):
    """Owner-only durable journal of normalized async completion identities."""

    def __init__(
        self,
        root: Any,
        *,
        clock: Clock | None = None,
        max_records: int = _DEFAULT_MAX_RECORDS,
        stale_claim_seconds: float = _DEFAULT_STALE_CLAIM_SECONDS,
    ) -> None:
        super().__init__(root)
        if max_records <= 0:
            raise ValueError("max_records must be positive")
        if stale_claim_seconds <= 0:
            raise ValueError("stale_claim_seconds must be positive")
        self._clock = clock or _utc_now
        self._max_records = max_records
        self._stale_claim_seconds = stale_claim_seconds

    # -- record identity ------------------------------------------------------

    @staticmethod
    def record_id_for(idempotency_key: str) -> str:
        """Map ``async-completion:<64-hex>`` to the core's record-id format."""

        prefix = "async-completion:"
        if not idempotency_key.startswith(prefix):
            raise ValueError("async completion idempotency key is malformed")
        record_id = idempotency_key[len(prefix) :]
        if re.fullmatch(r"[0-9a-f]{64}", record_id) is None:
            raise ValueError("async completion idempotency key is malformed")
        return record_id

    @staticmethod
    def _validate_event(event: NormalizedAsyncCompletionEvent) -> None:
        # NormalizedAsyncCompletionEvent validates itself in __post_init__;
        # the isinstance guard keeps foreign objects out of durable storage.
        if not isinstance(event, NormalizedAsyncCompletionEvent):
            raise ValueError("async completion journal requires a normalized event")
        if event.schema_version != ASYNC_COMPLETION_SCHEMA_VERSION:
            raise ValueError("async completion event schema version is unsupported")

    # -- durable record I/O (under the core's exclusive lock) -----------------

    def _read_unlocked(self, record_id: str) -> AsyncCompletionRecord:
        value = self._read_json_unlocked(record_id)
        return self._decode_record(value)

    def _write_unlocked(self, record: AsyncCompletionRecord) -> None:
        payload = {
            "schema_version": ASYNC_COMPLETION_JOURNAL_SCHEMA_VERSION,
            "idempotency_key": record.idempotency_key,
            "provider": record.provider,
            "state": record.state,
            "session_generation": record.session_generation,
            "created_at": _timestamp(record.created_at),
            "updated_at": _timestamp(record.updated_at),
            "attempts": record.attempts,
            "noticed_at": (
                _timestamp(record.noticed_at) if record.noticed_at else None
            ),
            "last_error_code": record.last_error_code,
        }
        self._write_json_unlocked(
            self.record_id_for(record.idempotency_key), payload
        )

    def _decode_record(self, value: Any) -> AsyncCompletionRecord:
        if not isinstance(value, dict):
            raise ValueError("async completion record must be an object")
        if value.get("schema_version") != ASYNC_COMPLETION_JOURNAL_SCHEMA_VERSION:
            raise ValueError("async completion record schema is unsupported")
        state = value.get("state")
        if state not in _TRANSITIONS and state != _QUEUED:
            raise ValueError("async completion record state is invalid")
        idempotency_key = value.get("idempotency_key")
        if not isinstance(idempotency_key, str):
            raise ValueError("async completion record idempotency key is invalid")
        self.record_id_for(idempotency_key)
        provider = value.get("provider")
        if not isinstance(provider, str) or not provider:
            raise ValueError("async completion record provider is invalid")
        generation = value.get("session_generation")
        if type(generation) is not int or generation <= 0:
            raise ValueError("async completion record generation is invalid")
        attempts = value.get("attempts")
        if type(attempts) is not int or not 0 <= attempts <= _MAX_ATTEMPTS:
            raise ValueError("async completion record attempts are invalid")
        error_code = value.get("last_error_code")
        if error_code is not None:
            if not isinstance(error_code, str) or _SAFE_ERROR_CODE_RE.fullmatch(error_code) is None:
                raise ValueError("async completion record error code is invalid")
        noticed_at = value.get("noticed_at")
        return AsyncCompletionRecord(
            idempotency_key=idempotency_key,
            provider=provider,
            state=state,
            session_generation=generation,
            created_at=_parse_timestamp(value["created_at"]),
            updated_at=_parse_timestamp(value["updated_at"]),
            attempts=attempts,
            noticed_at=_parse_timestamp(noticed_at) if noticed_at else None,
            last_error_code=error_code,
        )

    # -- public API -----------------------------------------------------------

    def observe(
        self, event: NormalizedAsyncCompletionEvent, *, now: datetime | None = None
    ) -> bool:
        """Insert one completion identity; return ``False`` on a duplicate.

        The first observation of an identity inserts a ``queued`` record and
        returns ``True`` — callers deliver their once-only side effects (for
        example the bounded owner fallback notice) on that window.  Every
        repeat or concurrent observation returns ``False`` and changes
        nothing, which is the exactly-once seam for downstream delivery.
        """

        self._validate_event(event)
        observed_at = _normalize_time(now or self._clock())
        record_id = self.record_id_for(event.idempotency_key)
        with self._exclusive():
            if self.record_path(record_id).exists():
                return False
            self._enforce_retention_unlocked(observed_at)
            self._write_unlocked(
                AsyncCompletionRecord(
                    idempotency_key=event.idempotency_key,
                    provider=event.provider,
                    state=_QUEUED,
                    session_generation=event.session_generation,
                    created_at=observed_at,
                    updated_at=observed_at,
                )
            )
            return True

    def get(self, idempotency_key: str) -> AsyncCompletionRecord | None:
        record_id = self.record_id_for(idempotency_key)
        with self._exclusive():
            if not self.record_path(record_id).exists():
                return None
            return self._read_unlocked(record_id)

    def list_records(self) -> tuple[AsyncCompletionRecord, ...]:
        with self._exclusive():
            records = [
                self._read_unlocked(record_id)
                for record_id in self._record_ids_unlocked()
            ]
        return tuple(sorted(records, key=lambda record: record.created_at))

    def mark(
        self,
        idempotency_key: str,
        state: AsyncCompletionRecordState,
        *,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> AsyncCompletionRecord:
        """Apply one validated state transition and return the new record."""

        if state not in _TRANSITIONS:
            raise ValueError("async completion state transition is invalid")
        if error_code is not None and _SAFE_ERROR_CODE_RE.fullmatch(error_code) is None:
            raise ValueError("async completion error code is invalid")
        marked_at = _normalize_time(now or self._clock())
        record_id = self.record_id_for(idempotency_key)
        with self._exclusive():
            record = self._read_unlocked(record_id)
            if record.state not in _TRANSITIONS[state]:
                raise ValueError(
                    "async completion state transition is invalid: "
                    f"{record.state} -> {state}"
                )
            attempts = record.attempts
            if state == _CLAIMED:
                attempts = min(record.attempts + 1, _MAX_ATTEMPTS)
            updated = replace(
                record,
                state=state,
                attempts=attempts,
                updated_at=marked_at,
                last_error_code=error_code,
            )
            self._write_unlocked(updated)
            return updated

    def mark_noticed(
        self, idempotency_key: str, *, now: datetime | None = None
    ) -> AsyncCompletionRecord:
        """Stamp the once-only owner fallback notice (idempotent)."""

        noticed_at = _normalize_time(now or self._clock())
        record_id = self.record_id_for(idempotency_key)
        with self._exclusive():
            record = self._read_unlocked(record_id)
            if record.noticed_at is not None:
                return record
            updated = replace(record, noticed_at=noticed_at, updated_at=noticed_at)
            self._write_unlocked(updated)
            return updated

    def recover_stale_claimed(
        self, *, now: datetime | None = None
    ) -> tuple[AsyncCompletionRecord, ...]:
        """Re-queue claims older than the staleness bound; return recoveries."""

        recovered_at = _normalize_time(now or self._clock())
        cutoff = recovered_at - timedelta(seconds=self._stale_claim_seconds)
        recovered: list[AsyncCompletionRecord] = []
        with self._exclusive():
            for record_id in self._record_ids_unlocked():
                record = self._read_unlocked(record_id)
                if record.state != _CLAIMED or record.updated_at > cutoff:
                    continue
                updated = replace(
                    record, state=_QUEUED, updated_at=recovered_at
                )
                self._write_unlocked(updated)
                recovered.append(updated)
        return tuple(recovered)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.list_records():
            counts[record.state] = counts.get(record.state, 0) + 1
        return counts

    # -- retention ------------------------------------------------------------

    def _enforce_retention_unlocked(self, observed_at: datetime) -> None:
        """Bound the journal, evicting oldest terminal records first.

        Runs inside the caller's ``_exclusive()`` window, so eviction unlinks
        directly (``complete_claimed`` would re-enter the non-reentrant file
        lock and deadlock, mirroring the ``prune_terminal_jobs`` idiom).
        When terminal records cannot cover the overflow the journal may hold
        more than ``max_records`` non-terminal records until they drain.
        """

        record_ids = self._record_ids_unlocked()
        if len(record_ids) < self._max_records:
            return
        records = sorted(
            (self._read_unlocked(record_id) for record_id in record_ids),
            key=lambda record: (not record.terminal, record.created_at),
        )
        overflow = len(records) - self._max_records + 1
        evictable = [record for record in records if record.terminal][
            :overflow
        ]
        removed_any = False
        for record in evictable:
            record_id = self.record_id_for(record.idempotency_key)
            path = self.record_path(record_id)
            try:
                path.unlink()
            except (FileNotFoundError, OSError):
                continue
            try:
                self.claim_path(record_id).unlink()
            except FileNotFoundError:
                pass
            removed_any = True
        if removed_any:
            _fsync_directory(self.root)
