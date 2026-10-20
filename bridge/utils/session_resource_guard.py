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

    Two passes: the parent map needs every process's ``stat`` (small, one line),
    but ``VmRSS`` is only needed for the subtree, and ``status`` is a ~50-line
    file. Reading it for every process on the host — on an idle tick, for a
    handful of descendants — was the bulk of the probe's cost.
    """

    root = os.getpid() if root_pid is None else int(root_pid)
    parents: dict[int, int] = {}
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return 0.0

    # Pass 1 — parent links only.
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
            raw_stat = (entry / "stat").read_text(encoding="utf-8")
            right_paren = raw_stat.rfind(")")
            fields = raw_stat[right_paren + 2 :].split()
            parent_pid = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
        parents[pid] = parent_pid

    if root not in parents:
        return 0.0
    children: dict[int, list[int]] = {}
    for pid, parent_pid in parents.items():
        children.setdefault(parent_pid, []).append(pid)

    # Pass 2 — VmRSS for the subtree only.
    total_kib = 0
    pending = [root]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen or pid not in parents:
            continue
        seen.add(pid)
        try:
            for line in (proc_root / str(pid) / "status").read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                if line.startswith("VmRSS:"):
                    total_kib += int(line.split()[1])
                    break
        except (OSError, ValueError, IndexError):
            # The process exited between the two passes, or its status is
            # unreadable: skip its RSS but keep walking its children.
            pass
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


def _meminfo_mb(field: str, *, proc_root: Path) -> float | None:
    """Return one ``<proc_root>/meminfo`` field in MiB, or None if unavailable.

    Missing, unreadable, or malformed input returns None rather than raising
    (non-Linux hosts have no ``/proc``; hardened or Android hosts may hide it).
    """
    try:
        text = (proc_root / "meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    prefix = f"{field}:"
    for line in text.splitlines():
        if not line.startswith(prefix):
            continue
        parts = line.split()
        if len(parts) < 2:
            return None
        try:
            kib = int(parts[1])
        except ValueError:
            return None
        return round(kib / 1024.0, 1) if kib >= 0 else None
    return None


def system_total_memory_mb(*, proc_root: Path = Path("/proc")) -> float:
    """Return total system memory in MiB, or 0.0 if it cannot be determined.

    Reads ``MemTotal:`` (kB) from ``<proc_root>/meminfo``. Missing, unreadable,
    or malformed input fails open (returns 0.0) rather than raising, so
    callers can fall back to a fixed default on hardened or non-Linux hosts.
    """
    return _meminfo_mb("MemTotal", proc_root=proc_root) or 0.0


def system_available_memory_mb(*, proc_root: Path = Path("/proc")) -> float | None:
    """Return the kernel's ``MemAvailable`` estimate in MiB, or None.

    ``MemAvailable`` (Linux 3.14+) counts reclaimable page cache, so it is the
    number that says whether the host is actually short of memory. None means
    "unknown" (no ``/proc`` on macOS, restricted ``/proc`` on Termux or a
    hardened host, an older kernel without the field, malformed input); it is
    never reported as zero, which would read as maximal pressure.
    """
    return _meminfo_mb("MemAvailable", proc_root=proc_root)


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


# -- host memory pressure gate for the RSS watermark (#2298) ------------------
#
# On a 4 GiB host the watermark sits at the 1024 MiB floor, and an idle bridge
# tree (bridge + claude + a few stdio MCP servers) is already at that floor.
# The RSS watermark alone then reads "pressure" on almost every tick while the
# host still has more than half of its memory available, and each eviction
# only buys a cold start of the CLI and its MCP servers. So the RSS watermark
# is a necessary condition, and the host must also be genuinely short of
# memory: ``MemAvailable`` below ``max(512 MiB, 15% of MemTotal)``.

HOST_PRESSURE_MIN_AVAILABLE_MB = 512
HOST_PRESSURE_AVAILABLE_FRACTION = 0.15


def host_pressure_available_threshold_mb(total_mb: float) -> float:
    """Return the ``MemAvailable`` level below which the host is pressured."""

    return max(
        float(HOST_PRESSURE_MIN_AVAILABLE_MB),
        max(0.0, float(total_mb)) * HOST_PRESSURE_AVAILABLE_FRACTION,
    )


def host_memory_pressure(
    *, proc_root: Path = Path("/proc")
) -> tuple[bool | None, float | None]:
    """Return ``(pressured, mem_available_mb)`` for the RSS-watermark gate.

    ``pressured`` is None when ``MemAvailable`` cannot be read; the caller
    then keeps the RSS-only rule so pressure protection never silently
    disappears on a host whose ``/proc/meminfo`` is restricted. An unreadable
    ``MemTotal`` alone only drops the 15 % term (the 512 MiB floor stays).
    """

    available = system_available_memory_mb(proc_root=proc_root)
    if available is None:
        return None, None
    threshold = host_pressure_available_threshold_mb(
        system_total_memory_mb(proc_root=proc_root)
    )
    return available < threshold, available
