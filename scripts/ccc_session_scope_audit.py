#!/usr/bin/env python3
"""Find and clear group session rows that resume a DM-derived session (#2075).

The bridge's ``sessions.json`` keys one row per conversation
(``bridge/core/session_scope.py``)::

    telegram_session:<uid>          the sender's DM row (also the pre-scope,
                                    unscoped legacy row of that user)
    telegram_session:<uid>:<chat>   per-user-chat group/room row
    telegram_session:0:<chat>       shared-groups room row
    telegram_session:0:0            shared-all row (operator opt-in)

Before #2075 a group row could end up holding a DM session id: the first-use
seed ``_seed_scoped_session_from_legacy`` copies the legacy ``<uid>`` row into
a new scoped row, and the pre-#2092 external-wait/continuation runners resumed
the DM session under the group's provider stream, which the next group turn
then saved into the group row. #2092 fixed the lookups; existing rows were out
of scope, so such a room keeps resuming the DM-derived session. The #2074
audience sidecar only marks those sessions ``ambiguous`` for memory
collection; the rows stay.

Detection (store-only, scope-independent): a group/room row is contaminated
when its non-empty ``session_id`` is also held by a row of a *different*
conversation surface — a DM/legacy ``<uid>`` row (``dm-session``) or another
room (``cross-room-session``). The surface of ``<uid>`` is the DM chat
``<uid>``; the surface of ``<a>:<chat>`` is ``<chat>``. Rows of the same room
under two scope keys (``7:-100`` and ``0:-100``) share one surface and are not
flagged. The ``0:0`` shared-all row shares DM and room context by explicit
operator design and is ignored on both sides.

Remediation (``--apply``): the flagged room row gets exactly what ``/new``
persists — ``session_id: null`` and ``new_session: true`` — so the next room
turn starts a fresh session. Every other field (model, effort, reply mode, …)
is kept, and the row is never deleted: an empty row would let the first-use
seed copy the DM row into it again. DM rows are never modified.

Guards: ``--apply`` refuses while the bridge owning the store is running (the
store is process-local; a live bridge would overwrite the edit and keep the
old id in memory) and while a pending external-wait or continuation record in
a flagged room is still bound to a flagged session id (its runner falls back
to the registered id when the row is empty). Before writing, the exact bytes
that were audited are copied to ``sessions.json.bak-2075-<utc>`` (0600); the
write is atomic and also refreshes ``sessions.json.bak`` with the previous
primary, as ``SessionStore`` does. A second run finds nothing and writes
nothing.

Output is body-free: counts and row keys only — never session ids, model
settings or any message-derived field.

Usage::

    python3 scripts/ccc_session_scope_audit.py [--store PATH ...]          # dry-run
    python3 scripts/ccc_session_scope_audit.py [--store PATH ...] --apply  # bridge stopped

Default stores: ``$BOT_DATA_DIR/sessions.json`` (when set), then
``~/.telegram_bot/sessions.json`` and ``~/.ccc-matrix/sessions.json`` when
present. Exit: 0 clean or applied · 1 flagged rows found (dry-run) ·
2 usage/unreadable store · 3 apply refused.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ccc_secure_fs as _secure_fs  # noqa: E402

KEY_PREFIX = "telegram_session:"
SHARED_ALL_SUFFIX = "0:0"
REASON_DM = "dm-session"
REASON_CROSS_ROOM = "cross-room-session"
BACKUP_INFIX = ".bak-2075-"
MAX_STORE_BYTES = 64 * 1024 * 1024
_LIVE_WAIT_STATE = "monitoring"
_LIVE_CONTINUATION_STATES = frozenset({"pending", "running", "cap-hold"})

RC_OK = 0
RC_FLAGGED = 1
RC_ERROR = 2
RC_REFUSED = 3


class AuditError(RuntimeError):
    """The store cannot be audited safely (body-free reason)."""


@dataclass(frozen=True)
class FlaggedRow:
    key: str
    reason: str


@dataclass
class StoreAudit:
    path: Path
    rows: int = 0
    scoped_rows: int = 0
    flagged: list[FlaggedRow] = field(default_factory=list)
    flagged_session_ids: frozenset[str] = frozenset()
    pending_runner_records: int | None = 0
    bridge_running: bool = False
    payload: bytes = b""
    signature: tuple[int, int, int, int] | None = None
    data: dict[str, Any] = field(default_factory=dict)


def _suffix_parts(key: str) -> tuple[int, ...]:
    if not key.startswith(KEY_PREFIX):
        raise AuditError("unexpected-key")
    try:
        parts = tuple(int(part) for part in key[len(KEY_PREFIX):].split(":"))
    except ValueError as error:
        raise AuditError("unexpected-key") from error
    if len(parts) not in {1, 2}:
        raise AuditError("unexpected-key")
    return parts


def surface_of(parts: tuple[int, ...]) -> int | None:
    """Conversation surface (chat id) of one row; ``None`` for shared-all."""

    if len(parts) == 1:
        return parts[0]
    if parts == (0, 0):
        return None
    return parts[1]


def is_room_row(parts: tuple[int, ...]) -> bool:
    return len(parts) == 2 and parts != (0, 0) and parts[0] != parts[1]


def find_contaminated(data: Mapping[str, Any]) -> tuple[list[FlaggedRow], frozenset[str]]:
    """Room rows whose session id is also held by another surface's row."""

    holders: dict[str, list[tuple[str, tuple[int, ...]]]] = defaultdict(list)
    for key, row in data.items():
        parts = _suffix_parts(key)
        if not isinstance(row, dict):
            raise AuditError("unexpected-row")
        session_id = row.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        if surface_of(parts) is None:
            continue
        holders[session_id].append((key, parts))
    flagged: list[FlaggedRow] = []
    session_ids: set[str] = set()
    for session_id, rows in holders.items():
        if len({surface_of(parts) for _key, parts in rows}) < 2:
            continue
        reason = REASON_DM if any(len(parts) == 1 for _key, parts in rows) else REASON_CROSS_ROOM
        for key, parts in rows:
            if is_room_row(parts):
                flagged.append(FlaggedRow(key[len(KEY_PREFIX):], reason))
                session_ids.add(session_id)
    flagged.sort(key=lambda row: row.key)
    return flagged, frozenset(session_ids)


