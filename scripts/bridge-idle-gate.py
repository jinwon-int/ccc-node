#!/usr/bin/env python3
"""bridge-idle-gate.py — is this bridge safe to restart right now? (#2567)

Every bridge restart path (fleet rollouts, the ``bridge-detached-self-restart``
skill, a self-update restart-cmd) used to wait for ``health.json``
``workload.active_requests == 0``. That counts in-flight turns only. A Claude
Code *background* Bash command — ``run_in_background``, a ``Monitor``, a
watcher an earlier turn left running — lives on inside the bridge's process
tree after its turn ended, and the restart SIGKILLs it: the completion notice
it promised never arrives (2026-10-28, one node: ``Killing process …
(bash|find|sort|head) with signal SIGKILL`` with 0 active requests).

Claude Code runs each Bash-tool command as ``bash -c source
<claude-dir>/shell-snapshots/snapshot-… && eval …`` under the ``claude`` CLI;
MCP servers are direct ``node``/``python3``/``ssh`` children without that
wrapper. So a tool command is a shell under the bridge whose argv names a
shell snapshot, or (other providers) a shell whose parent is the provider CLI.

Idle means all of:
  * health.json is fresh (``--max-health-age``) and names the same PID;
  * ``workload.active_requests == 0`` and ``requests.delegated_tasks_active == 0``;
  * no tool command is alive in the bridge's process tree.

Output is body-free: pids, ages and process names only — never argv, which
can carry a command line with secrets. Exit 0 idle, 1 busy, 2 undecidable
(callers must treat 2 as busy). stdlib only; Linux and Android/Termux ``/proc``.

Usage:
  bridge-idle-gate.py --unit ccc-hande-bridge [--user <account>] [--json]
  bridge-idle-gate.py --main-pid 1234 --health ~/.ccc-hande/health.json
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ash"})
PROVIDER_PREFIXES = ("claude", "codex", "danso", "piri", "crush")
SNAPSHOT_MARK = "/shell-snapshots/snapshot-"
DEFAULT_MAX_HEALTH_AGE_S = 300.0

EXIT_IDLE, EXIT_BUSY, EXIT_UNKNOWN = 0, 1, 2


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _stat(proc: Path, pid: int) -> tuple[int, int] | None:
    """(ppid, starttime ticks) from /proc/<pid>/stat; comm may hold spaces/parens."""

    raw = _read(proc / str(pid) / "stat")
    if raw is None:
        return None
    text = raw.decode("utf-8", "replace")
    close = text.rfind(")")
    fields = text[close + 2 :].split()
    try:
        return int(fields[1]), int(fields[19])
    except (IndexError, ValueError):
        return None


def _argv(proc: Path, pid: int) -> list[str]:
    raw = _read(proc / str(pid) / "cmdline") or b""
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _comm(proc: Path, pid: int) -> str:
    raw = _read(proc / str(pid) / "comm")
    return raw.decode("utf-8", "replace").strip() if raw else "?"


def _uptime(proc: Path) -> float:
    raw = _read(proc / "uptime")
    try:
        return float(raw.split()[0]) if raw else 0.0
    except (IndexError, ValueError):
        return 0.0


def process_table(proc: Path) -> dict[int, tuple[int, int]]:
    table: dict[int, tuple[int, int]] = {}
    try:
        entries = os.listdir(proc)
    except OSError:
        return table
    for entry in entries:
        if entry.isdigit():
            stat = _stat(proc, int(entry))
            if stat is not None:
                table[int(entry)] = stat
    return table


def descendants(table: dict[int, tuple[int, int]], root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    stack = list(children.get(root, ()))
    while stack:
        pid = stack.pop()
        found.append(pid)
        stack.extend(children.get(pid, ()))
    return sorted(found)


def _base(argv: list[str], fallback: str) -> str:
    return os.path.basename(argv[0]) if argv else fallback


def _is_provider(name: str) -> bool:
    return name.startswith(PROVIDER_PREFIXES)


def tool_commands(proc: Path, main_pid: int) -> list[dict[str, Any]]:
    """Outermost tool-command shells under ``main_pid``, with their subtree names."""

    table = process_table(proc)
    tree = descendants(table, main_pid)
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    uptime = _uptime(proc)
    marked: set[int] = set()
    for pid in tree:
        argv = _argv(proc, pid)
        name = _base(argv, _comm(proc, pid))
        if name not in SHELLS:
            continue
        parent = table[pid][0]
        parent_name = _base(_argv(proc, parent), _comm(proc, parent))
        if any(SNAPSHOT_MARK in arg for arg in argv) or _is_provider(parent_name):
            marked.add(pid)
    roots = [pid for pid in sorted(marked) if table[pid][0] not in marked]
    found = []
    for pid in roots:
        start = table[pid][1]
        age = max(0.0, uptime - start / hz) if uptime else None
        names = sorted({_comm(proc, child) for child in descendants(table, pid)})
        found.append(
            {
                "pid": pid,
                "age_s": round(age, 1) if age is not None else None,
                "processes": names,
            }
        )
    return found


def _age_of(stamp: Any, now: float) -> float | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return now - parsed.timestamp()


def evaluate(
    proc: Path,
    main_pid: int,
    health_path: Path,
    *,
    max_health_age: float = DEFAULT_MAX_HEALTH_AGE_S,
    now: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    report: dict[str, Any] = {
        "schema": "ccc.bridge-idle-gate.v1",
        "main_pid": main_pid,
        "verdict": "unknown",
        "reasons": [],
    }
    reasons: list[str] = report["reasons"]
    if main_pid <= 0 or _stat(proc, main_pid) is None:
        reasons.append("main-pid-not-running")
        return report
    try:
        health = json.loads(health_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        reasons.append("health-unreadable")
        return report
    age = _age_of(health.get("updated_at"), now)
    report["health_age_s"] = round(age, 1) if age is not None else None
    if age is None or age > max_health_age:
        reasons.append("health-stale")
        return report
    health_pid = (health.get("process") or {}).get("pid")
    if health_pid != main_pid:
        reasons.append("health-pid-mismatch")
        return report
    active = int((health.get("workload") or {}).get("active_requests") or 0)
    delegated = int((health.get("requests") or {}).get("delegated_tasks_active") or 0)
    commands = tool_commands(proc, main_pid)
    report.update(
        active_requests=active, delegated_tasks_active=delegated, tool_commands=commands
    )
    if active:
        reasons.append("active-requests")
    if delegated:
        reasons.append("delegated-tasks")
    if commands:
        reasons.append("tool-commands")
    report["verdict"] = "busy" if reasons else "idle"
    return report


def _systemctl(user: str | None) -> list[str]:
    return ["systemctl", "--user", "-M", f"{user}@"] if user else ["systemctl"]


def resolve_unit(unit: str, user: str | None) -> tuple[int, Path | None]:
    """MainPID and BOT_DATA_DIR/health.json of a systemd unit."""

    show = subprocess.run(
        [*_systemctl(user), "show", unit, "-p", "MainPID", "-p", "Environment", "--value"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    lines = show.stdout.splitlines()
    try:
        pid = int(lines[0])
    except (IndexError, ValueError):
        return 0, None
    data_dir = None
    for item in " ".join(lines[1:]).split():
        if item.startswith("BOT_DATA_DIR="):
            data_dir = item.split("=", 1)[1]
    return pid, (Path(data_dir) / "health.json") if data_dir else None


def _health_from_environ(proc: Path, pid: int) -> Path | None:
    raw = _read(proc / str(pid) / "environ") or b""
    for entry in raw.split(b"\0"):
        if entry.startswith(b"BOT_DATA_DIR="):
            return Path(entry.split(b"=", 1)[1].decode()) / "health.json"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--unit", help="systemd unit (e.g. ccc-hande-bridge)")
    target.add_argument("--main-pid", type=int, help="bridge PID (Termux/runit)")
    parser.add_argument("--user", help="systemd user manager owner for --unit")
    parser.add_argument("--health", type=Path, help="health.json (default: BOT_DATA_DIR)")
    parser.add_argument("--max-health-age", type=float, default=DEFAULT_MAX_HEALTH_AGE_S)
    parser.add_argument("--proc", type=Path, default=Path("/proc"), help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    health = args.health
    if args.unit:
        pid, unit_health = resolve_unit(args.unit, args.user)
        health = health or unit_health
    else:
        pid = args.main_pid
    if health is None and pid > 0:
        health = _health_from_environ(args.proc, pid)
    if health is None:
        report: dict[str, Any] = {
            "schema": "ccc.bridge-idle-gate.v1",
            "main_pid": pid,
            "verdict": "unknown",
            "reasons": ["health-path-unknown"],
        }
    else:
        report = evaluate(
            args.proc, pid, health.expanduser(), max_health_age=args.max_health_age
        )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        detail = ", ".join(report["reasons"]) or "no turn, task or tool command running"
        print(f"{report['verdict']}: {detail}")
        for command in report.get("tool_commands") or ():
            print(
                f"  tool command pid={command['pid']} age={command['age_s']}s "
                f"processes={','.join(command['processes']) or '-'}"
            )
    return {"idle": EXIT_IDLE, "busy": EXIT_BUSY}.get(report["verdict"], EXIT_UNKNOWN)


if __name__ == "__main__":
    sys.exit(main())
