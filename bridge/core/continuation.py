"""Durable yield-and-continue queue (#1113).

An agent that has more work than fits in one turn registers the *next bundle*
and ends its turn normally; the bridge-side continuation monitor then starts a
fresh autonomous turn for it. One pending continuation per conversation — a
new registration replaces the previous pending one, so the queue can never
stack the way external waits once did (#1110).

Records live in ``queue.json`` next to the other bridge state; the file is
rewritten atomically under a lock with a ``.bak`` backup, the same durability
contract as the external-wait registry (#740). The daily counter and the
consecutive-failure counter live beside the records so a bridge restart never
loses the loop-guard state.

States: ``pending`` → ``running`` → ``done`` | ``failed``. A pending record
can move to ``cap-hold`` (daily tripwire hit — the owner confirms with
/continue to keep going) or ``cancelled`` (/stop or CLI cancel; the owner's
word always wins). Terminal records are pruned after the retention window.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from telegram_bot.utils.secure_fs import (
    SessionStoreDurabilityError,
    _atomic_write_bytes,
)

logger = logging.getLogger(__name__)

STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CAP_HOLD = "cap-hold"
STATE_CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({STATE_DONE, STATE_FAILED, STATE_CANCELLED})

#: Terminal records are kept for audit, then pruned.
TERMINAL_RETENTION_SECONDS = 24 * 60 * 60

#: Stop auto-continuing after this many consecutive turn failures.
MAX_CONSECUTIVE_FAILURES = 3

MAX_PROMPT_CHARS = 4000


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def default_queue_path(home: Path) -> Path:
    return Path(home) / "queue.json"


def conversation_key_of(user_id: Any, chat_id: Any) -> str:
    return f"{int(user_id)}:{int(chat_id)}"


class ContinuationValidationError(ValueError):
    pass


def validate_prompt(prompt: Any) -> str:
    text = " ".join(str(prompt or "").split())
    if not text:
        raise ContinuationValidationError("prompt must not be empty")
    if len(text) > MAX_PROMPT_CHARS:
        raise ContinuationValidationError(
            f"prompt exceeds {MAX_PROMPT_CHARS} chars after normalization"
        )
    return text


class ContinuationQueue:
    """Locked read-modify-write store with a single-file backup."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = lambda: time.time()):
        self._path = Path(path)
        self._backup_path = self._path.with_suffix(self._path.suffix + ".bak")
        self._clock = clock
        self._lock = threading.Lock()

    # --- file plumbing ---------------------------------------------------------
    def _read_path(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except Exception:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("records"), dict):
            return None
        data.setdefault("counters", {})
        return data

    def _read(self) -> Dict[str, Any]:
        data = self._read_path(self._path)
        if data is None:
            backup = self._read_path(self._backup_path)
            if backup is not None:
                logger.warning("Continuation queue recovered from backup: %s", self._path)
                return backup
            return {"records": {}, "counters": {}}
        return data

    def _write(self, data: Dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
        try:
            previous = self._path.read_bytes() if self._path.exists() else None
            if previous:
                _atomic_write_bytes(self._backup_path, previous)
            _atomic_write_bytes(self._path, payload)
        except SessionStoreDurabilityError:
            logger.warning("Continuation queue dir-fsync unconfirmed; state written")

    def _mutate(self, fn) -> Any:
        with self._lock:
            data = self._read()
            result = fn(data["records"], data["counters"])
            self._write(data)
            return result

    @staticmethod
    def _prune(records: Dict[str, Dict[str, Any]], *, now: float) -> None:
        for cid in [
            cid
            for cid, rec in records.items()
            if rec.get("state") in TERMINAL_STATES
            and now - float(rec.get("updated_epoch") or 0) > TERMINAL_RETENTION_SECONDS
        ]:
            records.pop(cid, None)

    # --- registration ------------------------------------------------------------
    def register(
        self,
        *,
        user_id: int,
        chat_id: int,
        session_id: Optional[str],
        prompt: str,
        now: Optional[float] = None,
    ) -> Tuple[str, List[str]]:
        """Register the next bundle; returns (id, replaced pending ids).

        One pending continuation per conversation: a new registration replaces
        any pending/cap-hold record (the old bundle is cancelled, never run).
        A *running* record is the current turn itself and is never touched.
        The consecutive-failure counter is deliberately NOT reset here: a
        register-then-crash loop also registers each time, and resetting
        would let it run forever. Success (done), /stop, and /continue are
        what re-arm the counter.
        """
        prompt = validate_prompt(prompt)
        now = self._clock() if now is None else float(now)
        key = conversation_key_of(user_id, chat_id)

        def _do(records, counters):
            self._prune(records, now=now)
            replaced: List[str] = []
            for cid, rec in list(records.items()):
                if conversation_key_of(rec.get("user_id"), rec.get("chat_id")) != key:
                    continue
                if rec.get("state") not in {STATE_PENDING, STATE_CAP_HOLD}:
                    continue
                rec["state"] = STATE_CANCELLED
                rec["updated_at"] = _utc_now_iso()
                rec["updated_epoch"] = now
                replaced.append(cid)
            cid = uuid.uuid4().hex[:12]
            records[cid] = {
                "continuation_id": cid,
                "user_id": int(user_id),
                "chat_id": int(chat_id),
                "session_id": session_id or None,
                "prompt": prompt,
                "state": STATE_PENDING,
                "created_at": _utc_now_iso(),
                "created_epoch": now,
                "updated_at": _utc_now_iso(),
                "updated_epoch": now,
                "started_at": None,
                "last_error": None,
            }
            return cid, replaced

        return self._mutate(_do)

    # --- monitor transitions -------------------------------------------------------
    def pending(self) -> List[Dict[str, Any]]:
        with self._lock:
            records = self._read()["records"]
        return [rec for rec in records.values() if rec.get("state") == STATE_PENDING]

    def mark_running(self, continuation_id: str, *, now: Optional[float] = None) -> bool:
        now = self._clock() if now is None else float(now)

        def _do(records, counters):
            rec = records.get(continuation_id)
            if rec is None or rec.get("state") != STATE_PENDING:
                return False
            rec["state"] = STATE_RUNNING
            rec["started_at"] = _utc_now_iso()
            rec["updated_at"] = _utc_now_iso()
            rec["updated_epoch"] = now
            key = conversation_key_of(rec.get("user_id"), rec.get("chat_id"))
            counter = counters.setdefault(
                key, {"day": "", "count": 0, "consecutive_failures": 0}
            )
            day = time.strftime("%Y-%m-%d", time.gmtime(now))
            if counter.get("day") != day:
                counter["day"] = day
                counter["count"] = 0
            counter["count"] = int(counter.get("count") or 0) + 1
            return True

        return bool(self._mutate(_do))

    def mark_done(self, continuation_id: str, *, now: Optional[float] = None) -> None:
        now = self._clock() if now is None else float(now)

        def _do(records, counters):
            rec = records.get(continuation_id)
            if rec is None or rec.get("state") != STATE_RUNNING:
                return
            rec["state"] = STATE_DONE
            rec["updated_at"] = _utc_now_iso()
            rec["updated_epoch"] = now
            key = conversation_key_of(rec.get("user_id"), rec.get("chat_id"))
            counter = counters.get(key)
            if counter is not None:
                counter["consecutive_failures"] = 0

        self._mutate(_do)

    def mark_failed(
        self, continuation_id: str, reason: str, *, now: Optional[float] = None
    ) -> int:
        """Running -> failed; returns the consecutive-failure count."""
        now = self._clock() if now is None else float(now)

        def _do(records, counters):
            rec = records.get(continuation_id)
            if rec is None or rec.get("state") != STATE_RUNNING:
                return -1
            rec["state"] = STATE_FAILED
            rec["last_error"] = str(reason)[:120]
            rec["updated_at"] = _utc_now_iso()
            rec["updated_epoch"] = now
            key = conversation_key_of(rec.get("user_id"), rec.get("chat_id"))
            counter = counters.setdefault(
                key, {"day": "", "count": 0, "consecutive_failures": 0}
            )
            counter["consecutive_failures"] = int(
                counter.get("consecutive_failures") or 0
            ) + 1
            return int(counter["consecutive_failures"])

        return int(self._mutate(_do))

    def mark_cap_hold(
        self, continuation_id: str, reason: str = "", *, now: Optional[float] = None
    ) -> bool:
        """Park a pending bundle until the owner confirms (/continue)."""
        now = self._clock() if now is None else float(now)

        def _do(records, counters):
            rec = records.get(continuation_id)
            if rec is None or rec.get("state") != STATE_PENDING:
                return False
            rec["state"] = STATE_CAP_HOLD
            if reason:
                rec["last_error"] = str(reason)[:120]
            rec["updated_at"] = _utc_now_iso()
            rec["updated_epoch"] = now
            return True

        return bool(self._mutate(_do))

    def repend_cap_holds(
        self, user_id: int, chat_id: int, *, now: Optional[float] = None
    ) -> List[str]:
        """Owner-confirmed continuation (/continue): cap-hold -> pending.

        Also resets the daily counter, so the confirmation genuinely lets the
        queue keep going instead of re-tripping on the next tick.
        """
        now = self._clock() if now is None else float(now)
        key = conversation_key_of(user_id, chat_id)

        def _do(records, counters):
            repended: List[str] = []
            for cid, rec in records.items():
                if conversation_key_of(rec.get("user_id"), rec.get("chat_id")) != key:
                    continue
                if rec.get("state") != STATE_CAP_HOLD:
                    continue
                rec["state"] = STATE_PENDING
                rec["updated_at"] = _utc_now_iso()
                rec["updated_epoch"] = now
                repended.append(cid)
            if repended:
                counter = counters.get(key)
                if counter is not None:
                    counter["count"] = 0
                    counter["consecutive_failures"] = 0
            return repended

        return self._mutate(_do)

    def cancel_for(
        self,
        user_id: int,
        chat_id: int,
        *,
        include_running: bool = False,
        now: Optional[float] = None,
    ) -> List[str]:
        """Cancel this conversation's queued continuations (/stop wins).

        ``include_running`` marks the in-flight record cancelled too; the
        monitor's completion path then no-ops because the record already left
        ``running``, so a user stop is never miscounted as a turn failure.
        """
        now = self._clock() if now is None else float(now)
        key = conversation_key_of(user_id, chat_id)
        states = {STATE_PENDING, STATE_CAP_HOLD}
        if include_running:
            states = states | {STATE_RUNNING}

        def _do(records, counters):
            cancelled: List[str] = []
            for cid, rec in records.items():
                if conversation_key_of(rec.get("user_id"), rec.get("chat_id")) != key:
                    continue
                if rec.get("state") not in states:
                    continue
                rec["state"] = STATE_CANCELLED
                rec["updated_at"] = _utc_now_iso()
                rec["updated_epoch"] = now
                cancelled.append(cid)
            if cancelled:
                counter = counters.get(key)
                if counter is not None:
                    counter["consecutive_failures"] = 0
            return cancelled

        return self._mutate(_do)

    def cancel(self, continuation_id: str, *, now: Optional[float] = None) -> bool:
        now = self._clock() if now is None else float(now)

        def _do(records, counters):
            rec = records.get(continuation_id)
            if rec is None or rec.get("state") in TERMINAL_STATES:
                return False
            rec["state"] = STATE_CANCELLED
            rec["updated_at"] = _utc_now_iso()
            rec["updated_epoch"] = now
            return True

        return bool(self._mutate(_do))

    # --- reads ---------------------------------------------------------------------
    def records(self) -> List[Dict[str, Any]]:
        with self._lock:
            data = self._read()
        records = data["records"]
        self._prune(records, now=self._clock())
        return list(records.values())

    def get(self, continuation_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._read()["records"].get(continuation_id)

    def counter_for(self, user_id: int, chat_id: int) -> Dict[str, Any]:
        key = conversation_key_of(user_id, chat_id)
        with self._lock:
            counters = self._read()["counters"]
        return dict(counters.get(key) or {"day": "", "count": 0, "consecutive_failures": 0})


__all__ = [
    "ContinuationQueue",
    "ContinuationValidationError",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_PROMPT_CHARS",
    "STATE_CANCELLED",
    "STATE_CAP_HOLD",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_PENDING",
    "STATE_RUNNING",
    "TERMINAL_STATES",
    "conversation_key_of",
    "default_queue_path",
    "validate_prompt",
]
