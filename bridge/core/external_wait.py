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
import os
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
#: Merge-queue landing watch (#2118): a queued PR is dropped silently when its
#: speculative group run fails — no check rollup or PR state changes — so the
#: check-rollup source can never wake on it.
SOURCE_GITHUB_MERGE_QUEUE = "github_merge_queue"
#: A single GitHub Actions workflow run (#2590): a build, a release upload, a
#: dispatched job — anything the agent started and would otherwise watch with
#: an ad-hoc polling loop that ends with the turn. Keyed by run id, not PR.
SOURCE_GITHUB_ACTIONS_RUN = "github_actions_run"
#: A run-in-background task of the conversation's own provider session
#: (#2605, the #2081 §3 follow-up). Nothing polls it: the harness continues
#: the session itself when the task ends, and the bridge's task tracker
#: (``claude_session_task_tracking``) closes the record from that frame. The
#: registry entry only carries the agent's label and deadline so the route's
#: status message can name the work; a finish never journals a wake.
SOURCE_LOCAL_TASK = "local_task"
SOURCES = frozenset(
    {SOURCE_GITHUB_PR_CHECKS, SOURCE_GITHUB_MERGE_QUEUE, SOURCE_GITHUB_ACTIONS_RUN, SOURCE_LOCAL_TASK}
)
#: Sources the monitor never asks a transport about.
UNPOLLED_SOURCES = frozenset({SOURCE_LOCAL_TASK})

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
#: Merge-queue outcomes (#2118).
TERMINAL_MERGED = "merged"
TERMINAL_EVICTED = "evicted"
TERMINAL_CLOSED = "closed"
#: A local-task watch whose provider session went away before the task
#: reported a terminal frame (#2605): the outcome is unknown, not failed.
TERMINAL_LOST = "lost"

