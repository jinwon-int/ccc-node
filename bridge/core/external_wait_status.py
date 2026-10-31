"""One short per-conversation status message for external waits (#2081).

Why this exists
---------------
A registered CI wait (:mod:`telegram_bot.core.external_wait`) is durable, but
the conversation only learns about it from the agent's prose. After the turn
ends the chat shows nothing that says "the bridge is still watching PR #598";
the owner has to run ``/waits`` to find out. This module projects the route's
non-terminal waits into ONE status message that is sent once, edited as waits
finish, and deleted when nothing is left to show.

Channel-neutral by design: the renderer, the planner and the id store know
nothing about a chat network. The Telegram projection lives in
:mod:`telegram_bot.core.bot_wait_status` (stage 1, #2081) and the Matrix one
in :mod:`telegram_bot.core.matrix.wait_status` (stage 2, #2088); both share
the flag, the startup route selection and the plan below.

Body-free by contract: the store holds message ids and a text hash, never the
wait summary; the rendered summary passes through credential redaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from telegram_bot.core.external_wait import (
    STATE_MONITORING,
    SOURCE_GITHUB_ACTIONS_RUN,
    SOURCE_GITHUB_MERGE_QUEUE,
    SOURCE_LOCAL_TASK,
    TERMINAL_CANCELLED,
    TERMINAL_CLOSED,
    TERMINAL_EVICTED,
    TERMINAL_EXPIRED,
    TERMINAL_FAILURE,
    TERMINAL_LOST,
    TERMINAL_MERGED,
    TERMINAL_MONITOR_ERROR,
    TERMINAL_OWNER_CANCEL,
    TERMINAL_SUCCESS,
    TERMINAL_SUPERSEDED,
    conversation_key_of,
    validate_summary,
)
from telegram_bot.core.external_wait_monitor import ExternalWaitMonitor
from telegram_bot.utils.redaction import redact_credentials
from telegram_bot.utils.secure_fs import (
    SessionStoreDurabilityError,
    _atomic_write_bytes,
)

logger = logging.getLogger(__name__)

#: One flag for every channel projection (default on; ``false`` disables).
STATUS_ENV_FLAG = "CCC_EXTERNAL_WAIT_STATUS"
#: Startup reconcile touches at most this many routes (one channel call each).
MAX_RECONCILE_ROUTES = 50

#: Terminal outcomes younger than this still appear on the message; older
#: ones are dropped (and an all-stale message is deleted).
RECENT_TERMINAL_SECONDS = 30 * 60
#: Hard cap on rendered lines so the message stays "short".
MAX_STATUS_LINES = 10
_MAX_SUMMARY_DISPLAY_CHARS = 80

#: Clock times follow the repo-wide reporting convention (KST fixed offset,
#: as in ``usage_meter``) so the HH:MM stamps match the rest of the bridge.
_KST = timezone(timedelta(hours=9), name="KST")

WaitStatusAction = Literal["send", "edit", "delete", "move", "noop"]

#: Terminal line wording mirrors ``external_wait_monitor._WAKE_HEADLINE``.
_TERMINAL_LINE = {
    TERMINAL_SUCCESS: "✅ CI green → continuing",
    TERMINAL_FAILURE: "❌ CI failed → investigating",
    TERMINAL_CANCELLED: "⚠️ CI cancelled",
    TERMINAL_SUPERSEDED: "🔀 head moved",
    TERMINAL_EXPIRED: "⏰ expired",
    TERMINAL_MONITOR_ERROR: "⚠️ CI watch failed",
    TERMINAL_OWNER_CANCEL: "🚫 cancelled by owner",
    TERMINAL_MERGED: "✅ merged → continuing",
    TERMINAL_EVICTED: "❌ dropped from merge queue → investigating",
    TERMINAL_CLOSED: "⚠️ closed without merging",
    TERMINAL_LOST: "ℹ️ background watch lost",
}

#: Local background work (#2605) is not CI: its outcome lines say so.
_LOCAL_TERMINAL_LINE = {
    TERMINAL_SUCCESS: "✅ background task finished → continuing",
    TERMINAL_EXPIRED: "⏰ background task watch expired",
    TERMINAL_LOST: "ℹ️ background watch lost (session ended)",
    TERMINAL_OWNER_CANCEL: "🚫 cancelled by owner",
}

#: Pseudo-source for the bridge's own body-free background-task count
#: (#2605 layer 1). Never written to the registry: these records are built
#: per sync from the live session's tracker and live only in the rendered text.
SOURCE_LOCAL_BACKGROUND = "local_background"
#: A finished background task keeps its ✅ line this long (then it drops out
#: like any other stale terminal line).
BACKGROUND_FINISHED_RETENTION_SECONDS = RECENT_TERMINAL_SECONDS


def status_enabled() -> bool:
    """Whether the status projection is on (``CCC_EXTERNAL_WAIT_STATUS``, default true)."""
    return ExternalWaitMonitor.env_flag(STATUS_ENV_FLAG, default=True)


def default_status_store_path(home: Path) -> Path:
    """``<bot_data_dir>/external-wait/status-messages.json``."""
    return Path(home) / "status-messages.json"


def records_for_route(
    records: Iterable[Dict[str, Any]], user_id: int, chat_id: int
) -> List[Dict[str, Any]]:
    """Registry records bound to one conversation route, oldest first."""
    out = []
    for rec in records:
        uid, cid = rec.get("user_id"), rec.get("chat_id")
        if uid is None or cid is None:
            continue
        try:
            if int(uid) != int(user_id) or int(cid) != int(chat_id):
                continue
        except (TypeError, ValueError):
            continue
        out.append(rec)
    return _chronological(out)


def _chronological(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = [rec for rec in records if isinstance(rec, dict)]
    out.sort(key=lambda rec: (float(rec.get("created_epoch") or 0), str(rec.get("wait_id") or "")))
    return out


def text_hash_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# --- rendering -------------------------------------------------------------------


def _hhmm(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=_KST).strftime("%H:%M")


def _duration(seconds: float) -> str:
    total = max(0, int(round(float(seconds))))
    if total < 3600:
        return f"{max(1, total // 60)}m"
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    return f"{hours}h" if minutes == 0 else f"{hours}h{minutes:02d}m"


def _actions_run_label(rec: Dict[str, Any]) -> str:
    """``Actions run <workflow> #<n>`` (#2590); an approval gate is named."""
    name = redact_credentials(" ".join(str(rec.get("run_label") or "").split()))[:80].strip()
    number = rec.get("run_number")
    if name:
        label = f"Actions run {name}" + (f" #{number}" if number else "")
    else:
        label = f"Actions run {rec.get('run_id')}"
    if rec.get("state") == STATE_MONITORING and rec.get("run_status") == "waiting":
        label += " (awaiting approval)"
    return label


