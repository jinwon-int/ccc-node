#!/usr/bin/env python3
"""Fail-closed ownership and read-before-write contract for learned skills."""

from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import errno
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
_PROPOSAL_BACKUP_DIR = "skill-autosave-proposal-backups"
_LOCK_FILE = ".skill-autosave-ownership.lock"
_MAX_JSON_BYTES = 64 * 1024
_MAX_PROPOSAL_BACKUP_BYTES = 2 * 1024 * 1024
_MAX_TARGET_BYTES = 1024 * 1024
_MAX_PATCH_TEXT_BYTES = 32 * 1024
_MAX_SUPPORT_WRITE_BYTES = 48 * 1024
_MAX_SUPPORT_FILES = 16
_MAX_SUPPORT_TOTAL_BYTES = 256 * 1024
_ALLOWED_OPERATIONS = {"patch", "edit", "write_file"}
_SUPPORT_PREFIXES = {"references", "scripts", "templates"}
_DISTILL_TRIGGERS = {
    "new_command",
    "provider_switch",
    "auto_new",
    "explicit",
    "shutdown",
    "checkpoint",
}
_PROPOSAL_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIRECTIVE_RE = re.compile(
    r"(?:<\s*/?\s*(?:system|developer)\s*>|"
    r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+)?"
    r"(?:previous|prior|above|system)\s+(?:instructions?|rules?|prompts?)\b|"
    r"\byou\s+are\s+now\s+(?:the\s+)?system\b|"
    r"\bsystem\s+prompt\s*[:=]|"
    r"\btreat\s+this\s+as\s+(?:a\s+)?(?:system|developer)\s+message\b|"
    r"\breveal\s+(?:the\s+)?(?:system\s+prompt|developer\s+message|"
    r"secrets?|tokens?)\b|"
    r"\btool[- ]?invocation\s+request\b|"
    r"\bdo\s+not\s+follow\s+(?:the\s+)?(?:user|operator)\b)",
    re.IGNORECASE,
)
_CREDENTIAL_RE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b[0-9]{6,12}:[A-Za-z0-9_-]{20,}\b|"
    r"\bbearer\s+[A-Za-z0-9._~+/=-]{20,}|"
    r"\[REDACTED|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|"
    r"passwd|secret|authorization)\s*[:=]\s*[\"']?[A-Za-z0-9+/_-]{12,}|"
    r"\b[A-Za-z0-9+/]{40,}\b)",
    re.IGNORECASE,
)
_NODE_FACT_RE = re.compile(
    r"(?:/(?:root|home|Users)/\S+|"
    r"\b(?!127(?:\.\d{1,3}){3}\b)(?:\d{1,3}\.){3}\d{1,3}\b|"
    r"\b[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\b|"
    r"\b(?:https?|ssh)://\S+)",
    re.IGNORECASE,
)
_CODEX_INCOMPAT_RE = re.compile(
    r"(?:\bclaude\s+-p\b|~/\.claude\b|\bCLAUDE_[A-Z0-9_]+\b)"
)
_RENAME_NOREPLACE = 1


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


@dataclass(frozen=True)
class IncrementalMutationPlan:
    envelope: dict[str, Any]
    proposal: dict[str, Any]
    action: str
    proposal_id: str
    name: str
    relative: str
    classification: dict[str, Any]
    marker_before: dict[str, Any]
    marker_before_sha256: str
    marker_after: dict[str, Any]
    marker_after_sha256: str
    old_payload: bytes | None
    old_snapshot: TargetSnapshot | None
    new_payload: bytes
    revision: int


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


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON key")
        output[key] = value
    return output


