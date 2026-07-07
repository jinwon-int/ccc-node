"""
Orphan node-claude process reaper for ccc-telegram-bridge.

When the bridge restarts or crashes, its ``node claude`` child subprocesses
(spawned via claude-agent-sdk SubprocessCLITransport) may be reparented to
PID 1 (init/Termux) rather than being cleanly terminated.  These orphans
accumulate on resource-constrained devices (Android/Termux) and can never be
reaped by the new bridge instance.

This module provides:
- ``find_orphaned_claude_pids()``:  locate PPID=1 node-claude procs past an age threshold
- ``sweep_orphaned_claude_processes()``: find + SIGTERM in one call, returns killed PIDs
- ``start_periodic_reaper()``: asyncio long-running task for recurring sweeps

Reads ``/proc`` directly — no psutil dependency, works on Linux and Android/Termux.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────

#: Minimum process age (seconds) before a PPID=1 node-claude is considered
#: orphaned and eligible for reaping.  30 min gives plenty of headroom: a
#: legitimate fresh subprocess is never more than a few seconds old at the
#: moment a sweep fires.
DEFAULT_MIN_AGE_SECONDS: int = 1800

#: How often the periodic reaper wakes up and sweeps.
DEFAULT_SWEEP_INTERVAL_SECONDS: int = 900  # 15 minutes


# ── /proc helpers ─────────────────────────────────────────────────────────────


def _get_hz() -> int:
    """Clock ticks per second (SC_CLK_TCK).  Falls back to 100 on error."""
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, ValueError, OSError):
        return 100


def _read_text(path: str) -> str | None:
    """Read a small text file; return None on any OS/permission error."""
    try:
        with open(path, "r") as fh:
            return fh.read()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _read_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _get_uptime_seconds() -> float:
    """Return seconds since boot from /proc/uptime, or 0.0 on error."""
    raw = _read_text("/proc/uptime")
    if raw is None:
        return 0.0
    try:
        return float(raw.split()[0])
    except (ValueError, IndexError):
        return 0.0


def _parse_stat(stat: str) -> tuple[int, int] | None:
    """
    Parse ``/proc/<pid>/stat`` and return ``(ppid, starttime_ticks)``.

    The ``comm`` field (second field, wrapped in parentheses) may contain
    spaces and parentheses itself, so we anchor on the *last* closing paren.
    After that, the remaining fields are space-separated in a fixed order:
    state(0) ppid(1) pgrp(2) session(3) … starttime(19).
    """
    close_paren = stat.rfind(")")
    if close_paren < 0:
        return None
    fields = stat[close_paren + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        ppid = int(fields[1])
        starttime = int(fields[19])
        return ppid, starttime
    except (ValueError, IndexError):
        return None


def _cmdline_of(pid: int) -> str:
    """Return the null-byte-separated cmdline as a single whitespace string."""
    raw = _read_bytes(f"/proc/{pid}/cmdline")
    if raw is None:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def _argv_of(pid: int) -> list[str]:
    """Return the process argv as a list, preserving argument boundaries.

    /proc/<pid>/cmdline is NUL-separated, so splitting on ``\\x00`` keeps each
    argument intact even when a path contains spaces (e.g. a node binary under
    ``/opt/my node/bin/node``). Detection must use this rather than a
    space-joined string, which would mis-split such a path and miss the orphan.
    """
    raw = _read_bytes(f"/proc/{pid}/cmdline")
    if raw is None:
        return []
    return [
        a for a in raw.decode("utf-8", errors="replace").split("\x00") if a
    ]


def _is_node_claude(cmdline) -> bool:
    """
    Return True when the invocation looks like a ``node claude …`` process.

    The bridge spawns claude via the Agent SDK as:
        /path/to/node /path/to/claude --output-format stream-json …

    On JS-pinned nodes (e.g. daegyo) ``claude`` is the JS entry-point path,
    so we check that the first argument is ``node`` (basename) and that at
    least one subsequent argument contains the string ``claude``.

    Accepts either an argv list (preferred — preserves boundaries for paths
    with spaces) or a whitespace-joined string (best-effort, legacy).
    """
    parts = cmdline if isinstance(cmdline, list) else cmdline.split()
    if len(parts) < 2:
        return False
    first = parts[0].split("/")[-1].lower()
    if first not in ("node", "node.exe"):
        return False
    return any("claude" in p.lower() for p in parts[1:])


def _process_age_seconds(pid: int, uptime: float, hz: int) -> float | None:
    """
    Return approximate age (seconds) of ``pid`` using /proc/<pid>/stat.

    Returns ``None`` if the process has vanished or the data cannot be parsed.
    """
    stat = _read_text(f"/proc/{pid}/stat")
    if stat is None:
        return None
    parsed = _parse_stat(stat)
    if parsed is None:
        return None
    _, starttime_ticks = parsed
    age = uptime - (starttime_ticks / hz)
    return max(0.0, age)


# ── Core detection ────────────────────────────────────────────────────────────


def _is_orphaned_claude_process(
    pid: int,
    min_age_seconds: int,
    uptime: float,
    hz: int,
) -> bool:
    """
    Return True iff ``pid`` is:

    1. A ``node claude …`` process (cmdline check)
    2. Reparented to init (PPID == 1)
    3. At least ``min_age_seconds`` old

    All three conditions must hold.  Age guard prevents accidental kills of
    freshly-spawned legitimate subprocesses in edge-case timing windows.
    """
    stat = _read_text(f"/proc/{pid}/stat")
    if stat is None:
        return False

    parsed = _parse_stat(stat)
    if parsed is None:
        return False
    ppid, starttime_ticks = parsed

    if ppid != 1:
        return False

    age = uptime - (starttime_ticks / hz)
    if age < min_age_seconds:
        return False

    return _is_node_claude(_argv_of(pid))


def find_orphaned_claude_pids(
    min_age_seconds: int = DEFAULT_MIN_AGE_SECONDS,
) -> list[int]:
    """
    Scan ``/proc`` and return PIDs of orphaned node-claude processes.

    An orphan is a ``node claude`` process with PPID=1 that is at least
    ``min_age_seconds`` old.  The current process is never included.
    """
    try:
        entries = os.listdir("/proc")
    except OSError:
        logger.debug("Orphan reaper: cannot list /proc")
        return []

    hz = _get_hz()
    uptime = _get_uptime_seconds()
    own_pid = os.getpid()
    orphans: list[int] = []

    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == own_pid:
            continue
        if _is_orphaned_claude_process(pid, min_age_seconds, uptime, hz):
            orphans.append(pid)

    return sorted(orphans)


# ── Sweep + kill ──────────────────────────────────────────────────────────────


def sweep_orphaned_claude_processes(
    min_age_seconds: int = DEFAULT_MIN_AGE_SECONDS,
) -> list[int]:
    """
    Find and SIGTERM all orphaned node-claude processes.

    Each matched PID receives ``SIGTERM``.  The SDK's subprocess already
    installs a SIGTERM handler that flushes state and exits; a follow-up
    SIGKILL is intentionally *not* sent here — the periodic sweep will clean
    up any stragglers on the next tick if they have not exited.

    Returns the list of PIDs that were successfully signalled.
    """
    orphans = find_orphaned_claude_pids(min_age_seconds)
    if not orphans:
        return []

    killed: list[int] = []
    for pid in orphans:
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
            logger.info(
                "Orphan reaper: SIGTERM → PID %d (node-claude, PPID=1, age≥%ds)",
                pid,
                min_age_seconds,
            )
        except ProcessLookupError:
            logger.debug("Orphan reaper: PID %d already gone", pid)
        except PermissionError:
            logger.warning("Orphan reaper: no permission to signal PID %d", pid)
        except OSError as exc:
            logger.warning("Orphan reaper: os.kill(%d) failed: %s", pid, exc)

    return killed


# ── Async periodic task ───────────────────────────────────────────────────────


async def run_periodic_reaper(
    interval_seconds: int = DEFAULT_SWEEP_INTERVAL_SECONDS,
    min_age_seconds: int = DEFAULT_MIN_AGE_SECONDS,
) -> None:
    """
    Asyncio coroutine: sweep orphaned node-claude processes on a fixed interval.

    Intended to be launched as a background ``asyncio.Task``.  The first sweep
    fires after ``interval_seconds`` (not immediately — startup already does an
    explicit sweep via ``sweep_orphaned_claude_processes()``).

    Exceptions inside a sweep are caught and logged so the task never dies
    silently.
    """
    logger.info(
        "Orphan reaper: periodic task started (interval=%ds, min_age=%ds)",
        interval_seconds,
        min_age_seconds,
    )
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            killed = sweep_orphaned_claude_processes(min_age_seconds)
            if killed:
                logger.info(
                    "Orphan reaper periodic sweep: signalled %d orphan(s) — PIDs %s",
                    len(killed),
                    killed,
                )
            else:
                logger.debug("Orphan reaper periodic sweep: no orphans found")
        except asyncio.CancelledError:
            logger.info("Orphan reaper: periodic task cancelled")
            raise
        except Exception:
            logger.exception("Orphan reaper: unexpected error in periodic sweep")