def _background_label(rec: Dict[str, Any]) -> str:
    count = rec.get("task_count")
    try:
        count = int(count) if count is not None else 1
    except (TypeError, ValueError):
        count = 1
    return "background task" if count == 1 else f"{count} background tasks"


def _label(rec: Dict[str, Any]) -> str:
    if rec.get("source") == SOURCE_GITHUB_ACTIONS_RUN:
        return _actions_run_label(rec)
    if rec.get("source") == SOURCE_GITHUB_MERGE_QUEUE:
        return f"PR #{rec.get('pr_number')} merge queue"
    if rec.get("source") in (SOURCE_LOCAL_TASK, SOURCE_LOCAL_BACKGROUND):
        return _background_label(rec)
    return f"PR #{rec.get('pr_number')} CI"


def _is_local(rec: Dict[str, Any]) -> bool:
    return rec.get("source") in (SOURCE_LOCAL_TASK, SOURCE_LOCAL_BACKGROUND)


def _summary(rec: Dict[str, Any]) -> str:
    text = redact_credentials(validate_summary(rec.get("summary")))
    if len(text) > _MAX_SUMMARY_DISPLAY_CHARS:
        text = text[: _MAX_SUMMARY_DISPLAY_CHARS - 1].rstrip() + "…"
    return text


