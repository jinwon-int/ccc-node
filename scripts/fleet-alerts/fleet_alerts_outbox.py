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
import json
import os
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

MAX_BODY_BYTES = 16_000
DEFAULT_DEDUP_WINDOW_S = 900.0
# Contract v2 (#2413): every alert carries a short id the owner can quote back
# (``ok <id>`` / ``hold <id>``) and a tier; each channel records a delivery
# receipt. A "strong" receipt is a channel acknowledgement that the message is
# in the room; "socket-write" (the hande lane's hand-off to the relay bot) is
# weak — the bot has no notice ack. ``notified`` means at least one strong one.
TIERS = ("info", "action", "critical")
# Derived rows the policy module creates (never accepted from a node payload —
# the receiver normalises payload tiers with the closed TIERS set).
INTERNAL_TIERS = ("digest", "renotify")
DEFAULT_TIER = "info"
ALERT_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
ALERT_ID_LEN = 6
STRONG_RECEIPT_KINDS = ("matrix-event", "hande-ack")


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
        _migrate_v2(conn)
        conn.commit()
        yield conn
        conn.commit()
    finally:
        if conn is not None:
            conn.close()
        if lock is not None:
            os.close(lock)
        os.close(fd)


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Add the v2 columns/tables in place (idempotent; rows from before keep alert_id NULL)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
    if "alert_id" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN alert_id TEXT")
    if "tier" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN tier TEXT NOT NULL DEFAULT 'info'")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS alerts_alert_id ON alerts(alert_id)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS receipts (
        txn TEXT NOT NULL, channel TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '',
        at REAL NOT NULL, PRIMARY KEY (txn, channel))"""
    )


def normalize_tier(value: object) -> str:
    v = str(value or "").strip().lower()
    return v if v in TIERS else DEFAULT_TIER


def _alert_id(conn: sqlite3.Connection, txn: str) -> str:
    """6 base36 chars derived from the txn; re-hashed on the (rare) collision."""
    seed = txn
    for _ in range(16):
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        n = int.from_bytes(digest[:8], "big")
        out = ""
        for _ in range(ALERT_ID_LEN):
            out += ALERT_ID_ALPHABET[n % 36]
            n //= 36
        if conn.execute("SELECT 1 FROM alerts WHERE alert_id=?", (out,)).fetchone() is None:
            return out
        seed = digest.hex()
    raise RuntimeError("alert id space exhausted")


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
    tier: str = DEFAULT_TIER,
    body_with_id: Optional[Callable[[str], str]] = None,
) -> tuple[str, bool]:
    """Queue one alert. Returns ``(txn, duplicate)``.

    A record with the same fingerprint inside ``window`` seconds returns the
    existing txn with ``duplicate=True`` — this is what makes a node's retry
    after a lost 2xx idempotent.

    v2: the row gets a 6-char ``alert_id`` and a ``tier``. ``body_with_id``
    (id → final text) lets the caller put ``#<id>`` into the stored text; it
    runs inside the queue transaction, so the id is already unique. ``body``
    is still validated first (size, non-empty).
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
        alert_id = _alert_id(conn, txn)
        final = body_with_id(alert_id) if body_with_id is not None else body
        if not isinstance(final, str) or not final.strip() or len(final.encode("utf-8")) > MAX_BODY_BYTES + 64:
            raise ValueError("invalid alert text")
        conn.execute(
            "INSERT INTO alerts(txn,body,created,fingerprint,node,event,alert_id,tier) VALUES (?,?,?,?,?,?,?,?)",
            (txn, final, now, fingerprint, node[:64], event[:64], alert_id,
             tier if tier in INTERNAL_TIERS else normalize_tier(tier)),
        )
    return txn, False


def alert_id_of(root: os.PathLike | str, txn: str) -> Optional[str]:
    with database(root) as conn:
        row = conn.execute("SELECT alert_id FROM alerts WHERE txn=?", (txn,)).fetchone()
    return row[0] if row else None


def receipt(root: os.PathLike | str, txn: str, channel: str, kind: str, ref: str = "") -> None:
    """Record one channel's delivery receipt for ``txn`` (one per channel; later writes win)."""
    for v, name in ((channel, "channel"), (kind, "kind")):
        if not isinstance(v, str) or not v or len(v) > 32:
            raise ValueError(f"invalid receipt {name}")
    with database(root) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO receipts(txn,channel,kind,ref,at) VALUES (?,?,?,?,?)",
            (txn, channel, kind, str(ref or "")[:256], time.time()),
        )


def lookup(root: os.PathLike | str, key: str) -> Optional[dict]:
    """Receipt view of one alert by ``alert_id`` or ``txn`` (metadata only — never the body)."""
    with database(root) as conn:
        row = conn.execute(
            "SELECT txn,alert_id,tier,node,event,created,delivered,event_id,attempts FROM alerts"
            " WHERE alert_id=? OR txn=?", (key, key),
        ).fetchone()
        if row is None:
            return None
        rec = conn.execute(
            "SELECT channel,kind,ref,at FROM receipts WHERE txn=? ORDER BY at", (row[0],)
        ).fetchall()
    receipts = [{"channel": c, "kind": k, "ref": r, "at": a} for c, k, r, a in rec]
    return {
        "txn": row[0], "alert_id": row[1], "tier": row[2], "node": row[3], "event": row[4],
        "created": row[5], "matrix_delivered": row[6], "attempts": row[8],
        "receipts": receipts,
        "notified": any(r["kind"] in STRONG_RECEIPT_KINDS for r in receipts),
    }


def render_lookup(info: Optional[dict]) -> str:
    return json.dumps(info, ensure_ascii=False, sort_keys=True)


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
        now = time.time()
        conn.execute(
            "UPDATE alerts SET delivered=?,event_id=?,body=? WHERE txn=? AND delivered IS NULL",
            (now, event_id, "", txn),
        )
        # v2 receipt (#2413): the Matrix event id is a strong acknowledgement.
        conn.execute(
            "INSERT OR REPLACE INTO receipts(txn,channel,kind,ref,at) VALUES (?,?,?,?,?)",
            (txn, "matrix", "matrix-event", event_id[:256], now),
        )


def prune(root: os.PathLike | str, *, keep_seconds: float = 30 * 86400) -> int:
    """Drop delivered rows older than ``keep_seconds``; returns the number removed."""
    with database(root) as conn:
        cutoff = time.time() - keep_seconds
        conn.execute(
            "DELETE FROM receipts WHERE txn IN (SELECT txn FROM alerts WHERE delivered IS NOT NULL AND delivered < ?)",
            (cutoff,),
        )
        cur = conn.execute("DELETE FROM alerts WHERE delivered IS NOT NULL AND delivered < ?", (cutoff,))
        return int(cur.rowcount or 0)
