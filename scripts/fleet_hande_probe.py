#!/usr/bin/env python3
"""Body-free 「한데」 frontend probe. Transmitted over SSH by fleet-hande-watch.sh (#2569).

One node's ccc-hande-bridge could not start from 2026-10-27 19:20 to 2026-10-28
10:04 KST (a required EnvironmentFile had been retired) while the daily fleet
sweep watched only Telegram and Matrix and stayed quiet. On every node of the
fleet the same frontend had also been silently dropping every unsolicited
notice for days because ``notice_frames`` was off. This probe names both.

One verdict per node, read from ``/proc`` and the bridge's own health.json:

* RETIRED not-installed — no hande config, unit or runit service on this node
  (absence on purpose: fleet_watch_state.py treats RETIRED as normal).
* DOWN   no-process / bot-no-process / health-unavailable / crash-loop
* DEGRADED health-degraded / bot-disconnected / transport-error / notices-off
* UNVERIFIED … — the evidence could not be read; never claimed healthy.
* OK     available / degraded-stale-error

Nothing it prints carries message bodies, config values or paths beyond the
verdict. stdlib only; Linux (system and user units) and Android/Termux.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import time

STALE_ERROR_SECS = 3600
CRASH_LOOP_STREAK = 2
CRASH_WINDOW_SECS = 60
NO_BOT_MARK = "no bot connected"
PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _fields(path: Path) -> list[bytes] | None:
    try:
        return [part for part in path.read_bytes().split(b"\0") if part]
    except (OSError, ValueError):
        return None


def _is_bridge(argv: list[bytes]) -> bool:
    return any(argv[i : i + 2] == [b"-m", b"telegram_bot"] for i in range(len(argv) - 1))


def _is_native_bot(argv: list[bytes]) -> bool:
    return bool(argv) and os.path.basename(os.fsdecode(argv[0])) == "native-mls-bot"


def _scan(proc_root: Path) -> tuple[list[tuple[int, list[bytes]]], bool, bool]:
    """(hande bridge processes, a native bot is running, inspection was uncertain)."""

    bridges: list[tuple[int, list[bytes]]] = []
    bot = False
    uncertain = False
    try:
        processes = list(proc_root.iterdir())
    except OSError:
        return bridges, bot, True
    for proc in processes:
        if not proc.name.isdigit():
            continue
        argv = _fields(proc / "cmdline")
        if not argv:
            continue
        if _is_native_bot(argv):
            bot = True
            continue
        if not _is_bridge(argv):
            continue
        env = _fields(proc / "environ")
        try:
            cgroup = (proc / "cgroup").read_text(errors="replace")
        except OSError:
            cgroup = ""
        if (env and b"CCC_CHANNEL=hande" in env) or any(
            line.endswith("/ccc-hande-bridge.service") for line in cgroup.splitlines()
        ):
            bridges.append((int(proc.name), env or []))
        elif env is None and not cgroup:
            uncertain = True
    return bridges, bot, uncertain


def default_markers() -> list[Path]:
    """The probing account's own install markers (Termux has no /etc or /home)."""

    home = Path(os.environ.get("HOME", "/nonexistent"))
    return [home / ".config/ccc-hande/config.json", Path(PREFIX) / "var/service/ccc-hande-bridge"]


def installed(roots: list[Path], markers: list[Path]) -> bool:
    """Whether this node carries a hande frontend at all (config, unit or runit service)."""

    if any(marker.exists() for marker in markers):
        return True
    for root in roots:
        for rel in (
            "etc/ccc-hande/config.json",
            "etc/systemd/system/ccc-hande-bridge.service",
        ):
            if (root / rel).exists():
                return True
        for home in [root / "root", *sorted((root / "home").glob("*"))]:
            if (home / ".config/ccc-hande/config.json").exists() or (
                home / ".config/systemd/user/ccc-hande-bridge.service"
            ).exists():
                return True
    return False


def _env_path(env: list[bytes], name: bytes) -> Path | None:
    values = [entry.partition(b"=")[2] for entry in env if entry.startswith(name + b"=")]
    if len(values) != 1:
        return None
    path = Path(os.fsdecode(values[0]))
    return path if path.is_absolute() else None


def _crash_streak(data_dir: Path, now: float) -> int:
    try:
        record = json.loads((data_dir / "crash-budget.json").read_text())
    except (OSError, ValueError):
        return 0
    if not isinstance(record, dict) or record.get("running") is not True:
        return 0
    streak, started = record.get("streak"), record.get("started_at")
    if isinstance(streak, bool) or not isinstance(streak, int):
        return 0
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        return 0
    return max(0, streak) if now - started < CRASH_WINDOW_SECS else 0


