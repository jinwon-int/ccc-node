#!/usr/bin/env python3
"""Stateless completion evidence for detached jobs, for SessionStart (#1258).

Why this exists
---------------
``bridge-safe-detached-run`` (#822) already detaches the *work*: the job runs
as a ``systemd-run --unit ... --collect`` transient unit owned by PID 1, so a
bridge or session restart cannot kill it. What it does **not** detach is the
*watcher*. The skill's Step 2 tells the agent to poll the log with a bounded
``for`` loop or a ``Monitor`` until-loop, and both of those run as children of
the session process. When the session restarts, the watcher dies alone.

The result is the failure this module exists to prevent: the job finished fine,
but the agent receives only ``<task-notification status=stopped>`` and cannot
tell "the work failed" apart from "my watcher was lost and the work is
unknown". On 2026-08-24 that produced a job that was already complete
(``EXIT=0``, 12/12) being treated as possibly-failed until the owner asked
again.

``--collect`` garbage-collects the unit on exit, so ``systemctl status`` stops
being evidence the moment the job succeeds. **The log file plus its ``EXIT=``
marker is the only durable completion record** -- and reading it is a stateless,
one-shot act that needs no surviving process. So instead of keeping a watcher
alive, this module writes down where the evidence lives and re-reads it at every
SessionStart. The registry survives arbitrarily many restarts because it is just
a file.

Contract, matching ``pending_promises.py``
------------------------------------------
stdlib only, fail-open on every malformed input, and **silent when nothing is
outstanding**, so the loader's output stays byte-identical in the common case.

Classification
--------------
``done``
    An ``EXIT=<n>`` marker is present. Reported once, with the real exit code,
    then acked so it stops appearing. This is the case that was being misread
    as failure.

``running``
    No marker yet, and the job still looks alive -- the unit is active, or the
    log was written to recently. Nothing to do but keep waiting.

``lost``
    No marker, the unit is gone, and the log has been quiet past the stale
    threshold. This is the genuinely ambiguous case, and it is the only one the
    agent must investigate by hand. Separating it from ``done`` is the whole
    point: ``status=stopped`` conflated them.

Wait budget (#2636)
-------------------
``register`` also arms a bridge ``detached-job`` wait (#2605 layer 3): the
``⏳ Running detached`` status line plus a wake when ``EXIT=`` appears. That
wait has a budget (``WAIT_BUDGET_SECONDS``, the bridge default of 6 h), and a
job expected to outlive it must not be armed: the line would sit on the
owner's screen for the whole budget, end ``expired`` with no result, and the
real exit the next morning would reach nobody. So registration takes the
expectation -- ``--expect 90m`` / ``--until "05:30"`` -- and decides:

* within budget: arm, with the wait sized to the expectation (+ margin, capped
  at the budget) so "up to …" on the status line is honest;
* over budget: **no wait**; the row carries ``notify_on_exit`` and stdout says
  what to use instead (``schedule``, or a hand-written unit that calls
  ``notify`` on exit);
* no expectation given: arm as before (compatibility) with a one-line hint.

``schedule`` is the paved path for "start later, tell me when done": a
``systemd-run --on-calendar`` transient timer whose wrapper appends the
``EXIT=`` marker and emits one body-free spool notice through ``notify``
instead of a wake. ``notify`` writes that spool record on its own too, for
hand-written units. A transient timer does not survive a reboot (#2827): use
plain ``schedule`` only for waits that cannot cross one, and ``schedule
--durable`` for waits spanning nights or days.

Durable schedule (#2827 proposal A)
-----------------------------------
``schedule --durable`` writes the same wrapper as a persistent pair,
``<unit>.service`` (oneshot) + ``<unit>.timer`` (``Persistent=true``), into the
unit directory, enables the timer, and has the wrapper disable the timer and
delete both files after the run. The unit files live on disk, so a reboot
keeps the timer; ``Persistent=true`` fires a run whose time passed while the
node was down once, at the next boot. It is one-shot by construction, so the
time must be an absolute, future instant (``YYYY-MM-DD HH:MM[:SS] [zone]``).
Writing unit files is a node configuration change: it needs the same approval
as a hand-written persistent unit. ``--print`` shows both files and the
``systemctl`` commands without writing anything.

Reboot export / restore (#2827)
-------------------------------
A reboot that does happen must not take the armed timers with it. ``schedule``
therefore writes the exact ``systemd-run`` argv it launched into the registry
row (``launch_argv``): run time, command, ``Environment=`` and ``--user`` in
one piece, on disk, where a reboot cannot reach it. ``restore-timers`` replays
that argv for every scheduled job that has not started and whose timer is
gone, so nothing is rebuilt from the restoring shell's own HOME/PATH.
``export-timers`` is the pre-reboot snapshot of the same jobs plus the values
of each live unit (``systemctl cat``); ``restore-timers --from <snapshot>``
compares those values with the re-created unit. Neither command guesses: a job
whose one-shot time has passed is reported ``missed`` and never re-armed for a
later time, and a row written before ``launch_argv`` existed is reported
``unrestorable``.

Usage::

    detached_jobs.py register --unit U --log P [--summary S] [--workdir W]
                              [--expect 90m | --until "YYYY-MM-DD HH:MM"] [--no-wait] [--user]
    detached_jobs.py schedule --unit U --at "<systemd calendar>" --log P
                              [--summary S] [--workdir W] [--tier T] [--fail-tier T]
                              [--result-file P] [--user] [--durable] [--print] -- <cmd…>
    detached_jobs.py notify   --unit U --exit N [--summary S] [--log P]
                              [--tier T] [--fail-tier T] [--result-file P]
    detached_jobs.py export-timers  [--out FILE]
    detached_jobs.py restore-timers [--from FILE] [--dry-run]

Tiers (#2710) are the fleet-alert tiers ``info|notice|action|critical``.
``notify`` defaults to ``info`` (held for the 09:00 KST digest);
``schedule`` defaults to ``notice`` (delivered at once, mirrored to 「한데」).
    detached_jobs.py sweep [<registry path>] [--max-bytes N]
    detached_jobs.py ack --unit U [--all]
    detached_jobs.py list [--json]

With no explicit path the registry is ``$CCC_STATE_DIR/detached-jobs.jsonl``,
falling back to ``$HOME/.claude/state/detached-jobs.jsonl``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# Same reasoning as pending_promises: this block sits high in the memory
# payload and the materializer truncates from the tail, so a wall of jobs would
# push out real context.
DEFAULT_MAX_BYTES = 1024
MAX_ROWS_PER_SECTION = 5

# A detached job that has not touched its log in this long, whose unit is gone,
# and which never wrote an EXIT marker, is not "still working" -- it is
# unaccounted for. 15 minutes is deliberately generous: a job that logs only at
# the end (a long build, a big migration) must not be called lost while it is
# genuinely mid-flight, and the cost of a false "running" is only a later
# re-check, while the cost of a false "lost" is a needless re-run of work that
# may not be idempotent.
STALE_SECONDS = 900

# Never surface a record forever. A job nobody acked within a week is archived
# noise, not live context. A scheduled job counts its week from the time it is
# due to fire, not from when it was registered (#2827): a reminder armed 19 days
# ahead used to leave the sweep after 7, so a reboot that then took its
# transient timer raised no `lost` at all.
MAX_AGE_SECONDS = 7 * 24 * 3600

STATE_DONE = "done"
STATE_RUNNING = "running"
STATE_LOST = "lost"

# The bridge's default wait timeout (``external_wait_monitor.DEFAULT_TIMEOUT_SECONDS``).
# A detached-job wait longer than this is not a wait the conversation can
# resume on; it is an all-day status line that ends ``expired``.
DEFAULT_WAIT_BUDGET_SECONDS = 6 * 3600
# Headroom added to an expectation when sizing the wait: a job that says "90m"
# and takes 100 must still be caught by the wait, not expire just before.
WAIT_MARGIN_SECONDS = 30 * 60
# Spool event name for completion notices (read by the bridge PushNotifier).
NOTIFY_EVENT = "DetachedJob"
# Fleet-alert tiers (#2413/#2628, ``fleet_alert_relay.TIERS``). ``info`` is
# held for the next 09:00 KST digest and never mirrored to 「한데」 on its own;
# ``notice`` is delivered at once and mirrored, without re-notification (#2710).
TIERS = ("info", "notice", "action", "critical")
NOTIFY_DEFAULT_TIER = "info"      # bare ``notify``: unchanged for compatibility
SCHEDULE_DEFAULT_TIER = "notice"  # ``schedule`` exists to say "it finished"
# ``--result-file``: the job's own one-line result, capped like a summary.
RESULT_MAX_CHARS = 280
RESULT_READ_BYTES = 8192
SCOPE_USER = "user"
SCOPE_SYSTEM = "system"

# Reboot export/restore of scheduled timers (#2827). ``schedule`` stores the
# exact argv it launched under LAUNCH_KEY; the TIMER_* values say where a
# scheduled job that has not started stands.
LAUNCH_KEY = "launch_argv"
# ``schedule --durable`` rows (#2827 proposal A): persistent unit files instead
# of a transient timer, so there is nothing to replay after a reboot.
DURABLE_KEY = "durable"
SYSTEM_UNIT_DIR = "/etc/systemd/system"
UNIT_DIR_ENV = "CCC_DETACHED_UNIT_DIR"  # tests and unusual layouts only
EXPORT_VERSION = 1
TIMER_ARMED = "armed"                # its .timer is active: nothing to do
TIMER_MISSING = "missing"            # timer gone, still due: restore re-creates it
TIMER_MISSED = "missed"              # timer gone and the one-shot time has passed
TIMER_UNRESTORABLE = "unrestorable"  # timer gone and no usable launch argv
# Registry fields an export carries, so a snapshot row can be judged and
# replayed exactly like the registry row it was copied from.
_EXPORT_KEYS = (
    "unit", "log", "log_offset", "started_at", "scheduled_at", "scheduled_epoch",
    "scope", "summary", "workdir", "tier", LAUNCH_KEY, DURABLE_KEY, "unit_files",
)
# Unit-file settings compared by value between a snapshot and the live unit.
_LIVE_KEYS = ("OnCalendar", "Environment", "WorkingDirectory", "ExecStart")
# Operator-run commands: an unexpected error must be loud, unlike the sweep.
_LOUD_COMMANDS = ("export-timers", "restore-timers")

# Longest alternatives first: ``m`` must not swallow the start of ``min``.
_DURATION_RE = re.compile(r"(\d+)\s*(hours|hour|hrs|hr|days|day|secs|sec|min|d|h|m|s)?", re.IGNORECASE)
_UNIT_SECONDS = {
    "": 1, "s": 1, "sec": 1, "secs": 1,
    "m": 60, "min": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}


def wait_budget_seconds() -> int:
    raw = os.environ.get("CCC_DETACHED_WAIT_BUDGET_SECONDS", "").strip()
    if raw:
        try:
            return max(60, int(float(raw)))
        except ValueError:
            pass
    return DEFAULT_WAIT_BUDGET_SECONDS


def parse_duration(text: str) -> Optional[int]:
    """``3600`` / ``45m`` / ``2h`` / ``1h30m`` / ``1d`` → seconds, else None."""
    text = (text or "").strip().lower()
    if not text:
        return None
    pos = 0
    total = 0
    for match in _DURATION_RE.finditer(text):
        if match.start() != pos:
            return None
        unit = (match.group(2) or "").lower()
        total += int(match.group(1)) * _UNIT_SECONDS[unit]
        pos = match.end()
    if pos != len(text) or total <= 0:
        return None
    return total


def parse_until(text: str, *, now: Optional[float] = None) -> Optional[int]:
    """Seconds from now until a wall-clock instant, else None.

    Accepts ``HH:MM`` (next occurrence: today if still ahead, else tomorrow),
    ``YYYY-MM-DD HH:MM[:SS]`` and ISO-8601 (``T`` separator, optional offset or
    ``Z``). Naive values are the node's local clock, which is what the
    operator reads off the status line.
    """
    text = (text or "").strip()
    if not text:
        return None
    base = datetime.fromtimestamp(time.time() if now is None else now).astimezone()
    target: Optional[datetime] = None
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        hh, mm = (int(part) for part in text.split(":"))
        if hh > 23 or mm > 59:
            return None
        target = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= base:
            target += timedelta(days=1)
    else:
        candidate = text.replace("Z", "+00:00")
        if " " in candidate and "T" not in candidate:
            candidate = candidate.replace(" ", "T", 1)
        try:
            target = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=base.tzinfo)
    seconds = int((target - base).total_seconds())
    return seconds if seconds > 0 else None


def wait_plan(expect_seconds: Optional[int], *, budget: Optional[int] = None) -> Dict[str, Any]:
    """Decide whether a ⏳ wait is honest for this expectation.

    Returns ``{"arm": bool, "timeout_seconds": int|None, "reason": str}``.
    ``timeout_seconds`` is None when the bridge default should stand (no
    expectation given: compatibility path).
    """
    cap = wait_budget_seconds() if budget is None else int(budget)
    if expect_seconds is None:
        return {
            "arm": True,
            "timeout_seconds": None,
            "reason": "no --expect/--until given; wait armed with the bridge default — "
                      "pass --expect <duration> so the status line is honest (#2636)",
        }
    if expect_seconds > cap:
        return {
            "arm": False,
            "timeout_seconds": None,
            "reason": f"expected {_fmt_duration(expect_seconds)} exceeds the wait budget "
                      f"{_fmt_duration(cap)}: no ⏳ wait armed; completion is reported by "
                      "`detached_jobs.py notify` (use `schedule`, or call notify from the unit) (#2636)",
        }
    timeout = min(cap, expect_seconds + WAIT_MARGIN_SECONDS)
    return {
        "arm": True,
        "timeout_seconds": int(timeout),
        "reason": f"expected {_fmt_duration(expect_seconds)}; wait sized to {_fmt_duration(timeout)}",
    }


def _fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return f"{hours}h" if minutes == 0 else f"{hours}h{minutes:02d}m"


def registry_path(explicit: str = "") -> str:
    """Resolve the registry path: explicit arg, else state dir, else HOME."""
    if explicit:
        return explicit
    env = os.environ.get("CCC_DETACHED_JOBS_REGISTRY", "").strip()
    if env:
        return env
    state = os.environ.get("CCC_STATE_DIR", "").strip()
    if not state:
        state = os.path.join(os.environ.get("HOME", "/root"), ".claude", "state")
    return os.path.join(state, "detached-jobs.jsonl")


def load_records(path: str) -> List[Dict[str, Any]]:
    """Read the JSONL registry, last record per unit winning. Never raises.

    JSONL rather than a single JSON object because registration happens from
    shell, concurrently with whatever else is running, and an append of one
    line is the only write that cannot corrupt a prior record. A half-written
    tail line is skipped instead of killing the whole read.
    """
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    merged: Dict[str, Dict[str, Any]] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        unit = str(rec.get("unit") or "").strip()
        if not unit:
            continue
        prior = merged.get(unit)
        if prior and "log" not in rec:
            # An ack is a partial record; merge so it cannot erase the log path.
            merged_rec = dict(prior)
            merged_rec.update(rec)
            merged[unit] = merged_rec
        else:
            # A registration (it names the log) starts a new run of the unit:
            # nothing from the previous run carries over (#2740). Merging it
            # left the earlier ``acked: true`` in place, so a re-run of an
            # acknowledged unit was hidden from every sweep, and ``scope``,
            # ``scheduled_at`` and ``notify_on_exit`` leaked the same way.
            merged[unit] = rec
    return list(merged.values())


def read_exit_marker(log_path: str, start_offset: int = 0) -> Optional[int]:
    """Return the exit code from the last ``EXIT=<n>`` line, else None.

    Only the tail is read: these logs can be large, and the marker the skill
    writes is always appended last. A non-integer payload counts as "a marker
    exists but its code is unreadable", reported as -1 rather than swallowed,
    because a malformed marker still proves the job stopped.

    ``start_offset`` (#2740) is the ``log_offset`` a registration recorded
    when the log already held a marker from an earlier run: only bytes written
    after it are read. A log now shorter than the offset was truncated or
    replaced and is read whole.
    """
    if not log_path:
        return None
    try:
        size = os.path.getsize(log_path)
        start = int(start_offset) if 0 < int(start_offset or 0) <= size else 0
        with open(log_path, "rb") as handle:
            handle.seek(max(start, size - 8192, 0))
            tail = handle.read().decode("utf-8", "ignore")
    except (OSError, TypeError, ValueError):
        return None
    code: Optional[int] = None
    for line in tail.splitlines():
        line = line.strip()
        if not line.startswith("EXIT="):
            continue
        raw = line[5:].strip()
        try:
            code = int(raw)
        except ValueError:
            code = -1
    return code


def _log_age(log_path: str) -> Optional[float]:
    try:
        return max(0.0, time.time() - os.path.getmtime(log_path))
    except OSError:
        return None


def unit_is_active(unit: str, *, suffix: str = ".service", user: bool = False) -> bool:
    """Best-effort systemd liveness. Absent systemctl means 'cannot tell'.

    Returns False rather than raising on non-systemd nodes (Termux), where the
    mtime signal carries the classification on its own. ``suffix=".timer"``
    asks about a ``schedule``d job that has not fired yet. ``user=True``
    (#2710) asks the caller's user manager: a ``systemd-run --user`` unit is
    invisible to the system manager, and asking there called an armed user
    timer ``lost``.
    """
    if not unit:
        return False
    name = unit if unit.endswith(suffix) else unit + suffix
    argv = ["systemctl", "--user", "is-active", name] if user else ["systemctl", "is-active", name]
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.stdout.decode("utf-8", "ignore").strip() == "active"


def marker_baseline(log_path: str, unit: str, *, user: bool = False, scheduled: bool = False) -> int:
    """``log_offset`` for a new registration: where this run's output starts (#2740).

    The recipe appends to the log, so a re-run under the same unit name and
    log finds the previous run's ``EXIT=`` line still in the tail, and that
    line used to be read as the new run's result. The offset is non-zero only
    when that old marker provably is not this run's:

    - a ``schedule``d job has not started yet, so any marker is older;
    - otherwise the unit must be running right now. With the unit not running
      the marker may be this run's own (a job that finished before it was
      registered), so it is accepted as before.
    """
    if not log_path or read_exit_marker(log_path) is None:
        return 0
    if not scheduled and not unit_is_active(unit, user=user):
        return 0
    try:
        return int(os.path.getsize(log_path))
    except OSError:
        return 0


def _log_offset(rec: Dict[str, Any]) -> int:
    try:
        return int(rec.get("log_offset") or 0)
    except (TypeError, ValueError):
        return 0


def classify_one(rec: Dict[str, Any], *, now: Optional[float] = None) -> Tuple[str, Optional[int]]:
    """Return (state, exit_code). exit_code is None unless state is done."""
    log_path = str(rec.get("log") or "")
    code = read_exit_marker(log_path, _log_offset(rec))
    if code is not None:
        return STATE_DONE, code
    age = _log_age(log_path)
    if age is not None and age < STALE_SECONDS:
        return STATE_RUNNING, None
    # No log at all, or one that has been quiet past the threshold: the unit
    # decides. If it is up the job simply has not written (yet, or lately).
    # A scheduled job (#2636) is "running" while its timer is still armed —
    # with a quiet log too (#2778): scheduling again under the same log leaves
    # the previous run's output there, and that run's silence says nothing
    # about a job that has not started. Otherwise there is no evidence
    # anywhere and it must be investigated.
    unit = str(rec.get("unit") or "")
    user = rec.get("scope") == SCOPE_USER
    if unit_is_active(unit, user=user):
        return STATE_RUNNING, None
    if rec.get("scheduled_at") and unit_is_active(unit, suffix=".timer", user=user):
        return STATE_RUNNING, None
    return STATE_LOST, None


_CALENDAR_INSTANT_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2}(?::\d{2})?)(?:\s+([A-Za-z][A-Za-z0-9_+/-]*))?"
)


def calendar_instant(spec: str) -> Optional[float]:
    """Epoch of a one-shot systemd calendar spec, else None (#2827).

    Only absolute instants are understood — ``YYYY-MM-DD HH:MM[:SS]`` with an
    optional zone name (``Asia/Seoul``, ``UTC``); without one it is the node's
    local clock, as systemd reads it. Recurring or relative specs
    (``05:00``, ``Mon *-*-* 05:00``) have no single due time and return None.
    """
    m = _CALENDAR_INSTANT_RE.fullmatch(" ".join(str(spec or "").split()))
    if not m:
        return None
    date, clock, zone = m.groups()
    try:
        naive = datetime.fromisoformat(f"{date}T{clock}")
    except ValueError:
        return None
    if not zone:
        return naive.astimezone().timestamp()
    if zone.upper() == "UTC":
        return naive.replace(tzinfo=timezone.utc).timestamp()
    try:
        from zoneinfo import ZoneInfo

        return naive.replace(tzinfo=ZoneInfo(zone)).timestamp()
    except Exception:  # unknown zone or no tz database: no due time, not a crash
        return None


def scheduled_due(rec: Dict[str, Any]) -> Optional[float]:
    """When a scheduled record is due: the recorded epoch, else its parsed spec."""
    if not rec.get("scheduled_at"):
        return None
    try:
        due = float(rec.get("scheduled_epoch") or 0)
    except (TypeError, ValueError):
        due = 0.0
    if due > 0:
        return due
    return calendar_instant(str(rec.get("scheduled_at") or ""))


def _too_old(rec: Dict[str, Any], now: float) -> bool:
    # A scheduled job is not old before it has even fired: its week runs from
    # the due time (#2827). A spec with no single due time keeps the old rule.
    due = scheduled_due(rec)
    if due is not None:
        return (now - due) > MAX_AGE_SECONDS
    try:
        started = float(rec.get("started_at") or 0)
    except (TypeError, ValueError):
        return False
    if started <= 0:
        return False
    return (now - started) > MAX_AGE_SECONDS


def classify(records: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Bucket unacked, non-expired records by state."""
    now = time.time()
    out: Dict[str, List[Dict[str, Any]]] = {STATE_DONE: [], STATE_RUNNING: [], STATE_LOST: []}
    for rec in records:
        if rec.get("acked") is True:
            continue
        if _too_old(rec, now):
            continue
        state, code = classify_one(rec)
        enriched = dict(rec)
        enriched["_state"] = state
        enriched["_exit"] = code
        out[state].append(enriched)
    key = lambda r: str(r.get("started_at") or "")  # noqa: E731
    for state in out:
        out[state].sort(key=key)
    return out


def _row(rec: Dict[str, Any]) -> str:
    unit = str(rec.get("unit") or "?").strip()
    summary = " ".join(str(rec.get("summary") or "").split())[:100]
    row = f"- `{unit}`"
    if summary:
        row += f" — {summary}"
    code = rec.get("_exit")
    if code is not None:
        row += f"  (**EXIT={code}**)"
    at = " ".join(str(rec.get("scheduled_at") or "").split())[:40]
    if at and code is None:
        row += f"  ⏰ {at}"
    log_path = str(rec.get("log") or "").strip()
    if log_path:
        row += f"  로그: `{log_path}`"
    return row


def _section(title: str, rows: List[Dict[str, Any]]) -> List[str]:
    if not rows:
        return []
    out = [title]
    for rec in rows[:MAX_ROWS_PER_SECTION]:
        out.append(_row(rec))
    hidden = len(rows) - MAX_ROWS_PER_SECTION
    if hidden > 0:
        out.append(f"- …외 {hidden}건 (`detached_jobs.py list` 로 전체 확인)")
    return out


def render(records: List[Dict[str, Any]]) -> str:
    """Return the block body, or '' when nothing is outstanding."""
    buckets = classify(records)
    done, running, lost = buckets[STATE_DONE], buckets[STATE_RUNNING], buckets[STATE_LOST]
    if not done and not running and not lost:
        return ""
    lines: List[str] = []
    sections = [
        (
            "✅ 완료됨 — 감시자가 죽어 못 받았을 수 있음. 결과 확인 후 `detached_jobs.py ack --unit <U>`:",
            done,
        ),
        ("⏳ 진행 중 — 재실행하지 말 것:", running),
        (
            "⚠ 소식 없음 — EXIT 마커도 없고 유닛도 사라짐. 재실행 전에 로그를 직접 확인할 것:",
            lost,
        ),
    ]
    for title, rows in sections:
        block = _section(title, rows)
        if not block:
            continue
        if lines:
            lines.append("")
        lines += block
    return "\n".join(lines)


def limit_bytes(text: str, cap: int) -> str:
    """Truncate on a UTF-8 boundary, mirroring memory_render.limit-bytes."""
    if cap <= 0:
        return text
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text
    marker = "\n…(truncated)"
    room = max(0, cap - len(marker.encode("utf-8")))
    return raw[:room].decode("utf-8", "ignore") + marker


def _append(path: str, rec: Dict[str, Any]) -> int:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        sys.stderr.write(f"detached_jobs: cannot write registry: {exc}\n")
        return 1
    return 0


def _opt(argv: List[str], name: str, default: str = "") -> str:
    flag = "--" + name
    for i, arg in enumerate(argv):
        if arg == flag and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    return default


def _max_bytes(argv: List[str]) -> int:
    raw = _opt(argv, "max-bytes")
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_BYTES


_REGISTER_USAGE = (
    "usage: detached_jobs.py register --unit U --log PATH [--summary S] [--workdir W]"
    " [--expect <duration> | --until <time>] [--no-wait] [--user]\n"
)


def _expectation(argv: List[str]) -> Tuple[Optional[int], Optional[str]]:
    """``(seconds, error)`` from ``--expect`` / ``--until``; ``(None, None)`` when absent."""
    expect = _opt(argv, "expect").strip()
    until = _opt(argv, "until").strip()
    if expect and until:
        return None, "--expect and --until are mutually exclusive"
    if expect:
        seconds = parse_duration(expect)
        return (seconds, None) if seconds else (None, f"cannot parse --expect {expect!r} (try 45m, 2h, 1d)")
    if until:
        seconds = parse_until(until)
        return (seconds, None) if seconds else (None, f"cannot parse --until {until!r} or it is not in the future")
    return None, None


def cmd_register(argv: List[str]) -> int:
    unit = _opt(argv, "unit").strip()
    log_path = _opt(argv, "log").strip()
    if not unit or not log_path:
        sys.stderr.write(_REGISTER_USAGE)
        return 2
    expect_seconds, err = _expectation(argv)
    if err:
        sys.stderr.write(f"detached_jobs: {err}\n{_REGISTER_USAGE}")
        return 2
    rec: Dict[str, Any] = {
        "unit": unit,
        "log": os.path.abspath(os.path.expanduser(log_path)),
        "started_at": int(time.time()),
    }
    # Free text is capped at registration, the same discipline #740 applies to
    # wait summaries: this string is injected into a later session's context.
    summary = " ".join(_opt(argv, "summary").split())[:200]
    if summary:
        rec["summary"] = summary
    workdir = _opt(argv, "workdir").strip()
    if workdir:
        rec["workdir"] = workdir
    if "--user" in argv:
        # A hand-made ``systemd-run --user`` unit (#2710): liveness must be
        # asked of the user manager, or the sweep calls it lost.
        rec["scope"] = SCOPE_USER
    baseline = marker_baseline(rec["log"], unit, user=rec.get("scope") == SCOPE_USER)
    if baseline:
        rec["log_offset"] = baseline
    plan = wait_plan(expect_seconds)
    if expect_seconds is not None:
        rec["expect_seconds"] = int(expect_seconds)
    if not plan["arm"]:
        # Over budget: the job's own completion must announce itself (#2636).
        rec["notify_on_exit"] = True
    rc = _append(registry_path(_opt(argv, "registry")), rec)
    if rc != 0:
        return rc
    if _opt(argv, "registry") or "--no-wait" in argv:
        return 0
    if not plan["arm"]:
        sys.stdout.write(f"bridge wait not armed: {plan['reason']}\n")
        return 0
    if plan["timeout_seconds"] is None:
        sys.stdout.write(f"hint: {plan['reason']}\n")
    _arm_bridge_wait(unit, rec["log"], summary, timeout_seconds=plan["timeout_seconds"],
                     log_offset=baseline, user=rec.get("scope") == SCOPE_USER)
    return rc


def _spool_dir() -> str:
    explicit = os.environ.get("CCC_PUSH_SPOOL", "").strip()
    if explicit:
        return explicit
    state = os.environ.get("CCC_STATE_DIR", "").strip()
    if not state:
        state = os.path.join(os.environ.get("HOME", "/root"), ".claude", "state")
    return os.path.join(state, "telegram-spool")


def _node_name() -> str:
    return os.environ.get("CCC_NODE", "").strip() or (os.uname().nodename.split(".")[0] if hasattr(os, "uname") else "node")


def read_result_line(path: str) -> str:
    """The job's own result line from ``--result-file`` (#2710), or ''.

    The last non-empty line of the file's head, whitespace-collapsed and capped
    at ``RESULT_MAX_CHARS`` — the job chose to write it, the same discipline as
    ``--summary``. Missing or unreadable files yield '' (never raises).
    """
    if not path:
        return ""
    try:
        with open(os.path.expanduser(path), "rb") as handle:
            raw = handle.read(RESULT_READ_BYTES).decode("utf-8", "ignore")
    except OSError:
        return ""
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    lines = [line for line in lines if line]
    return lines[-1][:RESULT_MAX_CHARS] if lines else ""


def write_notice(
    unit: str, exit_code: int, *, summary: str = "", log_path: str = "", spool: str = "",
    tier: str = NOTIFY_DEFAULT_TIER, result: str = "",
) -> str:
    """Queue one body-free completion notice for the bridge PushNotifier (#2636).

    Same record shape as ``claude/hooks/notify.sh`` (``ts/event/node/text/dedup``)
    plus ``tier`` (#2710), which the fleet relay forwards (``RAW_KEYS``) and
    the receiver routes on — without it every notice was ``info`` and waited
    for the 09:00 digest. Returns the spool file path, or '' when the spool is
    unwritable (never raises).
    """
    spool = spool or _spool_dir()
    tier = tier if tier in TIERS else NOTIFY_DEFAULT_TIER
    mark = "✅" if exit_code == 0 else "❌"
    text = f"{mark} 분리 작업 `{unit}` 종료 EXIT={exit_code}"
    summary = " ".join((summary or "").split())[:200]
    if summary:
        text += f" — {summary}"
    result = " ".join((result or "").split())[:RESULT_MAX_CHARS]
    if result:
        text += f" · 결과: {result}"
    if log_path:
        text += f" · 로그 {log_path}"
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    node = _node_name()
    record = {
        "ts": ts, "event": NOTIFY_EVENT, "node": node, "text": text,
        "dedup": f"{NOTIFY_EVENT}:{unit}:{exit_code}", "tier": tier,
    }
    safe_unit = re.sub(r"[^A-Za-z0-9_.-]", "_", unit)[:80]
    # Exit code and pid keep two notices for one unit in the same second apart
    # (a retry after failure); the notifier dedups on ``dedup``, not the name.
    final = os.path.join(spool, f"{ts.replace(':', '-')}-{NOTIFY_EVENT}-{safe_unit}-{exit_code}-{os.getpid()}.json")
    tmp = final + f".{os.getpid()}.tmp"
    try:
        os.makedirs(spool, mode=0o700, exist_ok=True)
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        os.replace(tmp, final)
    except OSError as exc:
        sys.stderr.write(f"detached_jobs: cannot queue notice: {exc}\n")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return ""
    return final


_NOTIFY_USAGE = (
    "usage: detached_jobs.py notify --unit U --exit N [--summary S] [--log P]"
    " [--tier info|notice|action|critical] [--fail-tier T] [--result-file P]\n"
    "  default --tier info: held for the next 09:00 KST digest, not sent at once;"
    " use --tier notice for an immediate notice (schedule's default)\n"
)


def _tier_opt(argv: List[str], name: str, default: str) -> Tuple[str, Optional[str]]:
    raw = _opt(argv, name).strip().lower()
    if not raw:
        return default, None
    if raw not in TIERS:
        return default, f"--{name} must be one of {', '.join(TIERS)} (got {raw!r})"
    return raw, None


def cmd_notify(argv: List[str]) -> int:
    unit = _opt(argv, "unit").strip()
    raw_exit = _opt(argv, "exit").strip()
    if not unit or not raw_exit:
        sys.stderr.write(_NOTIFY_USAGE)
        return 2
    try:
        exit_code = int(raw_exit)
    except ValueError:
        sys.stderr.write("detached_jobs: --exit must be an integer\n")
        return 2
    tier, err = _tier_opt(argv, "tier", NOTIFY_DEFAULT_TIER)
    fail_tier, fail_err = _tier_opt(argv, "fail-tier", tier)
    if err or fail_err:
        sys.stderr.write(f"detached_jobs: {err or fail_err}\n")
        return 2
    path = write_notice(
        unit, exit_code, summary=_opt(argv, "summary"), log_path=_opt(argv, "log").strip(),
        tier=tier if exit_code == 0 else fail_tier,
        result=read_result_line(_opt(argv, "result-file").strip()),
    )
    if path:
        sys.stdout.write(f"notice queued: {path}\n")
    return 0 if path else 1


_SCHEDULE_USAGE = (
    "usage: detached_jobs.py schedule --unit U --at \"<systemd calendar>\" --log PATH"
    " [--summary S] [--workdir W] [--tier T (default notice)] [--fail-tier T]"
    " [--result-file P] [--user] [--durable] [--print] -- <command…>\n"
    "note: the timer is transient and a reboot erases it (#2827) — for a wait that\n"
    "spans nights or days add --durable (persistent unit files, Persistent=true,\n"
    "removed after the run; needs an absolute future --at). Around a reboot:\n"
    "`export-timers --out FILE` before, `restore-timers --from FILE` after.\n"
)


def _job_env() -> Tuple[str, str]:
    """HOME and PATH for the job: transient and persistent units inherit neither."""
    return os.environ.get("HOME", "/root"), os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")


def _wrapper_script(
    *, unit: str, log_path: str, command: List[str], summary: str = "", module: str = "",
    tier: str = SCHEDULE_DEFAULT_TIER, fail_tier: str = "", result_file: str = "", cleanup: str = "",
) -> str:
    """The bash body shared by transient and durable schedules.

    Output appended to the log, ``EXIT=<rc>`` as the last line, then ``notify``;
    ``cleanup`` (durable only) runs after the notice and before the exit.
    """
    module = module or os.path.abspath(__file__)
    notify = [sys.executable or "python3", module, "notify", "--unit", unit, "--log", log_path, "--tier", tier]
    if fail_tier:
        notify += ["--fail-tier", fail_tier]
    if result_file:
        notify += ["--result-file", result_file]
    if summary:
        notify += ["--summary", summary]
    return (
        f"{shlex.join(command)} >> {shlex.quote(log_path)} 2>&1; rc=$?; "
        f"echo \"EXIT=$rc\" >> {shlex.quote(log_path)}; "
        f"{shlex.join(notify)} --exit \"$rc\" >/dev/null 2>&1 || true; "
        + (f"{cleanup}; " if cleanup else "")
        + "exit \"$rc\""
    )


def durable_unit_dir(*, user: bool = False) -> str:
    """Where ``schedule --durable`` writes its unit files."""
    override = os.environ.get(UNIT_DIR_ENV, "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if user:
        base = os.environ.get("XDG_CONFIG_HOME", "").strip() or os.path.join(os.path.expanduser("~"), ".config")
        return os.path.join(base, "systemd", "user")
    return SYSTEM_UNIT_DIR


def _unit_exec_quote(arg: str) -> str:
    """One ``ExecStart=`` word: double-quoted, with systemd's own expansions escaped.

    systemd expands ``$VAR`` / ``${VAR}`` and ``%`` specifiers inside
    ``ExecStart=``, so the wrapper's ``$rc`` would be emptied before bash saw
    it; ``$$`` and ``%%`` keep both literal.
    """
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def build_durable_units(
    *, unit: str, at: str, log_path: str, command: List[str], summary: str = "", workdir: str = "", module: str = "",
    tier: str = SCHEDULE_DEFAULT_TIER, fail_tier: str = "", result_file: str = "", user: bool = False,
    unit_dir: str = "",
) -> Dict[str, str]:
    """``{path: content}`` of the persistent ``.service`` + ``.timer`` pair (#2827 A).

    Same wrapper as the transient schedule, plus a cleanup that disables the
    timer and deletes both files once the run has announced itself, so a
    one-shot durable job leaves nothing behind.
    """
    unit_dir = unit_dir or durable_unit_dir(user=user)
    service_path = os.path.join(unit_dir, f"{unit}.service")
    timer_path = os.path.join(unit_dir, f"{unit}.timer")
    ctl = "systemctl --user" if user else "systemctl"
    cleanup = (
        f"{ctl} disable {shlex.quote(unit + '.timer')} >/dev/null 2>&1; "
        f"rm -f {shlex.quote(timer_path)} {shlex.quote(service_path)}; "
        f"{ctl} daemon-reload >/dev/null 2>&1 || true"
    )
    script = _wrapper_script(
        unit=unit, log_path=log_path, command=command, summary=summary, module=module,
        tier=tier, fail_tier=fail_tier, result_file=result_file, cleanup=cleanup,
    )
    home, path_env = _job_env()
    header = "# Written by detached_jobs.py schedule --durable (#2827); removes itself after the run.\n"
    service = header + (
        "[Unit]\n"
        f"Description=detached job {unit} (durable schedule)\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"Environment={_unit_exec_quote('HOME=' + home)}\n"
        f"Environment={_unit_exec_quote('PATH=' + path_env)}\n"
        + (f"WorkingDirectory={workdir}\n" if workdir else "")
        + f"ExecStart=/bin/bash -c {_unit_exec_quote(script)}\n"
    )
    timer = header + (
        "[Unit]\n"
        f"Description=timer for detached job {unit} (durable schedule)\n\n"
        "[Timer]\n"
        f"OnCalendar={at}\n"
        "Persistent=true\n"
        "AccuracySec=1min\n"
        f"Unit={unit}.service\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return {service_path: service, timer_path: timer}


def _durable_systemctl(args: List[str], *, user: bool) -> Tuple[bool, str]:
    argv = ["systemctl"] + (["--user"] if user else []) + args
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{shlex.join(argv)}: {exc}"
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "ignore").strip().splitlines()
        return False, f"{shlex.join(argv)} rc={proc.returncode}: {tail[-1] if tail else ''}"
    return True, ""


def install_durable(files: Dict[str, str], unit: str, *, user: bool) -> str:
    """Write the unit files, reload, enable the timer. '' on success, else why.

    Refuses to overwrite an existing file (a name collision with a real unit)
    and removes what it wrote when any step fails, so a failed install leaves
    the node as it was.
    """
    existing = [path for path in files if os.path.lexists(path)]
    if existing:
        return f"unit file already exists, not overwritten: {existing[0]}"
    written: List[str] = []
    try:
        for path, content in files.items():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp-{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
            written.append(path)
    except OSError as exc:
        for path in written:
            _remove_quietly(path)
        return f"cannot write unit files: {exc}"
    for args in (["daemon-reload"], ["enable", "--now", f"{unit}.timer"]):
        ok, why = _durable_systemctl(args, user=user)
        if not ok:
            for path in written:
                _remove_quietly(path)
            _durable_systemctl(["daemon-reload"], user=user)
            return why
    return ""


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def build_schedule_command(
    *, unit: str, at: str, log_path: str, command: List[str], summary: str = "", workdir: str = "", module: str = "",
    tier: str = SCHEDULE_DEFAULT_TIER, fail_tier: str = "", result_file: str = "", user: bool = False,
) -> List[str]:
    """The exact ``systemd-run`` argv for a scheduled, self-announcing job.

    The wrapper is the ``bridge-safe-detached-run`` Step 1 recipe plus the
    notice: command output appended to the log, ``EXIT=<rc>`` as the last
    line, then ``notify`` with the same unit/exit so the owner hears the
    result without a ⏳ wait. HOME/PATH are injected because transient units
    inherit neither. The notice carries ``tier`` (default ``notice``, #2710)
    so it is delivered at once instead of in the next morning's digest;
    ``user=True`` launches on the caller's user manager (non-root nodes).
    """
    home, path_env = _job_env()
    script = _wrapper_script(
        unit=unit, log_path=log_path, command=command, summary=summary, module=module,
        tier=tier, fail_tier=fail_tier, result_file=result_file,
    )
    argv = ["systemd-run"] + (["--user"] if user else []) + [
        "--unit", unit, "--collect",
        "--on-calendar", at, "--timer-property=AccuracySec=1min",
        f"--property=Environment=HOME={home}",
        f"--property=Environment=PATH={path_env}",
    ]
    if workdir:
        argv.append(f"--property=WorkingDirectory={workdir}")
    argv += ["/bin/bash", "-c", script]
    return argv


def cmd_schedule(argv: List[str]) -> int:
    if "--" not in argv:
        sys.stderr.write(_SCHEDULE_USAGE)
        return 2
    split = argv.index("--")
    opts, command = argv[:split], argv[split + 1:]
    unit = _opt(opts, "unit").strip()
    at = _opt(opts, "at").strip()
    log_path = _opt(opts, "log").strip()
    if not unit or not at or not log_path or not command:
        sys.stderr.write(_SCHEDULE_USAGE)
        return 2
    if not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,200}", unit):
        sys.stderr.write("detached_jobs: --unit must be a plain systemd unit name\n")
        return 2
    log_path = os.path.abspath(os.path.expanduser(log_path))
    summary = " ".join(_opt(opts, "summary").split())[:200]
    workdir = _opt(opts, "workdir").strip()
    tier, err = _tier_opt(opts, "tier", SCHEDULE_DEFAULT_TIER)
    fail_tier, fail_err = _tier_opt(opts, "fail-tier", "")
    if err or fail_err:
        sys.stderr.write(f"detached_jobs: {err or fail_err}\n")
        return 2
    result_file = _opt(opts, "result-file").strip()
    if result_file:
        result_file = os.path.abspath(os.path.expanduser(result_file))
    user = "--user" in opts
    if "--durable" in opts:
        return _schedule_durable(
            opts, unit=unit, at=at, log_path=log_path, command=command, summary=summary,
            workdir=workdir, tier=tier, fail_tier=fail_tier, result_file=result_file, user=user,
        )
    cmd = build_schedule_command(
        unit=unit, at=at, log_path=log_path, command=command, summary=summary, workdir=workdir,
        tier=tier, fail_tier=fail_tier, result_file=result_file, user=user,
    )
    if "--print" in opts or "--dry-run" in opts:
        sys.stdout.write(shlex.join(cmd) + "\n")
        return 0
    # Measured before the timer exists: whatever marker the log holds now is
    # from an earlier run (#2740).
    baseline = marker_baseline(log_path, unit, user=user, scheduled=True)
    if not _launch_schedule(cmd, user=user):
        return 3
    rec: Dict[str, Any] = {
        "unit": unit,
        "log": log_path,
        "started_at": int(time.time()),
        "scheduled_at": at[:80],
        "notify_on_exit": True,
        "tier": tier,
        # The argv just launched, whole: what `restore-timers` replays after a
        # reboot erased the transient timer (#2827). Time, command,
        # Environment= and --user all live in it.
        LAUNCH_KEY: cmd,
    }
    due = calendar_instant(at)
    if due is not None:
        # The sweep's week starts here, not at registration (#2827).
        rec["scheduled_epoch"] = int(due)
    if baseline:
        rec["log_offset"] = baseline
    if user:
        rec["scope"] = SCOPE_USER
    if summary:
        rec["summary"] = summary
    if workdir:
        rec["workdir"] = workdir
    rc = _append(registry_path(_opt(opts, "registry")), rec)
    if rc == 0:
        sys.stdout.write(
            f"scheduled: unit={unit} at={at!r} scope={SCOPE_USER if user else SCOPE_SYSTEM} tier={tier}"
            " (no ⏳ wait; completion arrives as a spool notice)\n"
        )
    return rc


def _schedule_durable(
    opts: List[str], *, unit: str, at: str, log_path: str, command: List[str], summary: str, workdir: str,
    tier: str, fail_tier: str, result_file: str, user: bool,
) -> int:
    """``schedule --durable``: persistent unit files that survive a reboot (#2827 A)."""
    if "\n" in at or "\r" in at:
        sys.stderr.write("detached_jobs: --at must be one line\n")
        return 2
    due = calendar_instant(at)
    if due is None:
        sys.stderr.write(
            "detached_jobs: --durable is one-shot and needs an absolute time"
            " (\"YYYY-MM-DD HH:MM[:SS] [zone]\"); recurring or relative specs are refused\n"
        )
        return 2
    if due <= time.time():
        sys.stderr.write(f"detached_jobs: --at {at!r} is already in the past\n")
        return 2
    files = build_durable_units(
        unit=unit, at=at, log_path=log_path, command=command, summary=summary, workdir=workdir,
        tier=tier, fail_tier=fail_tier, result_file=result_file, user=user,
    )
    ctl = "systemctl --user" if user else "systemctl"
    if "--print" in opts or "--dry-run" in opts:
        for path, content in files.items():
            sys.stdout.write(f"### {path}\n{content}\n")
        sys.stdout.write(f"### then: {ctl} daemon-reload && {ctl} enable --now {unit}.timer\n")
        return 0
    baseline = marker_baseline(log_path, unit, user=user, scheduled=True)
    problem = install_durable(files, unit, user=user)
    if problem:
        sys.stderr.write(f"detached_jobs: durable schedule not installed: {problem}\n")
        return 3
    rec: Dict[str, Any] = {
        "unit": unit,
        "log": log_path,
        "started_at": int(time.time()),
        "scheduled_at": at[:80],
        "scheduled_epoch": int(due),
        "notify_on_exit": True,
        "tier": tier,
        DURABLE_KEY: True,
        "unit_files": sorted(files),
    }
    if baseline:
        rec["log_offset"] = baseline
    if user:
        rec["scope"] = SCOPE_USER
    if summary:
        rec["summary"] = summary
    if workdir:
        rec["workdir"] = workdir
    rc = _append(registry_path(_opt(opts, "registry")), rec)
    if rc == 0:
        sys.stdout.write(
            f"scheduled (durable): unit={unit} at={at!r} scope={SCOPE_USER if user else SCOPE_SYSTEM} tier={tier}"
            f" files={','.join(sorted(files))} (survives reboot; removed after the run)\n"
        )
    return rc


def _launch_schedule(cmd: List[str], *, user: bool) -> bool:
    """Run the ``systemd-run`` argv; explain failures on stderr. True on success."""
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    except FileNotFoundError:
        sys.stderr.write("detached_jobs: systemd-run not available on this node (Termux? use setsid + notify)\n")
        return False
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"detached_jobs: systemd-run failed: {exc}\n")
        return False
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "ignore").strip().splitlines()
        last = tail[-1] if tail else ""
        sys.stderr.write(f"detached_jobs: systemd-run rc={proc.returncode}: {last}\n")
        if not user and _geteuid() != 0 and "authentication required" in last.lower():
            # Non-root harness account (#2710): the system manager wants polkit.
            sys.stderr.write(
                "detached_jobs: this account cannot start system units — retry with"
                " `schedule --user ...` (a user timer; see `loginctl enable-linger` to"
                " keep it running after logout)\n"
            )
        return False
    if user and _linger_enabled() is False:
        sys.stderr.write(
            "detached_jobs: warning: Linger=no for this user — the user timer stops when"
            " the last session logs out; `loginctl enable-linger` keeps it armed\n"
        )
    return True


def _geteuid() -> int:
    try:
        return os.geteuid()
    except AttributeError:  # pragma: no cover - non-POSIX
        return 0


def _linger_enabled() -> Optional[bool]:
    """``loginctl`` Linger for the current user: True/False, None when unknown."""
    user = os.environ.get("USER", "").strip() or str(_geteuid())
    try:
        proc = subprocess.run(
            ["loginctl", "show-user", user, "--property=Linger"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = proc.stdout.decode("utf-8", "ignore").strip()
    if value == "Linger=yes":
        return True
    if value == "Linger=no":
        return False
    return None


# ---- #2827: export / restore of scheduled timers around a reboot -------------

_ENV_VALUE_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\S+')


def launch_argv_error(rec: Dict[str, Any]) -> str:
    """Why a row's saved launch argv cannot be replayed, or '' when it can.

    The argv is run as a command, so it must have the shape ``schedule`` wrote
    and agree with the row it sits in — same unit, same scope, same time.
    Anything else is reported, never run.
    """
    argv = rec.get(LAUNCH_KEY)
    if argv is None:
        return "no launch definition (scheduled before export/restore existed)"
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        return "launch definition is not a list of strings"
    user = rec.get("scope") == SCOPE_USER
    head = ["systemd-run"] + (["--user"] if user else []) + [
        "--unit", str(rec.get("unit") or ""), "--collect", "--on-calendar",
    ]
    if argv[:len(head)] != head or len(argv) < len(head) + 2:
        return "launch definition does not match the row's unit or scope"
    if argv[len(head)][:80] != str(rec.get("scheduled_at") or ""):
        return "launch definition does not match the row's scheduled time"
    return ""


def timer_status(rec: Dict[str, Any], *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Where a scheduled job that has not started stands, else None (#2827).

    None means there is nothing to restore: not a ``schedule`` row, acked,
    already ran (an ``EXIT=`` marker after its offset), running right now, or
    aged out of the registry. Otherwise ``{"state", "restorable", "detail"}``
    with a ``TIMER_*`` state; ``restorable`` says whether the saved argv could
    re-create the timer (asked of an armed timer too, so an export can warn
    before the reboot rather than after it).
    """
    if not rec.get("scheduled_at") or rec.get("acked") is True:
        return None
    unit = str(rec.get("unit") or "")
    user = rec.get("scope") == SCOPE_USER
    if read_exit_marker(str(rec.get("log") or ""), _log_offset(rec)) is not None:
        return None
    if unit_is_active(unit, user=user):
        return None
    if rec.get(DURABLE_KEY) is True:
        # Persistent unit files: a reboot keeps the timer, so there is nothing
        # to replay. Report where it stands and never re-arm it from here.
        if unit_is_active(unit, suffix=".timer", user=user):
            return {"state": TIMER_ARMED, "restorable": True, "detail": "durable: persistent unit, survives a reboot"}
        detail = (
            f"durable timer {unit}.timer is not active and no EXIT marker was written;"
            " check `systemctl status` and the unit files — not re-armed automatically"
        )
        return {"state": TIMER_UNRESTORABLE, "restorable": False, "detail": detail}
    problem = launch_argv_error(rec)
    if unit_is_active(unit, suffix=".timer", user=user):
        return {"state": TIMER_ARMED, "restorable": not problem, "detail": problem}
    now = time.time() if now is None else now
    if _too_old(rec, now):
        return None
    due = scheduled_due(rec)
    if due is not None and due <= now:
        # Re-arming it would run the job at a time nobody asked for, or (a past
        # calendar instant never elapses) not at all. Say so instead.
        detail = (
            f"due {rec.get('scheduled_at')} passed {_fmt_duration(int(now - due))} ago with no EXIT marker;"
            " not re-armed — run it by hand if it is still wanted, then ack the unit"
        )
        return {"state": TIMER_MISSED, "restorable": False, "detail": detail}
    if problem:
        return {"state": TIMER_UNRESTORABLE, "restorable": False, "detail": problem}
    return {"state": TIMER_MISSING, "restorable": True, "detail": ""}


def live_definition(unit: str, *, user: bool = False) -> Optional[Dict[str, List[str]]]:
    """The live timer and service settings that matter, as sorted values, else None.

    Read from ``systemctl cat`` and only ever compared with another reading of
    the same kind, so unit-file quoting is never parsed back into an argv.
    ``Environment=`` is split into its assignments first: one line carrying
    two and two lines carrying one each are the same environment, and a text
    diff of the two called them different (#2827, 2026-11-17).
    """
    argv = ["systemctl"] + (["--user"] if user else []) + [
        "cat", "--no-pager", f"{unit}.timer", f"{unit}.service",
    ]
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    found: Dict[str, List[str]] = {key: [] for key in _LIVE_KEYS}
    for line in proc.stdout.decode("utf-8", "ignore").splitlines():
        key, sep, value = line.strip().partition("=")
        value = value.strip()
        if not sep or key not in found or not value:
            continue  # other settings, and the empty `ExecStart=` reset line
        if key == "Environment":
            found[key] += [
                token[1:-1] if len(token) > 1 and token[0] == token[-1] == '"' else token
                for token in _ENV_VALUE_RE.findall(value)
            ]
        else:
            found[key].append(value)
    if not any(found.values()):
        return None
    return {key: sorted(values) for key, values in found.items()}


def export_timers(records: List[Dict[str, Any]], *, now: Optional[float] = None) -> Dict[str, Any]:
    """The pre-reboot snapshot: every scheduled job that has not started.

    ``timers`` are the ones ``restore-timers`` can re-create; ``skipped`` are
    the ones it cannot, each with a ``reason``. An armed timer also carries
    ``live``, the values of the unit as systemd holds it now — the reference
    for the after-reboot comparison, and for a ``skipped`` row the definition
    to re-create by hand.
    """
    now = time.time() if now is None else now
    timers: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for rec in sorted(records, key=lambda r: str(r.get("unit") or "")):
        status = timer_status(rec, now=now)
        if status is None:
            continue
        user = rec.get("scope") == SCOPE_USER
        entry = {key: rec[key] for key in _EXPORT_KEYS if key in rec}
        entry["scope"] = SCOPE_USER if user else SCOPE_SYSTEM
        entry["state"] = status["state"]
        if status["state"] == TIMER_ARMED:
            live = live_definition(str(rec.get("unit") or ""), user=user)
            if live:
                entry["live"] = live
        if status["restorable"]:
            timers.append(entry)
        else:
            entry["reason"] = status["detail"]
            skipped.append(entry)
    return {
        "version": EXPORT_VERSION, "exported_at": int(now), "node": _node_name(),
        "timers": timers, "skipped": skipped,
    }


def _write_private(path: str, text: str) -> bool:
    """Write ``text`` to ``path`` owner-only and atomically; explain a failure."""
    path = os.path.abspath(os.path.expanduser(path))
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError as exc:
        sys.stderr.write(f"detached_jobs: cannot write {path}: {exc}\n")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


_EXPORT_USAGE = (
    "usage: detached_jobs.py export-timers [--out FILE]\n"
    "  JSON snapshot of the scheduled jobs that have not started (stdout, or FILE\n"
    "  owner-only). exit 0: all of them can be restored; exit 1: some are listed\n"
    "  under `skipped` (missed, or no launch definition).\n"
)


def cmd_export_timers(argv: List[str]) -> int:
    if "--help" in argv or "-h" in argv:
        sys.stdout.write(_EXPORT_USAGE)
        return 0
    doc = export_timers(load_records(registry_path(_opt(argv, "registry"))))
    text = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    out = _opt(argv, "out").strip()
    if not out:
        sys.stdout.write(text)
    elif not _write_private(out, text):
        return 2
    else:
        sys.stdout.write(f"exported: timers={len(doc['timers'])} skipped={len(doc['skipped'])} -> {out}\n")
    for entry in doc["skipped"]:
        sys.stderr.write(
            f"detached_jobs: not restorable: {entry.get('unit')} ({entry.get('state')}) — {entry.get('reason')}\n"
        )
    return 1 if doc["skipped"] else 0


def _load_snapshot(path: str) -> Tuple[List[Dict[str, Any]], str]:
    """``(rows, error)`` from an ``export-timers`` file; rows cover timers and skipped."""
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as handle:
            # A snapshot row can stand in for a job the registry no longer has,
            # and its argv is then run. Take it only from a file this user owns
            # and nobody else can write (`export-timers --out` writes it 0600).
            info = os.fstat(handle.fileno())
            if info.st_uid != _geteuid() or info.st_mode & 0o022:
                return [], f"snapshot {path} must be owned by this user and not group/world-writable"
            doc = json.load(handle)
    except (OSError, ValueError) as exc:
        return [], f"cannot read snapshot {path}: {exc}"
    if not isinstance(doc, dict) or doc.get("version") != EXPORT_VERSION or not isinstance(doc.get("timers"), list):
        return [], f"{path} is not an export-timers snapshot (version {EXPORT_VERSION})"
    skipped = doc.get("skipped") if isinstance(doc.get("skipped"), list) else []
    rows = [row for row in doc["timers"] + skipped if isinstance(row, dict) and str(row.get("unit") or "").strip()]
    return rows, ""


def restore_candidates(records: List[Dict[str, Any]], snapshot: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Registry rows, with what a snapshot adds to them.

    The registry stays the source of truth: it is what ``schedule`` and ``ack``
    wrote last, while a snapshot may be days old. A snapshot row contributes
    its ``live`` values only to the registry row it was copied from (same
    argv, same registration), and stands in as the job itself only when the
    registry no longer knows the unit at all.
    """
    by_unit: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        by_unit[str(rec.get("unit") or "")] = {key: value for key, value in rec.items() if key != "live"}
    for row in snapshot:
        unit = str(row.get("unit") or "")
        current = by_unit.get(unit)
        if current is None:
            by_unit[unit] = dict(row)
        elif current.get(LAUNCH_KEY) == row.get(LAUNCH_KEY) and current.get("started_at") == row.get("started_at"):
            current["live"] = row.get("live")
    return [by_unit[unit] for unit in sorted(by_unit)]


def _verify_live(rec: Dict[str, Any], outcome: str) -> Tuple[str, str]:
    """Compare the live unit with the snapshot's values; ``differs`` when they are not equal."""
    expected = rec.get("live")
    if not isinstance(expected, dict):
        return outcome, ""
    current = live_definition(str(rec.get("unit") or ""), user=rec.get("scope") == SCOPE_USER)
    if current is None:
        return outcome, "not verified: the live unit could not be read"
    changed = []
    for key in _LIVE_KEYS:
        want = expected.get(key)
        values = sorted(str(item) for item in want) if isinstance(want, list) else []
        if values != current.get(key):
            changed.append(key)
    if changed:
        return "differs", "live unit differs from the snapshot in " + ", ".join(changed)
    return outcome, "time, command and environment match the snapshot"


def restore_timers(
    records: List[Dict[str, Any]], *, dry_run: bool = False, now: Optional[float] = None,
) -> List[Tuple[str, Dict[str, Any], str]]:
    """Re-create every missing timer from its saved argv: ``[(outcome, row, detail)]``.

    Outcomes: ``restored`` (``would-restore`` on a dry run), ``kept`` (still
    armed — never launched twice), ``missed``, ``unrestorable``, ``failed``
    (``systemd-run`` refused) and ``differs`` (armed, but not with the values
    the snapshot recorded). Rows with nothing to restore are not listed.
    """
    results: List[Tuple[str, Dict[str, Any], str]] = []
    for rec in records:
        status = timer_status(rec, now=now)
        if status is None:
            continue
        state = status["state"]
        if state == TIMER_ARMED:
            outcome, detail = _verify_live(rec, "kept")
            if outcome == "kept" and not status["restorable"]:
                detail = f"armed, but a reboot would lose it: {status['detail']}"
        elif state in (TIMER_MISSED, TIMER_UNRESTORABLE):
            outcome, detail = state, status["detail"]
        elif dry_run:
            outcome, detail = "would-restore", shlex.join(rec[LAUNCH_KEY])
        elif _launch_schedule(rec[LAUNCH_KEY], user=rec.get("scope") == SCOPE_USER):
            outcome, detail = _verify_live(rec, "restored")
        else:
            outcome, detail = "failed", "systemd-run did not start the timer (see stderr)"
        results.append((outcome, rec, detail))
    return results


_RESTORE_USAGE = (
    "usage: detached_jobs.py restore-timers [--from FILE] [--dry-run]\n"
    "  re-create the transient timer of every scheduled job that has not started\n"
    "  and whose timer is gone, from the argv `schedule` saved. --from FILE (an\n"
    "  export-timers snapshot) also compares each unit with its pre-reboot values.\n"
    "  --dry-run (or --print) launches nothing. exit 0: every such job is armed;\n"
    "  exit 1: at least one is missed, unrestorable, failed or differs.\n"
)
_RESTORE_OUTCOMES = ("restored", "would-restore", "kept", "missed", "unrestorable", "failed", "differs")
_RESTORE_ATTENTION = ("missed", "unrestorable", "failed", "differs")


def cmd_restore_timers(argv: List[str]) -> int:
    if "--help" in argv or "-h" in argv:
        sys.stdout.write(_RESTORE_USAGE)
        return 0
    dry_run = "--dry-run" in argv or "--print" in argv
    snapshot: List[Dict[str, Any]] = []
    source = _opt(argv, "from").strip()
    if source:
        snapshot, err = _load_snapshot(source)
        if err:
            sys.stderr.write(f"detached_jobs: {err}\n{_RESTORE_USAGE}")
            return 2
    records = restore_candidates(load_records(registry_path(_opt(argv, "registry"))), snapshot)
    results = restore_timers(records, dry_run=dry_run)
    counts = {name: 0 for name in _RESTORE_OUTCOMES}
    for outcome, rec, detail in results:
        counts[outcome] += 1
        scope = SCOPE_USER if rec.get("scope") == SCOPE_USER else SCOPE_SYSTEM
        row = f"{outcome}\t{rec.get('unit')}\t{rec.get('scheduled_at')}\t{scope}"
        sys.stdout.write(row + (f"\t{detail}" if detail else "") + "\n")
    shown = [name for name in _RESTORE_OUTCOMES if counts[name] or name in ("restored", "kept")]
    if dry_run:
        shown = [name for name in shown if name != "restored"]
    summary = " ".join(f"{name}={counts[name]}" for name in shown)
    sys.stdout.write(f"restore-timers: {summary}" + (" (dry run: nothing launched)" if dry_run else "") + "\n")
    return 1 if any(counts[name] for name in _RESTORE_ATTENTION) else 0


def _bridge_python() -> str:
    """The bridge venv interpreter when this node runs the ccc bridge, else ''."""
    explicit = os.environ.get("CCC_BRIDGE_PYTHON", "").strip()
    candidates = [explicit] if explicit else []
    candidates.append("/opt/ccc-node/bridge/venv/bin/python")
    for candidate in candidates:
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return ""


def _arm_bridge_wait(unit: str, log_path: str, summary: str, *, timeout_seconds: Optional[int] = None,
                     log_offset: int = 0, user: bool = False) -> None:
    """Best-effort: register the job as a bridge ``detached-job`` wait (ccc-node #2605).

    Inside a bridge turn this puts ``⏳ Running detached · <unit>`` on the
    conversation's status line and wakes the conversation when the log's
    ``EXIT=`` marker appears — the only background path that survives a bridge
    restart. Outside a turn (no active route), on a node without the bridge,
    or when ``CCC_DETACHED_JOBS_NO_WAIT=1`` / ``--no-wait`` / ``--registry``
    is given, it does nothing; the job registry above is never affected.
    ``timeout_seconds`` (#2636) sizes the wait to the caller's expectation;
    None keeps the bridge default. ``log_offset`` (#2740) is always passed, so
    the wait ignores exactly the markers this registry entry ignores.
    ``user`` (#2728) hands the bridge the unit's scope: without it the wait
    asked the system manager about a ``--user`` unit and called it lost. A
    system-scope registration sends the same argv as before.
    """
    if os.environ.get("CCC_DETACHED_JOBS_NO_WAIT", "").strip() in {"1", "true", "yes", "on"}:
        return
    python = _bridge_python()
    if not python:
        return
    cmd = [
        python, "-m", "telegram_bot.core.external_wait_cli", "register",
        "--source", "detached-job", "--unit", unit, "--log", log_path,
    ]
    if summary:
        cmd += ["--summary", summary]
    if timeout_seconds is not None:
        cmd += ["--timeout-seconds", str(int(timeout_seconds))]
    cmd += ["--log-offset", str(max(0, int(log_offset)))]
    if user:
        cmd += ["--scope", SCOPE_USER]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return
    out = proc.stdout.decode("utf-8", "ignore").strip().splitlines()
    line = out[-1] if out else ""
    try:
        payload = json.loads(line) if line else {}
    except ValueError:
        payload = {}
    if proc.returncode == 0 and payload.get("ok"):
        sys.stdout.write(f"bridge wait armed: wait_id={payload.get('wait_id')} (⏳ status line + wake on EXIT)\n")
    else:
        code = payload.get("code") or f"rc={proc.returncode}"
        sys.stdout.write(f"bridge wait not armed ({code}); the job registry entry stands on its own\n")


def cmd_ack(argv: List[str]) -> int:
    path = registry_path(_opt(argv, "registry"))
    if "--all" in argv:
        units = [str(r.get("unit")) for r in load_records(path) if r.get("acked") is not True]
    else:
        unit = _opt(argv, "unit").strip()
        if not unit:
            sys.stderr.write("usage: detached_jobs.py ack --unit U | --all\n")
            return 2
        units = [unit]
    rc = 0
    for unit in units:
        rc |= _append(path, {"unit": unit, "acked": True, "acked_at": int(time.time())})
    return rc


def cmd_list(argv: List[str]) -> int:
    records = load_records(registry_path(_opt(argv, "registry")))
    buckets = classify(records)
    if "--json" in argv:
        flat = [r for rows in buckets.values() for r in rows]
        sys.stdout.write(json.dumps(flat, ensure_ascii=False, sort_keys=True) + "\n")
        return 0
    for state in (STATE_DONE, STATE_RUNNING, STATE_LOST):
        for rec in buckets[state]:
            code = rec.get("_exit")
            suffix = f" EXIT={code}" if code is not None else ""
            sys.stdout.write(f"{state}\t{rec.get('unit')}\t{rec.get('log')}{suffix}\n")
    return 0


def cmd_sweep(argv: List[str]) -> int:
    positional = [a for a in argv if not a.startswith("--")]
    path = registry_path(positional[0] if positional else _opt(argv, "registry"))
    body = render(load_records(path))
    if body:
        sys.stdout.write(limit_bytes(body, _max_bytes(argv)))
        sys.stdout.write("\n")
    return 0


def main(argv: List[str]) -> int:
    if not argv:
        return cmd_sweep([])
    cmd, rest = argv[0], argv[1:]
    if cmd == "register":
        return cmd_register(rest)
    if cmd == "schedule":
        return cmd_schedule(rest)
    if cmd == "notify":
        return cmd_notify(rest)
    if cmd == "export-timers":
        return cmd_export_timers(rest)
    if cmd == "restore-timers":
        return cmd_restore_timers(rest)
    if cmd == "ack":
        return cmd_ack(rest)
    if cmd == "list":
        return cmd_list(rest)
    if cmd == "sweep":
        return cmd_sweep(rest)
    # No subcommand: treat the whole argv as sweep args so the hook can call
    # `detached_jobs.py <path> --max-bytes N` exactly like pending_promises.py.
    return cmd_sweep(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - fail open, never break the loader
        if sys.argv[1:2] and sys.argv[1] in _LOUD_COMMANDS:
            # An operator runs these around a reboot: "exit 0, no output" would
            # read as "every timer is back" (#2827).
            sys.stderr.write(f"detached_jobs: {sys.argv[1]} failed: {exc!r}\n")
            raise SystemExit(1)
        raise SystemExit(0)