def _monitoring_line(rec: Dict[str, Any]) -> str:
    created = float(rec.get("created_epoch") or 0)
    expires = float(rec.get("expires_epoch") or created)
    if _is_local(rec):
        # The harness resumes the session itself when the task ends; the
        # bridge only shows that the conversation is still busy (#2605).
        head = f"⏳ Running in background · {_label(rec)}"
        summary = _summary(rec)
        if summary:
            head = f"{head} — {summary}"
        tail = f"since {_hhmm(created)}" if rec.get("source") == SOURCE_LOCAL_BACKGROUND else (
            f"registered {_hhmm(created)}, up to {_duration(expires - created)}"
        )
        return f"{head} ({tail}; resumes automatically when done)"
    head = f"⏳ Waiting for results · {_label(rec)}"
    summary = _summary(rec)
    if summary:
        head = f"{head} — {summary}"
    return f"{head} (registered {_hhmm(created)}, up to {_duration(expires - created)})"


def _terminal_line(rec: Dict[str, Any]) -> str:
    status = str(rec.get("terminal_status") or rec.get("state") or "")
    if _is_local(rec):
        wording = _LOCAL_TERMINAL_LINE.get(status, "ℹ️ background watch ended")
    else:
        wording = _TERMINAL_LINE.get(status, "ℹ️ CI watch ended")
    completed = float(rec.get("completed_epoch") or 0)
    stamp = f" ({_hhmm(completed)})" if completed else ""
    return f"{wording} · {_label(rec)}{stamp}"


# --- layer 1: the bridge's own background-task count (#2605) -----------------------


def background_records(
    user_id: int,
    chat_id: int,
    running: int,
    oldest_age_seconds: float,
    finished_epochs: Iterable[float],
    now_epoch: float,
    *,
    labelled: int = 0,
    retention_seconds: float = BACKGROUND_FINISHED_RETENTION_SECONDS,
) -> List[Dict[str, Any]]:
    """Pseudo-records for the route's untracked background work (pure).

    ``running``/``oldest_age_seconds`` come from the live session's tracker
    (:meth:`ClaudeSessionTaskTrackingMixin.background_workload_snapshot`);
    ``labelled`` is how many of them already have a ``local_task`` registry
    record on this route (shown by their own line, so not counted twice).
    ``finished_epochs`` are recent terminal frames the handler remembered;
    those younger than ``retention_seconds`` render as one ✅ line each.
    Everything here is body-free: counts and clock times only.
    """
    out: List[Dict[str, Any]] = []
    try:
        running = max(0, int(running) - max(0, int(labelled)))
    except (TypeError, ValueError):
        running = 0
    if running > 0:
        started = float(now_epoch) - max(0.0, float(oldest_age_seconds or 0.0))
        out.append(
            {
                "wait_id": f"bg:{conversation_key_of(user_id, chat_id)}",
                "source": SOURCE_LOCAL_BACKGROUND,
                "user_id": int(user_id),
                "chat_id": int(chat_id),
                "state": STATE_MONITORING,
                "task_count": running,
                "summary": None,
                "created_epoch": started,
                "expires_epoch": started + 24 * 60 * 60,
            }
        )
    for index, completed in enumerate(sorted(float(value) for value in finished_epochs)):
        if float(now_epoch) - completed > float(retention_seconds):
            continue
        out.append(
            {
                "wait_id": f"bg-done:{conversation_key_of(user_id, chat_id)}:{index}",
                "source": SOURCE_LOCAL_BACKGROUND,
                "user_id": int(user_id),
                "chat_id": int(chat_id),
                "state": TERMINAL_SUCCESS,
                "terminal_status": TERMINAL_SUCCESS,
                "task_count": 1,
                "summary": None,
                "created_epoch": completed,
                "completed_epoch": completed,
            }
        )
    return out


BackgroundSnapshot = Tuple[int, float, List[float]]


def route_background_snapshot(handler: Any, user_id: int, chat_id: int) -> Optional[BackgroundSnapshot]:
    """Ask the chat handler for the route's live background workload (fail-open).

    ``handler.background_workload_for_route(user_id, chat_id)`` returns
    ``(running, oldest_age_seconds, finished_epochs)``; a handler without the
    seam, or one that raises, yields ``None`` so the status renders exactly
    as before #2605.
    """
    probe = getattr(handler, "background_workload_for_route", None)
    if not callable(probe):
        return None
    try:
        snapshot = probe(int(user_id), int(chat_id))
    except Exception:
        logger.debug("Background workload probe failed: route=%s:%s", user_id, chat_id)
        return None
    if snapshot is None:
        return None
    try:
        running, oldest_age, finished = snapshot
        return int(running), float(oldest_age), [float(value) for value in finished]
    except (TypeError, ValueError):
        return None


