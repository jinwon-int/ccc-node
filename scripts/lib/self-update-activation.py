#!/usr/bin/env python3
"""Durable activation receipts; caller holds the existing updater lock.

A terminal receipt replaces pending evidence instead of deleting its only copy.
Any interrupted transaction/temp, including a dangling link, requires operator
reconciliation. No service commands or automatic recovery are performed here.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccc_secure_fs import (  # noqa: E402
    SecureFsError as SecureFsError,
    atomic_write_bytes_at,
    read_owner_only_bytes,
)

NAME = "self-update.pending-activation.json"
LIMIT = 32768
SHA = re.compile(r"[0-9a-f]{40}")


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone-required")
    return result.timestamp()


@contextlib.contextmanager
def directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        meta = os.fstat(fd)
        if meta.st_uid != os.geteuid() or stat.S_IMODE(meta.st_mode) & 0o022:
            raise ValueError("unsafe-directory")
        yield fd
    finally:
        os.close(fd)


def residue(fd):
    return any(
        n == NAME + ".intent" or n.startswith(NAME + ".tmp.") or n.startswith("." + NAME + ".tmp.")
        for n in os.listdir(fd)
    )


def load(fd):
    if residue(fd):
        raise ValueError("interrupted-write")
    # /proc fd pins the validated parent; the shared reader rejects links,
    # hardlinks, loose modes, oversized files and concurrent file replacement.
    try:
        raw, _ = read_owner_only_bytes(
            f"/proc/self/fd/{fd}/{NAME}", max_bytes=LIMIT, exact_mode=0o600
        )
    except FileNotFoundError:
        return None
    record = json.loads(raw)
    if (
        not isinstance(record, dict)
        or record.get("schema") != "ccc.self-update.activation.v1"
        or not SHA.fullmatch(str(record.get("target_sha", "")))
        or not SHA.fullmatch(str(record.get("previous_sha", "")))
        or not isinstance(record.get("services"), list)
        or not isinstance(record.get("snapshot"), str)
        or not isinstance(record.get("outcome"), str)
    ):
        raise ValueError("unparsable")
    timestamp(record["updated_at"])
    timestamp(record["started_at"])
    return record


def durable_write(fd, record):
    load(fd)  # Refuse to overwrite unsafe or interrupted evidence.
    raw = (json.dumps(record, separators=(",", ":")) + "\n").encode()
    if len(raw) > LIMIT:
        raise ValueError("too-large")
    intent = NAME + ".intent"
    guard = os.open(intent, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        os.fsync(guard)
    finally:
        os.close(guard)
    os.fsync(fd)
    # False means unsupported directory fsync, never durable success.
    if not atomic_write_bytes_at(fd, NAME, raw):
        raise OSError("directory-sync-unsupported")
    try:
        os.unlink(intent, dir_fd=fd)
        os.fsync(fd)
    except OSError:
        # Restore visible uncertainty if the final directory sync failed.
        try:
            guard = os.open(
                intent, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
            )
            os.close(guard)
        except FileExistsError:
            pass
        raise


def write(fd, target, previous, outcome, services, snapshot):
    if not SHA.fullmatch(target) or not SHA.fullmatch(previous):
        raise ValueError("invalid-target")
    old = load(fd)
    now = datetime.now(timezone.utc).isoformat()
    started = (
        old["started_at"]
        if old and old["target_sha"] == target and old["outcome"] != "activated"
        else now
    )
    service_rows = json.loads(services)
    if not isinstance(service_rows, list):
        raise ValueError("invalid-services")
    durable_write(
        fd,
        dict(
            schema="ccc.self-update.activation.v1",
            target_sha=target,
            previous_sha=previous,
            started_at=started,
            updated_at=now,
            outcome=outcome,
            services=service_rows,
            snapshot=snapshot,
        ),
    )


def clear(fd, target):
    record = load(fd)
    if record is None:
        return
    if record["target_sha"] != target:
        raise ValueError("target-mismatch")
    record.update(
        outcome="activated", snapshot="", updated_at=datetime.now(timezone.utc).isoformat()
    )
    durable_write(fd, record)


def serving(record, repo, health, now=None):
    """Require frozen startup provenance, never a live checkout HEAD probe.

    The operator's bounded command supplies the existing health JSON. This is
    source-generation reconciliation, not dependency or authentication attestation.
    Legacy short-only health/probes remain unknown until upgraded.
    """
    now = time.time() if now is None else now
    process, generation = health["process"], health["runtime_generation"]
    started, observed, updated = (
        timestamp(process["started_at"]),
        timestamp(generation["observed_at"]),
        timestamp(health["updated_at"]),
    )
    git = generation["source_git"]
    pid = process["pid"]
    if (
        health.get("schema_version") != 1
        or type(pid) is not int
        or pid <= 1
        or health.get("service", {}).get("state") != "available"
        or health.get("telegram", {}).get("state") != "healthy"
        or health.get("agent", {}).get("state") != "healthy"
        or generation.get("schema") != "ccc.runtime-generation.v1"
        or generation.get("source_dir") != str(Path(repo).resolve() / "bridge")
        or git.get("head") != record["target_sha"]
        or git.get("tracked_changes") is not False
        or generation.get("collection_errors") != []
        or not timestamp(record["started_at"]) <= started <= observed <= updated <= now
        or now - updated > 150
    ):
        raise ValueError("startup-generation-mismatch")
    os.kill(pid, 0)
    # The caller's configured health command remains the readiness gate.
    return record["target_sha"]


def probe(fd, repo, command, seconds):
    record = load(fd)
    if record is None:
        raise ValueError("no-pending-record")
    # Read at most LIMIT+1, then kill/reap output overflow; no unbounded capture.
    proc = subprocess.Popen(
        ["bash", "-c", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + min(3600, max(1, int(seconds)))
    raw = bytearray()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise ValueError("probe-timeout")
            chunk = os.read(proc.stdout.fileno(), LIMIT + 1 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > LIMIT:
                raise ValueError("probe-output-too-large")
        if proc.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            raise ValueError("probe-failed")
        return serving(record, repo, json.loads(raw))
    finally:
        # Also reap descendants that inherited stdout after the shell exited.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        selector.close()
        proc.stdout.close()


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        action, state, *values = args
        with directory(state) as fd:
            if action == "load":
                record = load(fd)
                if record is None:
                    return 1
                if record["outcome"] == "activated":
                    if values and values[0] and values[0] != record["target_sha"]:
                        raise ValueError("completed-target-mismatch")
                    return 1
                print(json.dumps(record))
            elif action == "write":
                write(fd, *values)
            elif action == "clear":
                clear(fd, *values)
            elif action == "probe":
                print(probe(fd, *values))
            elif action == "residue":
                return 0 if residue(fd) else 1
            else:
                raise ValueError("unknown-action")
        return 0
    except Exception:  # Malformed state/probe data must never become the absent-state exit code.
        print("pending-activation unsafe or persistence/probe failure", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
