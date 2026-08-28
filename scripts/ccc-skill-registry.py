#!/usr/bin/env python3
"""Single generated registry over every repo skill source (#1338).

Scans the four skill audiences (``skills/shared``, ``claude/skills``,
``codex/skills``, ``piri/skills``), reads each ``SKILL.md`` frontmatter and
``codex/compatibility.json``, and emits ``skills/registry.json``.

``validate-harness`` re-derives the registry and fails closed on drift, so the
committed artifact is CI-enforced truth:

- adding a skill = skill files + a compatibility.json entry (when applicable)
  + one ``update`` run — the old four-place catalog tax collapses to its
  irreducible parts, and the hardcoded managed-skill count assertions in
  ``ccc_codex_skills_test.py`` become registry-derived;
- the optional ``status: active|deprecated`` frontmatter field is the skill
  lifecycle vocabulary. A deprecated managed skill stays in the repo and in
  the registry but is no longer planned or installed by the Codex provisioner;
  removing it from ``managed_skills`` remains a separate operator step.

The registry is a derived view, never an authority: it contains no secrets and
only mirrors content that is already public in this repository.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SCHEMA_VERSION = 1
STATUS_VALUES = ("active", "deprecated")
DESCRIPTION_MIN = 20
DESCRIPTION_MAX = 1024
MIN_BODY_LINES = 3
MAX_CATALOG_BYTES = 256 * 1024
MAX_REGISTRY_BYTES = 256 * 1024
MAX_SKILL_FILES = 64
MAX_SKILL_BYTES = 1024 * 1024

# audience -> repo-relative skill root
AUDIENCES: dict[str, str] = {
    "shared": "skills/shared",
    "claude": "claude/skills",
    "codex": "codex/skills",
    "piri": "piri/skills",
}
# Audiences whose files the compatibility catalog classifies (pattern roots
# "claude/" and "skills/shared/"). codex and piri skills carry no
# classification — their parity contract lives in the provisioner, not here.
CLASSIFIED_AUDIENCES = frozenset({"shared", "claude"})
CLASSIFICATION_VALUES = frozenset(
    {"shared", "adapted", "claude-only", "codex-only", "unsupported"}
)


class RegistryError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def artifact_path(repo: Path) -> Path:
    return repo / "skills" / "registry.json"


def _read_bounded(path: Path, max_bytes: int, code: str) -> str:
    try:
        raw = path.read_bytes()
    except (OSError, FileNotFoundError):
        raise RegistryError(code) from None
    if len(raw) > max_bytes:
        raise RegistryError(code)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RegistryError(code) from None


def _frontmatter(path: Path) -> dict[str, str]:
    """Parse the top-level frontmatter keys a registry entry needs.

    Tolerant by design: claude skills legally carry richer frontmatter (e.g. a
    nested ``metadata:`` block), so only indented/nested and key-only lines are
    ignored — every other non-empty line must be a ``key: value`` pair with a
    unique key. ``name`` and ``description`` are required, ``status`` is the
    optional lifecycle field, and unknown top-level keys pass through
    untouched so the registry never blocks an unrelated harness extension.
    """
    text = _read_bounded(path, 256 * 1024, "registry_skill_invalid")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise RegistryError("registry_skill_invalid")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise RegistryError("registry_skill_invalid") from None
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        if line[0] in " \t-#" or ":" not in line:
            continue  # nested block, list item, comment, or key-only line
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise RegistryError("registry_skill_invalid")
        if not value:
            continue  # key-only line opening a nested block (e.g. "metadata:")
        if key in values:
            raise RegistryError("registry_skill_invalid")
        values[key] = value
    name = values.get("name")
    description = values.get("description")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise RegistryError("registry_skill_invalid")
    if not description or not (
        DESCRIPTION_MIN <= len(description) <= DESCRIPTION_MAX
    ):
        raise RegistryError("registry_skill_invalid")
    status = values.get("status", "active")
    if status not in STATUS_VALUES:
        raise RegistryError("registry_status_invalid")
    if len(lines[end + 1 :]) < MIN_BODY_LINES:
        raise RegistryError("registry_skill_invalid")
    return values


def _skill_files(skill_dir: Path, listed: set[str] | None, root_prefix: str) -> list[Path]:
    """Files of one skill dir, git's view preferred, filesystem walk fallback.

    Git-first mirrors ccc_codex_skills: a long-lived node checkout can hold
    stray untracked files (build output, editor droppings) that a walk would
    count, making local validation answer a different question than CI.
    ``listed`` carries git-tracked repo-relative paths for the whole repo.
    """
    if listed is None:
        files: list[Path] = []
        total = 0
        if not skill_dir.is_dir() or skill_dir.is_symlink():
            raise RegistryError("registry_skill_source_invalid")
        for path in sorted(skill_dir.rglob("*")):
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise RegistryError("registry_skill_source_invalid")
            if stat.S_ISREG(metadata.st_mode):
                total += metadata.st_size
                files.append(path)
            elif not stat.S_ISDIR(metadata.st_mode):
                raise RegistryError("registry_skill_source_invalid")
    else:
        files = []
        total = 0
        # ``listed`` is repo-relative; scope it to THIS skill dir before
        # rebasing the remainder onto the skill dir path.
        prefix = f"{root_prefix.rstrip('/')}/{skill_dir.name}/"
        for relative in sorted(listed):
            if not relative.startswith(prefix):
                continue
            path = repo_join(skill_dir, relative[len(prefix):])
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue  # tracked but deleted in the worktree
            if stat.S_ISLNK(metadata.st_mode):
                raise RegistryError("registry_skill_source_invalid")
            if stat.S_ISREG(metadata.st_mode):
                total += metadata.st_size
                files.append(path)
        if not files:
            # Untracked new skill dir: git has no opinion yet, so walk the dir
            # itself. A tracked dir never lands here (its files are listed),
            # keeping the stray-file protection where it matters.
            if not skill_dir.is_dir() or skill_dir.is_symlink():
                raise RegistryError("registry_skill_source_invalid")
            for path in sorted(skill_dir.rglob("*")):
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise RegistryError("registry_skill_source_invalid")
                if stat.S_ISREG(metadata.st_mode):
                    total += metadata.st_size
                    files.append(path)
                elif not stat.S_ISDIR(metadata.st_mode):
                    raise RegistryError("registry_skill_source_invalid")
    if not files or len(files) > MAX_SKILL_FILES or total > MAX_SKILL_BYTES:
        raise RegistryError("registry_skill_source_invalid")
    return files


def repo_join(base: Path, relative: str) -> Path:
    return base.joinpath(*relative.split("/"))


def _file_hashes(files: list[Path], skill_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in files:
        relative = path.relative_to(skill_dir).as_posix()
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _tree_hash(file_hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(file_hashes.items()):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(value.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _git_listed(repo: Path, roots: list[str]) -> set[str] | None:
    """Tracked files under the audience roots, or None without a git checkout."""
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z", "--", *roots],
            cwd=repo,
            capture_output=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    listed = {
        entry
        for entry in completed.stdout.decode("utf-8", "replace").split("\0")
        if entry
    }
    return listed


def _load_catalog(repo: Path) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Classification rules + managed {name: source} from compatibility.json."""
    text = _read_bounded(
        repo / "codex" / "compatibility.json",
        MAX_CATALOG_BYTES,
        "registry_catalog_invalid",
    )
    try:
        catalog = json.loads(text)
    except json.JSONDecodeError:
        raise RegistryError("registry_catalog_invalid") from None
    if not isinstance(catalog, dict) or catalog.get("schema_version") != 1:
        raise RegistryError("registry_catalog_invalid")
    raw_rules = catalog.get("classifications")
    if not isinstance(raw_rules, list):
        raise RegistryError("registry_catalog_invalid")
    rules: list[dict[str, str]] = []
    for value in raw_rules:
        if not isinstance(value, dict):
            raise RegistryError("registry_catalog_invalid")
        pattern = value.get("pattern")
        compatibility = value.get("compatibility")
        if (
            not isinstance(pattern, str)
            or not pattern.startswith(("claude/", "skills/shared/"))
            or compatibility not in CLASSIFICATION_VALUES
        ):
            raise RegistryError("registry_catalog_invalid")
        rules.append({"pattern": pattern, "compatibility": compatibility})
    raw_managed = catalog.get("managed_skills")
    if not isinstance(raw_managed, list):
        raise RegistryError("registry_catalog_invalid")
    managed: dict[str, str] = {}
    for value in raw_managed:
        if not isinstance(value, dict):
            raise RegistryError("registry_catalog_invalid")
        name = value.get("name")
        source = value.get("source")
        if not isinstance(name, str) or not isinstance(source, str):
            raise RegistryError("registry_catalog_invalid")
        if name in managed:
            raise RegistryError("registry_catalog_invalid")
        managed[name] = source
    return rules, managed