def merge_background_records(
    records: Iterable[Dict[str, Any]],
    snapshot: Optional[Tuple[int, float, Iterable[float]]],
    user_id: int,
    chat_id: int,
    now_epoch: float,
) -> List[Dict[str, Any]]:
    """Registry records of a route plus its live background pseudo-records.

    ``snapshot`` is ``(running, oldest_age_seconds, finished_epochs)`` from
    the chat handler, or ``None`` when the route has no live session or the
    handler does not expose the seam — then the records pass through
    unchanged. Labelled ``local_task`` waits that are still monitoring are
    subtracted from the running count so one task never shows twice.
    """
    route_records = list(records)
    if snapshot is None:
        return route_records
    try:
        running, oldest_age, finished = snapshot
    except (TypeError, ValueError):
        return route_records
    labelled = sum(
        1
        for rec in route_records
        if rec.get("source") == SOURCE_LOCAL_TASK and rec.get("state") == STATE_MONITORING
    )
    extra = background_records(
        user_id, chat_id, running, oldest_age, finished, now_epoch, labelled=labelled
    )
    return _chronological(route_records + extra)


def _is_recent_terminal(rec: Dict[str, Any], now_epoch: float, window: float) -> bool:
    completed = float(rec.get("completed_epoch") or 0)
    return bool(completed) and now_epoch - completed <= window


def render_wait_status(
    records: Iterable[Dict[str, Any]],
    now_epoch: float,
    *,
    recent_terminal_seconds: float = RECENT_TERMINAL_SECONDS,
) -> Optional[str]:
    """Plain-text status for one route, or ``None`` when nothing is shown.

    One line per wait, chronological by registration: monitoring waits as
    ``⏳ Waiting for results …`` and terminal waits completed within
    ``recent_terminal_seconds`` as their outcome line (so a wait's line
    "becomes" its result in place). Stale terminal waits are omitted.
    """
    lines: List[str] = []
    for rec in _chronological(records):
        if rec.get("state") == STATE_MONITORING:
            lines.append(_monitoring_line(rec))
        elif _is_recent_terminal(rec, float(now_epoch), float(recent_terminal_seconds)):
            lines.append(_terminal_line(rec))
    if not lines:
        return None
    if len(lines) > MAX_STATUS_LINES:
        hidden = len(lines) - (MAX_STATUS_LINES - 1)
        lines = lines[: MAX_STATUS_LINES - 1] + [f"… and {hidden} more"]
    return "\n".join(lines)


# --- planning --------------------------------------------------------------------


def plan_status_update(
    records: Iterable[Dict[str, Any]],
    stored: Optional[Dict[str, Any]],
    now_epoch: float,
    *,
    recent_terminal_seconds: float = RECENT_TERMINAL_SECONDS,
    buried: bool = False,
) -> Tuple[WaitStatusAction, Optional[str]]:
    """Decide what the channel should do for one route (pure, no I/O).

    - no stored message: ``send`` only while a monitoring wait exists
      (a route with no waits and no message stays silent; a route whose
      waits already finished is covered by the wake notification);
    - stored message, nothing left to show: ``delete``;
    - stored message that newer events have ``buried`` while a wait is still
      monitored (#2288): ``move`` — the channel removes it and posts the
      current text again at the bottom, like the "⏳ Working" bubble, whether
      or not the text changed. A status that only shows finished waits is not
      moved: the resume turn's reply already announces the result, so the
      line is edited in place and expires as before. Only channels whose edits
      keep the original timeline position (Matrix) report ``buried``; Telegram
      never does and keeps editing in place;
    - stored message, same text hash: ``noop``;
    - otherwise ``edit``.
    """
    route_records = list(records)
    text = render_wait_status(
        route_records, now_epoch, recent_terminal_seconds=recent_terminal_seconds
    )
    monitoring = any(rec.get("state") == STATE_MONITORING for rec in route_records)
    if not stored:
        if text is not None and monitoring:
            return "send", text
        return "noop", None
    if text is None:
        return "delete", None
    if buried and monitoring:
        return "move", text
    if stored.get("text_hash") == text_hash_of(text):
        return "noop", text
    return "edit", text