def remediated(data: Mapping[str, Any], flagged: Iterable[FlaggedRow]) -> dict[str, Any]:
    """Copy of ``data`` with each flagged room row reset the way ``/new`` does."""

    result = {key: dict(value) for key, value in data.items()}
    for row in flagged:
        entry = result[KEY_PREFIX + row.key]
        entry["session_id"] = None
        entry["new_session"] = True
    return result


def _read_json_file(path: Path) -> Any:
    payload, _stat = _secure_fs.read_owner_only_bytes(path, max_bytes=MAX_STORE_BYTES)
    return json.loads(payload.decode("utf-8"))


def _live_wait_ids(path: Path) -> list[tuple[Any, Any]]:
    records = _read_json_file(path)
    if not isinstance(records, dict):
        raise AuditError("unexpected-wait-registry")
    live = []
    for record in records.values():
        if not isinstance(record, dict):
            continue
        wake = record.get("wake")
        pending_wake = isinstance(wake, dict) and wake.get("state") == "pending"
        if record.get("state") == _LIVE_WAIT_STATE or pending_wake:
            live.append((record.get("session_id"), record.get("chat_id")))
    return live


def _live_continuation_ids(path: Path) -> list[tuple[Any, Any]]:
    data = _read_json_file(path)
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, dict):
        raise AuditError("unexpected-continuation-queue")
    return [
        (record.get("session_id"), record.get("chat_id"))
        for record in records.values()
        if isinstance(record, dict) and record.get("state") in _LIVE_CONTINUATION_STATES
    ]


def pending_runner_records(
    data_dir: Path, session_ids: frozenset[str], room_chats: frozenset[int]
) -> int | None:
    """Live room wait/continuation records bound to a flagged id; ``None`` if unreadable.

    A runner whose room row was cleared falls back to the id registered on the
    record, so such a record would resume the DM-derived session once more.
    DM records are unaffected: their runner reads the DM row.
    """

    if not session_ids:
        return 0
    sources = (
        (data_dir / "external-wait" / "waits.json", _live_wait_ids),
        (data_dir / "continuation" / "queue.json", _live_continuation_ids),
    )
    count = 0
    for path, reader in sources:
        if not path.exists():
            continue
        try:
            count += sum(
                1 for sid, chat in reader(path) if sid in session_ids and chat in room_chats
            )
        except Exception:
            return None
    return count


