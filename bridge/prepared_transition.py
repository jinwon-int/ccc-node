#!/usr/bin/env python3
"""Private append-only evidence and a fail-closed lease for prepared restarts."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

if __package__:
    from .prepared_runtime import private_directory, safe_parent
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from prepared_runtime import private_directory, safe_parent

SCHEMA = "ccc.prepared-transition.v1"
NEXT = {
    "intent": {"validated", "rejected"},
    "validated": {"launching", "stop_failed"},
    "launching": {"candidate_available", "candidate_failed"},
    "candidate_failed": {"recovered", "recovery_failed"},
}
TERMINAL = {"rejected", "stop_failed", "candidate_available", "recovered", "recovery_failed"}
RESULT = {"rejected": 6, "stop_failed": 1, "candidate_available": 0, "recovered": 7, "recovery_failed": 8}
LIMIT = 1024 * 1024


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_record(path: Path, payload: dict) -> None:
    private_directory(path.parent)
    data = (json.dumps(payload, sort_keys=True) + "\n").encode()
    if len(data) > LIMIT:
        raise ValueError("record_too_large")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def read_record(path: Path) -> dict:
    private_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError("record_not_private_regular")
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("record_too_large")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("record_not_object")
    return value


def begin(root: Path, candidate: dict, previous: dict, launcher_pid: int) -> Path:
    root = Path(os.path.abspath(root))
    safe_parent(root)
    if not root.exists():
        root.mkdir(mode=0o700)
    private_directory(root)
    # Persist the root's directory entry as well as its later contents. This
    # also covers a prior attempt interrupted immediately after root creation.
    sync_directory(root.parent)
    # mkdir is the claim. No PID-based automatic reclamation: a dead driver
    # may have left a live candidate or an incomplete stop/start operation.
    active = root / "active"
    active.mkdir(mode=0o700)
    sync_directory(root)
    run = root / uuid.uuid4().hex
    run.mkdir(mode=0o700)
    sync_directory(root)
    write_record(active / "owner.json", {"run": run.name, "launcher_pid": launcher_pid})
    write_record(run / "00-intent.json", dict(schema=SCHEMA, phase="intent", run=run.name,
        recorded_at=datetime.now(timezone.utc).isoformat(), launcher_pid=launcher_pid,
        candidate=candidate, previous=previous))
    return run


def advance(run: Path, phase: str, exit_code: int, reports: list[dict] | None = None) -> None:
    run = Path(os.path.abspath(run))
    if not re.fullmatch(r"[0-9a-f]{32}", run.name):
        raise ValueError("invalid_run")
    private_directory(run.parent)
    private_directory(run)
    active = run.parent / "active"
    private_directory(active)
    if read_record(active / "owner.json").get("run") != run.name:
        raise ValueError("lease_owner_mismatch")
    records = sorted(run.glob("[0-9][0-9]-*.json"))
    if not records:
        raise ValueError("missing_intent")
    last = read_record(records[-1])
    if last.get("schema") != SCHEMA or last.get("run") != run.name or phase not in NEXT.get(last.get("phase"), set()):
        raise ValueError("invalid_phase_transition")
    record = dict(schema=SCHEMA, run=run.name, phase=phase, command_exit_code=exit_code,
                  recorded_at=datetime.now(timezone.utc).isoformat())
    if phase in RESULT:
        record["controller_exit_code"] = RESULT[phase]
    if phase == "validated":
        if (not isinstance(reports, list) or len(reports) != 2
                or any(not isinstance(r, dict) or r.get("schema") != "ccc.prepared-runtime.v1"
                       or r.get("status") != "ready" for r in reports)):
            raise ValueError("missing_validated_pair")
        record.update(candidate=reports[0], previous=reports[1])
    write_record(run / f"{len(records):02d}-{phase}.json", record)
    if phase in TERMINAL:
        # Preserve the lease evidence instead of unlinking state. Interrupted
        # writes/renames fail with evidence retained. A failure after rename
        # may leave the lease released; still report evidence failure, never
        # terminal success. No lifecycle action occurs after this boundary.
        released = run / "lease"
        if released.exists() or released.is_symlink():
            raise ValueError("lease_archive_exists")
        active.rename(released)
        sync_directory(run)
        sync_directory(run.parent)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("begin")
    create.add_argument("--root", type=Path, required=True)
    create.add_argument("--candidate-source", type=Path, required=True)
    create.add_argument("--candidate-runtime", type=Path, required=True)
    create.add_argument("--previous-source", type=Path, required=True)
    create.add_argument("--previous-runtime", type=Path, required=True)
    create.add_argument("--launcher-pid", type=int, required=True)
    step = commands.add_parser("advance")
    step.add_argument("--run", type=Path, required=True)
    step.add_argument("--phase", choices=sorted(set().union(*NEXT.values())), required=True)
    step.add_argument("--exit-code", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if args.command == "begin":
            candidate = {"source_dir": str(args.candidate_source.resolve()),
                         "prepared_dir": os.path.abspath(args.candidate_runtime)}
            previous = {"source_dir": str(args.previous_source.resolve()),
                        "prepared_dir": os.path.abspath(args.previous_runtime)}
            if candidate == previous or args.launcher_pid <= 1:
                raise ValueError("invalid_pair_or_launcher")
            print(begin(args.root, candidate, previous, args.launcher_pid))
        else:
            reports = None
            if args.phase == "validated":
                data = sys.stdin.buffer.read(LIMIT + 1)
                if len(data) > LIMIT:
                    raise ValueError("reports_too_large")
                reports = [json.loads(line) for line in data.splitlines()]
            advance(args.run, args.phase, args.exit_code, reports)
        return 0
    except (OSError, ValueError, TypeError, AttributeError):
        print("prepared transition journal unavailable; retain records and inspect active lease", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
