"""Owner-context delivery gate for the push spool — default off.

The owner's iPhone posts one label when a situation changes (driving, sleep, work focus, ...)
to the ``owner-context`` service on this node (jinwon-int/iphone-agent-cast
``server/owner_context``, design ``docs/design/context-server.md``). That service keeps the
current labels in a local state file. This module reads that file — no network — and decides,
per spool record, whether to deliver it now or keep it in the spool for a later cycle.

Off unless ``CCC_OWNER_CONTEXT_FILE`` names the state file; with it unset nothing here runs.
``CCC_OWNER_CONTEXT_MODE`` is ``shadow`` (default: log what would have been held, hold nothing)
or ``hold``. Decisions go to ``CCC_OWNER_CONTEXT_LOG`` (default
``~/.claude/state/owner-context-decisions.jsonl``, 0600, 14 days) — never to the bridge log,
which outlives that retention.

Fail-open everywhere: a missing, broken or stale state file, an unknown tier or any exception
means *send*. ``critical`` records and ``info`` / untiered records are never held. The
context only changes *when* a notice goes out — it never approves, merges or cancels anything.

The policy table and situation rules mirror the reference copy in iphone-agent-cast
``server/owner_context/owner_context.py``; both test files pin the same 30-cell grid.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ENV_FILE = "CCC_OWNER_CONTEXT_FILE"
ENV_MODE = "CCC_OWNER_CONTEXT_MODE"
ENV_LOG = "CCC_OWNER_CONTEXT_LOG"
MODE_SHADOW, MODE_HOLD = "shadow", "hold"
DEFAULT_LOG = "~/.claude/state/owner-context-decisions.jsonl"
LOG_RETENTION_SECONDS = 14 * 24 * 3600

KST = ZoneInfo("Asia/Seoul")  # the owner's clock: night window and the 08:00 sleep release
HOUR = 3600

# Situations, first match wins.
DRIVING, SLEEP, SLEEP_GUESS, FOCUS_WORK, OFFICE, FREE, UNKNOWN = (
    "driving", "sleep", "sleep-guess", "focus-work", "office", "free", "unknown")
SIGNALS = {
    "carplay": ("on", "off"),
    "place": ("home", "office", "away"),
    "focus": ("work", "sleep", "off"),
    "sleep": ("bedtime", "wake"),
    "charger": ("on", "off"),
    "ping": ("seen",),
}
SLEEP_GUESS_FROM = dt.time(22, 30)
SLEEP_GUESS_UNTIL = dt.time(7, 0)

SEND, HOLD = "send", "hold"
COLUMNS = (DRIVING, SLEEP, FOCUS_WORK, OFFICE, FREE, UNKNOWN)
COLUMN_OF = {SLEEP_GUESS: SLEEP}
TABLE: dict[str, dict[str, str]] = {
    "approval": dict(zip(COLUMNS, (HOLD, HOLD, HOLD, SEND, SEND, SEND))),
    "ci":       dict(zip(COLUMNS, (HOLD, HOLD, HOLD, SEND, SEND, SEND))),
    "critical": dict(zip(COLUMNS, (SEND, SEND, SEND, SEND, SEND, SEND))),
    "action":   dict(zip(COLUMNS, (HOLD, HOLD, SEND, SEND, SEND, SEND))),
    "notice":   dict(zip(COLUMNS, (HOLD, HOLD, HOLD, SEND, SEND, SEND))),
}
TIERS = ("info", "notice", "action", "critical")
APPROVAL_EVENT = "ApprovalAsk"
CI_EVENTS = frozenset({"DetachedJob", "PrStatusPoll", "CiResult"})  # same row as notice either way
MAX_HOLD_SECONDS = {DRIVING: 3 * HOUR, FOCUS_WORK: 4 * HOUR}
SLEEP_RELEASE = dt.time(8, 0)
SLEEP_MAX_HOLD_SECONDS = 12 * HOUR


def _entry(state: Any, name: str) -> Optional[dict]:
    signals = state.get("signals") if isinstance(state, dict) else None
    entry = signals.get(name) if isinstance(signals, dict) else None
    return entry if isinstance(entry, dict) else None


def is_valid(state: Any, name: str, value: str, now: float) -> bool:
    entry = _entry(state, name)
    if entry is None or entry.get("value") != value:
        return False
    try:
        return float(entry["expires"]) > now
    except (KeyError, TypeError, ValueError):
        return False


def situation(state: Any, now: float) -> str:
    """One label from the state at epoch ``now``; UNKNOWN for a missing/broken state or a stale ping."""
    if not isinstance(state, dict) or not is_valid(state, "ping", "seen", now):
        return UNKNOWN
    if is_valid(state, "carplay", "on", now):
        return DRIVING
    if is_valid(state, "focus", "sleep", now) or is_valid(state, "sleep", "bedtime", now):
        return SLEEP
    clock = dt.datetime.fromtimestamp(now, KST).time()
    night = clock >= SLEEP_GUESS_FROM or clock < SLEEP_GUESS_UNTIL
    if night and is_valid(state, "place", "home", now) and is_valid(state, "charger", "on", now):
        return SLEEP_GUESS
    if is_valid(state, "focus", "work", now):
        return FOCUS_WORK
    if is_valid(state, "place", "office", now):
        return OFFICE
    if any(is_valid(state, n, v, now) for n, vals in SIGNALS.items() if n != "ping" for v in vals):
        return FREE
    return UNKNOWN


def record_kind(record: Mapping[str, Any]) -> Optional[str]:
    """Policy row for a spool record, or None (info / unknown tier: never touched)."""
    tier = str(record.get("tier") or "").strip().lower()
    tier = tier if tier in TIERS else "info"
    event = str(record.get("event") or "")
    if tier == "critical":
        return "critical"
    if event == APPROVAL_EVENT:
        return "approval"
    if tier == "action":
        return "action"
    if tier == "notice":
        return "ci" if event in CI_EVENTS else "notice"
    return None


def record_time(record: Mapping[str, Any], created: Optional[float] = None) -> Optional[float]:
    """Epoch of the record's ``ts`` (ISO with an offset), else ``created`` (file mtime), else None."""
    try:
        when = dt.datetime.fromisoformat(str(record.get("ts") or ""))
        if when.tzinfo is not None:
            return when.timestamp()
    except ValueError:
        pass
    return created