def bridge_running(data_dir: Path) -> bool:
    """True when ``<data dir>/bot.pid`` names a live bridge process."""

    try:
        pid = int((data_dir / "bot.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True  # no /proc: a live pid is treated as the bridge (fail closed)
    return b"telegram_bot" in cmdline


def _signature(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    return (stat_result.st_dev, stat_result.st_ino, stat_result.st_size, stat_result.st_mtime_ns)


def audit_store(path: Path) -> StoreAudit:
    """Read-only audit of one ``sessions.json``. Raises ``AuditError``."""

    try:
        payload, stat_result = _secure_fs.read_owner_only_bytes(path, max_bytes=MAX_STORE_BYTES)
    except FileNotFoundError as error:
        raise AuditError("missing") from error
    except Exception as error:
        raise AuditError("unreadable") from error
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuditError("not-json") from error
    if not isinstance(data, dict):
        raise AuditError("unexpected-root")
    flagged, session_ids = find_contaminated(data)
    data_dir = path.parent
    return StoreAudit(
        path=path,
        rows=len(data),
        scoped_rows=sum(1 for key in data if is_room_row(_suffix_parts(key))),
        flagged=flagged,
        flagged_session_ids=session_ids,
        pending_runner_records=pending_runner_records(
            data_dir, session_ids, frozenset(_suffix_parts(KEY_PREFIX + row.key)[1] for row in flagged)
        ),
        bridge_running=bridge_running(data_dir),
        payload=payload,
        signature=_signature(stat_result),
        data=data,
    )


def _write_backup(path: Path, payload: bytes, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    for attempt in range(100):
        suffix = "" if attempt == 0 else f"-{attempt}"
        backup = path.with_name(f"{path.name}{BACKUP_INFIX}{stamp}{suffix}")
        try:
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(descriptor, view):]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return backup
    raise AuditError("backup-name-exhausted")


def apply_audit(audit: StoreAudit, *, now: datetime | None = None) -> Path | None:
    """Back up, then clear every flagged room row. Returns the backup path.

    Refuses (``AuditError``) while the bridge runs, while a pending runner
    record still points at a flagged id, or when the store changed after the
    audit read it. ``None`` when there is nothing to do (idempotent).
    """

    if not audit.flagged:
        return None
    if audit.bridge_running:
        raise AuditError("bridge-running")
    if audit.pending_runner_records is None:
        raise AuditError("runner-state-unreadable")
    if audit.pending_runner_records:
        raise AuditError("pending-runner-records")
    if _signature(os.lstat(audit.path)) != audit.signature:
        raise AuditError("store-changed")
    backup = _write_backup(audit.path, audit.payload, now or datetime.now(timezone.utc))
    if _signature(os.lstat(audit.path)) != audit.signature:
        raise AuditError("store-changed")
    new_payload = (
        json.dumps(remediated(audit.data, audit.flagged), ensure_ascii=False, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    _secure_fs.atomic_write_bytes(audit.path.with_name(audit.path.name + ".bak"), audit.payload, mode=0o600)
    _secure_fs.atomic_write_bytes(audit.path, new_payload, mode=0o600)
    return backup


def default_stores(environ: Mapping[str, str] | None = None) -> list[Path]:
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME") or Path.home()).expanduser()
    candidates = []
    if env.get("BOT_DATA_DIR"):
        candidates.append(Path(env["BOT_DATA_DIR"]).expanduser() / "sessions.json")
    candidates += [home / ".telegram_bot" / "sessions.json", home / ".ccc-matrix" / "sessions.json"]
    seen: list[Path] = []
    for candidate in candidates:
        if candidate.exists() and candidate not in seen:
            seen.append(candidate)
    return seen


def _summary_line(audit: StoreAudit) -> str:
    pending = "unknown" if audit.pending_runner_records is None else audit.pending_runner_records
    return (
        f"store={audit.path} rows={audit.rows} room_rows={audit.scoped_rows} "
        f"flagged={len(audit.flagged)} pending_runner_records={pending} "
        f"bridge={'running' if audit.bridge_running else 'stopped'}"
    )


def _run_store(path: Path, apply: bool, out: Any) -> int:
    try:
        audit = audit_store(path)
    except AuditError as error:
        print(f"store={path} error={error}", file=out)
        return RC_ERROR
    print(_summary_line(audit), file=out)
    for row in audit.flagged:
        print(f"  flagged key={row.key} reason={row.reason}", file=out)
    if not audit.flagged:
        return RC_OK
    if not apply:
        return RC_FLAGGED
    try:
        backup = apply_audit(audit)
    except AuditError as error:
        print(f"  apply refused: {error}", file=out)
        return RC_REFUSED
    print(f"  applied cleared={len(audit.flagged)} backup={backup}", file=out)
    try:
        after = audit_store(path)
    except AuditError as error:
        print(f"  post-apply audit failed: {error}", file=out)
        return RC_ERROR
    if after.flagged:
        print(f"  apply incomplete: flagged={len(after.flagged)}", file=out)
        return RC_ERROR
    return RC_OK


def main(argv: Sequence[str] | None = None, out: Any = None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(
        description="Audit (dry-run) or clear group session rows holding a DM-derived "
        "session id (#2075). Body-free output: counts and row keys only."
    )
    parser.add_argument("--store", action="append", type=Path, help="sessions.json path (repeatable)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="back up, then clear flagged room rows (bridge must be stopped)",
    )
    args = parser.parse_args(argv)
    stores = args.store or default_stores()
    if not stores:
        print("no session store found (pass --store PATH)", file=out)
        return RC_ERROR
    worst = RC_OK
    for path in stores:
        worst = max(worst, _run_store(path, args.apply, out))
    mode = "apply" if args.apply else "dry-run"
    print(f"mode={mode} stores={len(stores)} exit={worst}", file=out)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
