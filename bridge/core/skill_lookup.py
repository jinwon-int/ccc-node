"""Common skill search/read logic shared by the JSON CLI and the MCP server.

One deterministic, fail-closed implementation backs both consumers (#1678):
``scripts/ccc-skill-lookup.py`` and ``bridge/core/family_skills_server.py``
resolve the same inputs to the same JSON results.  The module is stdlib-only
on purpose so it runs under any python3 interpreter, inside or outside the
bridge virtualenv.

Sources are the approved roots only:

- repo skills from ``skills/registry.json`` (CI-enforced truth; the registry
  is a derived view, so every read re-validates the actual files),
- installed skills under ``~/.claude/skills``, ``${CODEX_HOME:-~/.codex}``
  ``/skills`` and ``~/.piri/agent/skills`` for the current OS user only.

Trust boundaries follow ``skill_command``: no symlinks along the skill path,
current-user ownership, no group/other write bits, bounded size, valid UTF-8
without NUL, and a frontmatter ``name`` matching the directory.  Search
results advertise a ``revision`` (repo: the registry tree hash recomputed
from the worktree; installed: the SKILL.md sha256); reads detect drift and
return a stale error instead of silently serving different content.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Mapping

MAX_QUERY_CHARS = 256
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
MAX_SKILL_BYTES = 128 * 1024
_FLEET_MARKER = ".ccc-fleet-skill.json"
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_TREE_SHA_RE = re.compile(r"[0-9a-f]{64}")
_SOURCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*")
_REGISTRY_SCHEMA_VERSION = 1

RUNTIMES = ("repo", "claude", "codex", "piri")
_RUNTIME_RANK = {name: rank for rank, name in enumerate(RUNTIMES)}


class SkillLookupError(ValueError):
    """One structured, fail-closed lookup failure."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": str(self), **self.details}}


def policy_denial(env: Mapping[str, str] | None = None) -> str | None:
    """Return a denial reason when the caller's context may not read skills.

    The decision comes from the server/CLI process environment only — never
    from tool arguments — so a caller cannot self-declare an audience.
    """

    environment = os.environ if env is None else env
    profile = str(environment.get("CCC_NODE_ISOLATION_PROFILE", "fleet")).strip()
    if profile == "external":
        return "external_isolation_profile"
    if profile != "fleet":
        return "unknown_isolation_profile"
    # CCC_MEMORY_AUDIENCE carries the resolved audience *kind* (private/shared),
    # exactly as MemoryAudience.hook_environment exports it.
    audience = str(environment.get("CCC_MEMORY_AUDIENCE", "")).strip()
    if audience == "shared":
        return "shared_audience"
    if audience and audience != "private":
        return "unknown_audience"
    return None


def _require_policy(env: Mapping[str, str] | None = None) -> None:
    denial = policy_denial(env)
    if denial is not None:
        raise SkillLookupError("policy_denied", "skill lookup denied by node policy", reason=denial)


def repo_root(env: Mapping[str, str] | None = None) -> Path:
    """The trusted repo root whose registry/skills are served."""

    environment = os.environ if env is None else env
    configured = str(environment.get("CCC_SKILL_LOOKUP_REPO_ROOT", "")).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    # .../<repo>/telegram_bot(core alias)/skill_lookup.py -> repo root.
    return Path(__file__).resolve().parents[2]


def _home(env: Mapping[str, str] | None) -> Path:
    environment = os.environ if env is None else env
    override = str(environment.get("CCC_SKILL_LOOKUP_HOME", "")).strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home()


def installed_roots(env: Mapping[str, str] | None = None) -> dict[str, Path]:
    """Per-runtime installed roots; never another OS user's home."""

    environment = os.environ if env is None else env
    home = _home(env)
    codex_home = str(environment.get("CODEX_HOME", "")).strip()
    return {
        "claude": home / ".claude" / "skills",
        "codex": (Path(codex_home).expanduser() if codex_home else home / ".codex") / "skills",
        "piri": home / ".piri" / "agent" / "skills",
    }


def _frontmatter(text: str) -> dict[str, str] | None:
    """Tolerant top-level frontmatter parse (registry-compatible rules)."""

    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        if line[0] in " \t-#" or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in values:
            return None
        if value:
            values[key] = value
    name = values.get("name")
    description = values.get("description")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return None
    if not description:
        return None
    return values


