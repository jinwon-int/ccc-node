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

Usage::

    detached_jobs.py register --unit U --log P [--summary S] [--workdir W]
    detached_jobs.py sweep [<registry path>] [--max-bytes N]
    detached_jobs.py ack --unit U [--all]
    detached_jobs.py list [--json]

With no explicit path the registry is ``$CCC_STATE_DIR/detached-jobs.jsonl``,
falling back to ``$HOME/.claude/state/detached-jobs.jsonl``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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
# noise, not live context.
MAX_AGE_SECONDS = 7 * 24 * 3600

STATE_DONE = "done"
STATE_RUNNING = "running"
STATE_LOST = "lost"


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
        if prior:
            # An ack is a partial record; merge so it cannot erase the log path.
            merged_rec = dict(prior)
            merged_rec.update(rec)
            merged[unit] = merged_rec
        else:
            merged[unit] = rec
    return list(merged.values())


def read_exit_marker(log_path: str) -> Optional[int]:
    """Return the exit code from the last ``EXIT=<n>`` line, else None.

    Only the tail is read: these logs can be large, and the marker the skill
    writes is always appended last. A non-integer payload counts as "a marker
    exists but its code is unreadable", reported as -1 rather than swallowed,
    because a malformed marker still proves the job stopped.
    """
    if not log_path:
        return None
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as handle:
            if size > 8192:
                handle.seek(-8192, os.SEEK_END)
            tail = handle.read().decode("utf-8", "ignore")
    except OSError:
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


def unit_is_active(unit: str) -> bool:
    """Best-effort systemd liveness. Absent systemctl means 'cannot tell'.

    Returns False rather than raising on non-systemd nodes (Termux), where the
    mtime signal carries the classification on its own.
    """
    if not unit:
        return False
    name = unit if unit.endswith(".service") else unit + ".service"
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.stdout.decode("utf-8", "ignore").strip() == "active"


def classify_one(rec: Dict[str, Any], *, now: Optional[float] = None) -> Tuple[str, Optional[int]]:
    """Return (state, exit_code). exit_code is None unless state is done."""
    log_path = str(rec.get("log") or "")
    code = read_exit_marker(log_path)
    if code is not None:
        return STATE_DONE, code
    age = _log_age(log_path)
    if age is None:
        # No log at all. If the unit is up the job simply has not written yet;
        # otherwise there is no evidence anywhere and it must be investigated.
        return (STATE_RUNNING, None) if unit_is_active(str(rec.get("unit") or "")) else (STATE_LOST, None)
    if age < STALE_SECONDS:
        return STATE_RUNNING, None
    if unit_is_active(str(rec.get("unit") or "")):
        return STATE_RUNNING, None
    return STATE_LOST, None


def _too_old(rec: Dict[str, Any], now: float) -> bool:
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


def cmd_register(argv: List[str]) -> int:
    unit = _opt(argv, "unit").strip()
    log_path = _opt(argv, "log").strip()
    if not unit or not log_path:
        sys.stderr.write("usage: detached_jobs.py register --unit U --log PATH [--summary S] [--workdir W]\n")
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
    return _append(registry_path(_opt(argv, "registry")), rec)


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
    except Exception:  # noqa: BLE001 - fail open, never break the loader
        raise SystemExit(0)
