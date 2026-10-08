#!/usr/bin/env python3
"""Expire stale autosave drafts out of the pending queue (#2184).

The human gate (`/skillsuggest`, #2011-B) keeps every draft until someone
reviews it, so `pending-skills/` only grows: 2026-10-08 fleet count 603
drafts, 211 older than 30 days, ~34 new per day. On 2026-10-07 the owner
approved a one-off move of the 19 drafts older than 90 days into
`skill-autosave-archive/pending-90d-20261007/` with a `manifest.jsonl`
(owner decision 4c). This module makes that sweep repeatable:

- `run`      move drafts older than CCC_SKILL_PENDING_EXPIRE_DAYS (default
             90; `0` turns the step off) into
             `<state>/skill-autosave-archive/pending-90d-<YYYYMMDD>/`, one
             manifest row per move. Nothing is deleted; `--dry-run` only
             reports.
- `restore`  move one archived draft back into the queue by name.

Scope is the *undecided* queue only. Directories carrying a decision suffix
(`.approved-/.rejected-/.installed-<stamp>`), a `meta.approved.json`
(human-approved, awaiting install) or a `proposal.json` (incremental
proposal, #1460 lifecycle) are never touched — they are counted under
`skipped` so the operator can see them. Installed skills are the curator's
business (#1739, mark-only); this module never looks at `skills/`.

Age comes from `meta.json:staged_at` (ISO-8601), falling back to the
`YYYYMMDD-HHMMSS-` prefix of the directory name, then to the directory
mtime. A draft whose age cannot be determined is skipped, never expired.

Moves are `os.rename` within one filesystem (the archive root lives under
the same state dir), so a draft is either in the queue or in the archive —
never half-copied. The manifest row is appended after the rename succeeds;
a rename that fails leaves the draft in place and is reported.

Output: one JSON object on stdout, exit 0 on success, 2 on usage error,
1 when the archive root cannot be prepared.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ARCHIVE_DIR = "skill-autosave-archive"
ARCHIVE_PREFIX = "pending-90d-"
MANIFEST = "manifest.jsonl"
DEFAULT_DAYS = 90
MAX_DAYS = 3650
MAX_MOVES_PER_RUN = 200  # bounded work per sweep; the rest goes next night

_DECIDED_RE = re.compile(r"\.(approved|rejected|installed)-[0-9]+$")
_STAMP_RE = re.compile(r"^(\d{8})-(\d{6})-")
# Archive dirs this module may restore from: its own expiry dirs and the
# pre-screen reject dirs (#2183) — same root, same manifest shape.
_ARCHIVE_DIR_RE = re.compile(r"^(pending-90d|prescreen-reject)-\d{8}$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


def _now_iso(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def _state_dir() -> Path:
    for key in ("CCC_SKILL_REVIEW_STATE_DIR", "CCC_STATE_DIR"):
        value = os.environ.get(key)
        if value:
            return Path(value).expanduser()
    claude = os.environ.get("CCC_CLAUDE_DIR") or os.path.join(os.environ.get("HOME", "/root"), ".claude")
    return Path(claude).expanduser() / "state"


def expire_days() -> int | None:
    """None means the step is off (explicit 0); malformed values fall back to 90."""
    raw = os.environ.get("CCC_SKILL_PENDING_EXPIRE_DAYS", "")
    if raw == "":
        return DEFAULT_DAYS
    if not raw.isdigit():
        return DEFAULT_DAYS
    value = int(raw)
    if value == 0:
        return None
    return min(value, MAX_DAYS)


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def staged_at(entry: Path) -> float | None:
    """When the draft entered the queue: meta.json, then the name stamp, then mtime."""
    meta = entry / "meta.json"
    try:
        if meta.is_file() and not meta.is_symlink() and meta.stat().st_size <= 65536:
            data = json.loads(meta.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                ts = _parse_iso(data.get("staged_at"))
                if ts is not None:
                    return ts
    except (OSError, ValueError):
        pass
    match = _STAMP_RE.match(entry.name)
    if match:
        try:
            parsed = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
            return parsed.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    try:
        return entry.lstat().st_mtime
    except OSError:
        return None


def classify(entry: Path) -> str | None:
    """Reason to skip this queue entry, or None when it is an undecided draft."""
    if entry.is_symlink() or not entry.is_dir():
        return "not-a-directory"
    if not _SAFE_NAME_RE.match(entry.name):
        return "unsafe-name"
    if _DECIDED_RE.search(entry.name):
        return "decided"
    if (entry / "meta.approved.json").exists():
        return "approved-awaiting-install"
    if (entry / "proposal.json").exists():
        return "incremental-proposal"
    return None


def _append_manifest(archive_dir: Path, row: dict[str, Any]) -> None:
    path = archive_dir / MANIFEST
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def prepare_archive(state: Path, now: float, *, prefix: str = ARCHIVE_PREFIX) -> Path:
    """Create (0700) and return `<state>/skill-autosave-archive/<prefix><YYYYMMDD>/`.

    Shared with prescreen.py (#2183) so every automatic move out of the queue
    lands under one root with one manifest shape and one restore path.
    """
    root = state / ARCHIVE_DIR
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink():
        raise OSError(f"archive root is a symlink: {root}")
    os.chmod(root, 0o700)
    target = root / (prefix + time.strftime("%Y%m%d", time.gmtime(now)))
    target.mkdir(mode=0o700, exist_ok=True)
    if target.is_symlink():
        raise OSError(f"archive dir is a symlink: {target}")
    if os.stat(root).st_dev != os.stat(state).st_dev:
        raise OSError("archive root is not on the state filesystem")
    return target


def _scan(pending: Path, cutoff: float, result: dict[str, Any]) -> tuple[list[tuple[float, Path]], float | None]:
    """Sort the queue into expirable drafts and the oldest draft that stays."""
    eligible: list[tuple[float, Path]] = []
    oldest_remaining: float | None = None
    for entry in sorted(pending.iterdir(), key=lambda p: p.name):
        result["scanned"] += 1
        reason = classify(entry)
        ts = None if reason else staged_at(entry)
        if reason is None and ts is None:
            reason = "unknown-age"
        if reason is not None:
            result["skipped"][reason] = result["skipped"].get(reason, 0) + 1
            continue
        if ts <= cutoff:
            eligible.append((ts, entry))
        elif oldest_remaining is None or ts < oldest_remaining:
            oldest_remaining = ts
    eligible.sort(key=lambda item: (item[0], item[1].name))
    return eligible, oldest_remaining


def _move_eligible(
    eligible: list[tuple[float, Path]], archive_dir: Path, pending: Path, result: dict[str, Any],
    *, now: float, days: int, node: str,
) -> None:
    for ts, entry in eligible:
        dest = archive_dir / entry.name
        if dest.exists() or dest.is_symlink():
            result["failed"].append({"name": entry.name, "code": "archive-name-taken"})
            continue
        try:
            os.rename(entry, dest)
        except OSError as error:
            result["failed"].append({"name": entry.name, "code": f"rename-failed:{error.errno}"})
            continue
        _append_manifest(
            archive_dir,
            {
                "ts": _now_iso(now),
                "node": node,
                "from": str(pending),
                "name": entry.name,
                "staged_at": _now_iso(ts),
                "age_days": int((now - ts) // 86400),
                "reason": f"pending draft older than {days}d (automatic expiry, #2184)",
            },
        )
        result["moved"].append(entry.name)


def run(state: Path, *, dry_run: bool, now: float | None = None, node: str = "") -> dict[str, Any]:
    now = time.time() if now is None else now
    days = expire_days()
    pending = state / "pending-skills"
    result: dict[str, Any] = {
        "ts": _now_iso(now),
        "node": node,
        "pending_dir": str(pending),
        "expire_days": days,
        "dry_run": dry_run,
        "scanned": 0,
        "eligible": 0,
        "moved": [],
        "failed": [],
        "skipped": {},
        "oldest_remaining_days": None,
        "archive_dir": None,
    }
    if days is None:
        result["status"] = "off"
        return result
    if pending.is_symlink() or not pending.is_dir():
        result["status"] = "no-queue"
        return result
    eligible, oldest_remaining = _scan(pending, now - days * 86400, result)
    result["eligible"] = len(eligible)
    if oldest_remaining is not None:
        result["oldest_remaining_days"] = int((now - oldest_remaining) // 86400)
    if not eligible:
        result["status"] = "clean"
        return result
    batch, deferred = eligible[:MAX_MOVES_PER_RUN], eligible[MAX_MOVES_PER_RUN:]
    if deferred:
        # The oldest draft still in the queue after this bounded run.
        result["oldest_remaining_days"] = int((now - deferred[0][0]) // 86400)
        result["deferred"] = len(deferred)
    if dry_run:
        result["moved"] = [entry.name for _, entry in batch]
        result["status"] = "dry-run"
        return result
    try:
        archive_dir = prepare_archive(state, now)
    except OSError as error:
        result["status"] = "archive-unavailable"
        result["error"] = str(error)
        return result
    result["archive_dir"] = str(archive_dir)
    _move_eligible(batch, archive_dir, pending, result, now=now, days=days, node=node)
    result["status"] = "moved" if result["moved"] else "failed"
    return result


# Public aliases for prescreen.py (#2183); the underscored names stay for callers inside this module.
append_manifest = _append_manifest


def restore(state: Path, name: str, *, now: float | None = None, node: str = "") -> dict[str, Any]:
    now = time.time() if now is None else now
    result: dict[str, Any] = {"ts": _now_iso(now), "node": node, "name": name}
    if not _SAFE_NAME_RE.match(name):
        result["status"] = "unsafe-name"
        return result
    root = state / ARCHIVE_DIR
    pending = state / "pending-skills"
    found: Path | None = None
    if root.is_dir() and not root.is_symlink():
        for archive_dir in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
            if not _ARCHIVE_DIR_RE.match(archive_dir.name) or archive_dir.is_symlink():
                continue
            candidate = archive_dir / name
            if candidate.is_dir() and not candidate.is_symlink():
                found = candidate
                break
    if found is None:
        result["status"] = "not-found"
        return result
    pending.mkdir(mode=0o700, exist_ok=True)
    dest = pending / name
    if dest.exists() or dest.is_symlink():
        result["status"] = "pending-name-taken"
        return result
    try:
        os.rename(found, dest)
    except OSError as error:
        result["status"] = f"rename-failed:{error.errno}"
        return result
    _append_manifest(
        found.parent,
        {"ts": _now_iso(now), "node": node, "name": name, "restored_to": str(pending), "reason": "restore"},
    )
    result["status"] = "restored"
    result["from"] = str(found.parent)
    return result


def status(state: Path, *, now: float | None = None) -> dict[str, Any]:
    """Read-only backlog view used by doctor/status: age buckets of undecided drafts."""
    now = time.time() if now is None else now
    pending = state / "pending-skills"
    buckets = {"lt7d": 0, "d7_30": 0, "d30_60": 0, "d60_90": 0, "ge90d": 0}
    oldest: float | None = None
    undecided = 0
    if pending.is_dir() and not pending.is_symlink():
        for entry in pending.iterdir():
            if classify(entry) is not None:
                continue
            ts = staged_at(entry)
            if ts is None:
                continue
            undecided += 1
            age = (now - ts) / 86400
            key = "lt7d" if age < 7 else "d7_30" if age < 30 else "d30_60" if age < 60 else "d60_90" if age < 90 else "ge90d"
            buckets[key] += 1
            if oldest is None or ts < oldest:
                oldest = ts
    archived = 0
    root = state / ARCHIVE_DIR
    if root.is_dir() and not root.is_symlink():
        for archive_dir in root.iterdir():
            if _ARCHIVE_DIR_RE.match(archive_dir.name) and archive_dir.is_dir() and not archive_dir.is_symlink():
                archived += sum(1 for p in archive_dir.iterdir() if p.is_dir() and not p.is_symlink())
    return {
        "undecided": undecided,
        "buckets": buckets,
        "oldest_days": None if oldest is None else int((now - oldest) // 86400),
        "archived": archived,
        "expire_days": expire_days(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="expire drafts older than CCC_SKILL_PENDING_EXPIRE_DAYS")
    run_p.add_argument("--dry-run", action="store_true", help="report only; move nothing")
    run_p.add_argument("--now", type=float, default=None, help="epoch override for tests")
    res_p = sub.add_parser("restore", help="move one archived draft back into the queue")
    res_p.add_argument("name")
    sub.add_parser("status", help="age buckets of the undecided queue (read-only)")
    args = parser.parse_args(argv)
    state = _state_dir()
    node = os.environ.get("CCC_NODE", "")
    if args.cmd == "run":
        out = run(state, dry_run=args.dry_run, now=args.now, node=node)
        rc = 1 if out.get("status") == "archive-unavailable" else 0
    elif args.cmd == "restore":
        out = restore(state, args.name, node=node)
        rc = 0 if out["status"] == "restored" else 1
    else:
        out = status(state)
        rc = 0
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
