#!/usr/bin/env python3
"""Mirror completed bridge extractions into their original audience's nunchi DB.

No model calls. No inference of missing audience routes. Each immutable job
gets a receipt only after ingest succeeds; growing/replaced output is retried.
Legacy unrouted journals remain the responsibility of the legacy feed.
"""

import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import stat
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("receipts", HERE / "feed-receipt.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def route(job, root):
    kind, scope = job.get("memory_audience"), job.get("memory_scope")
    if not (
        (kind == "shared" and scope == "shared")
        or (
            kind == "private"
            and isinstance(scope, str)
            and re.fullmatch(r"private-[0-9a-f]{32}", scope)
        )
    ):
        raise ValueError("unrouted")
    target = root / scope
    for p in [target, *target.parents]:
        if p.is_symlink():
            raise ValueError("unsafe_route")
    for directory in (root, target):
        meta = directory.lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_mode & 0o077:
            raise ValueError("unsafe_route")
    result = target / "nunchi"
    if result.exists() or result.is_symlink():
        meta = result.lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_mode & 0o077:
            raise ValueError("unsafe_target")
    return result


def record_failure(receipt, path, fp):
    if fp is None:
        return
    try:
        r.main(["failed", str(receipt), str(path), fp])
    except (OSError, ValueError, TypeError):
        pass


def write_status(home, status, name="journal-feed.status.json"):
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix=".journal-status-", dir=home)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(status, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, home / name)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run(home, bot, *, audience_root=None, channel=None):
    """Mirror one explicitly configured channel into its original audience root.

    Matrix may use a shared configured audience root rather than a child of
    BOT_DATA_DIR. The job's opaque route remains authoritative; neither an
    absent route nor a missing scope directory is inferred or created.
    Receipts for explicit channels bind the input AND destination so a fixed
    destination can retry previously held jobs without altering old evidence.
    """
    if channel not in (None, "telegram", "matrix"):
        raise ValueError("invalid_channel")
    root = Path(audience_root) if audience_root is not None else bot / "memory-audiences"
    if not root.is_absolute():
        raise ValueError("invalid_audience_root")
    status_name = f"{channel}-journal-feed.status.json" if channel else "journal-feed.status.json"
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = r.private_open(home / ".journal-feed.lock", os.O_RDWR | os.O_CREAT)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"skipped": "locked"}
        receipt = home / "journal-receipts.jsonl"
        if channel or audience_root is not None:
            context = hashlib.sha256(os.fsencode(str(bot) + "\0" + str(root))).hexdigest()[:16]
            receipt = home / f"journal-{channel or 'configured'}-{context}-receipts.jsonl"
        seen = failed = unrouted = budgeted = 0
        files = sorted(
            list((bot / "distill-journal").glob("*.json"))
            + list((bot / "danso-distill-journal").glob("*.json")),
            key=lambda p: p.name,
        )
        for path in files:
            if budgeted >= 20:
                break
            fp = None
            try:
                key, fp, _ = r.fingerprint(path)
                import contextlib
                import io

                with contextlib.redirect_stdout(io.StringIO()):
                    due = r.main(["due", str(receipt), str(path)])
                if due == 3:
                    continue
                if due != 0:
                    raise ValueError("receipt_failed")
                with os.fdopen(r.private_open(path, os.O_RDONLY)) as f:
                    if os.fstat(f.fileno()).st_size > 2 * 1024 * 1024:
                        raise ValueError("oversize_job")
                    job = json.load(f)
                if job.get("status") != "extraction_done":
                    continue
                target = route(job, root)
                raw = job.get("extraction_output")
                output = json.loads(raw) if isinstance(raw, str) else raw
                items = output["honcho"]
                if not isinstance(items, list) or not all(
                    isinstance(x, dict) and isinstance(x.get("text"), str) for x in items
                ):
                    raise ValueError("invalid_output")
                target.mkdir(mode=0o700, exist_ok=True)
                if target.is_symlink():
                    raise ValueError("unsafe_target")
                db = target / "facts.db"
                if db.exists() or db.is_symlink():
                    r.fingerprint(db)
                env = os.environ.copy()
                env.update(
                    NUNCHI_HOME=str(target),
                    NUNCHI_DB=str(db),
                    NUNCHI_SNAPSHOT=str(target / "snapshot.md"),
                    NUNCHI_NO_AUTO_SUPERSEDE="1",
                )
                payload = {
                    "session_id": job["thread_id"],
                    "distilled_at": (output.get("provenance") or {}).get("distilled_at")
                    or job["updated_at"],
                    "honcho": items,
                }
                p = subprocess.run(
                    ["python3", str(HERE / "nunchi.py"), "ingest", "-"],
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    env=env,
                    timeout=30,
                )
                if p.returncode:
                    raise ValueError("ingest_failed")
                subprocess.run(
                    ["python3", str(HERE / "nunchi.py"), "snapshot", "--limit", "25"],
                    env=env,
                    capture_output=True,
                    timeout=30,
                    check=True,
                )
                if r.main(["stored", str(receipt), str(path), fp]) != 0:
                    raise ValueError("changed_job")
                seen += 1
                budgeted += 1
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as e:
                if isinstance(e, ValueError) and str(e) == "unrouted":
                    unrouted += 1
                else:
                    failed += 1
                record_failure(receipt, path, fp)
                budgeted += 1
        status = {
            "schema": "ccc.nunchi.journal-feed.v1",
            "finished_at": int(time.time()),
            "mirrored_jobs": seen,
            "failed": failed,
            "unrouted": unrouted,
            "channel": channel,
            "sources": len(files),
        }
        # Held failures remain visible after their three attempts, instead of
        # making an empty successful tick look like recovery.
        latest = {}
        if receipt.exists():
            with os.fdopen(r.private_open(receipt, os.O_RDONLY)) as handle:
                if os.fstat(handle.fileno()).st_size > r.MAX_BYTES:
                    raise ValueError("receipt_capacity")
                for line in handle:
                    row = json.loads(line)
                    latest[row["key"]] = row
        status["held"] = sum(row.get("status") == "failed" and row.get("attempts", 0) >= 3
                             for row in latest.values())
        write_status(home, status, status_name)
        return status
    finally:
        os.close(lock)


if __name__ == "__main__":
    state = Path(os.environ.get("CCC_STATE_DIR", str(Path.home() / ".claude/state")))
    enabled = os.environ.get("CCC_NUNCHI_MODE")
    if enabled is None:
        try:
            enabled = (state / "nunchi.mode").read_text().strip()
        except OSError:
            enabled = "off"
    if enabled == "on":
        home = Path(os.environ.get("NUNCHI_HOME", str(Path.home() / ".nunchi")))
        bot = Path(os.environ.get("BOT_DATA_DIR", str(Path.home() / ".telegram_bot")))
        print(json.dumps(run(
            home, bot,
            audience_root=os.environ.get("CCC_NUNCHI_AUDIENCE_ROOT") or None,
            channel=os.environ.get("NUNCHI_JOURNAL_CHANNEL") or None,
        )))
