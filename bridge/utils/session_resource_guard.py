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
