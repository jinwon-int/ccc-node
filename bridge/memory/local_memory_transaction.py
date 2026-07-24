"""Crash-recoverable local-memory commits and latest-head rollback (#386).

The transaction covers exactly ``memory-facts.jsonl`` and ``resume.md`` below
one owner-only state directory.  Memory bodies live only in private pre-image
files; the ledger and manifests contain hashes and bounded metadata only.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Callable, Iterator, Mapping, Sequence

try:
    from telegram_bot.utils.secure_fs import (
        _atomic_write_bytes,
        _fsync_directory,
        ensure_private_directory,
    )
except ModuleNotFoundError:  # Standalone hook-tree copy installed by setup.sh.
    from ccc_secure_fs import _atomic_write_bytes, _fsync_directory, ensure_private_directory


_SCHEMA = "ccc.local-memory-rollback.v1"
_TARGETS = ("memory-facts.jsonl", "resume.md")
_SAFE_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ACTION_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_LEDGER_BYTES = 1024 * 1024
_ABSENT_HASH = hashlib.sha256(b"ccc-node:absent:v1").hexdigest()

StateMap = dict[str, bytes | None]
Transform = Callable[[Mapping[str, bytes | None]], Mapping[str, bytes | None]]


class LocalMemoryTransactionError(RuntimeError):
    """Base error for a local-memory transaction."""


class LocalMemoryConflict(LocalMemoryTransactionError):
    """The current files no longer match the selected action."""


class LocalMemorySecurityError(LocalMemoryTransactionError):
    """An owner, mode, link, path, or manifest invariant failed."""


@dataclass(frozen=True, slots=True)
class LocalMemoryCommitResult:
    action_id: str | None
    changed_targets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LocalMemoryRollbackResult:
    action_id: str
    status: str


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash(payload: bytes | None) -> str:
    return _ABSENT_HASH if payload is None else hashlib.sha256(payload).hexdigest()


def _session_ref(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _safe_label(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_LABEL_RE.fullmatch(value):
        raise ValueError(f"{field} must be a bounded machine label")
    return value


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _validate_regular(path: Path, *, mode: int = 0o600) -> os.stat_result | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise LocalMemorySecurityError(f"state entry must be a regular file: {path.name}")
    if metadata.st_nlink != 1:
        raise LocalMemorySecurityError(f"state entry must have one link: {path.name}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise LocalMemorySecurityError(f"state entry has the wrong owner: {path.name}")
    if stat.S_IMODE(metadata.st_mode) != mode:
        raise LocalMemorySecurityError(f"state entry has an unsafe mode: {path.name}")
    return metadata


def _read_bounded(path: Path, max_bytes: int) -> bytes:
    _validate_regular(path)
    payload = path.read_bytes()
    if len(payload) > max_bytes:
        raise LocalMemorySecurityError(f"state entry exceeds its safe bound: {path.name}")
    _validate_regular(path)
    return payload


class LocalMemoryTransaction:
    """Serialize, recover, and roll back one state directory's local memory."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(os.path.abspath(os.fspath(state_dir)))
        self.rollback_dir = self.state_dir / "memory-rollback"
        self.actions_dir = self.rollback_dir / "actions"
        self.ledger_path = self.rollback_dir / "ledger.jsonl"
        self.head_path = self.rollback_dir / "HEAD"
        self.lock_path = self.state_dir / ".local-memory-transaction.lock"

    def _ensure_layout(self) -> None:
        ensure_private_directory(self.state_dir)
        ensure_private_directory(self.rollback_dir)
        ensure_private_directory(self.actions_dir)
        for path in (self.state_dir, self.rollback_dir, self.actions_dir):
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise LocalMemorySecurityError(f"transaction directory is unsafe: {path.name}")
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise LocalMemorySecurityError(f"transaction directory owner is unsafe: {path.name}")
            if stat.S_IMODE(metadata.st_mode) != 0o700:
                raise LocalMemorySecurityError(f"transaction directory mode is unsafe: {path.name}")

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        self._ensure_layout()
        _validate_regular(self.lock_path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise LocalMemorySecurityError("transaction lock is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _target_path(self, name: str) -> Path:
        if name not in _TARGETS:
            raise ValueError("local-memory target is not allowlisted")
        return self.state_dir / name

    def _read_targets(self) -> StateMap:
        values: StateMap = {}
        for name in _TARGETS:
            path = self._target_path(name)
            metadata = _validate_regular(path)
            values[name] = None if metadata is None else _read_bounded(path, 8 << 20)
            _validate_regular(path)
        return values

    def _action_dir(self, action_id: str) -> Path:
        if not _ACTION_RE.fullmatch(action_id):
            raise ValueError("action_id must be 32 lowercase hex characters")
        return self.actions_dir / action_id

    def _manifest_path(self, action_id: str) -> Path:
        return self._action_dir(action_id) / "manifest.json"

    def _read_manifest(self, action_id: str) -> dict[str, object]:
        action_dir = self._action_dir(action_id)
        metadata = action_dir.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise LocalMemorySecurityError("rollback action directory is unsafe")
        manifest = json.loads(_read_bounded(self._manifest_path(action_id), _MAX_MANIFEST_BYTES))
        if not isinstance(manifest, dict) or manifest.get("schema") != _SCHEMA:
            raise LocalMemorySecurityError("rollback manifest schema is invalid")
        if manifest.get("action_id") != action_id:
            raise LocalMemorySecurityError("rollback manifest action id is invalid")
        self._validate_manifest_targets(manifest)
        return manifest

    @staticmethod
    def _validate_manifest_targets(manifest: Mapping[str, object]) -> None:
        targets = manifest.get("targets")
        if not isinstance(targets, dict) or set(targets) != set(_TARGETS):
            raise LocalMemorySecurityError("rollback manifest target set is invalid")
        for name in _TARGETS:
            item = targets[name]
            if not isinstance(item, dict):
                raise LocalMemorySecurityError("rollback manifest target is invalid")
            for key in ("before_hash", "after_hash"):
                if not isinstance(item.get(key), str) or not _SHA256_RE.fullmatch(item[key]):
                    raise LocalMemorySecurityError("rollback manifest hash is invalid")
            if not isinstance(item.get("before_exists"), bool) or not isinstance(
                item.get("after_exists"), bool
            ):
                raise LocalMemorySecurityError("rollback manifest existence flag is invalid")

    def _read_head(self) -> str | None:
        metadata = _validate_regular(self.head_path)
        if metadata is None:
            return None
        value = _read_bounded(self.head_path, 128).decode().strip()
        if not value:
            return None
        if not _ACTION_RE.fullmatch(value):
            raise LocalMemorySecurityError("rollback HEAD is invalid")
        return value

    def _write_head(self, action_id: str | None) -> None:
        payload = b"" if action_id is None else f"{action_id}\n".encode()
        _atomic_write_bytes(self.head_path, payload)
        _validate_regular(self.head_path)

    def _write_manifest(self, manifest: Mapping[str, object]) -> None:
        action_id = str(manifest["action_id"])
        _atomic_write_bytes(self._manifest_path(action_id), _json_bytes(manifest))
        _validate_regular(self._manifest_path(action_id))

    def _event_exists(self, action_id: str, event: str) -> bool:
        metadata = _validate_regular(self.ledger_path)
        if metadata is None:
            return False
        payload = _read_bounded(self.ledger_path, _MAX_LEDGER_BYTES)
        for line in payload.splitlines():
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                continue
            if item.get("action_id") == action_id and item.get("event") == event:
                return True
        return False

    def _record_event(self, manifest: Mapping[str, object], event: str) -> None:
        action_id = str(manifest["action_id"])
        if self._event_exists(action_id, event):
            return
        record = {
            "schema": _SCHEMA,
            "event": event,
            "action_id": action_id,
            "actor": manifest["actor"],
            "tool": manifest["tool"],
            "provider": manifest["provider"],
            "scope": "local-memory",
            "targets": list(_TARGETS),
            "diff": manifest["diff"],
            "session": manifest["session"],
            "ts": _now(),
        }
        existing = b""
        if _validate_regular(self.ledger_path) is not None:
            existing = _read_bounded(self.ledger_path, _MAX_LEDGER_BYTES)
        lines = [line for line in existing.splitlines(keepends=True) if line.strip()]
        lines.append(_json_bytes(record))
        while len(b"".join(lines)) > _MAX_LEDGER_BYTES and len(lines) > 1:
            lines.pop(0)
        payload = b"".join(lines)
        _atomic_write_bytes(self.ledger_path, payload)
        _validate_regular(self.ledger_path)

    def _snapshot_path(self, action_id: str, name: str) -> Path:
        return self._action_dir(action_id) / f"before-{name}"

    def _read_before(self, manifest: Mapping[str, object], name: str) -> bytes | None:
        item = manifest["targets"][name]
        if not item["before_exists"]:
            return None
        payload = _read_bounded(self._snapshot_path(str(manifest["action_id"]), name), 8 << 20)
        if _hash(payload) != item["before_hash"]:
            raise LocalMemorySecurityError("rollback pre-image hash is invalid")
        return payload

    def _write_target(self, name: str, payload: bytes | None) -> None:
        path = self._target_path(name)
        _validate_regular(path)
        if payload is None:
            path.unlink(missing_ok=True)
            _fsync_directory(self.state_dir)
            return
        _atomic_write_bytes(path, payload)
        _validate_regular(path)

    @staticmethod
    def _target_matches_side(
        current: bytes | None,
        target: Mapping[str, object],
        side: str,
    ) -> bool:
        return (
            (current is not None) == target[f"{side}_exists"]
            and _hash(current) == target[f"{side}_hash"]
        )

    @classmethod
    def _state_matches(
        cls,
        current: Mapping[str, bytes | None],
        manifest: Mapping[str, object],
        side: str,
    ) -> bool:
        return all(
            cls._target_matches_side(current[name], manifest["targets"][name], side)
            for name in _TARGETS
        )

    def _restore_before(self, manifest: Mapping[str, object]) -> None:
        for name in _TARGETS:
            self._write_target(name, self._read_before(manifest, name))
        if not self._state_matches(self._read_targets(), manifest, "before"):
            raise LocalMemoryTransactionError("rollback restore verification failed")

    def _finish_prepared(self, manifest: dict[str, object]) -> None:
        current = self._read_targets()
        if self._state_matches(current, manifest, "after"):
            self._write_head(str(manifest["action_id"]))
            manifest["state"] = "committed"
            self._write_manifest(manifest)
            self._record_event(manifest, "commit")
            return
        if not self._state_matches(current, manifest, "before"):
            allowed = all(
                any(
                    self._target_matches_side(
                        current[name], manifest["targets"][name], side
                    )
                    for side in ("before", "after")
                )
                for name in _TARGETS
            )
            if not allowed:
                raise LocalMemoryConflict("prepared action encountered an unknown target state")
        self._restore_before(manifest)
        manifest["state"] = "aborted"
        self._write_manifest(manifest)
        self._record_event(manifest, "abort")

    def _finish_undo(self, manifest: dict[str, object]) -> None:
        current = self._read_targets()
        allowed = all(
            any(
                self._target_matches_side(current[name], manifest["targets"][name], side)
                for side in ("before", "after")
            )
            for name in _TARGETS
        )
        if not allowed:
            raise LocalMemoryConflict("undo recovery encountered an unknown target state")
        self._restore_before(manifest)
        self._write_head(None)
        manifest["state"] = "rolled_back"
        self._write_manifest(manifest)
        self._record_event(manifest, "rollback")

    def _discard_unprepared_action(self, path: Path) -> None:
        allowed = {f"before-{name}" for name in _TARGETS}
        entries = list(path.iterdir())
        if any(entry.name not in allowed for entry in entries):
            raise LocalMemorySecurityError("rollback action has no manifest")
        for entry in entries:
            _validate_regular(entry)
            entry.unlink()
        path.rmdir()
        _fsync_directory(self.actions_dir)

    def _recover(self) -> None:
        candidates: list[dict[str, object]] = []
        for path in sorted(self.actions_dir.iterdir()):
            if not path.is_dir() or path.is_symlink():
                raise LocalMemorySecurityError("rollback action entry is unsafe")
            if not self._manifest_path(path.name).exists():
                self._discard_unprepared_action(path)
                continue
            manifest = self._read_manifest(path.name)
            if manifest.get("state") in {"prepared", "undoing"}:
                candidates.append(manifest)
        if len(candidates) > 1:
            raise LocalMemoryConflict("multiple incomplete rollback actions exist")
        if candidates:
            manifest = candidates[0]
            if manifest["state"] == "prepared":
                self._finish_prepared(manifest)
            else:
                self._finish_undo(manifest)
        head = self._read_head()
        if head is not None:
            current = self._read_manifest(head)
            if current.get("state") != "committed":
                raise LocalMemoryConflict("rollback HEAD is not committed")
            self._record_event(current, "commit")
            parent = current.get("parent")
            if isinstance(parent, str):
                self._supersede(parent)

    def _supersede(self, action_id: str | None) -> None:
        if action_id is None:
            return
        manifest = self._read_manifest(action_id)
        if manifest.get("state") not in {"committed", "superseded"}:
            return
        if manifest.get("state") == "committed":
            manifest["state"] = "superseded"
            self._write_manifest(manifest)
        for name in _TARGETS:
            self._snapshot_path(action_id, name).unlink(missing_ok=True)
        _fsync_directory(self._action_dir(action_id))
        self._record_event(manifest, "supersede")

    def commit(
        self,
        transform: Transform,
        *,
        provider: str,
        actor: str,
        tool: str,
        session: str,
        diff: str,
    ) -> LocalMemoryCommitResult:
        """Apply one two-target transform and make its pre-image the undo head."""
        provider = _safe_label(provider, "provider")
        actor = _safe_label(actor, "actor")
        tool = _safe_label(tool, "tool")
        diff = _safe_label(diff, "diff")
        if not isinstance(session, str):
            raise ValueError("session must be a string")
        with self._exclusive():
            self._recover()
            before = self._read_targets()
            requested = dict(transform(dict(before)))
            if not set(requested).issubset(_TARGETS):
                raise ValueError("transform returned a non-allowlisted target")
            after = dict(before)
            for name, payload in requested.items():
                if payload is not None and not isinstance(payload, bytes):
                    raise ValueError("transform payloads must be bytes or None")
                after[name] = payload
            changed = tuple(name for name in _TARGETS if before[name] != after[name])
            if not changed:
                return LocalMemoryCommitResult(None, ())
            previous_head = self._read_head()
            action_id = secrets.token_hex(16)
            action_dir = self._action_dir(action_id)
            os.mkdir(action_dir, mode=0o700)
            _fsync_directory(self.actions_dir)
            for name in _TARGETS:
                if before[name] is not None:
                    _atomic_write_bytes(self._snapshot_path(action_id, name), before[name])
                    _validate_regular(self._snapshot_path(action_id, name))
            manifest: dict[str, object] = {
                "schema": _SCHEMA,
                "action_id": action_id,
                "state": "prepared",
                "parent": previous_head,
                "provider": provider,
                "actor": actor,
                "tool": tool,
                "diff": diff,
                "session": _session_ref(session),
                "created_at": _now(),
                "targets": {
                    name: {
                        "before_exists": before[name] is not None,
                        "after_exists": after[name] is not None,
                        "before_hash": _hash(before[name]),
                        "after_hash": _hash(after[name]),
                    }
                    for name in _TARGETS
                },
            }
            self._write_manifest(manifest)
            for name in _TARGETS:
                if name in changed:
                    self._write_target(name, after[name])
            if not self._state_matches(self._read_targets(), manifest, "after"):
                raise LocalMemoryTransactionError("local-memory commit verification failed")
            self._write_head(action_id)
            manifest["state"] = "committed"
            self._write_manifest(manifest)
            self._record_event(manifest, "commit")
            self._supersede(previous_head)
            return LocalMemoryCommitResult(action_id, changed)

    def rollback(self, action_id: str) -> LocalMemoryRollbackResult:
        """Restore the latest committed head after full two-target CAS checks."""
        with self._exclusive():
            self._recover()
            manifest = self._read_manifest(action_id)
            state = manifest.get("state")
            if state == "rolled_back":
                if not self._state_matches(self._read_targets(), manifest, "before"):
                    raise LocalMemoryConflict("rolled-back action no longer matches its pre-image")
                self._record_event(manifest, "rollback")
                return LocalMemoryRollbackResult(action_id, "already-rolled-back")
            if state != "committed" or self._read_head() != action_id:
                raise LocalMemoryConflict("only the latest committed action can be rolled back")
            if not self._state_matches(self._read_targets(), manifest, "after"):
                raise LocalMemoryConflict("current memory does not match the action post-image")
            manifest["state"] = "undoing"
            self._write_manifest(manifest)
            self._finish_undo(manifest)
            return LocalMemoryRollbackResult(action_id, "rolled-back")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rollback one local-memory transaction head")
    subparsers = parser.add_subparsers(dest="command", required=True)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--state-dir", type=Path, required=True)
    rollback.add_argument("--action-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = LocalMemoryTransaction(args.state_dir).rollback(args.action_id)
    except (LocalMemoryTransactionError, OSError, ValueError) as error:
        print(f"local-memory rollback refused: {error}", file=os.sys.stderr)
        return 1
    print(f"local-memory rollback {result.status}: {result.action_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LocalMemoryCommitResult",
    "LocalMemoryConflict",
    "LocalMemoryRollbackResult",
    "LocalMemorySecurityError",
    "LocalMemoryTransaction",
    "LocalMemoryTransactionError",
]