TERMINAL_STATUSES = frozenset(
    {
        TERMINAL_SUCCESS,
        TERMINAL_FAILURE,
        TERMINAL_CANCELLED,
        TERMINAL_SUPERSEDED,
        TERMINAL_MONITOR_ERROR,
        TERMINAL_EXPIRED,
        TERMINAL_OWNER_CANCEL,
        TERMINAL_MERGED,
        TERMINAL_EVICTED,
        TERMINAL_CLOSED,
        TERMINAL_LOST,
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
#: Same bound as the task tracker's id filter: opaque, body-free, ≤256 chars.
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_MAX_SUMMARY_CHARS = 200
_MAX_RUN_LABEL_CHARS = 80
#: Live run status kept on an Actions-run record for display only (#2590).
_RUN_STATUS_RE = re.compile(r"^[a-z_]{1,24}$")


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


def validate_run_id(run_id: Any) -> int:
    """GitHub Actions run id (``databaseId``) — a positive integer (#2590)."""
    try:
        value = int(str(run_id).strip())
    except (TypeError, ValueError):
        raise ExternalWaitValidationError("run id must be a positive integer")
    if value <= 0 or value >= 10**15:
        raise ExternalWaitValidationError("run id must be a positive integer")
    return value


def validate_run_label(label: Optional[str]) -> str:
    """Bounded single-line workflow display name (may be empty, #2590)."""
    text = " ".join(str(label or "").split())[:_MAX_RUN_LABEL_CHARS]
    return text.strip()


def validate_task_id(task_id: Any) -> str:
    """One opaque background-task id (#2605); never a command or output body."""
    candidate = str(task_id or "").strip()
    if not _TASK_ID_RE.fullmatch(candidate):
        raise ExternalWaitValidationError("task id must be 1-256 chars of [A-Za-z0-9_.-]")
    return candidate


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


_DETAIL_KEY_RE = re.compile(r"^[a-z_]{1,32}$")
_DETAIL_TEXT_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")


def _same_route(rec: Dict[str, Any], user_id: int, chat_id: int) -> bool:
    try:
        return int(rec.get("user_id")) == int(user_id) and int(rec.get("chat_id")) == int(chat_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _bounded_detail(detail: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Keep only short identifier-shaped values (ids, reason codes)."""
    out: Dict[str, Any] = {}
    for key, value in (detail or {}).items():
        if not isinstance(key, str) or not _DETAIL_KEY_RE.match(key) or len(out) >= 4:
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 0 <= value < 10**15:
            out[key] = value
        elif isinstance(value, str) and _DETAIL_TEXT_RE.match(value):
            out[key] = value
    return out


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
        # Exact bytes of the last payload this process wrote; reused as the
        # previous-good backup source (see TaskLedger._write).
        self._last_payload: Optional[bytes] = None

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
                previous = self._last_payload
                if previous is None:
                    previous = self._path.read_bytes()
                    if previous.strip():
                        json.loads(previous)
                    else:
                        previous = b""
                if previous.strip():
                    _atomic_write_bytes(self._backup_path, previous)
        except Exception:
            pass
        try:
            _atomic_write_bytes(self._path, payload)
        except SessionStoreDurabilityError:
            logger.warning("External-wait registry dir-fsync unconfirmed; state written")
        self._last_payload = payload

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
        source: str = SOURCE_GITHUB_PR_CHECKS,
        run_id: Any = None,
        run_label: Optional[str] = None,
        run_number: Any = None,
        task_id: Any = None,
    ) -> str:
        """Register one wait; returns its id (idempotent per natural key).

        An Actions-run wait (#2590) is keyed by ``run_id`` instead of a PR:
        ``pr_number`` is stored as ``None`` and ``head_sha`` is optional
        (the run's head, kept for display and webhook acceleration).

        A local-task wait (#2605) is keyed by ``task_id``; ``repo`` is unused
        (stored empty) and the record is never due for polling — its
        ``next_poll_epoch`` is the deadline, so the monitor only ever sees it
        to expire it.
        """
        if source not in SOURCES:
            raise ExternalWaitValidationError(f"unknown wait source: {source}")
        extra: Dict[str, Any] = {}
        natural: tuple
        if source == SOURCE_LOCAL_TASK:
            repo = ""
            pr_number = None
            head_sha = ""
            task_id = validate_task_id(task_id)
            extra = {"task_id": task_id}
            natural = (source, task_id, int(user_id), int(chat_id))
        elif source == SOURCE_GITHUB_ACTIONS_RUN:
            repo = validate_repo(repo)
            run_id = validate_run_id(run_id)
            pr_number = None
            head_sha = validate_head_sha(head_sha) if str(head_sha or "").strip() else ""
            try:
                number = int(run_number) if run_number is not None and str(run_number).strip() else None
            except (TypeError, ValueError):
                number = None
            extra = {
                "run_id": run_id,
                "run_label": validate_run_label(run_label),
                "run_number": number if number is not None and 0 < number < 10**9 else None,
                "run_status": None,
            }
            natural = (source, repo, run_id, int(user_id), int(chat_id))
        else:
            repo = validate_repo(repo)
            pr_number = validate_pr_number(pr_number)
            head_sha = validate_head_sha(head_sha)
            natural = (source, repo, pr_number, head_sha, int(user_id), int(chat_id))
        summary = validate_summary(summary)
        now = self._clock() if now is None else float(now)

        def _natural_of(rec: Dict[str, Any]) -> tuple:
            if rec.get("source") == SOURCE_LOCAL_TASK:
                return (rec.get("source"), rec.get("task_id"), rec.get("user_id"), rec.get("chat_id"))
            if rec.get("source") == SOURCE_GITHUB_ACTIONS_RUN:
                return (rec.get("source"), rec.get("repo"), rec.get("run_id"), rec.get("user_id"), rec.get("chat_id"))
            return (
                rec.get("source"),
                rec.get("repo"),
                rec.get("pr_number"),
                rec.get("head_sha"),
                rec.get("user_id"),
                rec.get("chat_id"),
            )

        def _do(records):
            self._prune(records, now=now)
            for wait_id, rec in records.items():
                if rec.get("state") != STATE_MONITORING:
                    continue
                if _natural_of(rec) == natural:
                    return wait_id
            wait_id = uuid.uuid4().hex[:12]
            records[wait_id] = {
                "wait_id": wait_id,
                "source": source,
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
                # Unpolled sources surface to the monitor only at their deadline.
                "next_poll_epoch": (
                    now + max(60.0, float(timeout_seconds)) if source in UNPOLLED_SOURCES else now
                ),
                "poll_interval_seconds": max(5.0, float(poll_interval_seconds)),
                "terminal_status": None,
                "wake": None,
                **extra,
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

    def mark_delivery_failed(self, wait_id: str, reason: str) -> bool:
        """Record that an outbound send for this wait ultimately failed.

        The wake journal tracks the terminal *notification*; the continuation
        content is sent separately, and a raw-send failure there used to leave
        no durable trace — the owner simply never saw the message (2026-08-14:
        ``External-wait resume delivery failed: BadRequest``). Stamp the record
        so the silent-chat risk stays auditable and a later drain can see the
        send did not land.
        """

        def _do(records):
            rec = records.get(wait_id)
            if rec is None:
                return False
            rec["delivery_failed"] = {
                "at": _utc_now_iso(),
                "reason": str(reason)[:64],
            }
            rec["updated_at"] = _utc_now_iso()
            return True

        return bool(self._mutate(_do))

    def mark_queue_seen(self, wait_id: str) -> bool:
        """Remember a merge-queue wait's PR was observed in the queue (#2118).

        Leaving the queue only means eviction after the PR was seen in it;
        before that it may simply not be enqueued yet.
        """

        def _do(records):
            rec = records.get(wait_id)
            if rec is None or rec.get("state") != STATE_MONITORING or rec.get("queue_seen"):
                return False
            rec["queue_seen"] = True
            rec["updated_at"] = _utc_now_iso()
            return True

        return bool(self._mutate(_do))

    def note_run_status(self, wait_id: str, run_status: str) -> bool:
        """Remember an Actions run's live status for display (#2590).

        Only short GitHub status tokens (``queued``, ``in_progress``,
        ``waiting`` …) are kept; the record is rewritten only on a change so
        a long build does not churn the registry every poll. Returns True when
        the stored value changed.
        """
        value = str(run_status or "").strip().lower()
        if not _RUN_STATUS_RE.match(value):
            return False

        def _do(records):
            rec = records.get(wait_id)
            if rec is None or rec.get("state") != STATE_MONITORING:
                return False
            if rec.get("run_status") == value:
                return False
            rec["run_status"] = value
            rec["updated_at"] = _utc_now_iso()
            return True

        return bool(self._mutate(_do))

    def finish(
        self,
        wait_id: str,
        terminal_status: str,
        *,
        now: Optional[float] = None,
        detail: Optional[Dict[str, Any]] = None,
        wake: bool = True,
    ) -> bool:
        """Terminal transition, journaled before any wake. First write wins.

        Returns True only for the monitoring -> terminal transition; the wake
        is marked pending in the same durable write (exactly-once journal).
        ``detail`` carries bounded, body-free facts about the outcome (#2118:
        the failed merge-group run id, or ``reason``), never log content.
        ``wake=False`` (#2605) records the outcome without a wake: the
        conversation is already being continued by something else (the
        harness after a local task), so the monitor must not resume it again.
        """
        if terminal_status not in TERMINAL_STATUSES or terminal_status == TERMINAL_OWNER_CANCEL:
            raise ValueError(f"invalid terminal status: {terminal_status}")
        clean_detail = _bounded_detail(detail)

        def _do(records):
            rec = records.get(wait_id)
            if rec is None or rec.get("state") != STATE_MONITORING:
                return False
            now_value = self._clock() if now is None else float(now)
            rec["state"] = terminal_status
            rec["terminal_status"] = terminal_status
            rec["completed_epoch"] = now_value
            rec["updated_at"] = _utc_now_iso()
            rec["wake"] = (
                {"state": "pending", "attempts": 0}
                if wake
                else {"state": "skipped", "attempts": 0, "reason": "no-wake"}
            )
            if clean_detail:
                rec["terminal_detail"] = clean_detail
            return True

        return bool(self._mutate(_do))

    def finish_local_tasks(
        self,
        *,
        user_id: int,
        chat_id: int,
        task_id: Optional[str],
        terminal_status: str,
        now: Optional[float] = None,
    ) -> List[str]:
        """Close this route's monitoring local-task waits without a wake (#2605).

        ``task_id=None`` closes every local-task wait of the route (the
        session went away: ``TERMINAL_LOST``). Returns the wait ids that made
        the monitoring -> terminal transition; a route with none is a no-op
        that never touches the file.
        """
        wanted = validate_task_id(task_id) if task_id is not None else None
        with self._lock:
            records = self._read()
        matches = [
            str(rec.get("wait_id") or "")
            for rec in records.values()
            if rec.get("source") == SOURCE_LOCAL_TASK
            and rec.get("state") == STATE_MONITORING
            and _same_route(rec, user_id, chat_id)
            and (wanted is None or rec.get("task_id") == wanted)
        ]
        return [wait_id for wait_id in matches if self.finish(wait_id, terminal_status, now=now, wake=False)]

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
                if rec.get("state") != STATE_MONITORING:
                    continue
                if rec.get("source") == SOURCE_LOCAL_TASK:
                    # No provider session survives a restart, so the task's
                    # terminal frame can never arrive: close the label as
                    # ``lost`` without a wake (#2605). The harness itself
                    # resumes nothing here either.
                    rec["state"] = TERMINAL_LOST
                    rec["terminal_status"] = TERMINAL_LOST
                    rec["completed_epoch"] = now_value
                    rec["updated_at"] = _utc_now_iso()
                    rec["wake"] = {"state": "skipped", "attempts": 0, "reason": "no-wake"}
                    continue
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

    _MISSING_STATE_IDENTITY = ("missing",)

    def state_file_identity(self) -> Optional[tuple]:
        """Cheap stat-based identity of the registry file (no read/parse).

        A missing file is a stable sentinel (empty registry); an unreadable
        one is None so callers never treat it as unchanged.
        """
        try:
            info = os.stat(self._path)
        except FileNotFoundError:
            return self._MISSING_STATE_IDENTITY
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size, info.st_ino)

    def idle_snapshot_identity(self) -> Optional[tuple]:
        """Identity of the state file iff it holds no actionable work.

        Returns the current ``state_file_identity`` only when there is no
        monitoring record and no pending wake — the one situation where time
        alone cannot create work, so a caller may skip re-reading until the
        file identity changes. Any other state returns None.
        """
        identity = self.state_file_identity()
        if identity is None:
            return None
        with self._lock:
            records = self._read()
        for rec in records.values():
            if rec.get("state") == STATE_MONITORING:
                return None
            wake = rec.get("wake")
            if isinstance(wake, dict) and wake.get("state") == "pending":
                return None
        return identity

    def records(self) -> List[Dict[str, Any]]:
        with self._lock:
            records = self._read()
        self._prune(records, now=self._clock())
        return list(records.values())

    def records_for_route(self, user_id: int, chat_id: int) -> List[Dict[str, Any]]:
        """Records bound to one conversation route, oldest first (#2081)."""
        out = [
            rec
            for rec in self.records()
            if rec.get("user_id") == int(user_id) and rec.get("chat_id") == int(chat_id)
        ]
        out.sort(key=lambda rec: (float(rec.get("created_epoch") or 0), str(rec.get("wait_id") or "")))
        return out

    def get(self, wait_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._read().get(wait_id)


def render_waits(records: List[Dict[str, Any]]) -> str:
    """Channel-neutral listing for ``/waits`` (Telegram and Matrix, #2004)."""

    if not records:
        return "No external waits registered."
    lines = []
    for rec in records:
        state = rec.get("state")
        marker = "⏳" if state == STATE_MONITORING else "🏁"
        if rec.get("source") == SOURCE_LOCAL_TASK:
            lines.append(
                f"{marker} `{rec.get('wait_id')}` — background task {rec.get('task_id')}"
                f" (this session) · {state}"
            )
        elif rec.get("source") == SOURCE_GITHUB_ACTIONS_RUN:
            label = str(rec.get("run_label") or "").strip() or "workflow"
            sha = str(rec.get("head_sha") or "")[:8]
            lines.append(
                f"{marker} `{rec.get('wait_id')}` — {rec.get('repo')} Actions run {rec.get('run_id')}"
                f" ({label}){f' @ {sha}' if sha else ''} · {state}"
            )
        else:
            kind = " (merge queue)" if rec.get("source") == SOURCE_GITHUB_MERGE_QUEUE else ""
            lines.append(
                f"{marker} `{rec.get('wait_id')}` — {rec.get('repo')}#{rec.get('pr_number')}"
                f"{kind} @ {str(rec.get('head_sha') or '')[:8]} · {state}"
            )
        summary = str(rec.get("summary") or "").strip()
        if summary:
            lines.append(f"   ↳ {summary}")
    return "External waits (GitHub CI):\n" + "\n".join(lines)


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
    owner: Optional[str] = None,
) -> None:
    """Publish which conversation this turn serves (provider-neutral).

    The agent-side CLI binds registrations to this route. Best-effort: a
    hiccup must never break the turn itself. ``owner`` names the publisher
    (a user turn's generation, or the between-turns autonomous turn) so only
    that publisher's ``clear_active_turn`` removes the entry (#2122).
    """
    path = default_active_turns_path(home)
    now_value = time.time() if now is None else float(now)
    try:
        with _active_turns_lock:
            entries = _read_active_turns(path)
            entry: Dict[str, Any] = {
                "user_id": int(user_id),
                "chat_id": int(chat_id),
                "session_id": session_id or None,
                "heartbeat_epoch": now_value,
            }
            if owner:
                entry["owner"] = str(owner)
            entries[conversation_key_of(user_id, chat_id)] = entry
            _write_active_turns(path, entries)
    except Exception as exc:  # pragma: no cover - deliberately fail-open
        logger.warning("External-wait active-turn publish failed: %s", type(exc).__name__)


def clear_active_turn(
    home: Path,
    *,
    user_id: int,
    chat_id: int,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
) -> None:
    """Clear the route at turn end, only when this publisher still owns it.

    Same-session matching alone let one turn's ``finally`` erase a route
    another turn of the same conversation had re-published (#2122): an
    autonomous between-turns continuation and the next user turn share the
    session id. With ``owner`` set, an entry stamped by a different owner is
    left in place.
    """
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
            if owner is not None and entry.get("owner") not in (None, str(owner)):
                return  # another turn of this conversation re-published it
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
    fresh = _fresh_active_routes(home, now=now)
    if len(fresh) != 1:
        return None
    return fresh[0]


def _fresh_active_routes(home: Path, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
    path = default_active_turns_path(home)
    now_value = time.time() if now is None else float(now)
    with _active_turns_lock:
        entries = _read_active_turns(path)
    return [
        entry
        for entry in entries.values()
        if now_value - float(entry.get("heartbeat_epoch") or 0) <= ACTIVE_ROUTE_TTL_SECONDS
    ]


def describe_route_unavailable(home: Path, *, now: Optional[float] = None) -> Dict[str, Any]:
    """Body-free diagnosis for a ``route-unavailable`` refusal (#2122).

    ``none`` and ``ambiguous`` need different fixes (no turn published vs. a
    stale or concurrent route), so the CLI reports which one it saw and the
    file it scanned. Only counts and the path — never conversation ids.
    """
    path = default_active_turns_path(home)
    fresh = _fresh_active_routes(home, now=now)
    return {
        "fresh_routes": len(fresh),
        "reason": "none" if not fresh else "ambiguous",
        "routes_path": str(path),
    }


__all__ = [
    "render_waits",
    "ACTIVE_ROUTE_TTL_SECONDS",
    "ExternalWaitRegistry",
    "ExternalWaitValidationError",
    "SOURCE_GITHUB_ACTIONS_RUN",
    "SOURCE_GITHUB_MERGE_QUEUE",
    "SOURCE_GITHUB_PR_CHECKS",
    "SOURCES",
    "STATE_MONITORING",
    "TERMINAL_CANCELLED",
    "TERMINAL_CLOSED",
    "TERMINAL_EVICTED",
    "TERMINAL_MERGED",
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
    "describe_route_unavailable",
    "publish_active_turn",
    "resolve_active_route",
    "validate_head_sha",
    "validate_pr_number",
    "validate_repo",
    "validate_run_id",
    "validate_run_label",
    "validate_summary",
]