def _agent_error_is_stale(data: dict, now: float, max_error_age: int) -> bool:
    agent, service = data.get("agent"), data.get("service")
    if not isinstance(agent, dict) or not isinstance(service, dict):
        return False
    last_error, reason = agent.get("last_error"), service.get("reason")
    if not isinstance(last_error, str) or not last_error or not isinstance(reason, str):
        return False
    if last_error not in reason:
        return False
    failed_at = _timestamp(agent.get("last_error_at"))
    if failed_at is None or now - failed_at <= max_error_age:
        return False
    succeeded_at = _timestamp(agent.get("last_ok_at"))
    return succeeded_at is None or succeeded_at <= failed_at


Verdict = tuple[str, str]


def _read_json(path: Path | None) -> tuple[dict | None, float | None]:
    """(object, mtime) of a bounded JSON file; (None, None) when unreadable."""

    if path is None:
        return None, None
    try:
        stat = path.stat()
        if stat.st_size > 1024 * 1024:
            return None, None
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, None
    return (data, stat.st_mtime) if isinstance(data, dict) else (None, None)


def _no_bridge(uncertain: bool, roots: list[Path], markers: list[Path]) -> Verdict:
    if uncertain:
        return "UNVERIFIED", "process-inspection"
    if not installed(roots, markers):
        return "RETIRED", "not-installed"
    return "DOWN", "no-process"


def _pid_verdict(proc_root: Path, data_dir: Path, recorded: object, now: float) -> Verdict:
    if _crash_streak(data_dir, now) >= CRASH_LOOP_STREAK:
        return "DOWN", "crash-loop"
    alive = isinstance(recorded, int) and recorded > 0 and (proc_root / str(recorded)).exists()
    return "UNVERIFIED", "health-pid" if alive else "health-pid-restarting"


def _runtime_verdict(data: dict, config: dict, now: float, max_error_age: int) -> Verdict:
    """Health, transport and notice config of a bridge whose health is fresh and its own."""

    service = data.get("service")
    state = service.get("state") if isinstance(service, dict) else None
    if state == "unavailable":
        return "DOWN", "health-unavailable"
    transport = data.get("telegram")
    if isinstance(transport, dict) and transport.get("state") not in (None, "healthy"):
        last_error = str(transport.get("last_error") or "")
        return "DEGRADED", "bot-disconnected" if NO_BOT_MARK in last_error else "transport-error"
    stale_error = state == "degraded" and _agent_error_is_stale(data, now, max_error_age)
    if state == "degraded" and not stale_error:
        return "DEGRADED", "health-degraded"
    if config.get("notice_frames") is not True:
        # CI results, background-task completions and restart receipts are all
        # dropped on this node (2026-10-28: every hande node, for days).
        return "DEGRADED", "notices-off"
    return ("OK", "degraded-stale-error") if stale_error else ("OK", "available")


def probe(
    proc_root: Path,
    *,
    fs_roots: list[Path] | None = None,
    markers: list[Path] | None = None,
    now: float | None = None,
    max_age: int = 120,
    max_error_age: int = STALE_ERROR_SECS,
) -> Verdict:
    bridges, bot, uncertain = _scan(proc_root)
    if len(bridges) > 1:
        return "UNVERIFIED", "multiple-processes"
    if not bridges:
        roots = fs_roots if fs_roots is not None else [Path("/")]
        return _no_bridge(uncertain, roots, default_markers() if markers is None else markers)
    pid, env = bridges[0]
    data_dir = _env_path(env, b"BOT_DATA_DIR")
    if data_dir is None:
        return "UNVERIFIED", "data-directory"
    observed = time.time() if now is None else now
    data, mtime = _read_json(data_dir / "health.json")
    if data is None or mtime is None:
        return "UNVERIFIED", "health-unreadable"
    if not isinstance(data.get("process"), dict):
        return "UNVERIFIED", "health-shape"
    if data["process"].get("pid") != pid:
        return _pid_verdict(proc_root, data_dir, data["process"].get("pid"), observed)
    if abs(observed - mtime) > max_age:
        return "UNVERIFIED", "health-stale"
    if not bot:
        return "DOWN", "bot-no-process"
    config, _ = _read_json(_env_path(env, b"CCC_HANDE_CONFIG_PATH"))
    if config is None:
        return "UNVERIFIED", "hande-config"
    return _runtime_verdict(data, config, observed, max_error_age)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    parser.add_argument("--stale-error-secs", type=int, default=STALE_ERROR_SECS)
    args = parser.parse_args()
    status, reason = probe(args.proc_root, max_error_age=args.stale_error_secs)
    print(f"HANDE_STATUS={status}")
    print(f"HANDE_REASON={reason}")
    print("PROBE_COMPLETE=1")


if __name__ == "__main__":
    main()
