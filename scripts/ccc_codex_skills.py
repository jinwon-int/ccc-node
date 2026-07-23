#!/usr/bin/env python3
"""Validate and transactionally provision repo-shipped Codex skills (#647)."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import uuid
from typing import Any


_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MARKER = ".ccc-node-managed.json"
_MAX_CATALOG_BYTES = 256 * 1024
_MAX_SKILL_FILES = 64
_MAX_SKILL_BYTES = 1024 * 1024
_FORBIDDEN_CODEX_REFERENCES = (
    "/.claude",
    "CLAUDE_DIR",
    "claude/hooks",
    "claude/skills",
    "claude mcp",
    "Agent tool",
    "PreToolUse",
)
_ASSET_ROOTS = (
    "claude/commands",
    "claude/skills",
    "claude/agents",
    "claude/hooks",
)


class ContractError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _read_json(path: Path, *, max_bytes: int, error: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ContractError(error)
        payload = path.read_bytes()
        if len(payload) > max_bytes:
            raise ContractError(error)
        value = json.loads(payload)
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ContractError(error) from None
    if not isinstance(value, dict):
        raise ContractError(error)
    return value


def _repo_file_inventory(repo: Path) -> list[str]:
    inventory: list[str] = []
    for relative_root in _ASSET_ROOTS:
        root = repo / relative_root
        if not root.is_dir() or root.is_symlink():
            raise ContractError("catalog_asset_root_invalid")
        for path in root.rglob("*"):
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ContractError("catalog_asset_symlink")
            if stat.S_ISREG(metadata.st_mode):
                inventory.append(path.relative_to(repo).as_posix())
    return sorted(inventory)


def _frontmatter(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ContractError("codex_skill_invalid") from None
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ContractError("codex_skill_invalid")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise ContractError("codex_skill_invalid") from None
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            raise ContractError("codex_skill_invalid")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key in values or not value:
            raise ContractError("codex_skill_invalid")
        values[key] = value
    if set(values) != {"name", "description"}:
        raise ContractError("codex_skill_invalid")
    if not _NAME_RE.fullmatch(values["name"]):
        raise ContractError("codex_skill_invalid")
    if not 20 <= len(values["description"]) <= 1024:
        raise ContractError("codex_skill_invalid")
    if len(lines[end + 1 :]) < 3:
        raise ContractError("codex_skill_invalid")
    return values


def _source_files(source: Path) -> list[Path]:
    if not source.is_dir() or source.is_symlink():
        raise ContractError("codex_skill_source_invalid")
    files: list[Path] = []
    total = 0
    for path in sorted(source.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ContractError("codex_skill_source_invalid")
        if stat.S_ISREG(metadata.st_mode):
            total += metadata.st_size
            files.append(path)
        elif not stat.S_ISDIR(metadata.st_mode):
            raise ContractError("codex_skill_source_invalid")
    if not files or len(files) > _MAX_SKILL_FILES or total > _MAX_SKILL_BYTES:
        raise ContractError("codex_skill_source_invalid")
    return files


def _file_hashes(source: Path) -> dict[str, str]:
    return {
        path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in _source_files(source)
    }


def _tree_hash(file_hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(file_hashes.items()):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(value.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _load_catalog(repo: Path) -> dict[str, Any]:
    return _read_json(
        repo / "codex" / "compatibility.json",
        max_bytes=_MAX_CATALOG_BYTES,
        error="catalog_invalid",
    )


def _classification_rules(raw_rules: object) -> list[dict[str, str]]:
    if not isinstance(raw_rules, list):
        raise ContractError("catalog_invalid")
    rules: list[dict[str, str]] = []
    for value in raw_rules:
        if not isinstance(value, dict):
            raise ContractError("catalog_invalid")
        pattern = value.get("pattern")
        compatibility = value.get("compatibility")
        if (
            not isinstance(pattern, str)
            or not pattern.startswith("claude/")
            or compatibility
            not in {"shared", "adapted", "claude-only", "codex-only", "unsupported"}
        ):
            raise ContractError("catalog_invalid")
        rules.append({"pattern": pattern, "compatibility": compatibility})
    return rules


def _validated_inventory(repo: Path, rules: list[dict[str, str]]) -> int:
    inventory = _repo_file_inventory(repo)
    for asset in inventory:
        matches = [rule for rule in rules if fnmatch.fnmatchcase(asset, rule["pattern"])]
        if not matches:
            raise ContractError("catalog_unclassified")
        if len(matches) != 1:
            raise ContractError("catalog_overlap")
    return len(inventory)


def _managed_skill_entries(
    repo: Path,
    raw_skills: object,
) -> list[dict[str, str]]:
    if not isinstance(raw_skills, list):
        raise ContractError("catalog_invalid")
    skills: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in raw_skills:
        if not isinstance(value, dict):
            raise ContractError("catalog_invalid")
        name = value.get("name")
        source_raw = value.get("source")
        if (
            not isinstance(name, str)
            or not _NAME_RE.fullmatch(name)
            or name in seen
            or not isinstance(source_raw, str)
            or source_raw != f"codex/skills/{name}"
        ):
            raise ContractError("catalog_invalid")
        source = repo / source_raw
        frontmatter = _frontmatter(source / "SKILL.md")
        if frontmatter["name"] != name:
            raise ContractError("codex_skill_invalid")
        source_files = _source_files(source)
        if not (source / "agents" / "openai.yaml").is_file():
            raise ContractError("codex_skill_invalid")
        for path in source_files:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if any(token in text for token in _FORBIDDEN_CODEX_REFERENCES):
                raise ContractError("codex_incompatible_reference")
        interface = (source / "agents" / "openai.yaml").read_text(encoding="utf-8")
        if f"${name}" not in interface:
            raise ContractError("codex_skill_invalid")
        seen.add(name)
        skills.append({"name": name, "source": source_raw})
    if not skills:
        raise ContractError("catalog_invalid")
    return sorted(skills, key=lambda item: item["name"])


def _validated_catalog(repo: Path) -> tuple[list[dict[str, str]], int]:
    catalog = _load_catalog(repo)
    if catalog.get("schema_version") != 1:
        raise ContractError("catalog_invalid")
    rules = _classification_rules(catalog.get("classifications"))
    classified_count = _validated_inventory(repo, rules)
    skills = _managed_skill_entries(repo, catalog.get("managed_skills"))
    return skills, classified_count


def _validate_no_symlink_components(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise ContractError("unsafe_codex_home")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ContractError("unsafe_codex_home")


def _validate_private_dir(path: Path, *, missing_ok: bool, error: str) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return False
        raise ContractError(error) from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ContractError(error)
    return True


def _validate_private_file(path: Path, *, error: str) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ContractError(error)


def _mkdir_private_components(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            os.mkdir(current, 0o700)
            metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ContractError("unsafe_codex_home")
        if current == absolute and (
            metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ContractError("unsafe_codex_home")


def _marker_for(name: str, source_raw: str, source: Path) -> dict[str, Any]:
    files = _file_hashes(source)
    return {
        "schema_version": 1,
        "manager": "ccc-node",
        "name": name,
        "source": source_raw,
        "source_hash": _tree_hash(files),
        "files": files,
    }


def _validate_installed_target(target: Path, expected_name: str) -> dict[str, Any]:
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(metadata.st_mode):
        raise ContractError("unsafe_target")
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ContractError("unsafe_target")
    marker_path = target / _MARKER
    if not marker_path.exists():
        raise ContractError("unmanaged_collision")
    try:
        _validate_private_file(marker_path, error="managed_drift")
        marker = _read_json(
            marker_path,
            max_bytes=64 * 1024,
            error="managed_drift",
        )
    except ContractError as error:
        if error.code == "unsafe_target":
            raise
        raise ContractError("managed_drift") from None
    if (
        marker.get("schema_version") != 1
        or marker.get("manager") != "ccc-node"
        or marker.get("name") != expected_name
        or not isinstance(marker.get("source_hash"), str)
        or not isinstance(marker.get("files"), dict)
    ):
        raise ContractError("managed_drift")

    actual_files: dict[str, str] = {}
    for path in sorted(target.rglob("*")):
        relative = path.relative_to(target).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ContractError("unsafe_target")
        if stat.S_ISDIR(metadata.st_mode):
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise ContractError("unsafe_target")
            continue
        if relative == _MARKER:
            continue
        _validate_private_file(path, error="unsafe_target")
        actual_files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_files != marker["files"] or _tree_hash(actual_files) != marker["source_hash"]:
        raise ContractError("managed_drift")
    return marker


def _plan(repo: Path, codex_home: Path) -> tuple[list[dict[str, Any]], int]:
    skills, classified_count = _validated_catalog(repo)
    _validate_no_symlink_components(codex_home)
    home_exists = _validate_private_dir(
        codex_home,
        missing_ok=True,
        error="unsafe_codex_home",
    )
    skills_root = codex_home / "skills"
    if home_exists:
        _validate_private_dir(
            skills_root,
            missing_ok=True,
            error="unsafe_codex_home",
        )

    result: list[dict[str, Any]] = []
    for item in skills:
        source = repo / item["source"]
        expected = _marker_for(item["name"], item["source"], source)
        target = skills_root / item["name"]
        try:
            existing = _validate_installed_target(target, item["name"])
        except FileNotFoundError:
            status = "create"
        else:
            status = (
                "unchanged"
                if existing["source_hash"] == expected["source_hash"]
                and existing.get("source") == expected["source"]
                else "update"
            )
        result.append(
            {
                "name": item["name"],
                "status": status,
                "source": source,
                "source_raw": item["source"],
                "marker": expected,
                "target": target,
            }
        )
    return result, classified_count


def _write_private(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_skill(stage_target: Path, item: dict[str, Any]) -> None:
    stage_target.mkdir(mode=0o700)
    stage_target.chmod(0o700)
    source: Path = item["source"]
    for source_file in _source_files(source):
        relative = source_file.relative_to(source)
        destination = stage_target / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.parent.chmod(0o700)
        _write_private(destination, source_file.read_bytes())
    _write_private(stage_target / _MARKER, _canonical_json(item["marker"]))


def _remove_owned_tree(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink():
        raise ContractError("transaction_rollback_failed")
    shutil.rmtree(path)


def _test_fail_after(codex_home: Path) -> int:
    fail_after_raw = os.environ.get("CCC_CODEX_SKILLS_TEST_FAIL_AFTER", "")
    if not fail_after_raw:
        return 0
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        codex_home.resolve().relative_to(temp_root)
    except ValueError:
        raise ContractError("test_seam_refused") from None
    if not fail_after_raw.isdecimal() or int(fail_after_raw) <= 0:
        raise ContractError("test_seam_refused")
    return int(fail_after_raw)


def _commit_transaction(
    changed: list[dict[str, Any]],
    *,
    skills_root: Path,
    fail_after: int,
) -> None:
    transaction = uuid.uuid4().hex
    stage_root = skills_root / f".ccc-node-stage-{transaction}"
    backup_root = skills_root / f".ccc-node-backup-{transaction}"
    os.mkdir(stage_root, 0o700)
    installed: list[dict[str, Any]] = []
    try:
        for item in changed:
            _stage_skill(stage_root / item["name"], item)
        if any(item["status"] == "update" for item in changed):
            os.mkdir(backup_root, 0o700)
        for item in changed:
            target: Path = item["target"]
            backup = backup_root / item["name"]
            if item["status"] == "update":
                os.replace(target, backup)
            os.replace(stage_root / item["name"], target)
            installed.append({**item, "backup": backup})
            if fail_after and len(installed) >= fail_after:
                raise OSError("injected managed-skill transaction failure")
    except BaseException:
        rollback_failed = False
        for item in reversed(installed):
            try:
                _remove_owned_tree(item["target"])
                if item["status"] == "update" and item["backup"].exists():
                    os.replace(item["backup"], item["target"])
            except (OSError, ContractError):
                rollback_failed = True
        _remove_owned_tree(stage_root)
        _remove_owned_tree(backup_root)
        if rollback_failed:
            raise ContractError("transaction_rollback_failed") from None
        raise ContractError("transaction_rolled_back") from None

    _remove_owned_tree(stage_root)
    _remove_owned_tree(backup_root)


def _apply(repo: Path, codex_home: Path) -> tuple[list[dict[str, Any]], int]:
    plan, classified_count = _plan(repo, codex_home)
    changed = [item for item in plan if item["status"] != "unchanged"]
    if not changed:
        return plan, classified_count

    _mkdir_private_components(codex_home)
    skills_root = codex_home / "skills"
    if not skills_root.exists():
        os.mkdir(skills_root, 0o700)
    _validate_private_dir(skills_root, missing_ok=False, error="unsafe_codex_home")
    _commit_transaction(
        changed,
        skills_root=skills_root,
        fail_after=_test_fail_after(codex_home),
    )
    return plan, classified_count


def _public_result(
    command: str,
    plan: list[dict[str, Any]],
    classified_count: int,
) -> dict[str, Any]:
    return {
        "ok": True,
        "command": command,
        "classified_assets": classified_count,
        "managed_skills": len(plan),
        "skills": [
            {"name": item["name"], "status": item["status"]}
            for item in plan
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "plan", "apply"))
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--codex-home", type=Path)
    args = parser.parse_args(argv)
    repo = Path(os.path.abspath(os.fspath(args.repo_root)))
    try:
        if args.command == "validate":
            skills, classified_count = _validated_catalog(repo)
            result = {
                "ok": True,
                "command": "validate",
                "classified_assets": classified_count,
                "managed_skills": len(skills),
            }
        else:
            if args.codex_home is None:
                raise ContractError("codex_home_required")
            codex_home = Path(os.path.abspath(os.fspath(args.codex_home)))
            if args.command == "plan":
                plan, classified_count = _plan(repo, codex_home)
            else:
                plan, classified_count = _apply(repo, codex_home)
            result = _public_result(args.command, plan, classified_count)
    except ContractError as error:
        print(f"ccc-codex-skills: {error.code}", file=sys.stderr)
        return 70 if error.code.startswith("transaction_") else 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
