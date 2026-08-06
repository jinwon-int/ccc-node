"""Durable external-wait registry for GitHub CI monitoring (#740).

Why this exists
---------------
When an agent promises "I will continue once CI finishes", nothing durable
used to back that promise: the turn ended, nobody watched GitHub, and the
conversation only resumed when the user spoke first. This module is the
small, typed foundation that makes the promise real:

- :class:`ExternalWaitRegistry` persists one record per wait (repo + PR +
  exact head SHA + terminal condition + conversation route) with the same
  durability contract as the task ledger (atomic writes + previous-good
  backup + fail-open).
- Terminal transitions are journaled in the record *before* any wake-up,
  so a bridge restart cannot lose or duplicate the pending wake
  (exactly-once equivalent).
- The active-turn helpers let a provider-neutral turn publish *which*
  conversation it is serving right now, so the agent-side CLI can bind a
  registration to the correct route without per-session env plumbing —
  fail-closed when the route is absent or ambiguous.

Body-free by contract: records never store prompts, tokens, or check logs.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from telegram_bot.utils.secure_fs import (
    SessionStoreDurabilityError,
    _atomic_write_bytes,
)

logger = logging.getLogger(__name__)

SOURCE_GITHUB_PR_CHECKS = "github_pr_checks"

#: Non-terminal registry state.
STATE_MONITORING = "monitoring"

#: Terminal statuses (also the normalized check-rollups a monitor may report).
TERMINAL_SUCCESS = "success"
TERMINAL_FAILURE = "failure"
TERMINAL_CANCELLED = "cancelled"
#: The registered head SHA no longer matches the PR head — a newer push makes
#: the watched CI run meaningless for the promised next step.
TERMINAL_SUPERSEDED = "superseded"
#: GitHub transport could not be read after bounded retries (auth, rate
#: limit, malformed payload) — fail-closed, owner is told.
TERMINAL_MONITOR_ERROR = "monitor-error"
TERMINAL_EXPIRED = "expired"
TERMINAL_OWNER_CANCEL = "owner-cancel"

TERMINAL_STATUSES = frozenset(
    {
        TERMINAL_SUCCESS,
        TERMINAL_FAILURE,
        TERMINAL_CANCELLED,
        TERMINAL_SUPERSEDED,
        TERMINAL_MONITOR_ERROR,
        TERMINAL_EXPIRED,
        TERMINAL_OWNER_CANCEL,
    }
)

#: Terminal records stay visible to /waits for this long, then are pruned.
TERMINAL_RETENTION_SECONDS = 24 * 60 * 60
#: Give up delivering a wake after this many attempts (Telegram outages must
#: not pin a record forever; the wait itself already terminated).
MAX_WAKE_ATTEMPTS = 10
#: An active-turn route entry older than this is stale. Turns clear their
#: entry on exit; the TTL is only a crash backstop.
ACTIVE_ROUTE_TTL_SECONDS = 6 * 60 * 60

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_MAX_SUMMARY_CHARS = 200


class ExternalWaitValidationError(ValueError):
    """A registration field failed the bounded, body-free contract."""


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_repo(repo: str) -> str:
    value = str(repo or "").strip()
    if not _REPO_RE.match(value):
        raise ExternalWaitValidationError("repo must look like 'owner/name'")
    return value


def validate_pr_number(pr_number: Any) -> int:
    try:
        value = int(pr_number)
    except (TypeError, ValueError):
        raise ExternalWaitValidationError("pr must be a positive integer")
    if value <= 0:
        raise ExternalWaitValidationError("pr must be a positive integer")
    return value


def validate_head_sha(head_sha: str) -> str:
    value = str(head_sha or "").strip().lower()
    if not _SHA_RE.match(value):
        raise ExternalWaitValidationError("head SHA must be 7-40 hex chars")
    return value


def validate_summary(summary: Optional[str]) -> str:
    """Bounded, single-line, body-free next-action summary (may be empty)."""
    text = " ".join(str(summary or "").split())[:_MAX_SUMMARY_CHARS]
    return text.strip()


def default_registry_path(home: Path) -> Path:
    return Path(home) / "waits.json"


def default_active_turns_path(home: Path) -> Path:
    return Path(home) / "active-turns.json"


class ExternalWaitRegistry:
    """Small durable registry of external waits (task-ledger durability).

    Only monitoring records and recently terminal ones are kept; wake
    delivery is journaled on the record so restart recovery is a drain, not
    a guess. All methods are fail-open and idempotent. Time is injectable so
    retention/reaping never mixes wall-clock with a caller's test clock.
    """

    def __init__(self, path: Path, *, clock=lambda: time.time()):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._clock = clock

    @property
    def _backup_path(self) -> Path:
        return self._path.with_name(self._path.name + ".bak")

    # --- persistence (mirrors TaskLedger) -------------------------------------
    def _read_path(self, path: Path) -> Optional[Dict[str, Dict[str, Any]]]:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except Exception as exc:  # pragma: no cover - deliberately fail-open
            logger.warning("External-wait registry read failed: %s", type(exc).__name__)
            return None
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    def _read(self) -> Dict[str, Dict[str, Any]]:
        primary = self._read_path(self._path)
        if primary is not None:
            return primary
        backup = self._read_path(self._backup_path)
        if backup is not None:
            logger.warning("External-wait registry recovered from previous-good backup")
            return backup
        return {}

    def _write(self, records: Dict[str, Dict[str, Any]]) -> None:
        payload = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            if self._path.exists():
                previous = self._path.read_bytes()
                if previous.strip():
                    json.loads(previous)
                    _atomic_write_bytes(self._backup_path, previous)
        except Exception:
            pass
        try:
            _atomic_write_bytes(self._path, payload)
        except SessionStoreDurabilityError:
            logger.warning("External-wait registry dir-fsync unconfirmed; state written")

    def _mutate(self, fn) -> Any:
        with self._lock:
            try:
                records = self._read()
                result = fn(records)
                self._write(records)
                return result
            except Exception as exc:  # pragma: no cover - deliberately fail-open
                logger.warning("External-wait registry update failed: %s", type(exc).__name__)
                return None

    @staticmethod
    def _prune(records: Dict[str, Dict[str, Any]], *, now: float) -> None:
        cutoff = now - TERMINAL_RETENTION_SECONDS
        for wait_id in list(records.keys()):
            rec = records[wait_id]
            if rec.get("state") != STATE_MONITORING:
                completed = float(rec.get("completed_epoch") or 0)
                if completed and completed < cutoff:
                    records.pop(wait_id, None)

    # --- lifecycle -------------------------------------------------------------
    def register(
        self,
        *,
        repo: str,
        pr_number: Any,
        head_sha: str,
        user_id: int,
        chat_id: int,
        session_id: Optional[str],
        summary: Optional[str],
        timeout_seconds: float,
        poll_interval_seconds: float,
        now: Optional[float] = None,
    ) -> str:
        """Register one wait; returns its id (idempotent per natural key)."""
        repo = validate_repo(repo)
        pr_number = validate_pr_number(pr_number)
        head_sha = validate_head_sha(head_sha)
        summary = validate_summary(summary)
        now = self._clock() if now is None else float(now)
        natural = (SOURCE_GITHUB_PR_CHECKS, repo, pr_number, head_sha, int(user_id), int(chat_id))

        def _do(records):
            self._prune(records, now=now)
            for wait_id, rec in records.items():
                if rec.get("state") != STATE_MONITORING:
                    continue
                if (
                    rec.get("source"),
                    rec.get("repo"),
                    rec.get("pr_number"),
                    rec.get("head_sha"),
                    rec.get("user_id"),
                    rec.get("chat_id"),
                ) == natural:
                    return wait_id
            wait_id = uuid.uuid4().hex[:12]
            records[wait_id] = {
                "wait_id": wait_id,
                "source": SOURCE_GITHUB_PR_CHECKS,
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": head_sha,
                "user_id": int(user_id),
                "chat_id": int(chat_id),
                "session_id": session_id or None,
                "summary": summary,
                "state": STATE_MONITORING,
                "created_at": _utc_now_iso(),
                "created_epoch": now,
                "updated_at": _utc_now_iso(),
                "expires_epoch": now + max(60.0, float(timeout_seconds)),
                "next_poll_epoch": now,
                "poll_interval_seconds": max(5.0, float(poll_interval_seconds)),
                "terminal_status": None,
                "wake": None,
            }
            return wait_id

        return self._mutate(_do)

    def cancel(self, wait_id: str, *, now: Optional[float] = None) -> bool:
        """Owner cancellation; idempotent. Returns True when it transitioned."""

        def _do(records):
            rec = records.get(wait_id)
            if rec is None or rec.get("state") != STATE_MONITORING:
                return False
            rec["state"] = TERMINAL_OWNER_CANCEL
            rec["terminal_status"] = TERMINAL_OWNER_CANCEL
            rec["completed_epoch"] = self._clock() if now is None else float(now)
            rec["updated_at"] = _utc_now_iso()
            return True

        return bool(self._mutate(_do))

    def reschedule(self, wait_id: str, *, next_poll_epoch: float, poll_interval_seconds: float) -> None:
        def _do(records):
            rec = records.get(wait_id)
            if rec is None or rec.get("state") != STATE_MONITORING:
                return
            rec["next_poll_epoch"] = float(next_poll_epoch)
            rec["poll_interval_seconds"] = max(5.0, float(poll_interval_seconds))
            rec["updated_at"] = _utc_now_iso()

        self._mutate(_do)

    def correct_head_sha(self, wait_id: str, full_sha: str) -> bool:
        """Heal a legacy short-SHA registration to the full 40-char head.

        Registration now normalizes short SHAs to the full head via ``gh``
        (#961), but records written before that fix may hold 7-39 hex chars
        that can never equal GitHub's 40-char ``headRefOid``. The monitor
        calls this when the live head *starts with* the recorded short form;
        a genuine head change still supersedes. Returns True only when the
        record was monitoring and actually healed.
        """
        full_sha = validate_head_sha(full_sha)
        if len(full_sha) != 40:
            return False

        def _do(records):
            rec = records.get(wait_id)
            if rec is None or rec.get("state") != STATE_MONITORING:
                return False
            recorded = str(rec.get("head_sha") or "")
            if not recorded or len(recorded) >= 40 or not full_sha.startswith(recorded):
                return False
            rec["head_sha"] = full_sha
            rec["updated_at"] = _utc_now_iso()
            return True

        return bool(self._mutate(_do))

    def finish(self, wait_id: str, terminal_status: str, *, now: Optional[float] = None) -> bool:
        """Terminal transition, journaled before any wake. First write wins.

        Returns True only for the monitoring -> terminal transition; the wake
        is marked pending in the same durable write (exactly-once journal).
        """
        if terminal_status not in TERMINAL_STATUSES or terminal_status == TERMINAL_OWNER_CANCEL:
            raise ValueError(f"invalid terminal status: {terminal_status}")

        def _do(records):
            rec = records.get(wait_id)
            if rec is None or rec.get("state") != STATE_MONITORING:
                return False
            now_value = self._clock() if now is None else float(now)
            rec["state"] = terminal_status
            rec["terminal_status"] = terminal_status
            rec["completed_epoch"] = now_value
            rec["updated_at"] = _utc_now_iso()
            rec["wake"] = {"state": "pending", "attempts": 0}
            return True

        return bool(self._mutate(_do))

    # --- wake journal -------------------------------------------------------------
    def pending_wakes(self) -> List[Dict[str, Any]]:
        with self._lock:
            records = self._read()
        return [
            rec
            for rec in records.values()
            if isinstance(rec.get("wake"), dict) and rec["wake"].get("state") == "pending"
        ]

    def mark_wake(
        self,
        wait_id: str,
        *,
        delivered: bool,
        resumed: Optional[bool] = None,
        skip_reason: Optional[str] = None,
    ) -> None:
        """Record one wake outcome; retry-bounded, purges only the journal.

        ``delivered`` is about the notification. ``resumed``/``skip_reason`` are
        about the promise: a wake whose notification landed but whose
        continuation was skipped used to be indistinguishable from a fulfilled
        one (both ``wake.state == "done"``), so a dropped promise left no trace
        for the owner or for a later drain. Recording them separately is what
        makes ``dropped_promises`` possible.
        """

        def _do(records):
            rec = records.get(wait_id)
            if rec is None or not isinstance(rec.get("wake"), dict):
                return
            outcome: Dict[str, Any] = {}
            if resumed is not None:
                outcome["resumed"] = bool(resumed)
            if skip_reason:
                outcome["skip_reason"] = str(skip_reason)[:64]
            if delivered:
                rec["wake"] = {
                    "state": "done",
                    "attempts": int(rec["wake"].get("attempts") or 0),
                    **outcome,
                }
            else:
                attempts = int(rec["wake"].get("attempts") or 0) + 1
                if attempts >= MAX_WAKE_ATTEMPTS:
                    logger.warning(
                        "External-wait wake delivery gave up after %d attempts: wait=%s",
                        attempts,
                        wait_id,
                    )
                    rec["wake"] = {"state": "failed", "attempts": attempts, **outcome}
                else:
                    rec["wake"] = {"state": "pending", "attempts": attempts, **outcome}
            rec["updated_at"] = _utc_now_iso()

        self._mutate(_do)

    def dropped_promises(self) -> List[Dict[str, Any]]:
        """Terminal records whose wake landed but whose promise never continued.

        A promise is dropped when the notification was delivered (``wake.state``
        is ``done``) while the continuation did not run. These are the ones an
        owner still has to act on by hand, so they must be enumerable rather
        than merely inferable from a missing notification line.
        """
        with self._lock:
            records = self._read()
        out = []
        for rec in records.values():
            wake = rec.get("wake")
            if not isinstance(wake, dict) or wake.get("state") != "done":
                continue
            if wake.get("resumed") is False:
                out.append(rec)
        return out

    # --- startup / introspection ---------------------------------------------------
    def reconcile_on_start(self, *, now: Optional[float] = None) -> int:
        """Re-arm monitoring records after a restart; wakes stay journaled.

        Returns how many monitoring records were scheduled for an immediate
        re-poll so no terminal transition is missed across the restart.
        """
        now_value = self._clock() if now is None else float(now)

        def _do(records):
            count = 0
            for rec in records.values():
                if rec.get("state") == STATE_MONITORING:
                    rec["next_poll_epoch"] = now_value
                    count += 1
            self._prune(records, now=now_value)
            return count

        return int(self._mutate(_do) or 0)

    def due(self, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        now_value = self._clock() if now is None else float(now)
        with self._lock:
            records = self._read()
        return [
            rec
            for rec in records.values()
            if rec.get("state") == STATE_MONITORING
            and float(rec.get("next_poll_epoch") or 0) <= now_value
        ]

    def records(self) -> List[Dict[str, Any]]:
        with self._lock:
            records = self._read()
        self._prune(records, now=self._clock())
        return list(records.values())

    def get(self, wait_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._read().get(wait_id)


# --- active-turn route publication ----------------------------------------------

_active_turns_lock = threading.Lock()


def _read_active_turns(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _write_active_turns(path: Path, entries: Dict[str, Dict[str, Any]]) -> None:
    payload = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        _atomic_write_bytes(path, payload)
    except SessionStoreDurabilityError:
        logger.warning("External-wait active-turn dir-fsync unconfirmed; state written")


def conversation_key_of(user_id: Any, chat_id: Any) -> str:
    return f"{int(user_id)}:{int(chat_id)}"


def publish_active_turn(
    home: Path,
    *,
    user_id: int,
    chat_id: int,
    session_id: Optional[str],
    now: Optional[float] = None,
) -> None:
    """Publish which conversation this turn serves (provider-neutral).

    The agent-side CLI binds registrations to this route. Best-effort: a
    hiccup must never break the turn itself.
    """
    path = default_active_turns_path(home)
    now_value = time.time() if now is None else float(now)
    try:
        with _active_turns_lock:
            entries = _read_active_turns(path)
            entries[conversation_key_of(user_id, chat_id)] = {
                "user_id": int(user_id),
                "chat_id": int(chat_id),
                "session_id": session_id or None,
                "heartbeat_epoch": now_value,
            }
            _write_active_turns(path, entries)
    except Exception as exc:  # pragma: no cover - deliberately fail-open
        logger.warning("External-wait active-turn publish failed: %s", type(exc).__name__)


def clear_active_turn(
    home: Path,
    *,
    user_id: int,
    chat_id: int,
    session_id: Optional[str] = None,
) -> None:
    """Clear the route at turn end, only when it still names this session."""
    path = default_active_turns_path(home)
    try:
        with _active_turns_lock:
            entries = _read_active_turns(path)
            key = conversation_key_of(user_id, chat_id)
            entry = entries.get(key)
            if entry is None:
                return
            if session_id is not None and entry.get("session_id") not in (None, session_id):
                return  # a newer turn already owns this conversation entry
            entries.pop(key, None)
            _write_active_turns(path, entries)
    except Exception as exc:  # pragma: no cover - deliberately fail-open
        logger.warning("External-wait active-turn clear failed: %s", type(exc).__name__)


def resolve_active_route(
    home: Path, *, now: Optional[float] = None
) -> Optional[Dict[str, Any]]:
    """Return the single fresh active route, or None when absent/ambiguous.

    Fail-closed by contract (#740): when no turn is active or more than one
    conversation is mid-turn, the CLI must refuse to register rather than
    bind a wait to a guessed conversation.
    """
    path = default_active_turns_path(home)
    now_value = time.time() if now is None else float(now)
    with _active_turns_lock:
        entries = _read_active_turns(path)
    fresh = [
        entry
        for entry in entries.values()
        if now_value - float(entry.get("heartbeat_epoch") or 0) <= ACTIVE_ROUTE_TTL_SECONDS
    ]
    if len(fresh) != 1:
        return None
    return fresh[0]


__all__ = [
    "ACTIVE_ROUTE_TTL_SECONDS",
    "ExternalWaitRegistry",
    "ExternalWaitValidationError",
    "SOURCE_GITHUB_PR_CHECKS",
    "STATE_MONITORING",
    "TERMINAL_CANCELLED",
    "TERMINAL_EXPIRED",
    "TERMINAL_FAILURE",
    "TERMINAL_MONITOR_ERROR",
    "TERMINAL_OWNER_CANCEL",
    "TERMINAL_STATUSES",
    "TERMINAL_SUPERSEDED",
    "TERMINAL_SUCCESS",
    "clear_active_turn",
    "conversation_key_of",
    "default_active_turns_path",
    "default_registry_path",
    "publish_active_turn",
    "resolve_active_route",
    "validate_head_sha",
    "validate_pr_number",
    "validate_repo",
    "validate_summary",
]