def reconcile_routes(
    store_entries: Dict[str, Dict[str, Any]], records: Iterable[Dict[str, Any]]
) -> List[Tuple[int, int]]:
    """Routes a restart has to look at, stored messages first (unbounded; callers cap).

    A stored entry may now be stale (waits finished while the bridge was
    down) and a monitoring wait may have no message at all (the run died
    between register and turn end). Malformed entries are skipped.
    """
    routes: Dict[str, Tuple[int, int]] = {}
    for key, entry in store_entries.items():
        try:
            routes[key] = (int(entry.get("user_id")), int(entry.get("chat_id")))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    for rec in records:
        if rec.get("state") != STATE_MONITORING:
            continue
        try:
            uid, cid = int(rec.get("user_id")), int(rec.get("chat_id"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        routes.setdefault(conversation_key_of(uid, cid), (uid, cid))
    return list(routes.values())


# --- durable id store ------------------------------------------------------------


class ExternalWaitStatusStore:
    """``{"uid:cid": {message_id, chat_id, user_id, updated_epoch, text_hash}}``.

    Atomic writes, fail-open reads (a corrupt file reads as empty), one
    entry per conversation route. Channel-neutral: ``message_id`` is whatever
    opaque id the channel needs to edit or delete its message.
    """

    def __init__(self, path: Path, *, clock=lambda: time.time()):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._clock = clock

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> Dict[str, Dict[str, Any]]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning("External-wait status store read failed: %s", type(exc).__name__)
            return {}
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            logger.warning("External-wait status store unreadable; starting empty")
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    def _write(self, entries: Dict[str, Dict[str, Any]]) -> None:
        payload = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            _atomic_write_bytes(self._path, payload)
        except SessionStoreDurabilityError:
            logger.warning("External-wait status store dir-fsync unconfirmed; state written")

    def entries(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return self._read()

    def get(self, user_id: int, chat_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._read().get(conversation_key_of(user_id, chat_id))

    def put(
        self,
        user_id: int,
        chat_id: int,
        *,
        message_id: Any,
        text_hash: str,
        now: Optional[float] = None,
        tail: Optional[str] = None,
        posted_epoch: Optional[float] = None,
    ) -> None:
        """Record the route's message; ``tail`` / ``posted_epoch`` are optional placement facts (#2288).

        ``tail`` is the id of the message's latest edit event on channels
        where an edit is a timeline event of its own; ``posted_epoch`` is when
        the message itself was last posted (first send or a move). Both are
        omitted from the entry when unknown, so the stage-1 shape is unchanged.
        """
        entry: Dict[str, Any] = {
            "message_id": message_id,
            "chat_id": int(chat_id),
            "user_id": int(user_id),
            "updated_epoch": self._clock() if now is None else float(now),
            "text_hash": str(text_hash),
        }
        if tail:
            entry["tail"] = str(tail)
        if posted_epoch is not None:
            entry["posted_epoch"] = float(posted_epoch)
        try:
            with self._lock:
                entries = self._read()
                entries[conversation_key_of(user_id, chat_id)] = entry
                self._write(entries)
        except Exception as exc:
            logger.warning("External-wait status store put failed: %s", type(exc).__name__)

    def pop(self, user_id: int, chat_id: int) -> Optional[Dict[str, Any]]:
        try:
            with self._lock:
                entries = self._read()
                entry = entries.pop(conversation_key_of(user_id, chat_id), None)
                if entry is not None:
                    self._write(entries)
                return entry
        except Exception as exc:
            logger.warning("External-wait status store pop failed: %s", type(exc).__name__)
            return None


__all__ = [
    "MAX_RECONCILE_ROUTES",
    "MAX_STATUS_LINES",
    "RECENT_TERMINAL_SECONDS",
    "STATUS_ENV_FLAG",
    "ExternalWaitStatusStore",
    "WaitStatusAction",
    "default_status_store_path",
    "plan_status_update",
    "reconcile_routes",
    "records_for_route",
    "render_wait_status",
    "status_enabled",
    "text_hash_of",
]
