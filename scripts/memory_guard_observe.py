#!/usr/bin/env python3
"""Per-restart-cycle observer for the bridge memory guard (#1877, #2633 a).

A one-shot read of ``memory.peak`` / ``memory.events`` (the #2545 fleet
observers, doctor) answers "how bad was it since the cgroup was created", and
that is NOT "since the current process started":

- self-update restarts every bridge on each changed tick, so a one-shot
  snapshot keeps only the newest cycle and silently drops earlier pressure;
- when a stop leaves unit processes behind (systemd logs ``Unit process N
  remains running after unit stopped`` and, on the next start, ``Found
  left-over process N in control group while starting unit``) the cgroup is
  NOT recreated, so the kernel counters keep counting across restarts. That
  is why sogyo/hande reported the same ``1.9G memory peak, 257.4M memory
  swap peak`` on four consecutive stops (2026-11-01).

For each bridge unit this prints:

- ``cycles``: one row per stop in the window from the journal ``Consumed ...
  memory peak`` line (per-cycle peak/swap peak as systemd recorded it), with
  how many processes survived that stop;
- ``reused_cgroup``: whether the current start found left-over processes,
  i.e. ``peak``/``events`` span earlier process lifetimes;
- ``events_delta``: ``memory.events`` growth since the previous run, keyed by
  the cgroup id (``--state-file``; a recreated cgroup resets the baseline);
- ``psi``: ``memory.pressure`` some/full avg300;
- a verdict from the delta + oom-kill, never from "PID changed":

  ``FAIL``  memory.events max/oom_kill grew, or the journal shows an oom-kill;
  ``WARN``  high grew (reclaim pressure), unit not active, peak or a cycle
            peak >= ``--warn-pct`` of memory.high, or PSI some avg300 >= 10;
  ``PASS``  none of the above;
  ``UNVERIFIED`` the unit or its cgroup files could not be read.

Read-only except ``--state-file``. Prints numbers and counts only; journal
message text is never echoed.

Exit codes: 0 all PASS, 1 any WARN, 2 any FAIL, 3 any UNVERIFIED (and no
FAIL), 64 usage error.

Usage:
  memory_guard_observe.py [--unit UNIT ...] [--user] [--since SPEC |
      --lookback-hours 48] [--state-file PATH] [--warn-pct 80] [--json]

Without ``--unit`` it observes every loaded ``ccc-*bridge*.service``.
Test seams: ``CCC_MEMGUARD_SYSTEMCTL``, ``CCC_MEMGUARD_JOURNALCTL``,
``CCC_MEMGUARD_CGROUP_ROOT``, ``CCC_MEMGUARD_NOW``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

EVENT_KEYS = ("high", "max", "oom", "oom_kill", "oom_group_kill")
SHOW_PROPS = (
    "ActiveState", "MainPID", "ExecMainStartTimestamp", "ControlGroup",
    "ControlGroupId", "InvocationID",
)
TIMEOUT = 30
PSI_WARN_SOME_AVG300 = 10.0

_UNITS = {"B": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40, "P": 1 << 50, "E": 1 << 60}
_CONSUMED_RE = re.compile(r": Consumed (?P<rest>.*)$")
_PEAK_RE = re.compile(r"(?P<num>[0-9]+(?:\.[0-9]+)?)(?P<unit>[BKMGTPE]) memory peak")
_SWAP_PEAK_RE = re.compile(r"(?P<num>[0-9]+(?:\.[0-9]+)?)(?P<unit>[BKMGTPE]) memory swap peak")
_CPU_RE = re.compile(r"^(?P<cpu>.*?) CPU time")
_LEFTOVER_START_RE = re.compile(r": Found left-over process [0-9]+ .* in control group while starting unit")
_REMAINS_RE = re.compile(r": Unit process [0-9]+ .* remains running after unit stopped")
_STARTED_RE = re.compile(r"systemd\[[0-9]+\]: Started ")
_OOM_RE = re.compile(r"Failed with result 'oom-kill'|killed by the OOM killer")


def parse_size(num: str, unit: str) -> int:
    return int(float(num) * _UNITS[unit])


def run(cmd: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            cmd, text=True, capture_output=True, stdin=subprocess.DEVNULL,
            check=False, timeout=TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def systemctl_cmd(user: bool) -> list[str]:
    return [os.environ.get("CCC_MEMGUARD_SYSTEMCTL") or "systemctl"] + (["--user"] if user else [])


def discover_units(user: bool) -> list[str]:
    proc = run(systemctl_cmd(user) + [
        "list-units", "--type=service", "--all", "--plain", "--no-legend", "--no-pager",
        "ccc-*bridge*.service",
    ])
    if proc is None or proc.returncode != 0:
        return []
    units = []
    for line in proc.stdout.splitlines():
        name = line.split()[0] if line.split() else ""
        if re.fullmatch(r"ccc-[a-z0-9-]*bridge[a-z0-9-]*\.service", name):
            units.append(name)
    return sorted(set(units))


def systemctl_show(unit: str, user: bool) -> dict[str, str] | None:
    cmd = systemctl_cmd(user) + ["show", unit]
    for prop in SHOW_PROPS:
        cmd += ["-p", prop]
    proc = run(cmd)
    if proc is None or proc.returncode != 0:
        return None
    values: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def read_journal(unit: str, user: bool, since: str) -> list[tuple[float, str]] | None:
    cmd = [os.environ.get("CCC_MEMGUARD_JOURNALCTL") or "journalctl",
           "--user-unit" if user else "-u", unit,
           "--since", since, "--no-pager", "-o", "short-unix"]
    proc = run(cmd)
    if proc is None or proc.returncode != 0:
        return None
    lines: list[tuple[float, str]] = []
    for raw in proc.stdout.splitlines():
        head, _, rest = raw.partition(" ")
        try:
            lines.append((float(head), rest))
        except ValueError:
            continue
    return lines


def parse_journal(lines: list[tuple[float, str]]) -> dict[str, Any]:
    """Cycles (one per ``Consumed`` line), starts that reused the cgroup, oom-kills."""
    cycles: list[dict[str, Any]] = []
    starts: list[dict[str, Any]] = []
    remains = 0
    leftover = 0
    oom = 0
    for ts, msg in lines:
        if _OOM_RE.search(msg):
            oom += 1
        if _REMAINS_RE.search(msg):
            remains += 1
            continue
        if _LEFTOVER_START_RE.search(msg):
            leftover += 1
            continue
        m = _CONSUMED_RE.search(msg)
        if m:
            rest = m.group("rest")
            peak = _PEAK_RE.search(rest)
            swap = _SWAP_PEAK_RE.search(rest)
            cpu = _CPU_RE.search(rest)
            cycles.append({
                "stopped_at": ts,
                "cpu": cpu.group("cpu") if cpu else None,
                "memory_peak": parse_size(peak.group("num"), peak.group("unit")) if peak else None,
                "swap_peak": parse_size(swap.group("num"), swap.group("unit")) if swap else None,
                "survivors": remains,
            })
            remains = 0
            continue
        if _STARTED_RE.search(msg):
            starts.append({"started_at": ts, "leftover": leftover})
            leftover = 0
    return {"cycles": cycles, "starts": starts, "journal_oom_kills": oom}


def read_cgroup(cg_dir: Path) -> dict[str, Any] | None:
    def text(name: str) -> str | None:
        try:
            return (cg_dir / name).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    def num(name: str) -> int | str | None:
        value = text(name)
        if value is None:
            return None
        value = value.strip()
        return int(value) if value.isdigit() else value

    events_text = text("memory.events")
    if events_text is None:
        return None
    events = {k: 0 for k in EVENT_KEYS}
    for line in events_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in events and parts[1].isdigit():
            events[parts[0]] = int(parts[1])
    psi: dict[str, float] = {}
    for line in (text("memory.pressure") or "").splitlines():
        parts = line.split()
        if parts and parts[0] in ("some", "full"):
            for field in parts[1:]:
                key, _, value = field.partition("=")
                if key == "avg300":
                    try:
                        psi[f"{parts[0]}_avg300"] = float(value)
                    except ValueError:
                        pass
    try:
        inode = cg_dir.stat().st_ino
    except OSError:
        inode = None
    return {
        "high": num("memory.high"), "max": num("memory.max"),
        "current": num("memory.current"), "peak": num("memory.peak"),
        "swap_peak": num("memory.swap.peak"),
        "events": events, "psi": psi or None, "inode": inode,
    }


def load_state(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".memguard-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def events_delta(prev: dict[str, Any] | None, cgroup_key: str, events: dict[str, int]) -> tuple[dict[str, int] | None, str]:
    """Growth since the previous run of the same cgroup; (None, why) without a baseline."""
    if not prev or not isinstance(prev.get("events"), dict):
        return None, "no-baseline"
    if prev.get("cgroup") != cgroup_key:
        # A new cgroup starts counting at 0, so everything it shows is new.
        return dict(events), "cgroup-recreated"
    delta = {k: events[k] - int(prev["events"].get(k, 0)) for k in EVENT_KEYS}
    if any(v < 0 for v in delta.values()):
        return dict(events), "counter-reset"
    return delta, "same-cgroup"


def pct(value: Any, limit: Any) -> float | None:
    if isinstance(value, int) and isinstance(limit, int) and limit > 0:
        return round(value * 100.0 / limit, 1)
    return None


def observe_unit(unit: str, user: bool, since: str, state: dict[str, Any], warn_pct: float, now: float) -> dict[str, Any]:
    result: dict[str, Any] = {"unit": unit, "scope": "user" if user else "system", "reasons": []}
    props = systemctl_show(unit, user)
    if props is None:
        result.update(verdict="UNVERIFIED", reasons=["systemctl show failed"])
        return result
    result.update(
        active=props.get("ActiveState") or "unknown",
        main_pid=int(props["MainPID"]) if (props.get("MainPID") or "").isdigit() else None,
        main_start=props.get("ExecMainStartTimestamp") or None,
    )
    journal = read_journal(unit, user, since)
    parsed = parse_journal(journal) if journal is not None else None
    if parsed is None:
        result["reasons"].append("journal unreadable (cycles/oom-kill not checked)")
    else:
        result["cycles"] = parsed["cycles"]
        result["journal_oom_kills"] = parsed["journal_oom_kills"]
        result["stops_with_survivors"] = sum(1 for c in parsed["cycles"] if c["survivors"])
        result["starts_reusing_cgroup"] = sum(1 for s in parsed["starts"] if s["leftover"])
        result["reused_cgroup"] = bool(parsed["starts"][-1]["leftover"]) if parsed["starts"] else None
    cgroup = props.get("ControlGroup") or ""
    if result["active"] != "active" or not cgroup:
        result["verdict"] = "FAIL" if parsed and parsed["journal_oom_kills"] else "WARN"
        result["reasons"].append(f"unit {result['active']}")
        if parsed and parsed["journal_oom_kills"]:
            result["reasons"].append(f"journal oom-kill x{parsed['journal_oom_kills']}")
        return result
    root = Path(os.environ.get("CCC_MEMGUARD_CGROUP_ROOT") or "/sys/fs/cgroup")
    cg = read_cgroup(root / cgroup.lstrip("/"))
    if cg is None:
        result.update(verdict="UNVERIFIED")
        result["reasons"].append("cgroup v2 memory files unreadable")
        return result
    cg_id = props.get("ControlGroupId") or ""
    cgroup_key = f"id:{cg_id}" if cg_id.isdigit() and cg_id != "0" else f"ino:{cg['inode']}"
    delta, basis = events_delta(state.get(unit), cgroup_key, cg["events"])
    result.update(
        limits={"high": cg["high"], "max": cg["max"]},
        current=cg["current"], peak=cg["peak"], swap_peak=cg["swap_peak"],
        peak_pct_of_high=pct(cg["peak"], cg["high"]),
        events=cg["events"], events_delta=delta, delta_basis=basis, psi=cg["psi"],
        counters_span_restarts=bool(result.get("reused_cgroup")),
    )
    cycle_peaks = [c["memory_peak"] for c in result.get("cycles", []) if c["memory_peak"] is not None]
    result["window_max_cycle_peak"] = max(cycle_peaks) if cycle_peaks else None
    result["window_max_cycle_pct_of_high"] = pct(result["window_max_cycle_peak"], cg["high"])
    state[unit] = {"cgroup": cgroup_key, "events": cg["events"], "observed_at": now}

    judged = delta if delta is not None else cg["events"]
    fail, warn = [], []
    if judged["max"] > 0 or judged["oom_kill"] > 0:
        fail.append(f"memory.events max+{judged['max']} oom_kill+{judged['oom_kill']} ({basis})")
    if parsed and parsed["journal_oom_kills"]:
        fail.append(f"journal oom-kill x{parsed['journal_oom_kills']}")
    if judged["high"] > 0:
        warn.append(f"memory.events high+{judged['high']} ({basis}; reclaim pressure)")
    for label, value in (("peak", result["peak_pct_of_high"]), ("cycle peak", result["window_max_cycle_pct_of_high"])):
        if value is not None and value >= warn_pct:
            warn.append(f"{label} {value}% of memory.high (>= {warn_pct:g}%)")
    some = (cg["psi"] or {}).get("some_avg300")
    if some is not None and some >= PSI_WARN_SOME_AVG300:
        warn.append(f"PSI some avg300={some:g}")
    if result["counters_span_restarts"]:
        result["reasons"].append("current start reused the cgroup (left-over processes): peak/events span earlier process lifetimes")
    result["reasons"] = fail + warn + result["reasons"]
    result["verdict"] = "FAIL" if fail else ("WARN" if warn else "PASS")
    return result


def mib(value: Any) -> str:
    return f"{value // 1048576}M" if isinstance(value, int) else str(value)


def render_text(r: dict[str, Any]) -> str:
    head = f"{r['verdict']} {r['unit']}"
    if "limits" in r:
        ev = r["events"]
        d = r["events_delta"]
        head += (
            f" pid={r.get('main_pid')} high={mib(r['limits']['high'])} max={mib(r['limits']['max'])}"
            f" peak={mib(r['peak'])}({r['peak_pct_of_high']}%)"
            f" events=high:{ev['high']},max:{ev['max']},oom_kill:{ev['oom_kill']}"
            f" delta={'-' if d is None else ','.join(f'{k}:{d[k]}' for k in ('high', 'max', 'oom_kill'))}[{r['delta_basis']}]"
            f" psi_some300={(r['psi'] or {}).get('some_avg300')}"
        )
    if r.get("cycles") is not None:
        peaks = ",".join(mib(c["memory_peak"]) if c["memory_peak"] is not None else "?" for c in r["cycles"])
        head += f" cycles={len(r['cycles'])}[{peaks}] reused_cgroup={r.get('reused_cgroup')}"
    if r["reasons"]:
        head += " — " + "; ".join(r["reasons"])
    return head


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--unit", action="append", default=[])
    parser.add_argument("--user", action="store_true")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--since", help="journalctl --since value (e.g. '2026-11-02 11:00')")
    window.add_argument("--lookback-hours", type=float, default=48.0)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--warn-pct", type=float, default=80.0)
    parser.add_argument("--json", action="store_true", help="one JSON object per unit")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 64
    if args.lookback_hours <= 0 or not 0 < args.warn_pct <= 100:
        print("memory_guard_observe: --lookback-hours must be > 0 and --warn-pct in (0, 100]", file=sys.stderr)
        return 64
    now = float(os.environ.get("CCC_MEMGUARD_NOW") or time.time())
    since = args.since or f"@{int(now - args.lookback_hours * 3600)}"
    units = args.unit or discover_units(args.user)
    if not units:
        print("memory_guard_observe: no ccc-*bridge*.service unit found", file=sys.stderr)
        return 3
    state = load_state(args.state_file)
    results = [observe_unit(u, args.user, since, state, args.warn_pct, now) for u in units]
    if args.state_file is not None:
        save_state(args.state_file, state)
    for r in results:
        print(json.dumps(r, sort_keys=True) if args.json else render_text(r))
    verdicts = {r["verdict"] for r in results}
    if "FAIL" in verdicts:
        return 2
    if "UNVERIFIED" in verdicts:
        return 3
    return 1 if "WARN" in verdicts else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
