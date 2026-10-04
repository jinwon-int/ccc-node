"""Body-free review scheduling and a node-wide, channel-shared call ceiling."""

import fcntl
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccc_secure_fs


def fingerprint(conn, item, siblings):
    # Include reasons/rank/evidence, not merely text. The set of live siblings
    # changing must reopen a hold even when the queued row itself is unchanged.
    ids = sorted({item[0], *(s[0] for s in siblings)})
    rows = [conn.execute(
        "SELECT id,observed,kind,fact,because,source_rank,evidence,valid_from,valid_to "
        "FROM peer_facts WHERE id=?", (fid,)
    ).fetchone() for fid in ids]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def load(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='nunchi_review_state'").fetchone():
        return {}
    return {row[0]: row[1:] for row in conn.execute(
        "SELECT fact_id,fingerprint,disposition,last_attempt,next_attempt,attempts "
        "FROM nunchi_review_state"
    )}


def persist(conn, decisions, fingerprints, previous, stamp):
    conn.execute("""CREATE TABLE IF NOT EXISTS nunchi_review_state (
        fact_id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL,
        disposition TEXT NOT NULL, last_attempt REAL NOT NULL,
        next_attempt REAL NOT NULL, attempts INTEGER NOT NULL)""")
    for decision in decisions:
        fid = decision["id"]
        prior = previous.get(fid)
        attempts = prior[4] + 1 if prior and prior[0] == fingerprints[fid] else 1
        if decision.get("applied"):
            disposition, delay = "resolved", 0
        elif decision.get("class") == "skipped-stale":
            disposition, delay = "retry", 900
        elif decision.get("class") == "judge-unavailable" or (
            decision.get("class") == "judge" and decision.get("backend") is None
        ):
            disposition = "retry"
            delay = min(86400, 900 * 2 ** min(attempts - 1, 7))
        else:
            disposition, delay = "human", 7 * 86400
        conn.execute("INSERT OR REPLACE INTO nunchi_review_state VALUES(?,?,?,?,?,?)",
                     (fid, fingerprints[fid], disposition, stamp, stamp + delay, attempts))
    conn.commit()


def call_budget(state_dir, limit, *, charge=True, moment=None):
    """Reserve one actual backend invocation, including each fallback attempt.

    Fail closed on unsafe/corrupt state. A crash after charging costs one slot.
    CCC_STATE_DIR is shared by all audiences and both channel crons on a node.
    """
    root = Path(state_dir) / "nunchi-judge"
    ccc_secure_fs.ensure_private_directory(root)
    path = root / "calls.json"
    fd = ccc_secure_fs.open_lock_descriptor(root / ".calls.lock", unsafe_mode_mask=0o077)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        day = (moment or datetime.now(timezone.utc)).astimezone(
            timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
        try:
            payload, _ = ccc_secure_fs.read_owner_only_bytes(path, max_bytes=1024 * 1024,
                                                          unsafe_mode_mask=0o077)
            days = json.loads(payload)
            if not isinstance(days, dict) or any(
                not isinstance(k, str) or type(v) is not int or not 0 <= v <= 10**9
                for k, v in days.items()
            ):
                raise ValueError("invalid judge call counters")
        except FileNotFoundError:
            days = {}
        used = days.get(day, 0)
        allowed = used < limit
        if charge and allowed:
            days[day] = used + 1
            # Retain historical counters; never reset a day's spend.
            ccc_secure_fs.atomic_write_text(path, json.dumps(days), mode=0o600)
            used += 1
        return allowed, max(0, limit - used)
    finally:
        os.close(fd)


def status(home, payload):
    root = Path(home)
    ccc_secure_fs.ensure_private_directory(root)
    path = root / "judge.status.json"
    if path.is_symlink():
        raise ValueError("unsafe judge status path")
    ccc_secure_fs.atomic_write_text(path, json.dumps(payload, sort_keys=True), mode=0o600)
