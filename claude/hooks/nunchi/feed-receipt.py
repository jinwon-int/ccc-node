#!/usr/bin/env python3
"""Fingerprint receipts for nunchi feeds. Call only inside the feed's flock.

Legacy path-only seen files remain evidence, never proof of successful storage.
On upgrade, reconsider their last seven days; older history stays recoverable
with NUNCHI_FEED_REPLAY_DAYS. Failed fingerprints retry up to three times with
backoff, then remain explicitly held until their source changes.
"""

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time

MAX_BYTES = 8 * 1024 * 1024


def private_open(path, flags):
    path = Path(path)
    for part in [path.parent, *path.parent.parents]:
        if part.is_symlink():
            raise ValueError("symlink_parent")
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
        os.close(fd)
        raise ValueError("unsafe_receipt")
    os.fchmod(fd, 0o600)
    return fd


def fingerprint(path):
    p = Path(path)
    for part in [p, *p.parents]:
        if part.is_symlink():
            raise ValueError("symlink_source")
    st = p.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
        raise ValueError("unsafe_source")
    key = hashlib.sha256(os.fsencode(str(p.absolute()))).hexdigest()
    version = os.environ.get("NUNCHI_FEED_READER_VERSION", "1")
    return key, f"{version}:{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}", st.st_mtime


def latest(path, key):
    if not Path(path).exists():
        if Path(path).is_symlink():
            raise ValueError("symlink_receipt")
        return None
    with os.fdopen(private_open(path, os.O_RDONLY), "r") as f:
        if os.fstat(f.fileno()).st_size > MAX_BYTES:
            raise ValueError("receipt_capacity")
        found = None
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                raise ValueError("invalid_receipt") from None
            if row.get("key") == key:
                found = row
        return found


def main(args):
    action, receipt, source, *rest = args
    key, current, modified = fingerprint(source)
    previous = latest(receipt, key)
    now = time.time()
    if action == "due":
        if previous and previous.get("fingerprint") == current:
            if previous.get("status") == "stored":
                return 3
            if previous.get("attempts", 0) >= 3:
                return 3
            if now - previous.get("at", 0) < 600:
                return 3
        elif not previous and rest and Path(rest[0]).is_file():
            with os.fdopen(private_open(rest[0], os.O_RDONLY)) as f:
                if os.fstat(f.fileno()).st_size > MAX_BYTES:
                    raise ValueError("legacy_capacity")
                legacy = source in f.read().splitlines()
            days = max(0, min(3650, int(os.environ.get("NUNCHI_FEED_REPLAY_DAYS", "7"))))
            if legacy and modified < now - days * 86400:
                return 3
        # Token binds the eventual receipt to the pre-extraction snapshot.
        print(current)
        return 0
    if action not in ("stored", "failed") or not rest:
        raise ValueError("arguments")
    expected = rest[0]
    if expected != current:
        return 4  # source changed while extracting; retry it
    attempts = (
        previous.get("attempts", 0) if previous and previous.get("fingerprint") == current else 0
    ) + 1
    row = dict(key=key, fingerprint=current, status=action, attempts=attempts, at=now)
    fd = private_open(receipt, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    with os.fdopen(fd, "a") as f:
        if os.fstat(f.fileno()).st_size > MAX_BYTES:
            raise ValueError("receipt_capacity")
        f.write(json.dumps(row, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, ValueError, TypeError, IndexError):
        print("nunchi-feed: receipt/source unsafe or unavailable", file=sys.stderr)
        raise SystemExit(2)
