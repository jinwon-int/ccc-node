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
_ROLLBACK_DIR = "skill-autosave-rollback"
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
    ctime_ns: int


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
            after = os.fstat(descriptor)
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ContractError("metadata_changed_during_read")
        finally:
            os.close(descriptor)
        if len(payload) > max_bytes:
            raise ContractError("metadata_too_large")
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
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
    expected_directory = _lstat(skill_dir)
    if expected_directory is None:
        raise ContractError("skill_missing")
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
        opened_directory = os.fstat(current)
        if (
            opened_directory.st_dev != expected_directory.st_dev
            or opened_directory.st_ino != expected_directory.st_ino
            or not stat.S_ISDIR(opened_directory.st_mode)
            or opened_directory.st_uid != context.uid
            or stat.S_IMODE(opened_directory.st_mode) & 0o022
        ):
            raise ContractError("skill_directory_changed")
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
            (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            raise ContractError("target_changed_during_read")
        final_directory = os.fstat(descriptors[0])
        path_directory = _lstat(skill_dir)
        if (
            path_directory is None
            or (opened_directory.st_dev, opened_directory.st_ino)
            != (final_directory.st_dev, final_directory.st_ino)
            or (opened_directory.st_dev, opened_directory.st_ino)
            != (path_directory.st_dev, path_directory.st_ino)
        ):
            raise ContractError("skill_directory_changed")
        return TargetSnapshot(
            relative="/".join(parts),
            content=content,
            sha256=_sha256(content),
            device=after.st_dev,
            inode=after.st_ino,
            size=after.st_size,
            mtime_ns=after.st_mtime_ns,
            ctime_ns=after.st_ctime_ns,
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
        or type(value.get("revision")) is not int
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
    if type(marker.get("schema_version")) is int and marker["schema_version"] == 2:
        marker = _safe_json_file(path, owner=context.uid, exact_mode=0o600)
        if (
            type(marker.get("schema_version")) is not int
            or marker["schema_version"] != 2
            or marker.get("manager") != "ccc-node-skill-autosave"
            or marker.get("ownership") != "autosave-managed"
            or marker.get("name") != name
            or marker.get("provider") != context.provider
            or marker.get("target_id") != _target_id(context, name)
            or marker.get("skill_sha256") != skill_sha
            or marker.get("created_by") not in {"ccc-node", "operator-adopt"}
            or type(marker.get("provenance_revision")) is not int
            or marker["provenance_revision"] < 1
            or not isinstance(marker.get("rollback_eligible"), bool)
        ):
            raise ContractError("autosave_metadata_invalid")
        return marker["provenance_revision"], _sha256(_canonical_json(marker))
    if "schema_version" in marker:
        raise ContractError("autosave_metadata_invalid")
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
        directory_before = _lstat(skill_dir)
        if directory_before is None:
            raise ContractError("skill_missing")
        skill = _read_target(context, name, "SKILL.md")
        controls = _load_controls(context)
        pin = _control_record(context, controls, name)
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
        skill_after = _read_target(context, name, "SKILL.md")
        if (
            skill.sha256,
            skill.device,
            skill.inode,
            skill.size,
            skill.mtime_ns,
            skill.ctime_ns,
        ) != (
            skill_after.sha256,
            skill_after.device,
            skill_after.inode,
            skill_after.size,
            skill_after.mtime_ns,
            skill_after.ctime_ns,
        ):
            raise ContractError("skill_changed_during_classification")
        directory_after = _lstat(skill_dir)
        if (
            directory_after is None
            or (
                directory_before.st_dev,
                directory_before.st_ino,
                directory_before.st_mtime_ns,
                directory_before.st_ctime_ns,
            )
            != (
                directory_after.st_dev,
                directory_after.st_ino,
                directory_after.st_mtime_ns,
                directory_after.st_ctime_ns,
            )
        ):
            raise ContractError("skill_directory_changed")
    except (ContractError, FileNotFoundError) as error:
        code = error.code if isinstance(error, ContractError) else "metadata_changed_during_read"
        base = (
            "external/repo-installed"
            if code == "external_or_symlink_skill"
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
            "reason": code,
            "provenance_revision": None,
            "provenance_sha256": None,
        }

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
        _validate_existing_components(context.state_dir)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != context.uid
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ContractError("unsafe_state_directory")
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
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    directory_fd: int | None = None
    descriptor: int | None = None
    temporary = f".{path.name}.tmp.{uuid.uuid4().hex}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_fd = os.open(path.parent, directory_flags)
        directory_metadata = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != context.uid
            or stat.S_IMODE(directory_metadata.st_mode) != 0o700
        ):
            raise ContractError("unsafe_state_directory")
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except OSError:
        raise ContractError("metadata_write_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_fd is not None:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass
            os.close(directory_fd)


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
    before = _lstat(path)
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != context.uid
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (
                before is not None
                and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            )
        ):
            raise ContractError("unsafe_ownership_ledger")
        payload = _canonical_json(record)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ContractError("ownership_ledger_write_failed")
            view = view[written:]
        os.fsync(descriptor)
    except OSError:
        raise ContractError("ownership_ledger_write_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _transaction_record(
    event: str,
    transaction_id: str,
    *,
    outcome: str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event": event,
        "transaction_id": transaction_id,
        "ts": _timestamp(),
        **fields,
        "outcome": outcome,
    }


def _finish_transaction(
    context: Context,
    event: str,
    transaction_id: str,
    *,
    outcome: str,
    fields: dict[str, Any],
) -> bool:
    """Best-effort terminal row; the durable prepared row prevents audit gaps."""
    try:
        _append_ledger(
            context,
            _transaction_record(
                event,
                transaction_id,
                outcome=outcome,
                fields=fields,
            ),
        )
    except ContractError:
        return False
    return True


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
        try:
            self.descriptor = os.open(path, flags, 0o600)
            metadata = os.fstat(self.descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self.context.uid
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise ContractError("unsafe_ownership_lock")
            os.fchmod(self.descriptor, 0o600)
            fcntl.flock(self.descriptor, fcntl.LOCK_EX)
        except (OSError, ContractError) as error:
            if self.descriptor is not None:
                try:
                    os.close(self.descriptor)
                except OSError:
                    pass
                self.descriptor = None
            if isinstance(error, ContractError):
                raise
            raise ContractError("ownership_lock_failed") from None
        return self

    def __exit__(self, *_args: object) -> None:
        if self.descriptor is not None:
            descriptor = self.descriptor
            self.descriptor = None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


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
    published = False
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
        published = True
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError:
        if published:
            try:
                os.unlink(_AUTOSAVE_MARKER, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError:
                pass
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
    if dry_run:
        _preflight_mutation_state(context)
    if dry_run:
        return {
            "ok": True,
            "command": "adopt",
            "changed": False,
            "dry_run": True,
            "reason": "would-adopt",
            "target_id": before["target_id"],
            "from": before["classification"],
            "to": "pinned" if before["pinned"] else "autosave-managed",
        }
    with _MutationLock(context):
        current = _classification(context, name)
        if current["base_classification"] != "user-owned":
            raise ContractError("adopt_state_changed")
        if (
            current["target_id"] != before["target_id"]
            or current["skill_sha256"] != before["skill_sha256"]
            or current["pinned"] != before["pinned"]
        ):
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
        transaction_id = uuid.uuid4().hex
        transaction_fields = {
            "provider": context.provider,
            "name": name,
            "target_id": current["target_id"],
            "from_revision": None,
            "to_revision": 1,
            "metadata_sha256": _sha256(_canonical_json(marker)),
        }
        _append_ledger(
            context,
            _transaction_record(
                "adopt",
                transaction_id,
                outcome="prepared",
                fields=transaction_fields,
            ),
        )
        try:
            _write_marker_exclusive(context, _validate_skill_dir(context, name), marker)
        except ContractError:
            _finish_transaction(
                context,
                "adopt",
                transaction_id,
                outcome="aborted",
                fields=transaction_fields,
            )
            raise
        _finish_transaction(
            context,
            "adopt",
            transaction_id,
            outcome="changed",
            fields=transaction_fields,
        )
        after = _classification(context, name)
    return {
        "ok": True,
        "command": "adopt",
        "changed": True,
        "dry_run": False,
        "reason": "adopted",
        "target_id": current["target_id"],
        "from": current["classification"],
        "to": after["classification"],
        "skill": after,
    }


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
        transaction_id = uuid.uuid4().hex
        transaction_fields = {
            "provider": context.provider,
            "name": name,
            "target_id": current["target_id"],
            "from_revision": None,
            "to_revision": 1,
            "metadata_sha256": _sha256(_canonical_json(marker)),
        }
        _append_ledger(
            context,
            _transaction_record(
                "create",
                transaction_id,
                outcome="prepared",
                fields=transaction_fields,
            ),
        )
        try:
            _write_marker_exclusive(context, _validate_skill_dir(context, name), marker)
        except ContractError:
            _finish_transaction(
                context,
                "create",
                transaction_id,
                outcome="aborted",
                fields=transaction_fields,
            )
            raise
        _finish_transaction(
            context,
            "create",
            transaction_id,
            outcome="changed",
            fields=transaction_fields,
        )
    return {
        "ok": True,
        "command": "mark-created",
        "changed": True,
        "skill": _classification(context, name),
    }


def _rollback_record(context: Context, name: str) -> dict[str, Any]:
    record = _classification(context, name)
    if record["pinned"]:
        raise ContractError("rollback_denied_pinned")
    if record["base_classification"] != "autosave-managed":
        raise ContractError("rollback_denied_not_autosave_managed")
    marker_path = _validate_skill_dir(context, name) / _AUTOSAVE_MARKER
    try:
        marker = _safe_json_file(marker_path, owner=context.uid, exact_mode=0o600)
    except (ContractError, FileNotFoundError):
        raise ContractError("rollback_denied_unknown_marker_schema") from None
    if (
        type(marker.get("schema_version")) is not int
        or marker["schema_version"] != 2
        or marker.get("created_by") != "ccc-node"
        or marker.get("rollback_eligible") is not True
    ):
        raise ContractError("rollback_denied_unknown_marker_schema")
    return record


def _command_rollback_check(context: Context, name: str) -> dict[str, Any]:
    record = _rollback_record(context, name)
    return {
        "ok": True,
        "command": "rollback-check",
        "allowed": True,
        "name": name,
        "target_id": record["target_id"],
    }


def _command_rollback_archive(context: Context, name: str) -> dict[str, Any]:
    with _MutationLock(context):
        record = _rollback_record(context, name)
        rollback_dir = context.state_dir / _ROLLBACK_DIR
        archive_name = (
            f"{name}.{_now().strftime('%Y%m%d%H%M%S')}.{uuid.uuid4().hex[:8]}"
        )
        transaction_id = uuid.uuid4().hex
        transaction_fields = {
            "provider": context.provider,
            "name": name,
            "target_id": record["target_id"],
            "archive_name_sha256": _sha256(archive_name.encode()),
        }
        _append_ledger(
            context,
            _transaction_record(
                "rollback",
                transaction_id,
                outcome="prepared",
                fields=transaction_fields,
            ),
        )
        _ensure_private_dir(rollback_dir, context)
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_DIRECTORY", 0)
        )
        skills_fd: int | None = None
        rollback_fd: int | None = None
        renamed = False
        durable = True
        try:
            skills_fd = os.open(context.skills_dir, directory_flags)
            rollback_fd = os.open(rollback_dir, directory_flags)
            source = os.stat(name, dir_fd=skills_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(source.st_mode)
                or source.st_uid != context.uid
                or stat.S_IMODE(source.st_mode) & 0o022
            ):
                raise ContractError("rollback_source_changed")
            try:
                os.stat(archive_name, dir_fd=rollback_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ContractError("rollback_archive_exists")
            os.rename(
                name,
                archive_name,
                src_dir_fd=skills_fd,
                dst_dir_fd=rollback_fd,
            )
            renamed = True
            try:
                os.fsync(skills_fd)
                os.fsync(rollback_fd)
            except OSError:
                durable = False
        except ContractError:
            _finish_transaction(
                context,
                "rollback",
                transaction_id,
                outcome="aborted",
                fields=transaction_fields,
            )
            raise
        except OSError:
            _finish_transaction(
                context,
                "rollback",
                transaction_id,
                outcome="aborted",
                fields=transaction_fields,
            )
            raise ContractError("rollback_archive_failed") from None
        finally:
            if skills_fd is not None:
                os.close(skills_fd)
            if rollback_fd is not None:
                os.close(rollback_fd)
        if not renamed:
            raise ContractError("rollback_archive_failed")
        _finish_transaction(
            context,
            "rollback",
            transaction_id,
            outcome="archived" if durable else "archived-durability-uncertain",
            fields=transaction_fields,
        )
    return {
        "ok": True,
        "command": "rollback-archive",
        "changed": True,
        "name": name,
        "archive_path": str(rollback_dir / archive_name),
        "durable": durable,
    }


def _command_pin(context: Context, name: str, pin: bool, dry_run: bool) -> dict[str, Any]:
    verb = "pin" if pin else "unpin"
    before = _classification(context, name)
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
        _preflight_mutation_state(context)
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
        if current["pinned"] == pin:
            return {
                "ok": True,
                "command": verb,
                "changed": False,
                "dry_run": False,
                "reason": "already-pinned" if pin else "already-unpinned",
                "skill": current,
            }
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
        transaction_id = uuid.uuid4().hex
        transaction_fields = {
            "provider": context.provider,
            "name": name,
            "target_id": current["target_id"],
            "from_revision": controls["revision"],
            "to_revision": next_revision,
            "metadata_sha256": _sha256(_canonical_json(updated)),
        }
        _append_ledger(
            context,
            _transaction_record(
                verb,
                transaction_id,
                outcome="prepared",
                fields=transaction_fields,
            ),
        )
        try:
            _write_private_atomic(context.state_dir / _CONTROL_FILE, updated, context)
        except ContractError:
            _finish_transaction(
                context,
                verb,
                transaction_id,
                outcome="aborted",
                fields=transaction_fields,
            )
            raise
        _finish_transaction(
            context,
            verb,
            transaction_id,
            outcome="changed",
            fields=transaction_fields,
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
            "ctime_ns": snapshot.ctime_ns,
        },
        "created_at": _timestamp(created),
        "expires_at": _timestamp(created + timedelta(seconds=_receipt_ttl_seconds())),
        "consumed": False,
    }
    with _MutationLock(context):
        transaction_id = uuid.uuid4().hex
        transaction_fields = {
            "provider": context.provider,
            "name": name,
            "target_id": classification["target_id"],
            "receipt_id": receipt_id,
            "relative_target_sha256": _sha256(snapshot.relative.encode()),
            "content_sha256": snapshot.sha256,
        }
        _append_ledger(
            context,
            _transaction_record(
                "read-receipt",
                transaction_id,
                outcome="prepared",
                fields=transaction_fields,
            ),
        )
        receipt_dir = context.state_dir / _RECEIPT_DIR
        try:
            if not receipt_dir.exists():
                _ensure_private_dir(receipt_dir, context)
            _write_private_atomic(receipt_dir / f"{receipt_id}.json", receipt, context)
        except ContractError:
            _finish_transaction(
                context,
                "read-receipt",
                transaction_id,
                outcome="aborted",
                fields=transaction_fields,
            )
            raise
        _finish_transaction(
            context,
            "read-receipt",
            transaction_id,
            outcome="created",
            fields=transaction_fields,
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
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ContractError("receipt_invalid") from None
    if parsed.tzinfo is None:
        raise ContractError("receipt_invalid")
    return parsed


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


def _validate_proposal_fields(proposal: dict[str, Any]) -> None:
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
    if (
        type(proposal.get("schema_version")) is not int
        or not isinstance(proposal.get("attempt_id"), str)
        or not _ATTEMPT_RE.fullmatch(proposal["attempt_id"])
        or not isinstance(proposal.get("operation"), str)
        or proposal.get("operation") not in _ALLOWED_OPERATIONS
        or not isinstance(proposal.get("provider"), str)
        or proposal.get("provider") not in {"claude", "codex"}
        or not isinstance(proposal.get("name"), str)
        or not _NAME_RE.fullmatch(proposal["name"])
        or not isinstance(proposal.get("target_id"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", proposal["target_id"])
        or not isinstance(proposal.get("relative_target"), str)
        or not isinstance(proposal.get("expected_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", proposal["expected_sha256"])
        or (
            proposal.get("expected_provenance_revision") is not None
            and (
                type(proposal["expected_provenance_revision"]) is not int
                or proposal["expected_provenance_revision"] < 0
            )
        )
        or (
            proposal.get("expected_provenance_sha256") is not None
            and (
                not isinstance(proposal["expected_provenance_sha256"], str)
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    proposal["expected_provenance_sha256"],
                )
            )
        )
    ):
        raise ContractError("proposal_fields_invalid")
    _relative_parts(proposal["relative_target"])


def _validate_receipt_context(
    receipt: dict[str, Any],
    receipt_id: str,
    context: Context,
) -> None:
    if (
        type(receipt.get("schema_version")) is not int
        or receipt["schema_version"] != 1
        or receipt.get("receipt_id") != receipt_id
    ):
        raise ContractError("receipt_invalid")
    if receipt.get("consumed") is not False:
        raise ContractError("receipt_consumed")
    if receipt.get("provider") != context.provider:
        raise ContractError("receipt_context_mismatch")


def _validate_receipt_payload(receipt: dict[str, Any]) -> None:
    required = {
        "attempt_id",
        "operation",
        "provider",
        "name",
        "target_id",
        "relative_target",
        "expected_sha256",
        "expected_provenance_revision",
        "expected_provenance_sha256",
        "file_identity",
        "created_at",
        "expires_at",
    }
    if not required.issubset(receipt):
        raise ContractError("receipt_invalid")
    identity = receipt["file_identity"]
    if (
        not isinstance(receipt["attempt_id"], str)
        or not _ATTEMPT_RE.fullmatch(receipt["attempt_id"])
        or not isinstance(receipt["operation"], str)
        or receipt["operation"] not in _ALLOWED_OPERATIONS
        or not isinstance(receipt["provider"], str)
        or receipt["provider"] not in {"claude", "codex"}
        or not isinstance(receipt["name"], str)
        or not _NAME_RE.fullmatch(receipt["name"])
        or not isinstance(receipt["target_id"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", receipt["target_id"])
        or not isinstance(receipt["relative_target"], str)
        or not isinstance(receipt["expected_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", receipt["expected_sha256"])
        or (
            receipt["expected_provenance_revision"] is not None
            and (
                type(receipt["expected_provenance_revision"]) is not int
                or receipt["expected_provenance_revision"] < 0
            )
        )
        or (
            receipt["expected_provenance_sha256"] is not None
            and (
                not isinstance(receipt["expected_provenance_sha256"], str)
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    receipt["expected_provenance_sha256"],
                )
            )
        )
        or not isinstance(identity, dict)
        or any(
            type(identity.get(field)) is not int or identity[field] < 0
            for field in ("device", "inode", "size", "mtime_ns", "ctime_ns")
        )
    ):
        raise ContractError("receipt_invalid")
    _relative_parts(receipt["relative_target"])
    created = _parse_timestamp(receipt["created_at"])
    expires = _parse_timestamp(receipt["expires_at"])
    if expires <= created:
        raise ContractError("receipt_invalid")


def _command_guard_proposal(context: Context, proposal_path: Path) -> dict[str, Any]:
    proposal = _proposal(proposal_path, context)
    _validate_proposal_fields(proposal)
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
        _validate_receipt_context(receipt, receipt_id, context)
        transaction_id = uuid.uuid4().hex
        outcome = "denied"
        code = "proposal_receipt_mismatch"
        transaction_fields = {
            "provider": context.provider,
            "name": proposal["name"],
            "target_id": proposal["target_id"],
            "receipt_id": receipt_id,
            "operation": proposal["operation"],
        }
        _append_ledger(
            context,
            _transaction_record(
                "write-guard",
                transaction_id,
                outcome="prepared",
                fields=transaction_fields,
            ),
        )
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
            _validate_receipt_payload(receipt)
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
                or snapshot.ctime_ns != identity.get("ctime_ns")
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
            if _now() > _parse_timestamp(receipt.get("expires_at")):
                raise ContractError("receipt_expired")
            outcome = "authorized"
            code = "authorized"
        except ContractError as error:
            code = error.code
        try:
            _consume_receipt(context, receipt_path, receipt, outcome)
        except ContractError:
            _finish_transaction(
                context,
                "write-guard",
                transaction_id,
                outcome="consume-failed",
                fields={**transaction_fields, "code": code},
            )
            raise
        _finish_transaction(
            context,
            "write-guard",
            transaction_id,
            outcome=outcome,
            fields={**transaction_fields, "code": code},
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
    rollback_archive_parser = subparsers.add_parser("rollback-archive")
    rollback_archive_parser.add_argument("name")
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
        elif args.command == "rollback-archive":
            result = _command_rollback_archive(context, args.name)
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
    except (TypeError, ValueError, RecursionError):
        print(json.dumps({"ok": False, "code": "invalid_data"}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.command == "guard-proposal" and not result["allowed"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
