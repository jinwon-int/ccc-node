"""Bounded, redacted inventory for incremental skill proposals (#751).

The inventory is advisory model input, never write authority.  Autonomous
eligibility comes from the deployed #750 ownership classifier; every eventual
mutation must re-read and re-classify the target under the ownership lock.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any

from .skill_candidate import _CREDENTIAL_PATTERNS, _DIRECTIVE_RE

MAX_WRITABLE_SKILLS = 8
MAX_OVERLAP_SKILLS = 64
MAX_FILES_PER_SKILL = 4
MAX_FILE_BYTES = 16 * 1024
MAX_TOTAL_CONTENT_BYTES = 64 * 1024
MAX_INVENTORY_JSON_BYTES = 128 * 1024
_SUPPORT_PREFIXES = {"references", "scripts", "templates"}
_SAFE_CLASSIFICATIONS = {
    "autosave-managed",
    "pinned",
    "managed/bundled",
    "external/repo-installed",
    "user-owned",
    "unknown/unreadable",
}
_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SAFE_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_IPV4_RE = re.compile(r"\b(?!127(?:\.\d{1,3}){3}\b)(?:\d{1,3}\.){3}\d{1,3}\b")
_URL_RE = re.compile(r"\b(?:https?|ssh)://[^\s<>()]+", re.IGNORECASE)
_ROOT_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?:root|home|Users)/[^\s`'\"<>]+"
)
_HOST_RE = re.compile(r"\b[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\b")


class SkillInventoryError(RuntimeError):
    """A body-free inventory construction failure."""


def _redact_inventory_text(value: str) -> str:
    text = value
    for pattern in _CREDENTIAL_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = _URL_RE.sub("[REDACTED_ENDPOINT]", text)
    text = _ROOT_PATH_RE.sub("[REDACTED_PATH]", text)
    text = _IPV4_RE.sub("[REDACTED_ADDRESS]", text)
    text = _HOST_RE.sub("[REDACTED_HOST]", text)
    return text


def _safe_metadata(value: object, *, kind: str, max_bytes: int = 512) -> dict[str, object]:
    if not isinstance(value, str):
        return {f"{kind}_excluded_reason": "invalid_metadata_type"}
    encoded = value.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    if len(encoded) > max_bytes:
        return {
            f"{kind}_excluded_reason": "metadata_too_large",
            f"{kind}_sha256": digest,
            f"{kind}_size": len(encoded),
        }
    if _DIRECTIVE_RE.search(value):
        return {
            f"{kind}_excluded_reason": "metadata_injected_directive",
            f"{kind}_sha256": digest,
            f"{kind}_size": len(encoded),
        }
    redacted = _redact_inventory_text(value)
    if kind in {"name", "relative_target"} and redacted != value:
        return {
            f"{kind}_excluded_reason": "metadata_redacted",
            f"{kind}_sha256": digest,
            f"{kind}_size": len(encoded),
        }
    if kind == "name" and not _KEBAB_RE.fullmatch(redacted):
        return {
            f"{kind}_excluded_reason": "metadata_invalid_name",
            f"{kind}_sha256": digest,
            f"{kind}_size": len(encoded),
        }
    if kind == "relative_target" and any(
        not _SAFE_PATH_COMPONENT_RE.fullmatch(part)
        for part in redacted.split("/")
    ):
        return {
            f"{kind}_excluded_reason": "metadata_invalid_path",
            f"{kind}_sha256": digest,
            f"{kind}_size": len(encoded),
        }
    if kind == "classification" and redacted not in _SAFE_CLASSIFICATIONS:
        return {
            f"{kind}_excluded_reason": "metadata_invalid_classification",
            f"{kind}_sha256": digest,
            f"{kind}_size": len(encoded),
        }
    return {kind: redacted}


def _read_regular_owner_file(
    skill_dir: Path,
    relative: str,
    *,
    uid: int,
) -> tuple[bytes, os.stat_result]:
    parts = relative.split("/")
    if (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or "\\" in relative
        or Path(relative).is_absolute()
    ):
        raise SkillInventoryError("inventory_path_invalid")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    try:
        expected_root = skill_dir.lstat()
        current = os.open(skill_dir, directory_flags)
        descriptors.append(current)
        opened_root = os.fstat(current)
        if (
            not stat.S_ISDIR(opened_root.st_mode)
            or opened_root.st_uid != uid
            or stat.S_IMODE(opened_root.st_mode) & 0o022
            or (opened_root.st_dev, opened_root.st_ino)
            != (expected_root.st_dev, expected_root.st_ino)
        ):
            raise SkillInventoryError("inventory_directory_unsafe")
        for component in parts[:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            descriptors.append(current)
            directory = os.fstat(current)
            if (
                not stat.S_ISDIR(directory.st_mode)
                or directory.st_uid != uid
                or stat.S_IMODE(directory.st_mode) & 0o022
            ):
                raise SkillInventoryError("inventory_directory_unsafe")
        descriptor = os.open(parts[-1], file_flags, dir_fd=current)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != uid
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size > MAX_FILE_BYTES
        ):
            raise SkillInventoryError("inventory_file_unsafe")
        chunks: list[bytes] = []
        remaining = MAX_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) > MAX_FILE_BYTES
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            raise SkillInventoryError("inventory_file_changed")
        final_root = os.fstat(descriptors[0])
        path_root = skill_dir.lstat()
        if (
            (opened_root.st_dev, opened_root.st_ino)
            != (final_root.st_dev, final_root.st_ino)
            or (opened_root.st_dev, opened_root.st_ino)
            != (path_root.st_dev, path_root.st_ino)
        ):
            raise SkillInventoryError("inventory_directory_changed")
        return payload, after
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class SkillCandidateInventoryBuilder:
    """Build a deterministic privacy-bounded model input inventory."""

    def __init__(
        self,
        *,
        skills_dir: Path,
        state_dir: Path,
        ownership_tool: Path,
        provider: str = "codex",
    ) -> None:
        self._skills_dir = Path(os.path.abspath(skills_dir))
        self._state_dir = Path(os.path.abspath(state_dir))
        self._ownership_tool = Path(os.path.abspath(ownership_tool))
        self._provider = provider

    @classmethod
    def from_environment(
        cls, environment: dict[str, str] | None = None
    ) -> "SkillCandidateInventoryBuilder":
        env = dict(os.environ if environment is None else environment)
        home = Path(env.get("HOME", "/root"))
        codex_home = Path(env.get("CODEX_HOME", home / ".codex"))
        claude_dir = Path(env.get("CCC_CLAUDE_DIR", home / ".claude"))
        return cls(
            skills_dir=Path(env.get("CODEX_SKILLS_DIR", codex_home / "skills")),
            state_dir=Path(env.get("CCC_STATE_DIR", claude_dir / "state")),
            ownership_tool=Path(
                env.get(
                    "CCC_SKILL_OWNERSHIP_TOOL",
                    claude_dir / "hooks" / "skill-review" / "ownership.py",
                )
            ),
        )

    def _status(self) -> list[dict[str, Any]]:
        if not self._ownership_tool_is_safe():
            return []
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(self._ownership_tool),
                    "--provider",
                    self._provider,
                    "--skills-dir",
                    str(self._skills_dir),
                    "--state-dir",
                    str(self._state_dir),
                    "status",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if completed.returncode != 0 or len(completed.stdout) > 512 * 1024:
            return []
        try:
            output = json.loads(completed.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return []
        rows = output.get("skills") if isinstance(output, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def _ownership_tool_is_safe(self) -> bool:
        uid = os.geteuid()
        current = Path("/")
        for component in self._ownership_tool.parts[1:]:
            current /= component
            try:
                metadata = current.lstat()
            except OSError:
                return False
            if stat.S_ISLNK(metadata.st_mode):
                return False
            if metadata.st_uid not in {0, uid} or stat.S_IMODE(metadata.st_mode) & 0o022:
                return False
        try:
            tool = self._ownership_tool.lstat()
        except OSError:
            return False
        return (
            stat.S_ISREG(tool.st_mode)
            and tool.st_uid in {0, uid}
            and tool.st_nlink == 1
            and tool.st_size <= 2 * 1024 * 1024
        )

    @staticmethod
    def _candidate_paths(skill_dir: Path) -> list[tuple[str, Path]]:
        candidates: list[tuple[str, Path]] = [("SKILL.md", skill_dir / "SKILL.md")]
        for prefix in sorted(_SUPPORT_PREFIXES):
            root = skill_dir / prefix
            try:
                root_meta = root.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(root_meta.st_mode) or stat.S_ISLNK(root_meta.st_mode):
                continue
            for current, directories, files in os.walk(root, followlinks=False):
                directories[:] = sorted(
                    directory
                    for directory in directories
                    if not (Path(current) / directory).is_symlink()
                )
                for filename in sorted(files):
                    path = Path(current) / filename
                    relative = path.relative_to(skill_dir).as_posix()
                    candidates.append((relative, path))
        return candidates[:MAX_FILES_PER_SKILL]

    def _overlap_description(
        self,
        row: dict[str, Any],
        *,
        uid: int,
    ) -> dict[str, object]:
        if "description" in row:
            return _safe_metadata(
                row.get("description"),
                kind="description",
                max_bytes=1024,
            )
        safe_name = _safe_metadata(row.get("name"), kind="name")
        name = safe_name.get("name")
        if not isinstance(name, str):
            return {"description_excluded_reason": "target_name_unsafe"}
        try:
            payload, _metadata = _read_regular_owner_file(
                self._skills_dir / name,
                "SKILL.md",
                uid=uid,
            )
            text = payload.decode("utf-8")
        except (OSError, UnicodeDecodeError, SkillInventoryError):
            return {"description_excluded_reason": "unsafe_or_changed"}
        lines = text.splitlines()
        if not lines or lines[0] != "---":
            return {"description_excluded_reason": "frontmatter_invalid"}
        for line in lines[1:]:
            if line == "---":
                break
            if line.startswith("description:"):
                description = line.split(":", 1)[1].strip()
                return _safe_metadata(
                    description,
                    kind="description",
                    max_bytes=1024,
                )
        return {"description_excluded_reason": "description_missing"}

    def _overlap_records(
        self,
        rows: list[dict[str, Any]],
        writable_rows: list[dict[str, Any]],
        *,
        uid: int,
    ) -> list[dict[str, object]]:
        writable_ids = {id(row) for row in writable_rows}
        overlaps: list[dict[str, object]] = []
        for row in rows:
            if id(row) in writable_ids:
                continue
            overlaps.append(
                {
                    **_safe_metadata(row.get("name"), kind="name"),
                    **_safe_metadata(
                        row.get("classification"),
                        kind="classification",
                    ),
                    **_safe_metadata(row.get("reason"), kind="reason"),
                    **self._overlap_description(row, uid=uid),
                }
            )
            if len(overlaps) >= MAX_OVERLAP_SKILLS:
                break
        return overlaps

    @staticmethod
    def _attach_bounded_content(
        entry: dict[str, object],
        payload: bytes,
        *,
        total: int,
    ) -> int:
        if total + len(payload) > MAX_TOTAL_CONTENT_BYTES:
            entry["excluded_reason"] = "total_content_cap"
            return total
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            entry["excluded_reason"] = "non_utf8"
            return total
        if _DIRECTIVE_RE.search(text):
            entry["excluded_reason"] = "injected_directive"
            return total
        redacted = _redact_inventory_text(text)
        encoded = redacted.encode("utf-8")
        if total + len(encoded) > MAX_TOTAL_CONTENT_BYTES:
            entry["excluded_reason"] = "total_content_cap"
            return total
        entry["content"] = redacted
        return total + len(encoded)

    def _file_record(
        self,
        skill_dir: Path,
        relative: str,
        *,
        uid: int,
        total: int,
    ) -> tuple[dict[str, object], int]:
        safe_relative = _safe_metadata(
            relative,
            kind="relative_target",
            max_bytes=240,
        )
        if "relative_target" not in safe_relative:
            return safe_relative, total
        entry: dict[str, object] = {
            "relative_target": safe_relative["relative_target"]
        }
        try:
            payload, metadata = _read_regular_owner_file(
                skill_dir,
                relative,
                uid=uid,
            )
        except (OSError, SkillInventoryError):
            entry["excluded_reason"] = "unsafe_or_changed"
            return entry, total
        entry["sha256"] = hashlib.sha256(payload).hexdigest()
        entry["size"] = metadata.st_size
        return entry, self._attach_bounded_content(entry, payload, total=total)

    def _writable_record(
        self,
        row: dict[str, Any],
        *,
        uid: int,
        total: int,
    ) -> tuple[dict[str, object] | None, int]:
        name = row.get("name")
        if not isinstance(name, str):
            return None, total
        safe_name = _safe_metadata(name, kind="name")
        if "name" not in safe_name:
            target_id = row.get("target_id")
            target = (
                {"target_id": target_id}
                if isinstance(target_id, str)
                and re.fullmatch(r"[0-9a-f]{64}", target_id)
                else {"target_id_excluded_reason": "metadata_invalid_hash"}
            )
            return {**safe_name, **target, "files": []}, total
        skill_dir = self._skills_dir / name
        files: list[dict[str, object]] = []
        for relative, _path in self._candidate_paths(skill_dir):
            entry, total = self._file_record(
                skill_dir,
                relative,
                uid=uid,
                total=total,
            )
            files.append(entry)
        record: dict[str, object] = {
            "name": safe_name["name"],
            "files": files,
            **_safe_metadata(
                row.get("classification"),
                kind="classification",
            ),
            **_safe_metadata(row.get("reason"), kind="reason"),
        }
        for field in ("target_id", "provenance_sha256"):
            value = row.get(field)
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                record[field] = value
            else:
                record[f"{field}_excluded_reason"] = "metadata_invalid_hash"
        revision = row.get("provenance_revision")
        if type(revision) is int and revision >= 0:
            record["provenance_revision"] = revision
        else:
            record["provenance_revision_excluded_reason"] = (
                "metadata_invalid_revision"
            )
        return record, total

    @staticmethod
    def _serialized_size(inventory: dict[str, object]) -> int:
        return len(
            json.dumps(
                inventory,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    @classmethod
    def _trim_inventory(cls, inventory: dict[str, object]) -> None:
        while cls._serialized_size(inventory) > MAX_INVENTORY_JSON_BYTES:
            overlap_rows = inventory["read_only_overlaps"]
            assert isinstance(overlap_rows, list)
            if overlap_rows:
                overlap_rows.pop()
                continue
            writable_rows = inventory["writable"]
            assert isinstance(writable_rows, list)
            for writable_row in reversed(writable_rows):
                files = writable_row.get("files")
                if not isinstance(files, list):
                    continue
                content_row = next(
                    (
                        file_row
                        for file_row in reversed(files)
                        if isinstance(file_row, dict) and "content" in file_row
                    ),
                    None,
                )
                if content_row is not None:
                    content_row.pop("content")
                    content_row["excluded_reason"] = "inventory_json_cap"
                    break
            else:
                raise SkillInventoryError("inventory_json_cap_unreachable")

    def build(self) -> dict[str, object]:
        rows = sorted(self._status(), key=lambda row: str(row.get("name", "")))
        writable_rows = [
            row
            for row in rows
            if row.get("autonomous_write_allowed") is True
            and row.get("base_classification") == "autosave-managed"
            and row.get("pinned") is False
        ][:MAX_WRITABLE_SKILLS]
        uid = os.geteuid()
        overlaps = self._overlap_records(rows, writable_rows, uid=uid)
        total = 0
        writable: list[dict[str, object]] = []
        for row in writable_rows:
            record, total = self._writable_record(
                row,
                uid=uid,
                total=total,
            )
            if record is not None:
                writable.append(record)
        inventory: dict[str, object] = {
            "schema_version": 1,
            "content_trust": "untrusted",
            "writable": writable,
            "read_only_overlaps": overlaps,
            "limits": {
                "max_writable_skills": MAX_WRITABLE_SKILLS,
                "max_overlap_skills": MAX_OVERLAP_SKILLS,
                "max_files_per_skill": MAX_FILES_PER_SKILL,
                "max_file_bytes": MAX_FILE_BYTES,
                "max_total_content_bytes": MAX_TOTAL_CONTENT_BYTES,
                "max_inventory_json_bytes": MAX_INVENTORY_JSON_BYTES,
            },
        }
        self._trim_inventory(inventory)
        return inventory


__all__ = [
    "SkillCandidateInventoryBuilder",
    "SkillInventoryError",
    "MAX_WRITABLE_SKILLS",
    "MAX_OVERLAP_SKILLS",
    "MAX_FILES_PER_SKILL",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_CONTENT_BYTES",
    "MAX_INVENTORY_JSON_BYTES",
]
