#!/usr/bin/env python3
"""Body-free Matrix frontend probe. Transmitted over SSH by fleet-matrix-watch.sh."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote


def _fields(path: Path) -> list[bytes] | None:
    try:
        return [part for part in path.read_bytes().split(b"\0") if part]
    except (OSError, ValueError):
        return None


def _is_bridge(argv: list[bytes]) -> bool:
    return b"--path" in argv and any(
        argv[i : i + 2] == [b"-m", b"telegram_bot"] for i in range(len(argv) - 1)
    )


def _matrix_processes(proc_root: Path) -> tuple[list[tuple[int, list[bytes]]], bool]:
    matches: list[tuple[int, list[bytes]]] = []
    uncertain = False
    try:
        processes = list(proc_root.iterdir())
    except OSError:
        return matches, True
    for proc in processes:
        if not proc.name.isdigit():
            continue
        argv = _fields(proc / "cmdline")
        if not argv or not _is_bridge(argv):
            continue
        env = _fields(proc / "environ")
        try:
            cgroup = (proc / "cgroup").read_text(errors="replace")
        except OSError:
            cgroup = ""
        if (env and b"CCC_CHANNEL=matrix" in env) or any(
            line.endswith("/ccc-matrix-bridge.service") for line in cgroup.splitlines()
        ):
            matches.append((int(proc.name), env or []))
        elif env is None and not cgroup:
            uncertain = True
    return matches, uncertain


def _env_value(env: list[bytes], name: bytes) -> Path | None:
    values = [entry.partition(b"=")[2] for entry in env if entry.startswith(name + b"=")]
    if len(values) != 1:
        return None
    path = Path(os.fsdecode(values[0]))
    return path if path.is_absolute() else None


def _db_health(
    env: list[bytes], now: float, max_age: int, process_started: float
) -> tuple[str, str]:
    config_path = _env_value(env, b"CCC_MATRIX_CONFIG_PATH")
    if config_path is None:
        return "UNVERIFIED", "matrix-config"
    try:
        if config_path.stat().st_size > 1024 * 1024:
            return "UNVERIFIED", "matrix-config"
        config = json.loads(config_path.read_text())
        state_dir = Path(config["state_directory"])
        if not state_dir.is_absolute():
            return "UNVERIFIED", "matrix-config"
        db_path = state_dir / "inbox.sqlite3"
        with sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True, timeout=2) as db:
            row = db.execute("SELECT value FROM meta WHERE key='health'").fetchone()
        data = json.loads(row[0]) if row else None
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return "UNVERIFIED", "matrix-db"
    if not isinstance(data, dict):
        return "UNVERIFIED", "matrix-db"
    updated = data.get("updated")
    if not isinstance(updated, (int, float)) or abs(now - updated) > max_age:
        return "UNVERIFIED", "matrix-db-stale"
    if updated < process_started - 2:
        return "UNVERIFIED", "matrix-db-before-process"
    state = data.get("state")
    if state == "ready":
        return "OK", "db-ready"
    if state == "network-retry":
        return "DEGRADED", "db-network-retry"
    if state == "stopped":
        return "DOWN", "db-stopped"
    return "UNVERIFIED", "matrix-db-state"


def probe(proc_root: Path, *, now: float | None = None, max_age: int = 120) -> tuple[str, str]:
    matches, uncertain = _matrix_processes(proc_root)
    if len(matches) > 1:
        return "UNVERIFIED", "multiple-processes"
    if not matches:
        return ("UNVERIFIED", "process-inspection") if uncertain else ("DOWN", "no-process")

    pid, env = matches[0]
    data_dir = _env_value(env, b"BOT_DATA_DIR")
    if data_dir is None:
        return "UNVERIFIED", "data-directory"
    observed = time.time() if now is None else now
    health = data_dir / "health.json"
    try:
        stat = health.stat()
        if stat.st_size > 1024 * 1024:
            return "UNVERIFIED", "health-size"
        stale = abs(observed - stat.st_mtime) > max_age
        data = json.loads(health.read_text())
    except (OSError, ValueError):
        return "UNVERIFIED", "health-unreadable"
    if not isinstance(data, dict) or not isinstance(data.get("process"), dict):
        return "UNVERIFIED", "health-shape"
    process = data["process"]
    if process.get("pid") != pid:
        return "UNVERIFIED", "health-pid"
    try:
        started = datetime.fromisoformat(process["started_at"].replace("Z", "+00:00")).timestamp()
    except (KeyError, AttributeError, ValueError):
        return "UNVERIFIED", "health-started"
    if stale:
        return _db_health(env, observed, max_age, started)
    service = data.get("service")
    state = service.get("state") if isinstance(service, dict) else None
    if state == "available":
        return "OK", "available"
    if state == "degraded":
        return "DEGRADED", "health-degraded"
    if state == "unavailable":
        return "DOWN", "health-unavailable"
    return _db_health(env, observed, max_age, started)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    args = parser.parse_args()
    status, reason = probe(args.proc_root)
    print(f"MATRIX_STATUS={status}")
    print(f"MATRIX_REASON={reason}")
    print("PROBE_COMPLETE=1")


if __name__ == "__main__":
    main()
