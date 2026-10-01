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
    TERMINAL_CANCELLED,
    TERMINAL_EXPIRED,
    TERMINAL_FAILURE,
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

WaitStatusAction = Literal["send", "edit", "delete", "noop"]

#: Terminal line wording mirrors ``external_wait_monitor._WAKE_HEADLINE``.
_TERMINAL_LINE = {
    TERMINAL_SUCCESS: "✅ CI green → continuing",
    TERMINAL_FAILURE: "❌ CI failed → investigating",
    TERMINAL_CANCELLED: "⚠️ CI cancelled",
    TERMINAL_SUPERSEDED: "🔀 head moved",
    TERMINAL_EXPIRED: "⏰ expired",
    TERMINAL_MONITOR_ERROR: "⚠️ CI watch failed",
    TERMINAL_OWNER_CANCEL: "🚫 cancelled by owner",
}


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


def _label(rec: Dict[str, Any]) -> str:
    return f"PR #{rec.get('pr_number')} CI"


def _summary(rec: Dict[str, Any]) -> str:
    text = redact_credentials(validate_summary(rec.get("summary")))
    if len(text) > _MAX_SUMMARY_DISPLAY_CHARS:
        text = text[: _MAX_SUMMARY_DISPLAY_CHARS - 1].rstrip() + "…"
    return text


def _monitoring_line(rec: Dict[str, Any]) -> str:
    created = float(rec.get("created_epoch") or 0)
    expires = float(rec.get("expires_epoch") or created)
    head = f"⏳ Waiting for results · {_label(rec)}"
    summary = _summary(rec)
    if summary:
        head = f"{head} — {summary}"
    return f"{head} (registered {_hhmm(created)}, up to {_duration(expires - created)})"


def _terminal_line(rec: Dict[str, Any]) -> str:
    status = str(rec.get("terminal_status") or rec.get("state") or "")
    wording = _TERMINAL_LINE.get(status, "ℹ️ CI watch ended")
    completed = float(rec.get("completed_epoch") or 0)
    stamp = f" ({_hhmm(completed)})" if completed else ""
    return f"{wording} · {_label(rec)}{stamp}"


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
) -> Tuple[WaitStatusAction, Optional[str]]:
    """Decide what the channel should do for one route (pure, no I/O).

    - no stored message: ``send`` only while a monitoring wait exists
      (a route with no waits and no message stays silent; a route whose
      waits already finished is covered by the wake notification);
    - stored message, nothing left to show: ``delete``;
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
    ) -> None:
        entry = {
            "message_id": message_id,
            "chat_id": int(chat_id),
            "user_id": int(user_id),
            "updated_epoch": self._clock() if now is None else float(now),
            "text_hash": str(text_hash),
        }
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