def _resolve_skill_file(root: Path, name: str) -> tuple[Path, bytes]:
    """Resolve and fail-closed validate the skill path; returns file + bytes."""

    try:
        if root.is_symlink():
            raise SkillLookupError("unsafe_skill", "skill root must not be a symlink", root=str(root))
        root_resolved = root.resolve(strict=True)
        root_stat = root_resolved.stat()
    except FileNotFoundError as exc:
        raise SkillLookupError("not_found", "skill root is missing", root=str(root)) from exc
    except OSError as exc:
        raise SkillLookupError("unsafe_skill", "skill root is unreadable", root=str(root)) from exc
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != os.geteuid()
        or root_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise SkillLookupError("unsafe_skill", "skill root permissions are unsafe", root=str(root))

    candidate_dir = root_resolved / name
    candidate = candidate_dir / "SKILL.md"
    if not candidate_dir.exists() and not candidate.exists():
        raise SkillLookupError("not_found", "skill is not installed here", skill=f"{name}")
    try:
        if candidate_dir.is_symlink() or candidate.is_symlink():
            raise SkillLookupError("unsafe_skill", "skill path must not be a symlink", skill=name)
        directory_stat = candidate_dir.stat()
        file_stat = candidate.stat()
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise SkillLookupError("not_found", "skill installation is incomplete", skill=name) from exc
    except OSError as exc:
        raise SkillLookupError("unsafe_skill", "skill path is unreadable", skill=name) from exc

    if not resolved.is_relative_to(root_resolved):
        raise SkillLookupError("unsafe_skill", "skill path escapes its trusted root", skill=name)
    if not stat.S_ISDIR(directory_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise SkillLookupError("unsafe_skill", "skill installation type is invalid", skill=name)
    if directory_stat.st_uid != os.geteuid() or file_stat.st_uid != os.geteuid():
        raise SkillLookupError("unsafe_skill", "skill installation owner is invalid", skill=name)
    if (directory_stat.st_mode | file_stat.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
        raise SkillLookupError("unsafe_skill", "skill is writable by another user", skill=name)

    payload = _validated_skill_payload(resolved, name)
    return resolved, payload


def _validated_skill_payload(resolved: Path, name: str) -> bytes:
    """Bounded, fail-closed content read: size, NUL, UTF-8, frontmatter name."""

    try:
        file_stat = resolved.stat()
    except OSError as exc:
        raise SkillLookupError("invalid_content", "skill file is unreadable", skill=name) from exc
    if file_stat.st_size <= 0 or file_stat.st_size > MAX_SKILL_BYTES:
        raise SkillLookupError("invalid_content", "skill file size is invalid", skill=name)
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise SkillLookupError("invalid_content", "skill file is unreadable", skill=name) from exc
    if len(payload) != file_stat.st_size or b"\x00" in payload:
        raise SkillLookupError("invalid_content", "skill file content is invalid", skill=name)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillLookupError("invalid_content", "skill file is not UTF-8", skill=name) from exc
    frontmatter = _frontmatter(text)
    if frontmatter is None or frontmatter.get("name") != name:
        raise SkillLookupError(
            "invalid_content", "skill frontmatter name does not match", skill=name
        )
    return payload


def _validated_skill_file(root: Path, name: str) -> tuple[Path, bytes]:
    """Fail-closed path + content resolution; raises or returns (SKILL.md, bytes)."""

    return _resolve_skill_file(root, name)


def _git_listed(repo: Path, roots: list[str]) -> set[str] | None:
    """Git-tracked repo-relative paths under ``roots``, or None without git."""

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
    return {
        entry
        for entry in completed.stdout.decode("utf-8", "replace").split("\0")
        if entry
    }


def _tree_hash(file_hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(file_hashes.items()):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(value.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def repo_tree_revision(skill_dir: Path, repo: Path) -> str:
    """Recompute the registry tree hash from the worktree; "" means drift.

    Mirrors ``ccc-skill-registry`` enumeration: git-tracked files preferred,
    filesystem walk fallback (untracked new skill dir), symlinks are drift.
    An unchanged skill hashes back to its registry ``tree_sha256``; any local
    modification, deletion or stray-file change makes this differ, which the
    caller reports as ``stale_revision`` instead of serving new content.
    """

    try:
        relative = skill_dir.resolve(strict=True).relative_to(repo).as_posix()
    except (OSError, ValueError):
        return ""
    prefix = f"{relative}/"
    listed = _git_listed(repo, [relative])
    hashes: dict[str, str] = {}
    if listed is None or not any(entry.startswith(prefix) for entry in listed):
        try:
            entries = sorted(skill_dir.rglob("*"))
        except OSError:
            return ""
        for item in entries:
            rel = item.relative_to(skill_dir).as_posix()
            if rel == _FLEET_MARKER:
                continue
            try:
                metadata = item.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    return ""
                if stat.S_ISDIR(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    return ""
                hashes[rel] = hashlib.sha256(item.read_bytes()).hexdigest()
            except OSError:
                return ""
    else:
        for entry in sorted(listed):
            if not entry.startswith(prefix) or entry == prefix + _FLEET_MARKER:
                continue
            path = repo.joinpath(*entry.split("/"))
            rel = entry[len(prefix):]
            try:
                metadata = path.lstat()
            except OSError:
                continue  # tracked but deleted in this worktree: drift by absence
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                return ""
            try:
                hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                return ""
    if not hashes:
        return ""
    return _tree_hash(hashes)


def _load_registry(repo: Path) -> dict[str, dict[str, Any]]:
    """Parse and validate skills/registry.json into {source: entry}.

    Sources are unique registry keys; the same skill *name* may legally exist
    under two sources (e.g. ``skills/gh-pr-flow`` and
    ``codex/skills/gh-pr-flow``), which is why repo skill ids carry the
    source path rather than the bare name.
    """

    path = repo / "skills" / "registry.json"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SkillLookupError("registry_invalid", "skill registry is unreadable") from exc
    if len(raw) > MAX_SKILL_BYTES * 8:
        raise SkillLookupError("registry_invalid", "skill registry exceeds the size bound")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillLookupError("registry_invalid", "skill registry is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("schema_version") != _REGISTRY_SCHEMA_VERSION:
        raise SkillLookupError("registry_invalid", "skill registry schema is unsupported")
    skills = document.get("skills")
    if not isinstance(skills, list):
        raise SkillLookupError("registry_invalid", "skill registry skills list is invalid")
    entries: dict[str, dict[str, Any]] = {}
    for skill in skills:
        if not isinstance(skill, dict):
            raise SkillLookupError("registry_invalid", "skill registry entry is invalid")
        name = skill.get("name")
        source = skill.get("source")
        revision = skill.get("tree_sha256")
        status = skill.get("status")
        audience = skill.get("audience")
        if (
            not isinstance(name, str)
            or not _NAME_RE.fullmatch(name)
            or not isinstance(source, str)
            or not _SOURCE_RE.fullmatch(source)
            or source in entries
            or ".." in source.split("/")
            or not isinstance(revision, str)
            or not _TREE_SHA_RE.fullmatch(revision)
            or status not in ("active", "deprecated")
            or not isinstance(audience, str)
            or not audience
        ):
            raise SkillLookupError("registry_invalid", "skill registry entry is invalid")
        resolved = repo.joinpath(*source.split("/"))
        if not resolved.resolve().is_relative_to(repo):
            raise SkillLookupError("registry_invalid", "skill registry source escapes the repo")
        entries[source] = dict(skill)
    return entries


def _repo_result(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "skill_id": f"repo:{entry['source']}",
        "name": entry["name"],
        "description": entry["description"],
        "runtime": "repo",
        "audience": entry["audience"],
        "source": entry["source"],
        "active": entry["status"] == "active",
        "revision": entry["tree_sha256"],
    }


def _installed_result(runtime: str, name: str, source: Path, frontmatter: Mapping[str, str], revision: str) -> dict[str, Any]:
    return {
        "skill_id": f"{runtime}:{name}",
        "name": name,
        "description": frontmatter["description"],
        "runtime": runtime,
        "audience": runtime,
        "source": str(source),
        "active": frontmatter.get("status", "active") == "active",
        "revision": revision,
    }


def _search_repo(entries: dict[str, dict[str, Any]], tokens: list[str]) -> list[dict[str, Any]]:
    results = []
    for source in sorted(entries):
        entry = entries[source]
        if entry["status"] != "active":
            continue
        haystack = f"{entry['name']}\n{entry['description']}".lower()
        if all(token in haystack for token in tokens):
            results.append(_repo_result(entry))
    return results


def _search_installed(runtime: str, root: Path, tokens: list[str]) -> list[dict[str, Any]]:
    if root.is_symlink() or not root.is_dir():
        return []
    results: list[dict[str, Any]] = []
    try:
        candidates = sorted(root.iterdir())
    except OSError:
        return []
    for candidate in candidates:
        name = candidate.name
        if not _NAME_RE.fullmatch(name) or not candidate.is_dir() or candidate.is_symlink():
            continue
        skill_file = candidate / "SKILL.md"
        try:
            if skill_file.is_symlink() or not skill_file.is_file():
                continue
            payload = skill_file.read_bytes()
        except OSError:
            continue
        if len(payload) == 0 or len(payload) > MAX_SKILL_BYTES or b"\x00" in payload:
            continue
        try:
            frontmatter = _frontmatter(payload.decode("utf-8"))
        except UnicodeDecodeError:
            continue
        if frontmatter is None or frontmatter.get("name") != name:
            continue
        if frontmatter.get("status", "active") != "active":
            continue
        haystack = f"{name}\n{frontmatter['description']}".lower()
        if all(token in haystack for token in tokens):
            results.append(
                _installed_result(runtime, name, skill_file, frontmatter, hashlib.sha256(payload).hexdigest())
            )
    return results


def search(
    query: str,
    runtime: str | None = None,
    limit: int | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Deterministic name/description search across the approved roots."""

    _require_policy(env)
    if not isinstance(query, str) or not query.strip():
        raise SkillLookupError("invalid_query", "query must be a non-empty string")
    query = query.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise SkillLookupError("invalid_query", f"query exceeds {MAX_QUERY_CHARS} characters")
    if runtime is not None and runtime not in RUNTIMES:
        raise SkillLookupError("invalid_query", f"runtime must be one of {', '.join(RUNTIMES)}")
    if limit is None:
        limit = DEFAULT_LIMIT
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise SkillLookupError("invalid_query", f"limit must be an integer in [1, {MAX_LIMIT}]")
    tokens = [token.lower() for token in query.split() if token]

    results: list[dict[str, Any]] = []
    if runtime in (None, "repo"):
        results.extend(_search_repo(_load_registry(repo_root(env)), tokens))
    if runtime in (None, "claude", "codex", "piri"):
        for name, root in installed_roots(env).items():
            if runtime is None or name == runtime:
                results.extend(_search_installed(name, root, tokens))
    results.sort(key=lambda result: (_RUNTIME_RANK[result["runtime"]], result["name"]))
    truncated = len(results) > limit
    return {
        "query": query,
        "runtime": runtime,
        "limit": limit,
        "results": results[:limit],
        "truncated": truncated,
    }


def read(
    skill_id: str,
    expected_revision: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Read one skill by exact id (``repo:<source>`` or ``<runtime>:<name>``)."""

    _require_policy(env)
    if not isinstance(skill_id, str) or skill_id.count(":") != 1:
        raise SkillLookupError(
            "invalid_skill_id",
            "skill_id must be 'repo:<source>' or '<runtime>:<name>'",
        )
    runtime, selector = skill_id.split(":", 1)
    if runtime not in RUNTIMES:
        raise SkillLookupError("invalid_skill_id", "skill_id runtime prefix is unknown")
    if runtime == "repo":
        if (
            not selector
            or "/" not in selector
            or not _SOURCE_RE.fullmatch(selector)
            or ".." in selector.split("/")
        ):
            raise SkillLookupError("invalid_skill_id", "repo skill_id must be a registry source path")
    elif not _NAME_RE.fullmatch(selector):
        raise SkillLookupError("invalid_skill_id", "installed skill_id must be a skill directory name")
    if expected_revision is not None and not (
        isinstance(expected_revision, str) and _TREE_SHA_RE.fullmatch(expected_revision)
    ):
        raise SkillLookupError("invalid_revision", "revision must be a 64-hex digest")

    if runtime == "repo":
        repo = repo_root(env)
        entries = _load_registry(repo)
        entry = entries.get(selector)
        if entry is None:
            raise SkillLookupError("not_found", "skill is not in the repo registry", skill_id=skill_id)
        name = str(entry["name"])
        skill_dir = repo.joinpath(*selector.split("/"))
        try:
            # Same fail-closed layout as installed roots: parent dir + skill
            # name (registry build already enforces frontmatter name == dir).
            resolved, payload = _validated_skill_file(skill_dir.parent, skill_dir.name)
        except SkillLookupError as exc:
            exc.details.setdefault("skill_id", skill_id)
            raise
        current = repo_tree_revision(resolved.parent, repo)
        advertised = str(entry["tree_sha256"])
        if current != advertised:
            raise SkillLookupError(
                "stale_revision",
                "repo skill content no longer matches the registry revision",
                skill_id=skill_id,
                revision=advertised,
                current_revision=current or None,
            )
        revision = advertised
    else:
        name = selector
        root = installed_roots(env)[runtime]
        try:
            resolved, payload = _validated_skill_file(root, name)
        except SkillLookupError as exc:
            exc.details.setdefault("skill_id", skill_id)
            raise
        revision = hashlib.sha256(payload).hexdigest()

    if expected_revision is not None and expected_revision != revision:
        raise SkillLookupError(
            "stale_revision",
            "skill changed since the search result; search again",
            skill_id=skill_id,
            revision=revision,
            current_revision=revision,
        )
    text = payload.decode("utf-8")
    frontmatter = _frontmatter(text)
    assert frontmatter is not None  # _validated_skill_file guarantees this
    return {
        "skill_id": skill_id,
        "name": name,
        "runtime": runtime,
        "source": str(resolved),
        "revision": revision,
        "description": frontmatter["description"],
        "bytes": len(payload),
        "body": text,
    }
