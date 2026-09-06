#!/usr/bin/env python3
"""Read-only serving-generation check; expected pre-stop validation is stdin JSON."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

LIMIT = 1024 * 1024
HASH = re.compile(r"[0-9a-f]{64}\Z")


def timestamp(value) -> float:
    if not isinstance(value, str):
        raise ValueError("timestamp_missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp_without_timezone")
    return parsed.timestamp()


def read_health(path: Path) -> dict:
    path = Path(os.path.abspath(path))
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("health_parent_not_directory")
    info = path.parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("health_parent_not_owner_controlled")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_nlink != 1):
            raise ValueError("health_not_owner_controlled_regular_file")
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("health_too_large")
    return json.loads(data)


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        status = Path(f"/proc/{pid}/status")
        if Path("/proc/self/status").exists():
            # Linux/Termux: zombies have exited despite kill -0 succeeding.
            for line in status.read_text().splitlines():
                if line.startswith("State:"):
                    return line.split()[1] in {"R", "S", "D", "T", "t", "W", "I"}
            return False
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
            text=True, timeout=1,
        )
        state = result.stdout.strip()
        return result.returncode == 0 and bool(state) and state[0] in {"R", "S", "D", "T", "t", "W", "I"}
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def verify(expected: dict, health: dict, pid: int, not_before: float,
           max_age: float, now: float) -> dict:
    """Compare one health snapshot with the identity pinned before stop.

    This is owner-controlled startup provenance, not package/code attestation.
    No imports, installations, provider requests or process signals are run
    beyond the signal-zero liveness probe.
    """
    if (type(pid) is not int or pid <= 1 or
            not all(math.isfinite(v) for v in (not_before, max_age, now)) or
            not 0 < max_age <= 3600 or not 0 < not_before <= now):
        raise ValueError("invalid_observation_bounds")
    if expected.get("schema") != "ccc.prepared-runtime.v1" or expected.get("status") != "ready":
        raise ValueError("expected_validation_not_ready")
    source, runtime = expected.get("source_dir"), expected.get("runtime_dir")
    if not all(isinstance(p, str) and os.path.isabs(p) for p in (source, runtime)):
        raise ValueError("expected_paths_missing")
    seal, fingerprint = expected.get("source_seal"), expected.get("dependency_fingerprint")
    if (not isinstance(seal, dict) or not HASH.fullmatch(str(seal.get("sha256", "")))
            or type(seal.get("files")) is not int or seal["files"] <= 0
            or type(seal.get("bytes")) is not int or seal["bytes"] < 0
            or not isinstance(fingerprint, str) or not HASH.fullmatch(fingerprint)):
        raise ValueError("expected_fingerprints_missing")
    process = health.get("process", {})
    generation = health.get("runtime_generation", {})
    if type(health.get("schema_version")) is not int or health["schema_version"] != 1 or type(process.get("pid")) is not int or process["pid"] != pid:
        raise ValueError("health_pid_or_schema_mismatch")
    if (health.get("service", {}).get("state") != "available"
            or health.get("telegram", {}).get("state") != "healthy"
            or health.get("agent", {}).get("state") != "healthy"):
        raise ValueError("service_not_available")
    started = timestamp(process.get("started_at"))
    observed = timestamp(generation.get("observed_at"))
    updated = timestamp(health.get("updated_at"))
    if not not_before <= started <= observed <= updated <= now or now - updated > max_age:
        raise ValueError("health_stale_or_outside_launch_window")
    if (generation.get("schema") != "ccc.runtime-generation.v1"
            or generation.get("source_dir") != source
            or generation.get("source_seal") != seal
            or generation.get("python_prefix") != runtime
            or generation.get("dependency_fingerprint") != fingerprint
            or generation.get("collection_errors") != []):
        raise ValueError("serving_generation_mismatch")
    git_head = (expected.get("source_git") or {}).get("head")
    if git_head is not None and (generation.get("source_git") or {}).get("head") != git_head:
        raise ValueError("serving_git_head_mismatch")
    executable = generation.get("python_executable")
    if (not isinstance(executable, str) or not os.path.isabs(executable)
            or os.path.abspath(executable) != str(Path(runtime) / "bin/python")):
        raise ValueError("serving_interpreter_mismatch")
    if not process_alive(pid):
        raise ValueError("serving_process_not_alive")
    return {"schema": "ccc.prepared-serving.v1", "status": "available", "pid": pid,
            "source_dir": source, "source_seal": seal, "runtime_dir": runtime,
            "dependency_fingerprint": fingerprint, "health_updated_at": health["updated_at"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health-file", type=Path, required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--not-before", type=float, required=True)
    parser.add_argument("--max-age", type=float, default=150)
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("expected_validation_too_large")
        report = verify(json.loads(raw), read_health(args.health_file), args.pid,
                        args.not_before, args.max_age, datetime.now(timezone.utc).timestamp())
    except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError, RecursionError):
        # Never echo untrusted health bodies, provider errors or malformed JSON.
        report = {"schema": "ccc.prepared-serving.v1", "status": "unready",
                  "reason": "serving_generation_unverified"}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "available" else 1


if __name__ == "__main__":
    raise SystemExit(main())
