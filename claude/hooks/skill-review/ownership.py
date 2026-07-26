#!/usr/bin/env python3
"""Fail-closed ownership and read-before-write contract for learned skills."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import uuid
from typing import Any


_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ATTEMPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_AUTOSAVE_MARKER = ".autosave-meta.json"
_MANAGED_MARKER = ".ccc-node-managed.json"
_CONTROL_FILE = "skill-autosave-control.json"
_LEDGER_FILE = "skill-autosave-ownership.jsonl"
_RECEIPT_DIR = "skill-autosave-read-receipts"
_LOCK_FILE = ".skill-autosave-ownership.lock"
_MAX_JSON_BYTES = 64 * 1024
_MAX_TARGET_BYTES = 1024 * 1024
_ALLOWED_OPERATIONS = {"patch", "edit", "write_file"}


class ContractError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Context:
    provider: str
    skills_dir: Path
    state_dir: Path
    uid: int


@dataclass(frozen=True)
class TargetSnapshot:
    relative: str
    content: bytes
    sha256: str
    device: int
    inode: int
    size: int
    mtime_ns: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _target_id(context: Context, name: str) -> str:
    root_id = _sha256(os.fsencode(os.path.abspath(context.skills_dir)))
    return _sha256(f"{context.provider}\0{root_id}\0{name}".encode())


def _validate_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name):
        raise ContractError("invalid_skill_name")


def _provider_from_env() -> str:
    explicit = os.environ.get("CCC_SKILL_PROVIDER", "")
    if explicit in {"claude", "codex"}:
        return explicit
    home = Path(os.environ.get("HOME", "/root"))
    if (
        ("CODEX_HOME" in os.environ or (home / ".codex").is_dir())
        and not (home / ".claude").is_dir()
        and not any((Path(part) / "claude").exists() for part in os.environ.get("PATH", "").split(":"))
    ):
        return "codex"
    return "claude"


def _build_context(args: argparse.Namespace) -> Context:
    provider = args.provider or _provider_from_env()
    if provider not in {"claude", "codex"}:
        raise ContractError("invalid_provider")
    home = Path(os.environ.get("HOME", "/root"))
    claude_dir = Path(os.environ.get("CCC_CLAUDE_DIR", home / ".claude"))
    if args.skills_dir is not None:
        skills_dir = args.skills_dir
    elif provider == "codex":
        codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex"))
        skills_dir = Path(os.environ.get("CODEX_SKILLS_DIR", codex_home / "skills"))
    else:
        skills_dir = Path(os.environ.get("CLAUDE_SKILLS_DIR", claude_dir / "skills"))
    state_dir = args.state_dir or Path(os.environ.get("CCC_STATE_DIR", claude_dir / "state"))
    return Context(
        provider=provider,
        skills_dir=Path(os.path.abspath(skills_dir)),
        state_dir=Path(os.path.abspath(state_dir)),
        uid=os.geteuid(),
    )


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ContractError("path_unreadable") from None


def _validate_existing_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        metadata = _lstat(current)
        if metadata is None:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise ContractError("symlink_component")
        if current != absolute and not stat.S_ISDIR(metadata.st_mode):
            raise ContractError("path_component_not_directory")


def _validate_skills_root(context: Context, *, missing_ok: bool = False) -> None:
    _validate_existing_components(context.skills_dir)
    metadata = _lstat(context.skills_dir)
    if metadata is None:
        if missing_ok:
            return
        raise ContractError("skills_root_missing")
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != context.uid
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ContractError("unsafe_skills_root")


def _validate_skill_dir(context: Context, name: str) -> Path:
    _validate_name(name)
    _validate_skills_root(context)
    skill_dir = context.skills_dir / name
    metadata = _lstat(skill_dir)
    if metadata is None:
        raise ContractError("skill_missing")
    if stat.S_ISLNK(metadata.st_mode):
        raise ContractError("external_or_symlink_skill")
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != context.uid
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ContractError("unsafe_skill_dir")
    return skill_dir


def _safe_json_file(
    path: Path,
    *,
    owner: int,
    exact_mode: int | None = None,
    max_bytes: int = _MAX_JSON_BYTES,
) -> dict[str, Any]:
    metadata = _lstat(path)
    if metadata is None:
        raise FileNotFoundError(path)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != owner
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or (exact_mode is not None and stat.S_IMODE(metadata.st_mode) != exact_mode)
        or metadata.st_size > max_bytes
    ):
        raise ContractError("unsafe_metadata")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != owner
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) & 0o022
                or (exact_mode is not None and stat.S_IMODE(opened.st_mode) != exact_mode)
            ):
                raise ContractError("unsafe_metadata")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
        finally:
            os.close(descriptor)
        if len(payload) > max_bytes:
            raise ContractError("metadata_too_large")
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ContractError("metadata_unreadable") from None
    if not isinstance(value, dict):
        raise ContractError("metadata_invalid")
    return value


def _relative_parts(relative: str) -> tuple[str, ...]:
    candidate = PurePosixPath(relative)
    if (
        not relative
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or "\\" in relative
    ):
        raise ContractError("target_outside_skill")
    return candidate.parts


def _read_target(context: Context, name: str, relative: str) -> TargetSnapshot:
    skill_dir = _validate_skill_dir(context, name)
    parts = _relative_parts(relative)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        current = os.open(skill_dir, directory_flags)
        descriptors.append(current)
        for component in parts[:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            metadata = os.fstat(current)
            if metadata.st_uid != context.uid or stat.S_IMODE(metadata.st_mode) & 0o022:
                raise ContractError("unsafe_target_directory")
            descriptors.append(current)
        target_fd = os.open(parts[-1], file_flags, dir_fd=current)
        descriptors.append(target_fd)
        metadata = os.fstat(target_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != context.uid
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or metadata.st_size > _MAX_TARGET_BYTES
        ):
            raise ContractError("unsafe_target_file")
        chunks: list[bytes] = []
        remaining = _MAX_TARGET_BYTES + 1
        while remaining > 0:
            chunk = os.read(target_fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > _MAX_TARGET_BYTES:
            raise ContractError("target_too_large")
        after = os.fstat(target_fd)
        if (
            (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ContractError("target_changed_during_read")
        return TargetSnapshot(
            relative="/".join(parts),
            content=content,
            sha256=_sha256(content),
            device=after.st_dev,
            inode=after.st_ino,
            size=after.st_size,
            mtime_ns=after.st_mtime_ns,
        )
    except FileNotFoundError:
        raise ContractError("target_missing") from None
    except OSError:
        raise ContractError("unsafe_target_path") from None
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _load_controls(context: Context) -> dict[str, Any]:
    path = context.state_dir / _CONTROL_FILE
    try:
        value = _safe_json_file(path, owner=context.uid, exact_mode=0o600)
    except FileNotFoundError:
        return {"schema_version": 1, "revision": 0, "records": {}}
    if (
        value.get("schema_version") != 1
        or not isinstance(value.get("revision"), int)
        or value["revision"] < 0
        or not isinstance(value.get("records"), dict)
    ):
        raise ContractError("control_metadata_invalid")
    return value


def _control_record(context: Context, controls: dict[str, Any], name: str) -> dict[str, Any] | None:
    record = controls["records"].get(f"{context.provider}:{name}")
    if record is None:
        return None
    if (
        not isinstance(record, dict)
        or record.get("target_id") != _target_id(context, name)
        or record.get("provider") != context.provider
        or record.get("name") != name
        or record.get("pinned") is not True
    ):
        raise ContractError("control_metadata_invalid")
    return record


def _validate_managed_marker(context: Context, name: str, path: Path) -> dict[str, Any]:
    marker = _safe_json_file(path, owner=context.uid)
    if (
        marker.get("schema_version") != 1
        or marker.get("manager") != "ccc-node"
        or marker.get("name") != name
        or not isinstance(marker.get("source"), str)
        or not isinstance(marker.get("source_hash"), str)
        or not isinstance(marker.get("files"), dict)
    ):
        raise ContractError("managed_metadata_invalid")
    return marker


def _validate_autosave_marker(
    context: Context,
    name: str,
    path: Path,
    skill_sha: str,
) -> tuple[int, str]:
    marker = _safe_json_file(path, owner=context.uid)
    if marker.get("schema_version") == 2:
        if (
            stat.S_IMODE(path.lstat().st_mode) != 0o600
            or marker.get("manager") != "ccc-node-skill-autosave"
            or marker.get("ownership") != "autosave-managed"
            or marker.get("name") != name
            or marker.get("provider") != context.provider
            or marker.get("target_id") != _target_id(context, name)
            or marker.get("skill_sha256") != skill_sha
            or marker.get("created_by") not in {"ccc-node", "operator-adopt"}
            or not isinstance(marker.get("provenance_revision"), int)
            or marker["provenance_revision"] < 1
            or not isinstance(marker.get("rollback_eligible"), bool)
        ):
            raise ContractError("autosave_metadata_invalid")
        return marker["provenance_revision"], _sha256(_canonical_json(marker))
    if (
        marker.get("installed_by") != "autosave"
        or marker.get("name") != name
        or marker.get("sha256") != skill_sha
        or not isinstance(marker.get("path"), str)
        or Path(os.path.abspath(marker["path"]))
        != context.skills_dir / name / "SKILL.md"
    ):
        raise ContractError("legacy_autosave_metadata_invalid")
    return 0, _sha256(_canonical_json(marker))


def _classification(context: Context, name: str) -> dict[str, Any]:
    _validate_name(name)
    target_id = _target_id(context, name)
    try:
        skill_dir = _validate_skill_dir(context, name)
        skill = _read_target(context, name, "SKILL.md")
        controls = _load_controls(context)
        pin = _control_record(context, controls, name)
    except ContractError as error:
        base = (
            "external/repo-installed"
            if error.code == "external_or_symlink_skill"
            else "unknown/unreadable"
        )
        return {
            "name": name,
            "provider": context.provider,
            "target_id": target_id,
            "classification": base,
            "base_classification": base,
            "pinned": False,
            "autonomous_write_allowed": False,
            "reason": error.code,
            "provenance_revision": None,
            "provenance_sha256": None,
        }

    managed_path = skill_dir / _MANAGED_MARKER
    autosave_path = skill_dir / _AUTOSAVE_MARKER
    managed_exists = _lstat(managed_path) is not None
    autosave_exists = _lstat(autosave_path) is not None
    external_exists = any(
        _lstat(skill_dir / marker) is not None
        for marker in (".git", ".repo-installed.json", ".external-skill.json")
    )
    base = "user-owned"
    reason = "no-autonomous-provenance"
    revision: int | None = None
    provenance_sha: str | None = None
    try:
        if managed_exists:
            managed = _validate_managed_marker(context, name, managed_path)
            base = "managed/bundled"
            reason = "managed-marker"
            revision = 1
            provenance_sha = _sha256(_canonical_json(managed))
            if autosave_exists:
                reason = "managed-marker-conflicts-with-autosave"
        elif autosave_exists:
            if external_exists:
                raise ContractError("conflicting_external_autosave_metadata")
            revision, provenance_sha = _validate_autosave_marker(
                context,
                name,
                autosave_path,
                skill.sha256,
            )
            base = "autosave-managed"
            reason = "valid-autosave-provenance"
        elif external_exists:
            base = "external/repo-installed"
            reason = "external-provenance-marker"
    except ContractError as error:
        base = "unknown/unreadable"
        reason = error.code
        revision = None
        provenance_sha = None

    pinned = pin is not None
    classification = "pinned" if pinned else base
    allowed = base == "autosave-managed" and not pinned
    return {
        "name": name,
        "provider": context.provider,
        "target_id": target_id,
        "classification": classification,
        "base_classification": base,
        "pinned": pinned,
        "autonomous_write_allowed": allowed,
        "reason": "pinned" if pinned else reason,
        "skill_sha256": skill.sha256,
        "provenance_revision": revision,
        "provenance_sha256": provenance_sha,
    }


def _skill_names(context: Context) -> list[str]:
    _validate_skills_root(context, missing_ok=True)
    if not context.skills_dir.exists():
        return []
    try:
        return sorted(
            entry.name
            for entry in os.scandir(context.skills_dir)
            if _NAME_RE.fullmatch(entry.name)
        )
    except OSError:
        raise ContractError("skills_root_unreadable") from None


def _ensure_private_dir(path: Path, context: Context) -> None:
    _validate_existing_components(path.parent)
    metadata = _lstat(path)
    if metadata is None:
        try:
            path.mkdir(mode=0o700, parents=False)
        except OSError:
            raise ContractError("state_directory_create_failed") from None
        metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != context.uid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ContractError("unsafe_state_directory")


def _prepare_state(context: Context) -> None:
    if context.state_dir.exists():
        _ensure_private_dir(context.state_dir, context)
        return
    parent = context.state_dir.parent
    if not parent.exists():
        raise ContractError("state_parent_missing")
    _ensure_private_dir(context.state_dir, context)


def _preflight_mutation_state(context: Context) -> None:
    metadata = _lstat(context.state_dir)
    if metadata is not None:
        _ensure_private_dir(context.state_dir, context)
        _preflight_ledger(context)
        return
    _validate_existing_components(context.state_dir.parent)
    parent = _lstat(context.state_dir.parent)
    if (
        parent is None
        or not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != context.uid
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise ContractError("unsafe_state_parent")


def _write_private_atomic(path: Path, value: object, context: Context) -> None:
    payload = _canonical_json(value)
    _ensure_private_dir(path.parent, context)
    temporary = path.parent / f".{path.name}.tmp.{uuid.uuid4().hex}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise ContractError("metadata_write_failed") from None
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600, follow_symlinks=False)


def _preflight_ledger(context: Context) -> None:
    path = context.state_dir / _LEDGER_FILE
    metadata = _lstat(path)
    if metadata is not None and (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != context.uid
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ContractError("unsafe_ownership_ledger")


def _append_ledger(context: Context, record: dict[str, Any]) -> None:
    _preflight_ledger(context)
    path = context.state_dir / _LEDGER_FILE
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        payload = _canonical_json(record)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ContractError("ownership_ledger_write_failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _MutationLock:
    def __init__(self, context: Context):
        self.context = context
        self.descriptor: int | None = None

    def __enter__(self) -> "_MutationLock":
        _prepare_state(self.context)
        path = self.context.state_dir / _LOCK_FILE
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        self.descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(self.descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self.context.uid
            or metadata.st_nlink != 1
        ):
            os.close(self.descriptor)
            self.descriptor = None
            raise ContractError("unsafe_ownership_lock")
        os.fchmod(self.descriptor, 0o600)
        fcntl.flock(self.descriptor, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_args: object) -> None:
        if self.descriptor is not None:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = None


def _write_marker_exclusive(
    context: Context,
    skill_dir: Path,
    marker: dict[str, Any],
) -> None:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    directory_fd = os.open(skill_dir, directory_flags)
    temporary = f".{_AUTOSAVE_MARKER}.tmp.{uuid.uuid4().hex}"
    descriptor: int | None = None
    try:
        try:
            os.stat(_AUTOSAVE_MARKER, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ContractError("autosave_marker_already_exists")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        os.fchmod(descriptor, 0o600)
        payload = _canonical_json(marker)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ContractError("autosave_marker_write_failed")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(
                temporary,
                _AUTOSAVE_MARKER,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise ContractError("autosave_marker_already_exists") from None
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError:
        raise ContractError("autosave_marker_write_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        os.close(directory_fd)


def _command_status(context: Context, name: str | None) -> dict[str, Any]:
    names = [name] if name else _skill_names(context)
    return {
        "ok": True,
        "command": "status",
        "provider": context.provider,
        "skills": [_classification(context, item) for item in names],
    }


def _command_list_unmanaged(context: Context) -> dict[str, Any]:
    records = [_classification(context, name) for name in _skill_names(context)]
    return {
        "ok": True,
        "command": "list-unmanaged",
        "provider": context.provider,
        "skills": [
            record
            for record in records
            if not record["autonomous_write_allowed"]
        ],
    }


def _command_adopt(context: Context, name: str, dry_run: bool) -> dict[str, Any]:
    before = _classification(context, name)
    if dry_run:
        _preflight_mutation_state(context)
    if before["base_classification"] == "autosave-managed":
        return {
            "ok": True,
            "command": "adopt",
            "changed": False,
            "dry_run": dry_run,
            "reason": "already-autosave-managed",
            "skill": before,
        }
    if before["base_classification"] != "user-owned":
        raise ContractError(f"adopt_denied_{before['base_classification'].replace('/', '_')}")
    result = {
        "ok": True,
        "command": "adopt",
        "changed": not dry_run,
        "dry_run": dry_run,
        "reason": "would-adopt" if dry_run else "adopted",
        "target_id": before["target_id"],
        "from": before["classification"],
        "to": "pinned" if before["pinned"] else "autosave-managed",
    }
    if dry_run:
        return result
    with _MutationLock(context):
        current = _classification(context, name)
        if current["base_classification"] != "user-owned":
            raise ContractError("adopt_state_changed")
        _preflight_ledger(context)
        marker = {
            "schema_version": 2,
            "manager": "ccc-node-skill-autosave",
            "ownership": "autosave-managed",
            "name": name,
            "provider": context.provider,
            "target_id": current["target_id"],
            "created_by": "operator-adopt",
            "adopted": True,
            "rollback_eligible": False,
            "skill_sha256": current["skill_sha256"],
            "provenance_revision": 1,
            "created_at": _timestamp(),
        }
        _write_marker_exclusive(context, _validate_skill_dir(context, name), marker)
        _append_ledger(
            context,
            {
                "schema_version": 1,
                "event": "adopt",
                "ts": _timestamp(),
                "provider": context.provider,
                "name": name,
                "target_id": current["target_id"],
                "from_revision": None,
                "to_revision": 1,
                "metadata_sha256": _sha256(_canonical_json(marker)),
                "outcome": "changed",
            },
        )
    result["skill"] = _classification(context, name)
    return result


def _command_mark_created(context: Context, name: str) -> dict[str, Any]:
    """Create v2 provenance for a just-installed autosave skill."""
    with _MutationLock(context):
        current = _classification(context, name)
        if current["base_classification"] != "user-owned" or current["pinned"]:
            raise ContractError("mark_created_denied")
        _preflight_ledger(context)
        marker = {
            "schema_version": 2,
            "manager": "ccc-node-skill-autosave",
            "ownership": "autosave-managed",
            "name": name,
            "provider": context.provider,
            "target_id": current["target_id"],
            "created_by": "ccc-node",
            "installed_by": "autosave",
            "adopted": False,
            "rollback_eligible": True,
            "skill_sha256": current["skill_sha256"],
            "provenance_revision": 1,
            "created_at": _timestamp(),
        }
        _write_marker_exclusive(context, _validate_skill_dir(context, name), marker)
        _append_ledger(
            context,
            {
                "schema_version": 1,
                "event": "create",
                "ts": _timestamp(),
                "provider": context.provider,
                "name": name,
                "target_id": current["target_id"],
                "from_revision": None,
                "to_revision": 1,
                "metadata_sha256": _sha256(_canonical_json(marker)),
                "outcome": "changed",
            },
        )
    return {
        "ok": True,
        "command": "mark-created",
        "changed": True,
        "skill": _classification(context, name),
    }


def _command_rollback_check(context: Context, name: str) -> dict[str, Any]:
    record = _classification(context, name)
    if record["pinned"]:
        raise ContractError("rollback_denied_pinned")
    if record["base_classification"] != "autosave-managed":
        raise ContractError("rollback_denied_not_autosave_managed")
    marker_path = _validate_skill_dir(context, name) / _AUTOSAVE_MARKER
    marker = _safe_json_file(marker_path, owner=context.uid)
    if marker.get("schema_version") == 2 and marker.get("rollback_eligible") is not True:
        raise ContractError("rollback_denied_not_rollback_eligible")
    return {
        "ok": True,
        "command": "rollback-check",
        "allowed": True,
        "name": name,
        "target_id": record["target_id"],
    }


def _command_pin(context: Context, name: str, pin: bool, dry_run: bool) -> dict[str, Any]:
    verb = "pin" if pin else "unpin"
    before = _classification(context, name)
    if dry_run:
        _preflight_mutation_state(context)
    if before["base_classification"] not in {"user-owned", "autosave-managed"}:
        raise ContractError(f"{verb}_denied_{before['base_classification'].replace('/', '_')}")
    if before["pinned"] == pin:
        return {
            "ok": True,
            "command": verb,
            "changed": False,
            "dry_run": dry_run,
            "reason": "already-pinned" if pin else "already-unpinned",
            "skill": before,
        }
    if dry_run:
        return {
            "ok": True,
            "command": verb,
            "changed": False,
            "dry_run": True,
            "reason": f"would-{verb}",
            "target_id": before["target_id"],
            "from": before["classification"],
            "to": "pinned" if pin else before["base_classification"],
        }
    with _MutationLock(context):
        current = _classification(context, name)
        if current["base_classification"] not in {"user-owned", "autosave-managed"}:
            raise ContractError(f"{verb}_state_changed")
        controls = _load_controls(context)
        _preflight_ledger(context)
        records = dict(controls["records"])
        key = f"{context.provider}:{name}"
        next_revision = controls["revision"] + 1
        if pin:
            records[key] = {
                "provider": context.provider,
                "name": name,
                "target_id": current["target_id"],
                "pinned": True,
                "updated_at": _timestamp(),
            }
        else:
            records.pop(key, None)
        updated = {
            "schema_version": 1,
            "revision": next_revision,
            "records": records,
        }
        _write_private_atomic(context.state_dir / _CONTROL_FILE, updated, context)
        _append_ledger(
            context,
            {
                "schema_version": 1,
                "event": verb,
                "ts": _timestamp(),
                "provider": context.provider,
                "name": name,
                "target_id": current["target_id"],
                "from_revision": controls["revision"],
                "to_revision": next_revision,
                "metadata_sha256": _sha256(_canonical_json(updated)),
                "outcome": "changed",
            },
        )
    return {
        "ok": True,
        "command": verb,
        "changed": True,
        "dry_run": False,
        "reason": f"{verb}ned" if pin else "unpinned",
        "skill": _classification(context, name),
    }


def _receipt_ttl_seconds() -> int:
    raw = os.environ.get("CCC_SKILL_READ_RECEIPT_TTL_SECONDS", "900")
    try:
        value = int(raw)
    except ValueError:
        value = 900
    return min(3600, max(60, value))


def _command_read_target(
    context: Context,
    name: str,
    relative: str,
    attempt_id: str,
    operation: str,
) -> dict[str, Any]:
    if not _ATTEMPT_RE.fullmatch(attempt_id):
        raise ContractError("invalid_attempt_id")
    if operation not in _ALLOWED_OPERATIONS:
        raise ContractError("invalid_operation")
    classification = _classification(context, name)
    if classification["base_classification"] == "unknown/unreadable":
        raise ContractError("read_denied_unknown")
    snapshot = _read_target(context, name, relative)
    receipt_id = uuid.uuid4().hex
    created = _now()
    receipt = {
        "schema_version": 1,
        "receipt_id": receipt_id,
        "attempt_id": attempt_id,
        "operation": operation,
        "provider": context.provider,
        "name": name,
        "target_id": classification["target_id"],
        "relative_target": snapshot.relative,
        "expected_sha256": snapshot.sha256,
        "expected_provenance_revision": classification["provenance_revision"],
        "expected_provenance_sha256": classification["provenance_sha256"],
        "file_identity": {
            "device": snapshot.device,
            "inode": snapshot.inode,
            "size": snapshot.size,
            "mtime_ns": snapshot.mtime_ns,
        },
        "created_at": _timestamp(created),
        "expires_at": _timestamp(created + timedelta(seconds=_receipt_ttl_seconds())),
        "consumed": False,
    }
    with _MutationLock(context):
        _preflight_ledger(context)
        receipt_dir = context.state_dir / _RECEIPT_DIR
        if not receipt_dir.exists():
            _ensure_private_dir(receipt_dir, context)
        _write_private_atomic(receipt_dir / f"{receipt_id}.json", receipt, context)
        _append_ledger(
            context,
            {
                "schema_version": 1,
                "event": "read-receipt",
                "ts": _timestamp(),
                "provider": context.provider,
                "name": name,
                "target_id": classification["target_id"],
                "receipt_id": receipt_id,
                "relative_target_sha256": _sha256(snapshot.relative.encode()),
                "content_sha256": snapshot.sha256,
                "outcome": "created",
            },
        )
    try:
        content_text = snapshot.content.decode("utf-8")
        content_encoding = "utf-8"
    except UnicodeDecodeError:
        content_text = base64.b64encode(snapshot.content).decode()
        content_encoding = "base64"
    public_receipt = dict(receipt)
    public_receipt.pop("file_identity")
    return {
        "ok": True,
        "command": "read-target",
        "content_encoding": content_encoding,
        "content": content_text,
        "receipt": public_receipt,
    }


def _proposal(path: Path, context: Context) -> dict[str, Any]:
    value = _safe_json_file(path, owner=context.uid, max_bytes=_MAX_JSON_BYTES)
    if value.get("schema_version") != 1:
        raise ContractError("proposal_schema_invalid")
    return value


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ContractError("receipt_invalid")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ContractError("receipt_invalid") from None


def _consume_receipt(
    context: Context,
    receipt_path: Path,
    receipt: dict[str, Any],
    outcome: str,
) -> None:
    updated = dict(receipt)
    updated["consumed"] = True
    updated["consumed_at"] = _timestamp()
    updated["outcome"] = outcome
    _write_private_atomic(receipt_path, updated, context)


def _command_guard_proposal(context: Context, proposal_path: Path) -> dict[str, Any]:
    proposal = _proposal(proposal_path, context)
    required = {
        "attempt_id",
        "receipt_id",
        "operation",
        "provider",
        "name",
        "target_id",
        "relative_target",
        "expected_sha256",
        "expected_provenance_revision",
        "expected_provenance_sha256",
    }
    if not required.issubset(proposal):
        raise ContractError("proposal_fields_missing")
    receipt_id = proposal["receipt_id"]
    if not isinstance(receipt_id, str) or not re.fullmatch(r"[0-9a-f]{32}", receipt_id):
        raise ContractError("receipt_id_invalid")
    receipt_path = context.state_dir / _RECEIPT_DIR / f"{receipt_id}.json"
    with _MutationLock(context):
        _preflight_ledger(context)
        try:
            receipt = _safe_json_file(receipt_path, owner=context.uid, exact_mode=0o600)
        except FileNotFoundError:
            raise ContractError("receipt_missing") from None
        if receipt.get("schema_version") != 1 or receipt.get("receipt_id") != receipt_id:
            raise ContractError("receipt_invalid")
        if receipt.get("consumed") is not False:
            raise ContractError("receipt_consumed")
        outcome = "denied"
        code = "proposal_receipt_mismatch"
        comparable = (
            "attempt_id",
            "operation",
            "provider",
            "name",
            "target_id",
            "relative_target",
            "expected_sha256",
            "expected_provenance_revision",
            "expected_provenance_sha256",
        )
        try:
            if any(proposal.get(field) != receipt.get(field) for field in comparable):
                raise ContractError(code)
            if proposal["operation"] not in _ALLOWED_OPERATIONS:
                raise ContractError("invalid_operation")
            if _now() > _parse_timestamp(receipt.get("expires_at")):
                raise ContractError("receipt_expired")
            snapshot = _read_target(
                context,
                proposal["name"],
                proposal["relative_target"],
            )
            identity = receipt.get("file_identity")
            if not isinstance(identity, dict):
                raise ContractError("receipt_invalid")
            if (
                snapshot.sha256 != receipt["expected_sha256"]
                or snapshot.device != identity.get("device")
                or snapshot.inode != identity.get("inode")
                or snapshot.size != identity.get("size")
                or snapshot.mtime_ns != identity.get("mtime_ns")
            ):
                raise ContractError("target_drift")
            classification = _classification(context, proposal["name"])
            if not classification["autonomous_write_allowed"]:
                raise ContractError(f"autonomous_write_denied_{classification['classification'].replace('/', '_')}")
            if (
                classification["target_id"] != receipt["target_id"]
                or classification["provenance_revision"]
                != receipt["expected_provenance_revision"]
                or classification["provenance_sha256"]
                != receipt["expected_provenance_sha256"]
            ):
                raise ContractError("provenance_drift")
            outcome = "authorized"
            code = "authorized"
        except ContractError as error:
            code = error.code
        _consume_receipt(context, receipt_path, receipt, outcome)
        _append_ledger(
            context,
            {
                "schema_version": 1,
                "event": "write-guard",
                "ts": _timestamp(),
                "provider": context.provider,
                "name": str(proposal.get("name", ""))[:64],
                "target_id": str(proposal.get("target_id", ""))[:64],
                "receipt_id": receipt_id,
                "operation": str(proposal.get("operation", ""))[:16],
                "outcome": outcome,
                "code": code,
            },
        )
    return {
        "ok": outcome == "authorized",
        "command": "guard-proposal",
        "allowed": outcome == "authorized",
        "code": code,
        "receipt_id": receipt_id,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("claude", "codex"))
    parser.add_argument("--skills-dir", type=Path)
    parser.add_argument("--state-dir", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("name", nargs="?")
    subparsers.add_parser("list-unmanaged")

    for command in ("adopt", "pin", "unpin"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("name")
        command_parser.add_argument("--dry-run", action="store_true")

    read_parser = subparsers.add_parser("read-target")
    read_parser.add_argument("name")
    read_parser.add_argument("relative_target")
    read_parser.add_argument("--attempt-id", required=True)
    read_parser.add_argument("--operation", required=True, choices=sorted(_ALLOWED_OPERATIONS))

    guard_parser = subparsers.add_parser("guard-proposal")
    guard_parser.add_argument("--proposal", type=Path, required=True)
    mark_parser = subparsers.add_parser("mark-created")
    mark_parser.add_argument("name")
    rollback_parser = subparsers.add_parser("rollback-check")
    rollback_parser.add_argument("name")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        context = _build_context(args)
        if args.command == "status":
            result = _command_status(context, args.name)
        elif args.command == "list-unmanaged":
            result = _command_list_unmanaged(context)
        elif args.command == "adopt":
            result = _command_adopt(context, args.name, args.dry_run)
        elif args.command == "mark-created":
            result = _command_mark_created(context, args.name)
        elif args.command == "rollback-check":
            result = _command_rollback_check(context, args.name)
        elif args.command == "pin":
            result = _command_pin(context, args.name, True, args.dry_run)
        elif args.command == "unpin":
            result = _command_pin(context, args.name, False, args.dry_run)
        elif args.command == "read-target":
            result = _command_read_target(
                context,
                args.name,
                args.relative_target,
                args.attempt_id,
                args.operation,
            )
        else:
            result = _command_guard_proposal(context, args.proposal)
    except ContractError as error:
        print(json.dumps({"ok": False, "code": error.code}, sort_keys=True))
        return 2
    except OSError:
        print(json.dumps({"ok": False, "code": "filesystem_error"}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.command == "guard-proposal" and not result["allowed"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