def _classification_for(
    relative_files: list[str], rules: list[dict[str, str]]
) -> str | None:
    matched: set[str] = set()
    for relative in relative_files:
        for rule in rules:
            if fnmatch.fnmatchcase(relative, rule["pattern"]):
                matched.add(rule["compatibility"])
    if not matched:
        return None
    if len(matched) > 1:
        raise RegistryError("registry_classification_overlap")
    return matched.pop()


def build(repo: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Derive the registry. Returns (skills, errors); a skill with an error is
    skipped so one bad skill cannot hide the state of the other 38."""
    rules, managed = _load_catalog(repo)
    managed_sources = set(managed.values())
    roots = [root for root in AUDIENCES.values() if (repo / root).is_dir()]
    listed = _git_listed(repo, roots)
    skills: list[dict[str, Any]] = []
    errors: list[str] = []
    for audience in sorted(AUDIENCES):
        root = repo / AUDIENCES[audience]
        if not root.is_dir():
            continue
        if root.is_symlink():
            errors.append(f"registry_skill_source_invalid {AUDIENCES[audience]}")
            continue
        for skill_dir in sorted(root.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            source = f"{AUDIENCES[audience]}/{skill_dir.name}"
            try:
                frontmatter = _frontmatter(skill_dir / "SKILL.md")
                if frontmatter["name"] != skill_dir.name:
                    raise RegistryError("registry_skill_invalid")
                files = _skill_files(skill_dir, listed, AUDIENCES[audience])
                hashes = _file_hashes(files, skill_dir)
                classification = _classification_for(
                    sorted(f"{source}/{relative}" for relative in hashes),
                    rules,
                )
                if classification is None and audience in CLASSIFIED_AUDIENCES:
                    raise RegistryError("registry_unclassified")
                skills.append(
                    {
                        "audience": audience,
                        "classification": classification,
                        "description": frontmatter["description"],
                        "files": len(hashes),
                        "managed": source in managed_sources,
                        "name": frontmatter["name"],
                        "source": source,
                        "status": frontmatter.get("status", "active"),
                        "tree_sha256": _tree_hash(hashes),
                    }
                )
            except RegistryError as error:
                errors.append(f"{error.code} {source}")
    for name, source in sorted(managed.items()):
        if not any(skill["source"] == source for skill in skills):
            errors.append(f"registry_managed_unknown {name} {source}")
    skills.sort(key=lambda skill: skill["source"])
    return skills, errors


def render(skills: list[dict[str, Any]]) -> str:
    return (
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "skills": skills},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def validate(repo: Path) -> dict[str, Any]:
    skills, errors = build(repo)
    path = artifact_path(repo)
    if not path.exists():
        errors.append("registry_missing")
    else:
        try:
            text = _read_bounded(path, MAX_REGISTRY_BYTES, "registry_artifact_invalid")
            artifact = json.loads(text)
            if not isinstance(artifact, dict) or artifact.get("schema_version") != SCHEMA_VERSION:
                raise RegistryError("registry_artifact_invalid")
        except (RegistryError, json.JSONDecodeError):
            errors.append("registry_artifact_invalid")
        else:
            if text != render(skills):
                errors.append("registry_stale")
    return {
        "ok": not errors,
        "command": "validate",
        "skills": len(skills),
        "errors": sorted(errors),
    }


def update(repo: Path) -> dict[str, Any]:
    skills, errors = build(repo)
    if errors:
        for error in sorted(errors):
            print(f"ccc-skill-registry: {error}", file=sys.stderr)
        raise RegistryError("registry_update_refused")
    content = render(skills)
    path = artifact_path(repo)
    written = False
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        directory = path.parent
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, delete=False
        )
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.chmod(handle.name, 0o644)
            os.replace(handle.name, path)
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise
        written = True
    return {"ok": True, "command": "update", "skills": len(skills), "written": written}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("render", "validate", "update"))
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args(argv)
    repo = Path(os.path.abspath(os.fspath(args.repo_root)))
    try:
        if args.command == "render":
            skills, errors = build(repo)
            if errors:
                for error in sorted(errors):
                    print(f"ccc-skill-registry: {error}", file=sys.stderr)
                return 2
            print(render(skills), end="")
        elif args.command == "validate":
            result = validate(repo)
            if not result["ok"]:
                for error in result["errors"]:
                    print(f"ccc-skill-registry: {error}", file=sys.stderr)
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                return 2
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            result = update(repo)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except RegistryError as error:
        print(f"ccc-skill-registry: {error.code}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