def _reject_nonfinite_json(_value: str) -> object:
    raise ValueError("non-finite JSON number")


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
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ):
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
        type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
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
        type(marker.get("schema_version")) is not int
        or marker["schema_version"] != 1
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
    parent = _lstat(path.parent)
    if (
        parent is None
        or not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != context.uid
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise ContractError("unsafe_state_parent")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    parent_fd: int | None = None
    try:
        parent_fd = os.open(path.parent, directory_flags)
        opened_parent = os.fstat(parent_fd)
        if (
            (opened_parent.st_dev, opened_parent.st_ino)
            != (parent.st_dev, parent.st_ino)
            or not stat.S_ISDIR(opened_parent.st_mode)
            or opened_parent.st_uid != context.uid
            or stat.S_IMODE(opened_parent.st_mode) & 0o022
        ):
            raise ContractError("unsafe_state_parent")
        try:
            metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            except OSError:
                raise ContractError("state_directory_create_failed") from None
            metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            os.fsync(parent_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != context.uid
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ContractError("unsafe_state_directory")
    except OSError:
        raise ContractError("state_directory_create_failed") from None
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _prepare_state(context: Context) -> None:
    if context.state_dir.exists():
        _ensure_private_dir(context.state_dir, context)
        return
    parent = context.state_dir.parent
    parent_metadata = _lstat(parent)
    if (
        parent_metadata is None
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or parent_metadata.st_uid != context.uid
        or stat.S_IMODE(parent_metadata.st_mode) & 0o022
    ):
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
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory_fd: int | None = None
    descriptor: int | None = None
    try:
        directory_fd = os.open(context.state_dir, directory_flags)
        directory = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != context.uid
            or stat.S_IMODE(directory.st_mode) != 0o700
        ):
            raise ContractError("unsafe_state_directory")
        descriptor = os.open(_LEDGER_FILE, flags, 0o600, dir_fd=directory_fd)
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
        os.fsync(directory_fd)
    except OSError:
        raise ContractError("ownership_ledger_write_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_fd is not None:
            os.close(directory_fd)


def _read_ledger(context: Context) -> list[dict[str, Any]]:
    """Read the body-free ownership ledger for idempotency/cap accounting."""

    _preflight_ledger(context)
    path = context.state_dir / _LEDGER_FILE
    metadata = _lstat(path)
    if metadata is None:
        return []
    if metadata.st_size > 8 * 1024 * 1024:
        raise ContractError("ownership_ledger_too_large")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != context.uid
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise ContractError("unsafe_ownership_ledger")
        payload = b""
        while len(payload) <= 8 * 1024 * 1024:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            payload += chunk
        after = os.fstat(descriptor)
        if (
            len(payload) > 8 * 1024 * 1024
            or (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            raise ContractError("ownership_ledger_changed")
    finally:
        os.close(descriptor)
    rows: list[dict[str, Any]] = []
    try:
        for line in payload.decode("utf-8").splitlines():
            if not line:
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError
            rows.append(row)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ContractError("ownership_ledger_invalid") from None
    return rows


def _transaction_record(
    event: str,
    transaction_id: str,
    *,
    outcome: str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    return {
        **fields,
        "schema_version": 1,
        "event": event,
        "transaction_id": transaction_id,
        "ts": _timestamp(),
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
    if (
        type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
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


def _bounded_proposal_text(
    proposal: dict[str, Any],
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> str:
    value = proposal.get(field)
    if (
        not isinstance(value, str)
        or len(value) < minimum
        or len(value) > maximum
        or len(value.encode("utf-8")) > maximum
    ):
        raise ContractError("incremental_proposal_invalid")
    return value


def _validate_incremental_provenance(
    envelope: dict[str, Any],
    context: Context,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if set(envelope) != {"schema_version", "proposal_id", "provenance", "proposal"}:
        raise ContractError("incremental_proposal_fields_invalid")
    proposal_id = envelope.get("proposal_id")
    provenance = envelope.get("provenance")
    proposal = envelope.get("proposal")
    if (
        type(envelope.get("schema_version")) is not int
        or envelope["schema_version"] != 2
        or not isinstance(proposal_id, str)
        or not _PROPOSAL_ID_RE.fullmatch(proposal_id)
        or not isinstance(provenance, dict)
        or set(provenance)
        != {"provider", "source_thread_hash", "trigger", "distilled_at"}
        or provenance.get("provider") != context.provider
        or not isinstance(provenance.get("source_thread_hash"), str)
        or not _SHA256_RE.fullmatch(provenance["source_thread_hash"])
        or provenance.get("trigger") not in _DISTILL_TRIGGERS
        or not isinstance(provenance.get("distilled_at"), str)
        or not isinstance(proposal, dict)
    ):
        raise ContractError("incremental_proposal_invalid")
    try:
        distilled_at = datetime.fromisoformat(
            provenance["distilled_at"].replace("Z", "+00:00")
        )
    except ValueError:
        raise ContractError("incremental_proposal_invalid") from None
    if distilled_at.tzinfo is None:
        raise ContractError("incremental_proposal_invalid")
    return proposal_id, provenance, proposal


def _validate_incremental_target(
    proposal: dict[str, Any],
    action: str,
) -> None:
    name = proposal.get("target_skill")
    relative = proposal.get("relative_target")
    if (
        not isinstance(name, str)
        or not _NAME_RE.fullmatch(name)
        or not isinstance(relative, str)
        or len(relative.encode("utf-8")) > 240
    ):
        raise ContractError("incremental_target_invalid")
    parts = _relative_parts(relative)
    support_target = (
        relative != "SKILL.md"
        and len(parts) >= 2
        and parts[0] in _SUPPORT_PREFIXES
    )
    if (
        relative != "/".join(parts)
        or any(not _SAFE_PATH_COMPONENT_RE.fullmatch(part) for part in parts)
        or (relative != "SKILL.md" and not support_target)
        or (action == "write_file" and relative == "SKILL.md")
    ):
        raise ContractError("incremental_target_invalid")
    _bounded_proposal_text(
        proposal,
        "improvement_reason",
        minimum=1,
        maximum=600,
    )


def _validate_patch_proposal(proposal: dict[str, Any]) -> None:
    expected = proposal.get("expected_sha256")
    if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
        raise ContractError("incremental_patch_invalid")
    old_text = _bounded_proposal_text(
        proposal,
        "old_text",
        minimum=1,
        maximum=_MAX_PATCH_TEXT_BYTES,
    )
    new_text = _bounded_proposal_text(
        proposal,
        "new_text",
        minimum=0,
        maximum=_MAX_PATCH_TEXT_BYTES,
    )
    if (
        len(old_text.encode("utf-8")) > _MAX_PATCH_TEXT_BYTES
        or len(new_text.encode("utf-8")) > _MAX_PATCH_TEXT_BYTES
    ):
        raise ContractError("incremental_content_too_large")


def _validate_write_file_proposal(proposal: dict[str, Any]) -> None:
    revision = proposal.get("expected_provenance_revision")
    provenance_sha = proposal.get("expected_provenance_sha256")
    if (
        proposal.get("expected_absent") is not True
        or type(revision) is not int
        or revision < 0
        or not isinstance(provenance_sha, str)
        or not _SHA256_RE.fullmatch(provenance_sha)
    ):
        raise ContractError("incremental_write_file_invalid")
    _bounded_proposal_text(
        proposal,
        "content",
        minimum=1,
        maximum=_MAX_SUPPORT_WRITE_BYTES,
    )


def _validate_create_proposal(proposal: dict[str, Any]) -> None:
    name = proposal.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ContractError("incremental_create_invalid")
    _bounded_proposal_text(proposal, "summary", minimum=1, maximum=600)
    skill_md = _bounded_proposal_text(
        proposal,
        "skill_md",
        minimum=1,
        maximum=16 * 1024,
    )
    _validate_skill_md_structure(name, skill_md.encode("utf-8"))


def _validate_incremental_action(proposal: dict[str, Any]) -> str:
    common = {"action", "reason", "evidence_excerpt"}
    action_fields = {
        "create": common | {"name", "summary", "skill_md"},
        "noop": common,
        "patch": common
        | {
            "target_skill",
            "relative_target",
            "expected_sha256",
            "old_text",
            "new_text",
            "improvement_reason",
        },
        "write_file": common
        | {
            "target_skill",
            "relative_target",
            "expected_absent",
            "expected_provenance_revision",
            "expected_provenance_sha256",
            "content",
            "improvement_reason",
        },
    }
    action = proposal.get("action")
    if not isinstance(action, str) or action not in action_fields:
        raise ContractError("incremental_action_invalid")
    if set(proposal) != action_fields[action]:
        raise ContractError("incremental_action_fields_invalid")
    _bounded_proposal_text(proposal, "reason", minimum=1, maximum=600)
    _bounded_proposal_text(
        proposal,
        "evidence_excerpt",
        minimum=0,
        maximum=200,
    )
    if action in {"patch", "write_file"}:
        _validate_incremental_target(proposal, action)
    if action == "patch":
        _validate_patch_proposal(proposal)
    elif action == "write_file":
        _validate_write_file_proposal(proposal)
    elif action == "create":
        _validate_create_proposal(proposal)
    return action


def _incremental_proposal(path: Path, context: Context) -> dict[str, Any]:
    envelope = _safe_json_file(
        path,
        owner=context.uid,
        exact_mode=0o600,
        max_bytes=_MAX_JSON_BYTES,
    )
    _proposal_id, provenance, proposal = _validate_incremental_provenance(
        envelope,
        context,
    )
    _validate_incremental_action(proposal)
    skipped_gate_fields = {
        "action",
        "target_skill",
        "relative_target",
        "expected_sha256",
        "expected_provenance_sha256",
        "name",
    }
    for field, value in proposal.items():
        if isinstance(value, str) and field not in skipped_gate_fields:
            _gate_incremental_content(value, context)
    canonical = _canonical_json(
        {
            "schema_version": envelope["schema_version"],
            "provenance": provenance,
            "proposal": proposal,
        }
    )
    envelope["_canonical_sha256"] = _sha256(canonical)
    return envelope


def _gate_incremental_content(value: str, context: Context) -> None:
    if _DIRECTIVE_RE.search(value):
        raise ContractError("incremental_content_injection")
    if _CREDENTIAL_RE.search(value):
        raise ContractError("incremental_content_secret")
    if _NODE_FACT_RE.search(value):
        raise ContractError("incremental_content_node_fact")
    if context.provider == "codex" and _CODEX_INCOMPAT_RE.search(value):
        raise ContractError("incremental_content_provider_incompatible")


def _command_validate_incremental_proposal(
    context: Context,
    proposal_path: Path,
) -> dict[str, Any]:
    envelope = _incremental_proposal(proposal_path, context)
    proposal = envelope["proposal"]
    assert isinstance(proposal, dict)
    action = proposal["action"]
    return {
        "ok": True,
        "command": "validate-proposal",
        "proposal_id": envelope["proposal_id"],
        "action": action,
        "name": proposal.get("name") or proposal.get("target_skill"),
        "relative_target": proposal.get("relative_target"),
        "proposal_sha256": envelope["_canonical_sha256"],
    }


def _rename_noreplace(
    parent_fd: int,
    source: str,
    destination: str,
) -> None:
    """Move one entry without ever replacing an existing destination."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ContractError("incremental_atomic_noreplace_unsupported")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(destination)
    if error_number == errno.ENOENT:
        raise FileNotFoundError(source)
    if error_number in {errno.ENOSYS, errno.EINVAL}:
        raise ContractError("incremental_atomic_noreplace_unsupported")
    raise OSError(error_number, "renameat2 failed")


def _claimed_target_matches(
    parent_fd: int,
    name: str,
    expected: TargetSnapshot,
    *,
    uid: int,
) -> bool:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != uid
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size > _MAX_TARGET_BYTES
            or (before.st_dev, before.st_ino)
            != (expected.device, expected.inode)
        ):
            return False
        chunks: list[bytes] = []
        remaining = _MAX_TARGET_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        return (
            len(payload) <= _MAX_TARGET_BYTES
            and _sha256(payload) == expected.sha256
            and (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            == (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        )
    finally:
        os.close(descriptor)


@contextmanager
def _target_parent_fd(
    context: Context, name: str, relative: str
) -> Any:
    skill_dir = _validate_skill_dir(context, name)
    parts = _relative_parts(relative)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    descriptors: list[int] = []
    try:
        current = os.open(skill_dir, directory_flags)
        descriptors.append(current)
        for component in parts[:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            metadata = os.fstat(current)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != context.uid
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise ContractError("unsafe_target_directory")
            descriptors.append(current)
        yield current, parts[-1], os.fstat(current)
    except FileNotFoundError:
        raise ContractError("target_parent_missing") from None
    except OSError:
        raise ContractError("unsafe_target_path") from None
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _support_stats(context: Context, name: str) -> tuple[int, int]:
    skill_dir = _validate_skill_dir(context, name)
    count = 0
    total = 0
    for prefix in sorted(_SUPPORT_PREFIXES):
        root = skill_dir / prefix
        metadata = _lstat(root)
        if metadata is None:
            continue
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != context.uid
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise ContractError("unsafe_support_tree")
        for current, directories, files in os.walk(root, followlinks=False):
            directories.sort()
            files.sort()
            for directory in directories:
                child = _lstat(Path(current) / directory)
                if (
                    child is None
                    or not stat.S_ISDIR(child.st_mode)
                    or stat.S_ISLNK(child.st_mode)
                    or child.st_uid != context.uid
                    or stat.S_IMODE(child.st_mode) & 0o022
                ):
                    raise ContractError("unsafe_support_tree")
            for filename in files:
                child = _lstat(Path(current) / filename)
                if (
                    child is None
                    or not stat.S_ISREG(child.st_mode)
                    or stat.S_ISLNK(child.st_mode)
                    or child.st_uid != context.uid
                    or child.st_nlink != 1
                    or stat.S_IMODE(child.st_mode) & 0o022
                ):
                    raise ContractError("unsafe_support_tree")
                count += 1
                total += child.st_size
    return count, total


def _write_temporary_target(
    parent_fd: int,
    name: str,
    payload: bytes,
    *,
    mode: int,
) -> tuple[int, int]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(name, flags, mode, dir_fd=parent_fd)
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        return metadata.st_dev, metadata.st_ino
    finally:
        os.close(descriptor)


def _unlink_if_identity(
    parent_fd: int,
    name: str,
    identity: tuple[int, int] | None,
) -> None:
    if identity is None:
        return
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == identity:
            os.unlink(name, dir_fd=parent_fd)
    except OSError:
        pass


def _replace_target_no_clobber(
    context: Context,
    name: str,
    relative: str,
    *,
    expected: TargetSnapshot,
    payload: bytes,
    mode: int,
) -> None:
    nonce = uuid.uuid4().hex
    temporary = f".ccc-skill-proposal.{nonce}"
    previous = f".ccc-skill-previous.{nonce}"
    temporary_identity: tuple[int, int] | None = None
    previous_claimed = False
    published = False
    with _target_parent_fd(context, name, relative) as (parent_fd, leaf, _parent):
        try:
            temporary_identity = _write_temporary_target(
                parent_fd,
                temporary,
                payload,
                mode=mode,
            )
            try:
                _rename_noreplace(parent_fd, leaf, previous)
            except FileNotFoundError:
                raise ContractError("target_drift")
            except FileExistsError:
                raise ContractError("incremental_write_failed") from None
            previous_claimed = True
            if not _claimed_target_matches(
                parent_fd,
                previous,
                expected,
                uid=context.uid,
            ):
                try:
                    _rename_noreplace(parent_fd, previous, leaf)
                except FileExistsError:
                    raise ContractError("incremental_rollback_conflict") from None
                previous_claimed = False
                os.fsync(parent_fd)
                raise ContractError("target_drift")
            try:
                _rename_noreplace(parent_fd, temporary, leaf)
            except (FileExistsError, FileNotFoundError):
                raise ContractError("incremental_rollback_conflict") from None
            published = True
            current = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_uid != context.uid
                or current.st_nlink != 1
                or (current.st_dev, current.st_ino) != temporary_identity
                or not _claimed_target_matches(
                    parent_fd,
                    previous,
                    expected,
                    uid=context.uid,
                )
            ):
                raise ContractError("incremental_rollback_conflict")
            os.unlink(previous, dir_fd=parent_fd)
            previous_claimed = False
            os.fsync(parent_fd)
        except ContractError:
            raise
        except OSError:
            raise ContractError("incremental_write_failed") from None
        finally:
            _unlink_if_identity(parent_fd, temporary, temporary_identity)
            if previous_claimed and not published:
                try:
                    _rename_noreplace(parent_fd, previous, leaf)
                    os.fsync(parent_fd)
                except (ContractError, OSError):
                    pass


def _remove_target_if_snapshot(
    context: Context,
    name: str,
    relative: str,
    *,
    expected: TargetSnapshot,
) -> None:
    """Remove only the exact inspected entry, preserving any raced replacement."""

    quarantine = f".ccc-skill-remove.{uuid.uuid4().hex}"
    claimed = False
    with _target_parent_fd(context, name, relative) as (parent_fd, leaf, _parent):
        try:
            try:
                _rename_noreplace(parent_fd, leaf, quarantine)
            except FileNotFoundError:
                raise ContractError("target_drift") from None
            except FileExistsError:
                raise ContractError("incremental_write_failed") from None
            claimed = True
            if not _claimed_target_matches(
                parent_fd,
                quarantine,
                expected,
                uid=context.uid,
            ):
                try:
                    _rename_noreplace(parent_fd, quarantine, leaf)
                except FileExistsError:
                    raise ContractError("incremental_rollback_conflict") from None
                claimed = False
                os.fsync(parent_fd)
                raise ContractError("target_drift")
            os.unlink(quarantine, dir_fd=parent_fd)
            claimed = False
            os.fsync(parent_fd)
        except ContractError:
            raise
        except OSError:
            raise ContractError("incremental_write_failed") from None
        finally:
            if claimed:
                try:
                    _rename_noreplace(parent_fd, quarantine, leaf)
                    os.fsync(parent_fd)
                except (ContractError, OSError):
                    pass


def _create_target_noreplace(
    context: Context, name: str, relative: str, payload: bytes
) -> None:
    temporary = f".ccc-skill-proposal.{uuid.uuid4().hex}"
    temporary_identity: tuple[int, int] | None = None
    with _target_parent_fd(context, name, relative) as (parent_fd, leaf, parent_before):
        try:
            try:
                os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ContractError("target_already_exists")
            temporary_identity = _write_temporary_target(
                parent_fd,
                temporary,
                payload,
                mode=0o600,
            )
            with _target_parent_fd(context, name, relative) as (
                current_parent_fd,
                _current_leaf,
                current_parent,
            ):
                if (current_parent.st_dev, current_parent.st_ino) != (
                    parent_before.st_dev,
                    parent_before.st_ino,
                ):
                    raise ContractError("target_parent_drift")
            try:
                os.link(
                    temporary,
                    leaf,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raise ContractError("target_already_exists") from None
            linked = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if (linked.st_dev, linked.st_ino) != temporary_identity:
                raise ContractError("incremental_rollback_conflict")
            with _target_parent_fd(context, name, relative) as (
                current_parent_fd,
                _current_leaf,
                current_parent,
            ):
                if (current_parent.st_dev, current_parent.st_ino) != (
                    parent_before.st_dev,
                    parent_before.st_ino,
                ):
                    os.fsync(parent_fd)
                    raise ContractError("target_parent_drift")
            linked = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if (linked.st_dev, linked.st_ino) != temporary_identity:
                raise ContractError("incremental_rollback_conflict")
            os.unlink(temporary, dir_fd=parent_fd)
            temporary_identity = None
            os.fsync(parent_fd)
        except ContractError:
            raise
        except OSError:
            raise ContractError("incremental_write_failed") from None
        finally:
            _unlink_if_identity(parent_fd, temporary, temporary_identity)


def _write_existing_json_in_skill(
    context: Context,
    name: str,
    relative: str,
    value: dict[str, Any],
) -> None:
    before = _read_target(context, name, relative)
    metadata = _lstat(context.skills_dir / name / relative)
    if metadata is None:
        raise ContractError("target_missing")
    _replace_target_no_clobber(
        context,
        name,
        relative,
        expected=before,
        payload=_canonical_json(value),
        mode=stat.S_IMODE(metadata.st_mode),
    )


def _proposal_ledger_state(
    rows: list[dict[str, Any]], proposal_id: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    latest_prepared: dict[str, Any] | None = None
    terminal_by_transaction: dict[str, dict[str, Any]] = {}
    applied: dict[str, Any] | None = None
    for row in rows:
        if (
            row.get("event") != "skill-proposal-apply"
            or row.get("proposal_id") != proposal_id
        ):
            continue
        if row.get("outcome") == "prepared":
            latest_prepared = row
        else:
            transaction_id = row.get("transaction_id")
            if isinstance(transaction_id, str):
                terminal_by_transaction[transaction_id] = row
            if row.get("outcome") == "applied":
                applied = row
    if applied is not None:
        return latest_prepared, applied
    if latest_prepared is None:
        return None, None
    transaction_id = latest_prepared.get("transaction_id")
    terminal = (
        terminal_by_transaction.get(transaction_id)
        if isinstance(transaction_id, str)
        else None
    )
    return latest_prepared, terminal


def _automatic_cap_used(rows: list[dict[str, Any]], cap_day: str) -> int:
    states: dict[str, str] = {}
    for row in rows:
        proposal_id = row.get("proposal_id")
        outcome = row.get("outcome")
        if (
            row.get("event") == "skill-proposal-apply"
            and row.get("automatic") is True
            and row.get("cap_day") == cap_day
            and isinstance(proposal_id, str)
            and isinstance(outcome, str)
        ):
            states[proposal_id] = outcome
    return sum(outcome in {"prepared", "applied"} for outcome in states.values())


def _command_automatic_usage(
    context: Context,
    day: str | None,
) -> dict[str, Any]:
    cap_day = day or _now().date().isoformat()
    try:
        parsed = datetime.strptime(cap_day, "%Y-%m-%d")
    except ValueError:
        raise ContractError("incremental_cap_day_invalid") from None
    if parsed.strftime("%Y-%m-%d") != cap_day:
        raise ContractError("incremental_cap_day_invalid")
    rows = _read_ledger(context)
    return {
        "ok": True,
        "command": "automatic-usage",
        "day": cap_day,
        "used": _automatic_cap_used(rows, cap_day),
    }


def _transaction_fields_from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key not in {"schema_version", "event", "transaction_id", "ts", "outcome"}
    }


def _append_recovery_terminal(
    context: Context,
    prepared: dict[str, Any],
    *,
    outcome: str,
) -> None:
    transaction_id = prepared.get("transaction_id")
    if not isinstance(transaction_id, str) or not transaction_id:
        raise ContractError("incremental_recovery_invalid")
    _append_ledger(
        context,
        _transaction_record(
            "skill-proposal-apply",
            transaction_id,
            outcome=outcome,
            fields=_transaction_fields_from_record(prepared),
        ),
    )


def _incremental_target_state(
    context: Context,
    backup: dict[str, Any],
) -> str:
    name = backup["name"]
    relative = backup["relative_target"]
    assert isinstance(name, str) and isinstance(relative, str)
    if backup["old_absent"] is True:
        with _target_parent_fd(context, name, relative) as (parent_fd, leaf, _parent):
            try:
                os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return "before"
        current = _read_target(context, name, relative)
        return "after" if current.sha256 == backup["new_sha256"] else "conflict"
    current = _read_target(context, name, relative)
    if current.sha256 == backup["new_sha256"]:
        return "after"
    if current.sha256 == backup["old_sha256"]:
        return "before"
    return "conflict"


def _incremental_marker_state(
    context: Context,
    backup: dict[str, Any],
) -> str:
    marker = _safe_json_file(
        context.skills_dir / backup["name"] / _AUTOSAVE_MARKER,
        owner=context.uid,
        exact_mode=0o600,
    )
    digest = _sha256(_canonical_json(marker))
    if digest == backup["marker_after_sha256"]:
        return "after"
    if digest == backup["marker_before_sha256"]:
        return "before"
    return "conflict"


def _recover_incremental_transaction(
    context: Context,
    prepared: dict[str, Any],
    envelope: dict[str, Any],
) -> str:
    """Finish an interrupted prepared transaction while holding _MutationLock.

    Returns ``applied`` when durable mutation already happened (or is safely
    completed), and ``aborted`` when no mutation happened and a fresh attempt
    may proceed. Any ambiguous state is recorded as conflict and fails closed.
    """

    proposal_id = envelope["proposal_id"]
    assert isinstance(proposal_id, str)
    backup_path = context.state_dir / _PROPOSAL_BACKUP_DIR / f"{proposal_id}.json"
    try:
        backup = _safe_json_file(
            backup_path,
            owner=context.uid,
            exact_mode=0o600,
            max_bytes=_MAX_PROPOSAL_BACKUP_BYTES,
        )
    except FileNotFoundError:
        raise ContractError("incremental_recovery_backup_missing") from None
    required = {
        "schema_version",
        "proposal_id",
        "proposal_sha256",
        "provider",
        "name",
        "relative_target",
        "action",
        "old_absent",
        "old_content_base64",
        "old_sha256",
        "new_sha256",
        "marker_before",
        "marker_before_sha256",
        "marker_after",
        "marker_after_sha256",
        "created_at",
    }
    old_payload: bytes | None = None
    if backup.get("old_absent") is False:
        encoded = backup.get("old_content_base64")
        if not isinstance(encoded, str):
            raise ContractError("incremental_recovery_backup_invalid")
        try:
            old_payload = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ContractError("incremental_recovery_backup_invalid") from None
    marker_before = backup.get("marker_before")
    marker_after = backup.get("marker_after")
    if (
        set(backup) != required
        or backup.get("schema_version") != 1
        or backup.get("proposal_id") != proposal_id
        or backup.get("proposal_sha256") != envelope["_canonical_sha256"]
        or backup.get("provider") != context.provider
        or backup.get("proposal_sha256") != prepared.get("proposal_sha256")
        or backup.get("name") != prepared.get("name")
        or backup.get("action") != prepared.get("action")
        or not isinstance(backup.get("relative_target"), str)
        or _sha256(backup["relative_target"].encode())
        != prepared.get("relative_target_sha256")
        or not isinstance(backup.get("new_sha256"), str)
        or not _SHA256_RE.fullmatch(backup["new_sha256"])
        or backup.get("old_absent") not in {True, False}
        or (
            backup.get("old_absent") is True
            and (
                backup.get("old_content_base64") is not None
                or backup.get("old_sha256") is not None
            )
        )
        or (
            backup.get("old_absent") is False
            and (
                old_payload is None
                or backup.get("old_sha256") != _sha256(old_payload)
            )
        )
        or not isinstance(marker_before, dict)
        or not isinstance(marker_after, dict)
        or _sha256(_canonical_json(marker_before))
        != backup.get("marker_before_sha256")
        or _sha256(_canonical_json(marker_after))
        != backup.get("marker_after_sha256")
        or marker_before.get("provider") != context.provider
        or marker_after.get("provider") != context.provider
        or marker_before.get("name") != backup.get("name")
        or marker_after.get("name") != backup.get("name")
        or marker_before.get("target_id") != prepared.get("target_id")
        or marker_after.get("target_id") != prepared.get("target_id")
        or marker_before.get("provenance_revision") != prepared.get("from_revision")
        or marker_after.get("provenance_revision") != prepared.get("to_revision")
        or marker_after.get("last_mutation_sha256")
        != envelope["_canonical_sha256"]
    ):
        raise ContractError("incremental_recovery_backup_invalid")
    target_state = _incremental_target_state(context, backup)
    marker_state = _incremental_marker_state(context, backup)
    if target_state == "after" and marker_state == "before":
        _write_existing_json_in_skill(
            context,
            backup["name"],
            _AUTOSAVE_MARKER,
            backup["marker_after"],
        )
        marker_state = "after"
    elif target_state == "before" and marker_state == "after":
        _write_existing_json_in_skill(
            context,
            backup["name"],
            _AUTOSAVE_MARKER,
            backup["marker_before"],
        )
        marker_state = "before"
    if target_state == "after" and marker_state == "after":
        _append_recovery_terminal(context, prepared, outcome="applied")
        return "applied"
    if target_state == "before" and marker_state == "before":
        _append_recovery_terminal(context, prepared, outcome="aborted")
        return "aborted"
    _append_recovery_terminal(context, prepared, outcome="conflict")
    raise ContractError("incremental_recovery_conflict")


def _validate_skill_md_structure(name: str, payload: bytes) -> None:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ContractError("incremental_content_non_utf8") from None
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ContractError("incremental_skill_structure_invalid")
    try:
        closing = lines.index("---", 1)
    except ValueError:
        raise ContractError("incremental_skill_structure_invalid") from None
    frontmatter = lines[1:closing]
    name_values = [
        line.split(":", 1)[1].strip().strip("\"'")
        for line in frontmatter
        if line.startswith("name:")
    ]
    descriptions = [
        line.split(":", 1)[1].strip()
        for line in frontmatter
        if line.startswith("description:")
    ]
    if name_values != [name] or len(descriptions) != 1 or not descriptions[0]:
        raise ContractError("incremental_skill_structure_invalid")


def _idempotent_apply_result(
    action: str,
    proposal_id: str,
    record: dict[str, Any],
    *,
    recovered: bool,
) -> dict[str, Any]:
    result = {
        "ok": True,
        "command": "apply-proposal",
        "action": action,
        "changed": False,
        "idempotent": True,
        "counted": record.get("automatic") is True,
        "proposal_id": proposal_id,
        "code": "recovered_applied" if recovered else "already_applied",
    }
    if recovered:
        result["recovered"] = True
    return result


def _replay_incremental_apply(
    context: Context,
    envelope: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    action: str,
    proposal_id: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    prepared, terminal = _proposal_ledger_state(rows, proposal_id)
    if terminal is not None and terminal.get("outcome") == "applied":
        return (
            _idempotent_apply_result(
                action,
                proposal_id,
                terminal,
                recovered=False,
            ),
            rows,
        )
    if terminal is not None and terminal.get("outcome") == "conflict":
        raise ContractError("incremental_prior_conflict")
    if prepared is None or terminal is not None:
        return None, rows
    recovery = _recover_incremental_transaction(context, prepared, envelope)
    if recovery == "applied":
        return (
            _idempotent_apply_result(
                action,
                proposal_id,
                prepared,
                recovered=True,
            ),
            rows,
        )
    return None, _read_ledger(context)


def _patch_plan_payload(
    context: Context,
    proposal: dict[str, Any],
    *,
    name: str,
    relative: str,
) -> tuple[TargetSnapshot, bytes, bytes]:
    snapshot = _read_target(context, name, relative)
    if snapshot.sha256 != proposal["expected_sha256"]:
        raise ContractError("target_drift")
    old_bytes = proposal["old_text"].encode("utf-8")
    if snapshot.content.count(old_bytes) != 1:
        raise ContractError("patch_match_not_unique")
    new_payload = snapshot.content.replace(
        old_bytes,
        proposal["new_text"].encode("utf-8"),
        1,
    )
    return snapshot, snapshot.content, new_payload


def _write_plan_payload(
    context: Context,
    proposal: dict[str, Any],
    classification: dict[str, Any],
    *,
    name: str,
    relative: str,
) -> bytes:
    if (
        classification["provenance_revision"]
        != proposal["expected_provenance_revision"]
        or classification["provenance_sha256"]
        != proposal["expected_provenance_sha256"]
    ):
        raise ContractError("provenance_drift")
    with _target_parent_fd(context, name, relative) as (parent_fd, leaf, _parent):
        try:
            os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ContractError("target_already_exists")
    return proposal["content"].encode("utf-8")


def _validate_incremental_output(
    context: Context,
    *,
    name: str,
    relative: str,
    old_snapshot: TargetSnapshot | None,
    new_payload: bytes,
    enforce_support_caps: bool = True,
) -> None:
    try:
        new_text = new_payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ContractError("incremental_content_non_utf8") from None
    _gate_incremental_content(new_text, context)
    if relative == "SKILL.md":
        _validate_skill_md_structure(name, new_payload)
        return
    if not enforce_support_caps:
        return
    support_count, support_bytes = _support_stats(context, name)
    old_size = old_snapshot.size if old_snapshot is not None else 0
    projected_count = support_count + (1 if old_snapshot is None else 0)
    projected_bytes = support_bytes - old_size + len(new_payload)
    if (
        projected_count > _MAX_SUPPORT_FILES
        or projected_bytes > _MAX_SUPPORT_TOTAL_BYTES
    ):
        raise ContractError("support_tree_cap_exceeded")


def _build_incremental_plan(
    context: Context,
    envelope: dict[str, Any],
    *,
    automatic: bool,
) -> IncrementalMutationPlan:
    proposal = envelope["proposal"]
    assert isinstance(proposal, dict)
    action = proposal["action"]
    proposal_id = envelope["proposal_id"]
    name = proposal["target_skill"]
    relative = proposal["relative_target"]
    assert isinstance(action, str)
    assert isinstance(proposal_id, str)
    assert isinstance(name, str) and isinstance(relative, str)
    classification = _classification(context, name)
    if not classification["autonomous_write_allowed"]:
        reason = classification["classification"].replace("/", "_")
        raise ContractError(f"autonomous_write_denied_{reason}")
    marker_before = _safe_json_file(
        context.skills_dir / name / _AUTOSAVE_MARKER,
        owner=context.uid,
        exact_mode=0o600,
    )
    if automatic and marker_before.get("rollback_eligible") is not True:
        raise ContractError("incremental_auto_rollback_unavailable")
    old_snapshot: TargetSnapshot | None
    old_payload: bytes | None
    if action == "patch":
        old_snapshot, old_payload, new_payload = _patch_plan_payload(
            context,
            proposal,
            name=name,
            relative=relative,
        )
    else:
        old_snapshot = None
        old_payload = None
        new_payload = _write_plan_payload(
            context,
            proposal,
            classification,
            name=name,
            relative=relative,
        )
    _validate_incremental_output(
        context,
        name=name,
        relative=relative,
        old_snapshot=old_snapshot,
        new_payload=new_payload,
    )
    revision = classification["provenance_revision"]
    if type(revision) is not int:
        raise ContractError("provenance_drift")
    marker_after = dict(marker_before)
    marker_after["provenance_revision"] = revision + 1
    marker_after["updated_at"] = _timestamp()
    marker_after["last_mutation_sha256"] = envelope["_canonical_sha256"]
    if relative == "SKILL.md":
        marker_after["skill_sha256"] = _sha256(new_payload)
    return IncrementalMutationPlan(
        envelope=envelope,
        proposal=proposal,
        action=action,
        proposal_id=proposal_id,
        name=name,
        relative=relative,
        classification=classification,
        marker_before=marker_before,
        marker_before_sha256=_sha256(_canonical_json(marker_before)),
        marker_after=marker_after,
        marker_after_sha256=_sha256(_canonical_json(marker_after)),
        old_payload=old_payload,
        old_snapshot=old_snapshot,
        new_payload=new_payload,
        revision=revision,
    )


def _dry_run_incremental_result(plan: IncrementalMutationPlan) -> dict[str, Any]:
    return {
        "ok": True,
        "command": "apply-proposal",
        "action": plan.action,
        "changed": False,
        "dry_run": True,
        "proposal_id": plan.proposal_id,
        "target": {
            "name": plan.name,
            "relative_target": plan.relative,
        },
        "expected_sha256": (
            plan.old_snapshot.sha256
            if plan.old_snapshot is not None
            else None
        ),
        "new_sha256": _sha256(plan.new_payload),
    }


def _incremental_backup(
    context: Context,
    plan: IncrementalMutationPlan,
) -> dict[str, Any]:
    backup = {
        "schema_version": 1,
        "proposal_id": plan.proposal_id,
        "proposal_sha256": plan.envelope["_canonical_sha256"],
        "provider": context.provider,
        "name": plan.name,
        "relative_target": plan.relative,
        "action": plan.action,
        "old_absent": plan.old_payload is None,
        "old_content_base64": (
            base64.b64encode(plan.old_payload).decode("ascii")
            if plan.old_payload is not None
            else None
        ),
        "old_sha256": (
            _sha256(plan.old_payload)
            if plan.old_payload is not None
            else None
        ),
        "new_sha256": _sha256(plan.new_payload),
        "marker_before": plan.marker_before,
        "marker_before_sha256": plan.marker_before_sha256,
        "marker_after": plan.marker_after,
        "marker_after_sha256": plan.marker_after_sha256,
        "created_at": _timestamp(),
    }
    backup_path = (
        context.state_dir
        / _PROPOSAL_BACKUP_DIR
        / f"{plan.proposal_id}.json"
    )
    _write_private_atomic(backup_path, backup, context)
    return backup


def _incremental_transaction_fields(
    context: Context,
    plan: IncrementalMutationPlan,
    backup: dict[str, Any],
    *,
    automatic: bool,
    cap_day: str,
    cap_slot: int | None,
) -> dict[str, Any]:
    return {
        "provider": context.provider,
        "name": plan.name,
        "target_id": plan.classification["target_id"],
        "proposal_id": plan.proposal_id,
        "proposal_sha256": plan.envelope["_canonical_sha256"],
        "action": plan.action,
        "relative_target_sha256": _sha256(plan.relative.encode()),
        "old_sha256": backup["old_sha256"],
        "new_sha256": backup["new_sha256"],
        "from_revision": plan.revision,
        "to_revision": plan.revision + 1,
        "automatic": automatic,
        "cap_day": cap_day if automatic else None,
        "cap_slot": cap_slot,
    }


def _publish_incremental_plan(
    context: Context,
    plan: IncrementalMutationPlan,
) -> None:
    if plan.action == "patch":
        assert plan.old_snapshot is not None
        metadata = _lstat(context.skills_dir / plan.name / plan.relative)
        if metadata is None:
            raise ContractError("target_missing")
        _replace_target_no_clobber(
            context,
            plan.name,
            plan.relative,
            expected=plan.old_snapshot,
            payload=plan.new_payload,
            mode=stat.S_IMODE(metadata.st_mode),
        )
    else:
        latest = _classification(context, plan.name)
        if (
            latest["target_id"] != plan.classification["target_id"]
            or latest["provenance_revision"] != plan.revision
            or latest["provenance_sha256"]
            != plan.classification["provenance_sha256"]
            or not latest["autonomous_write_allowed"]
        ):
            raise ContractError("provenance_drift")
        _create_target_noreplace(
            context,
            plan.name,
            plan.relative,
            plan.new_payload,
        )
    published = _read_target(context, plan.name, plan.relative)
    if published.sha256 != _sha256(plan.new_payload):
        raise ContractError("target_drift")
    _validate_incremental_output(
        context,
        name=plan.name,
        relative=plan.relative,
        old_snapshot=plan.old_snapshot,
        new_payload=published.content,
        enforce_support_caps=False,
    )
    _write_existing_json_in_skill(
        context,
        plan.name,
        _AUTOSAVE_MARKER,
        plan.marker_after,
    )


def _rollback_incremental_plan(
    context: Context,
    plan: IncrementalMutationPlan,
) -> None:
    if plan.old_payload is None:
        current = _read_target(
            context,
            plan.name,
            plan.relative,
        )
        if current.sha256 == _sha256(plan.new_payload):
            _remove_target_if_snapshot(
                context,
                plan.name,
                plan.relative,
                expected=current,
            )
    else:
        current = _read_target(context, plan.name, plan.relative)
        if current.sha256 == _sha256(plan.new_payload):
            metadata = _lstat(
                context.skills_dir / plan.name / plan.relative
            )
            if metadata is None:
                raise ContractError("target_missing")
            _replace_target_no_clobber(
                context,
                plan.name,
                plan.relative,
                expected=current,
                payload=plan.old_payload,
                mode=stat.S_IMODE(metadata.st_mode),
            )
    marker_now = _safe_json_file(
        context.skills_dir / plan.name / _AUTOSAVE_MARKER,
        owner=context.uid,
        exact_mode=0o600,
    )
    if _sha256(_canonical_json(marker_now)) == plan.marker_after_sha256:
        _write_existing_json_in_skill(
            context,
            plan.name,
            _AUTOSAVE_MARKER,
            plan.marker_before,
        )


def _incremental_target_matches_new(
    context: Context,
    plan: IncrementalMutationPlan,
) -> bool:
    try:
        current = _read_target(context, plan.name, plan.relative)
    except ContractError as error:
        if error.code == "target_missing" and plan.old_payload is None:
            return False
        raise
    return current.sha256 == _sha256(plan.new_payload)


def _apply_incremental_plan(
    context: Context,
    plan: IncrementalMutationPlan,
    rows: list[dict[str, Any]],
    *,
    automatic: bool,
    daily_cap: int | None,
) -> dict[str, Any]:
    cap_day = _now().date().isoformat()
    cap_slot: int | None = None
    if automatic:
        assert daily_cap is not None
        used = _automatic_cap_used(rows, cap_day)
        if used >= daily_cap:
            raise ContractError("incremental_daily_cap_exhausted")
        cap_slot = used + 1
    backup = _incremental_backup(context, plan)
    fields = _incremental_transaction_fields(
        context,
        plan,
        backup,
        automatic=automatic,
        cap_day=cap_day,
        cap_slot=cap_slot,
    )
    transaction_id = uuid.uuid4().hex
    _append_ledger(
        context,
        _transaction_record(
            "skill-proposal-apply",
            transaction_id,
            outcome="prepared",
            fields=fields,
        ),
    )
    mutated = False
    try:
        _publish_incremental_plan(context, plan)
        mutated = True
    except ContractError as error:
        if error.code == "incremental_rollback_conflict":
            _finish_transaction(
                context,
                "skill-proposal-apply",
                transaction_id,
                outcome="conflict",
                fields=fields,
            )
            raise
        try:
            mutated = _incremental_target_matches_new(context, plan)
        except ContractError:
            _finish_transaction(
                context,
                "skill-proposal-apply",
                transaction_id,
                outcome="conflict",
                fields=fields,
            )
            raise ContractError("incremental_rollback_conflict") from None
        if mutated:
            try:
                _rollback_incremental_plan(context, plan)
            except (ContractError, OSError):
                _finish_transaction(
                    context,
                    "skill-proposal-apply",
                    transaction_id,
                    outcome="conflict",
                    fields=fields,
                )
                raise ContractError("incremental_rollback_conflict") from None
        _finish_transaction(
            context,
            "skill-proposal-apply",
            transaction_id,
            outcome="rolled_back" if mutated else "aborted",
            fields=fields,
        )
        raise
    _append_ledger(
        context,
        _transaction_record(
            "skill-proposal-apply",
            transaction_id,
            outcome="applied",
            fields=fields,
        ),
    )
    return {
        "ok": True,
        "command": "apply-proposal",
        "action": plan.action,
        "changed": True,
        "idempotent": False,
        "counted": automatic,
        "proposal_id": plan.proposal_id,
        "code": "applied",
        "new_sha256": _sha256(plan.new_payload),
    }


def _command_apply_proposal(
    context: Context,
    proposal_path: Path,
    *,
    dry_run: bool,
    automatic: bool,
    daily_cap: int | None,
) -> dict[str, Any]:
    envelope = _incremental_proposal(proposal_path, context)
    proposal = envelope["proposal"]
    assert isinstance(proposal, dict)
    action = proposal["action"]
    proposal_id = envelope["proposal_id"]
    assert isinstance(action, str) and isinstance(proposal_id, str)
    if action not in {"patch", "write_file"}:
        raise ContractError("incremental_apply_action_unsupported")
    if automatic and (
        type(daily_cap) is not int
        or daily_cap is None
        or daily_cap < 1
        or daily_cap > 100
    ):
        raise ContractError("incremental_daily_cap_invalid")
    automatic = automatic and not dry_run
    with _MutationLock(context):
        rows = _read_ledger(context)
        replay, rows = _replay_incremental_apply(
            context,
            envelope,
            rows,
            action=action,
            proposal_id=proposal_id,
        )
        if replay is not None:
            return replay
        plan = _build_incremental_plan(
            context,
            envelope,
            automatic=automatic,
        )
        if dry_run:
            return _dry_run_incremental_result(plan)
        return _apply_incremental_plan(
            context,
            plan,
            rows,
            automatic=automatic,
            daily_cap=daily_cap,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("claude", "codex"))
    parser.add_argument("--skills-dir", type=Path)
    parser.add_argument("--state-dir", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("name", nargs="?")
    subparsers.add_parser("list-unmanaged")
    usage_parser = subparsers.add_parser("automatic-usage")
    usage_parser.add_argument("--day")

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
    validate_parser = subparsers.add_parser("validate-proposal")
    validate_parser.add_argument("--proposal", type=Path, required=True)
    apply_parser = subparsers.add_parser("apply-proposal")
    apply_parser.add_argument("--proposal", type=Path, required=True)
    apply_parser.add_argument("--dry-run", action="store_true")
    apply_parser.add_argument("--automatic", action="store_true")
    apply_parser.add_argument("--daily-cap", type=int)
    mark_parser = subparsers.add_parser("mark-created")
    mark_parser.add_argument("name")
    rollback_parser = subparsers.add_parser("rollback-check")
    rollback_parser.add_argument("name")
    rollback_archive_parser = subparsers.add_parser("rollback-archive")
    rollback_archive_parser.add_argument("name")
    return parser


def _dispatch_command(
    context: Context,
    args: argparse.Namespace,
) -> dict[str, Any]:
    handlers = {
        "status": lambda: _command_status(context, args.name),
        "list-unmanaged": lambda: _command_list_unmanaged(context),
        "automatic-usage": lambda: _command_automatic_usage(
            context,
            args.day,
        ),
        "adopt": lambda: _command_adopt(
            context,
            args.name,
            args.dry_run,
        ),
        "mark-created": lambda: _command_mark_created(context, args.name),
        "rollback-check": lambda: _command_rollback_check(context, args.name),
        "rollback-archive": lambda: _command_rollback_archive(
            context,
            args.name,
        ),
        "pin": lambda: _command_pin(
            context,
            args.name,
            True,
            args.dry_run,
        ),
        "unpin": lambda: _command_pin(
            context,
            args.name,
            False,
            args.dry_run,
        ),
        "read-target": lambda: _command_read_target(
            context,
            args.name,
            args.relative_target,
            args.attempt_id,
            args.operation,
        ),
        "apply-proposal": lambda: _command_apply_proposal(
            context,
            args.proposal,
            dry_run=args.dry_run,
            automatic=args.automatic,
            daily_cap=args.daily_cap,
        ),
        "validate-proposal": lambda: _command_validate_incremental_proposal(
            context,
            args.proposal,
        ),
        "guard-proposal": lambda: _command_guard_proposal(
            context,
            args.proposal,
        ),
    }
    handler = handlers.get(args.command)
    if handler is None:
        raise ContractError("unknown_command")
    return handler()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        context = _build_context(args)
        result = _dispatch_command(context, args)
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
