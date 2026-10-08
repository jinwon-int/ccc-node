"""Owner-only durable queue shared by the fleet alert receiver and the Matrix sender (#2182).

Same shape as the card-alert outbox that has run on the relay host since
2026-09-18: one SQLite file in a 0700 directory, opened through a directory
fd with symlink/hardlink/ownership checks, an exclusive flock around every
transaction, and a stable per-alert transaction id so a timeout or restart
never produces a duplicate Matrix event. Alert bodies are wiped on delivery
(metadata stays for audit) so the queue is not a second indefinite archive.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

MAX_BODY_BYTES = 16_000
DEFAULT_DEDUP_WINDOW_S = 900.0


def _file(fd: int, name: str) -> int:
    result = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
    try:
        info = os.fstat(result)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("unsafe queue file")
        os.fchmod(result, 0o600)
        return result
    except BaseException:
        os.close(result)
        raise


def _directory(path: os.PathLike | str) -> int:
    p = Path(path)
    if not p.is_absolute() or ".." in p.parts:
        raise ValueError("absolute private queue directory required")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = p.parts[1:]
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("queue directory must be owned and mode 0700")
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def database(root: os.PathLike | str) -> Iterator[sqlite3.Connection]:
    fd = _directory(root)
    lock = None
    conn = None
    try:
        lock = _file(fd, "queue.lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        dbfd = _file(fd, "queue.sqlite3")
        os.close(dbfd)
        for name in ("queue.sqlite3-journal", "queue.sqlite3-wal", "queue.sqlite3-shm"):
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError("unsafe queue sidecar")
        conn = sqlite3.connect(f"/proc/self/fd/{fd}/queue.sqlite3", timeout=5)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS alerts (
            txn TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL, fingerprint TEXT,
            node TEXT NOT NULL DEFAULT '', event TEXT NOT NULL DEFAULT '',
            delivered REAL, event_id TEXT, attempts INTEGER NOT NULL DEFAULT 0)"""
        )
        conn.commit()
        yield conn
        conn.commit()
    finally:
        if conn is not None:
            conn.close()
        if lock is not None:
            os.close(lock)
        os.close(fd)


def fingerprint(node: str, key: str) -> str:
    """Stable dedup fingerprint for one node's record/dedup key."""
    return hashlib.sha256(f"{node}\n{key}".encode("utf-8")).hexdigest()


def enqueue(
    root: os.PathLike | str,
    body: str,
    *,
    node: str = "",
    event: str = "",
    fingerprint: Optional[str] = None,
    window: float = DEFAULT_DEDUP_WINDOW_S,
) -> tuple[str, bool]:
    """Queue one alert. Returns ``(txn, duplicate)``.

    A record with the same fingerprint inside ``window`` seconds returns the
    existing txn with ``duplicate=True`` — this is what makes a node's retry
    after a lost 2xx idempotent.
    """
    if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError("invalid alert text")
    if fingerprint is not None and (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(c not in "0123456789abcdef" for c in fingerprint)
    ):
        raise ValueError("invalid dedup fingerprint")
    txn = "fleet-" + uuid.uuid4().hex
    with database(root) as conn:
        now = time.time()
        if fingerprint:
            existing = conn.execute(
                "SELECT txn FROM alerts WHERE fingerprint=? AND created>=? ORDER BY created DESC LIMIT 1",
                (fingerprint, now - window),
            ).fetchone()
            if existing:
                return existing[0], True
        conn.execute(
            "INSERT INTO alerts(txn,body,created,fingerprint,node,event) VALUES (?,?,?,?,?,?)",
            (txn, body, now, fingerprint, node[:64], event[:64]),
        )
    return txn, False


def pending(root: os.PathLike | str) -> Optional[tuple[str, str]]:
    with database(root) as conn:
        return conn.execute(
            "SELECT txn,body FROM alerts WHERE delivered IS NULL ORDER BY created LIMIT 1"
        ).fetchone()


def pending_count(root: os.PathLike | str) -> int:
    with database(root) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM alerts WHERE delivered IS NULL").fetchone()[0])


def attempted(root: os.PathLike | str, txn: str) -> None:
    with database(root) as conn:
        conn.execute("UPDATE alerts SET attempts=attempts+1 WHERE txn=? AND delivered IS NULL", (txn,))


def delivered(root: os.PathLike | str, txn: str, event_id: str) -> None:
    if not isinstance(event_id, str) or not event_id.startswith("$"):
        raise ValueError("Matrix event acknowledgement required")
    with database(root) as conn:
        # Keep delivery metadata for audit, not a second indefinite alert-text archive.
        conn.execute(
            "UPDATE alerts SET delivered=?,event_id=?,body=? WHERE txn=? AND delivered IS NULL",
            (time.time(), event_id, "", txn),
        )


def prune(root: os.PathLike | str, *, keep_seconds: float = 30 * 86400) -> int:
    """Drop delivered rows older than ``keep_seconds``; returns the number removed."""
    with database(root) as conn:
        cur = conn.execute(
            "DELETE FROM alerts WHERE delivered IS NOT NULL AND delivered < ?", (time.time() - keep_seconds,)
        )
        return int(cur.rowcount or 0)
