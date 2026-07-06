"""Persistent task ledger — the Hermes/A2A task-lifecycle model, bridge-sized.

Why this exists
---------------
The bridge used to *infer* request status from in-memory stream liveness
(typing keepalives, an elapsed-timer heartbeat message). Every crash, hang, or
race left indicators stranded, and each fix (stall timers, id sweeps) patched
one symptom of the same design. The Hermes broker (jinwon-int/a2a-nexus) solves
this class of problem structurally: tasks have an explicit lifecycle with
terminal states (``task-projection.ts``), records are persisted so any process
can reconcile them, and terminal transitions are delivered through an outbox
that retries until they land (``broker-cleanup-discovery.ts``).

This module ports that model at bridge scale:

- Every Telegram request gets a ledger record with an explicit state:
  ``working`` / ``input-required`` and the terminals ``completed`` / ``failed``
  / ``canceled`` / ``timeout`` / ``interrupted``.
- The transient "⏳ Working" status message is a *projection* of the record —
  its id is registered here, and terminal transitions must clean it up.
- If the terminal cleanup edit/delete fails, a ``terminal_op`` stays on the
  record (the mini terminal-outbox) and is retried on later ticks/startups.
- On startup, any record left non-terminal by a dead process transitions to
  ``interrupted`` and its message is cleaned — an indicator can never outlive
  its task record.

Pure stdlib, fail-open: a ledger hiccup must never break message delivery.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- states (A2A vocabulary, plus the bridge-specific terminals) --------------
WORKING = "working"
INPUT_REQUIRED = "input-required"
COMPLETED = "completed"
FAILED = "failed"
CANCELED = "canceled"
TIMEOUT = "timeout"
INTERRUPTED = "interrupted"

TERMINAL_STATES = frozenset({COMPLETED, FAILED, CANCELED, TIMEOUT, INTERRUPTED})

#: Give up retrying a terminal op after this many attempts (Telegram cannot
#: delete messages older than ~48h; an immortal op would pin the record).
MAX_TERMINAL_OP_ATTEMPTS = 20

#: What an `interrupted` task's status message is edited into (op kind "notice").
INTERRUPTED_NOTICE_TEXT = "🛑 Interrupted by a bridge restart — please resend your last message."


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_task_ledger_path(bot_data_dir: Path) -> Path:
    return Path(bot_data_dir) / "tasks.json"


def ledger_path_for(
    bot_data_dir: Optional[Path], override: Optional[Path] = None
) -> Optional[Path]:
    """Resolve the ledger path from config, or None when no data dir is known."""
    if override:
        return Path(override)
    if bot_data_dir:
        return default_task_ledger_path(Path(bot_data_dir))
    return None


class TaskLedger:
    """Small persistent registry of bridge request lifecycles.

    Only *active* records and records with a pending terminal op are kept —
    a terminal transition whose cleanup succeeded purges the record, so the
    file stays tiny. All methods are fail-open and idempotent.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()

    # --- persistence ----------------------------------------------------------
    def _read(self) -> Dict[str, Dict[str, Any]]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except Exception as exc:  # pragma: no cover - deliberately fail-open
            logger.warning("Task ledger read failed: %s", type(exc).__name__)
            return {}
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    def _write(self, records: Dict[str, Dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(records, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)

    def _mutate(self, fn) -> Any:
        """Run ``fn(records) -> result`` under the lock, persisting mutations."""
        with self._lock:
            try:
                records = self._read()
                result = fn(records)
                self._write(records)
                return result
            except Exception as exc:  # pragma: no cover - deliberately fail-open
                logger.warning("Task ledger update failed: %s", type(exc).__name__)
                return None

    # --- lifecycle -------------------------------------------------------------
    def create(self, user_id: int, chat_id: int) -> str:
        """Register a new in-flight request; returns its task id."""
        task_id = uuid.uuid4().hex

        def _do(records):
            records[task_id] = {
                "task_id": task_id,
                "user_id": int(user_id),
                "chat_id": int(chat_id),
                "state": WORKING,
                "status_message_id": None,
                "created_at": _utc_now_iso(),
                "updated_at": _utc_now_iso(),
                "terminal_op": None,
            }

        self._mutate(_do)
        return task_id

    def set_state(self, task_id: Optional[str], state: str) -> None:
        """Non-terminal transition (working <-> input-required)."""
        if not task_id or state in TERMINAL_STATES:
            return

        def _do(records):
            rec = records.get(task_id)
            if rec and rec.get("state") not in TERMINAL_STATES:
                rec["state"] = state
                rec["updated_at"] = _utc_now_iso()

        self._mutate(_do)

    def set_status_message(
        self, task_id: Optional[str], message_id: Optional[int]
    ) -> None:
        """Register (or clear) the transient status message projected from this task."""
        if not task_id:
            return

        def _do(records):
            rec = records.get(task_id)
            if rec is not None:
                rec["status_message_id"] = message_id
                rec["updated_at"] = _utc_now_iso()

        self._mutate(_do)

    def finish(
        self,
        task_id: Optional[str],
        state: str,
        *,
        cleanup_done: bool = True,
        op_kind: str = "delete",
    ) -> None:
        """Terminal transition. Idempotent: absent/already-terminal is a no-op.

        When ``cleanup_done`` is False and the record still has a status
        message, a ``terminal_op`` is kept for retry (the mini terminal-outbox);
        otherwise the record is purged.
        """
        if not task_id or state not in TERMINAL_STATES:
            return

        def _do(records):
            rec = records.get(task_id)
            if rec is None or rec.get("state") in TERMINAL_STATES:
                return
            message_id = rec.get("status_message_id")
            if cleanup_done or not message_id:
                records.pop(task_id, None)
                return
            rec["state"] = state
            rec["updated_at"] = _utc_now_iso()
            rec["terminal_op"] = {
                "kind": op_kind,
                "chat_id": rec.get("chat_id"),
                "message_id": message_id,
                "attempts": 0,
            }

        self._mutate(_do)

    # --- terminal outbox --------------------------------------------------------
    def pending_terminal_ops(self) -> List[Tuple[str, Dict[str, Any]]]:
        with self._lock:
            records = self._read()
        return [
            (task_id, rec["terminal_op"])
            for task_id, rec in records.items()
            if isinstance(rec.get("terminal_op"), dict)
        ]

    def resolve_terminal_op(self, task_id: str, *, success: bool) -> None:
        """Mark a retried op done (purge) or bump attempts (give up past cap)."""

        def _do(records):
            rec = records.get(task_id)
            if rec is None:
                return
            if success:
                records.pop(task_id, None)
                return
            op = rec.get("terminal_op")
            if not isinstance(op, dict):
                records.pop(task_id, None)
                return
            op["attempts"] = int(op.get("attempts") or 0) + 1
            if op["attempts"] >= MAX_TERMINAL_OP_ATTEMPTS:
                logger.warning(
                    "Task ledger: giving up terminal op for %s after %d attempts",
                    task_id,
                    op["attempts"],
                )
                records.pop(task_id, None)

        self._mutate(_do)

    # --- startup reconciliation ---------------------------------------------------
    def reconcile_interrupted(self, *, op_kind: str = "delete") -> int:
        """Transition every non-terminal record to ``interrupted``.

        Called once at startup: every record in the file was written by a
        previous process, so anything still non-terminal died with it. Records
        with a status message keep a terminal op for the caller to drain;
        message-less records are purged. Returns how many were transitioned.
        """

        def _do(records):
            count = 0
            for task_id in list(records.keys()):
                rec = records[task_id]
                if rec.get("state") in TERMINAL_STATES:
                    continue
                count += 1
                message_id = rec.get("status_message_id")
                if not message_id:
                    records.pop(task_id, None)
                    continue
                rec["state"] = INTERRUPTED
                rec["updated_at"] = _utc_now_iso()
                rec["terminal_op"] = {
                    "kind": op_kind,
                    "chat_id": rec.get("chat_id"),
                    "message_id": message_id,
                    "attempts": 0,
                }
            return count

        result = self._mutate(_do)
        return int(result or 0)

    # --- introspection -------------------------------------------------------------
    def records(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._read().values())
