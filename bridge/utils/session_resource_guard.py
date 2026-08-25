"""Body-free Linux/Android process-tree memory accounting for the session guard."""

from __future__ import annotations

import os
from pathlib import Path


def process_tree_rss_mb(
    root_pid: int | None = None, *, proc_root: Path = Path("/proc")
) -> float:
    """Return summed resident memory for ``root_pid`` and its descendants.

    ``/proc`` can be restricted on Android or hardened Linux hosts. Missing or
    unreadable records are ignored, and a completely unavailable probe returns
    zero so resource enforcement fails open. Summed RSS may count shared pages
    more than once; the configured watermark is therefore a conservative
    high-water signal, not billing-grade accounting.
    """

    root = os.getpid() if root_pid is None else int(root_pid)
    processes: dict[int, tuple[int, int]] = {}
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return 0.0

    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
            raw_stat = (entry / "stat").read_text(encoding="utf-8")
            right_paren = raw_stat.rfind(")")
            fields = raw_stat[right_paren + 2 :].split()
            parent_pid = int(fields[1])
            rss_kib = 0
            for line in (entry / "status").read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                if line.startswith("VmRSS:"):
                    rss_kib = int(line.split()[1])
                    break
        except (OSError, ValueError, IndexError):
            continue
        processes[pid] = (parent_pid, rss_kib)

    if root not in processes:
        return 0.0
    children: dict[int, list[int]] = {}
    for pid, (parent_pid, _rss_kib) in processes.items():
        children.setdefault(parent_pid, []).append(pid)

    total_kib = 0
    pending = [root]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        process = processes.get(pid)
        if process is None:
            continue
        total_kib += process[1]
        pending.extend(children.get(pid, ()))
    return round(total_kib / 1024.0, 1)


# -- idle-session RSS watermark scaling (#1277) --------------------------------
#
# The session resource guard closes an idle agent session (and kills any
# background children riding along in its process tree, e.g. an ad-hoc
# ``Bash(run_in_background)`` job) once the process tree's RSS crosses a
# configured watermark. A single fixed watermark does not fit a heterogeneous
# fleet: observed on a 22 GiB VPS with 20 GiB free, a single idle session with
# two MCP servers attached (searxng, firecrawl) routinely sat at 1.0-1.2 GiB
# RSS — right at the old fixed 1024 MiB default — so the guard fired on
# almost every idle tick (48 evictions/day observed) even though the host had
# no real memory pressure at all.

DEFAULT_SESSION_TREE_RSS_LIMIT_MB = 1024
MIN_SESSION_TREE_RSS_LIMIT_MB = 1024
MAX_SESSION_TREE_RSS_LIMIT_MB = 8192
SESSION_TREE_RSS_LIMIT_FRACTION = 0.25


def system_total_memory_mb(*, proc_root: Path = Path("/proc")) -> float:
    """Return total system memory in MiB, or 0.0 if it cannot be determined.

    Reads ``MemTotal:`` (kB) from ``<proc_root>/meminfo``. Missing, unreadable,
    or malformed input fails open (returns 0.0) rather than raising, so
    callers can fall back to a fixed default on hardened or non-Linux hosts.
    """
    try:
        text = (proc_root / "meminfo").read_text(encoding="utf-8")
    except OSError:
        return 0.0
    for line in text.splitlines():
        if not line.startswith("MemTotal:"):
            continue
        parts = line.split()
        if len(parts) < 2:
            return 0.0
        try:
            return round(int(parts[1]) / 1024.0, 1)
        except ValueError:
            return 0.0
    return 0.0


def default_session_tree_rss_limit_mb(*, proc_root: Path = Path("/proc")) -> int:
    """Scale the idle-session RSS watermark default to the host's real memory.

    A quarter of total system memory, floored at the historical 1024 MiB
    default and capped at 8192 MiB, keeps the watermark meaningful on
    genuinely small hosts (e.g. a resource-constrained Termux phone) while
    giving generously-provisioned nodes (VPS-class, 16+ GiB) enough room that
    a normal idle baseline is not mistaken for memory pressure. Detection
    failure falls back to the fixed default. An explicit
    ``CCC_BRIDGE_SESSION_TREE_RSS_LIMIT_MB`` always overrides this.
    """
    total = system_total_memory_mb(proc_root=proc_root)
    if total <= 0:
        return DEFAULT_SESSION_TREE_RSS_LIMIT_MB
    scaled = round(total * SESSION_TREE_RSS_LIMIT_FRACTION)
    return max(
        MIN_SESSION_TREE_RSS_LIMIT_MB, min(MAX_SESSION_TREE_RSS_LIMIT_MB, scaled)
    )