def hold_deadline(column: str, since: float) -> float:
    """Epoch after which a record held in ``column`` since ``since`` goes out anyway."""
    if column == SLEEP:
        start = dt.datetime.fromtimestamp(since, KST)
        release = dt.datetime.combine(start.date(), SLEEP_RELEASE, KST)
        if release <= start:
            release += dt.timedelta(days=1)
        return min(release.timestamp(), since + SLEEP_MAX_HOLD_SECONDS)
    return since + MAX_HOLD_SECONDS[column]


def decide(record: Any, now: float, state: Any, created: Optional[float] = None) -> str:
    """SEND or HOLD for one spool record. Anything unexpected is SEND (fail-open)."""
    try:
        kind = record_kind(record) if isinstance(record, dict) else None
        if kind is None or kind == "critical":
            return SEND
        sit = situation(state, now)
        column = COLUMN_OF.get(sit, sit)
        if TABLE[kind][column] != HOLD:
            return SEND
        since = record_time(record, created)
        if since is not None and now >= hold_deadline(column, since):
            return SEND
        return HOLD
    except Exception:
        return SEND


def load_state(path: Path) -> Optional[dict]:
    """The state dict, or None when the file is missing or broken."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class OwnerContextGate:
    """Per-notifier gate: ``should_hold(path, data)`` before a spool record is delivered.

    Never raises. In shadow mode it always answers False and logs ``would-hold`` once per
    record; in hold mode it logs ``held`` when a record is first kept and ``released`` when
    a kept record is let through (so the held and released counts can be compared).
    """

    _MAX_TRACKED = 10_000

    def __init__(
        self,
        state_file: Path,
        mode: str = MODE_SHADOW,
        log_path: Optional[Path] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state_file = Path(state_file).expanduser()
        self.mode = mode if mode in (MODE_SHADOW, MODE_HOLD) else MODE_SHADOW
        self.log_path = Path(log_path).expanduser() if log_path else None
        self._clock = clock
        self._cache_key: Optional[tuple[int, int]] = None
        self._cache: Optional[dict] = None
        self._tracked: set[str] = set()  # names logged as would-hold / currently held
        self._last_prune = 0.0

    def holding(self, name: str) -> bool:
        """True while a hold-mode record with this file name is being kept."""
        return self.mode == MODE_HOLD and name in self._tracked

    def _state(self) -> Optional[dict]:
        try:
            st = self.state_file.stat()
        except OSError:
            self._cache_key, self._cache = None, None
            return None
        key = (st.st_mtime_ns, st.st_size)
        if key != self._cache_key:
            self._cache_key, self._cache = key, load_state(self.state_file)
        return self._cache

    def should_hold(self, path: Path, data: Any) -> bool:
        try:
            return self._should_hold(Path(path), data)
        except Exception:
            logger.warning("owner context gate failed; sending", exc_info=True)
            return False

    def _should_hold(self, path: Path, data: Any) -> bool:
        if not isinstance(data, dict) or record_kind(data) in (None, "critical"):
            return False  # no file read for records the table never holds
        now = self._clock()
        state = self._state()
        try:
            created: Optional[float] = path.stat().st_mtime
        except OSError:
            created = None
        verdict = decide(data, now, state, created)
        name = path.name
        if verdict == HOLD:
            if name not in self._tracked:
                if len(self._tracked) >= self._MAX_TRACKED:
                    self._tracked.clear()
                self._tracked.add(name)
                self._log(now, "would-hold" if self.mode == MODE_SHADOW else "held", name, data, state)
            return self.mode == MODE_HOLD
        if self.mode == MODE_HOLD and name in self._tracked:
            self._tracked.discard(name)
            self._log(now, "released", name, data, state)
        return False

    def _log(self, now: float, action: str, name: str, data: dict, state: Optional[dict]) -> None:
        """One JSON line: time, action, record file name, event, tier, situation. Never the text."""
        if self.log_path is None:
            return
        line = json.dumps({
            "at": dt.datetime.fromtimestamp(now, KST).isoformat(timespec="seconds"),
            "action": action,
            "record": name,
            "event": str(data.get("event") or ""),
            "tier": str(data.get("tier") or ""),
            "situation": situation(state, now),
        }, ensure_ascii=False)
        try:
            self.log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if now - self._last_prune >= 24 * HOUR:
                self._last_prune = now
                self._prune_log(now)
        except OSError as e:
            logger.warning("owner context decision log not written: %s", e.__class__.__name__)

    def _prune_log(self, now: float) -> None:
        assert self.log_path is not None
        cutoff = now - LOG_RETENTION_SECONDS
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        keep = []
        for ln in lines:
            try:
                if dt.datetime.fromisoformat(json.loads(ln)["at"]).timestamp() >= cutoff:
                    keep.append(ln)
            except (ValueError, KeyError, TypeError):
                continue
        if len(keep) != len(lines):
            tmp = self.log_path.with_name(self.log_path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("".join(k + "\n" for k in keep))
            os.replace(tmp, self.log_path)


def gate_from_env(environ: Mapping[str, str] = os.environ) -> Optional[OwnerContextGate]:
    """The gate when ``CCC_OWNER_CONTEXT_FILE`` is set, else None (feature off — the default)."""
    state_file = (environ.get(ENV_FILE) or "").strip()
    if not state_file:
        return None
    mode = (environ.get(ENV_MODE) or MODE_SHADOW).strip().lower()
    if mode not in (MODE_SHADOW, MODE_HOLD):
        logger.warning("%s=%r is not shadow|hold; using shadow", ENV_MODE, mode)
        mode = MODE_SHADOW
    log_path = (environ.get(ENV_LOG) or DEFAULT_LOG).strip()
    logger.info("owner context gate on (mode %s)", mode)
    return OwnerContextGate(Path(state_file), mode, Path(log_path))
