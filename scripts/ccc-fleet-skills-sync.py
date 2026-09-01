#!/usr/bin/env python3
"""Plan or atomically install approved private fleet skills from an exact commit."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any


NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SUPPORT_DIRS = {"references", "scripts", "templates"}
AUDIENCES = {"shared", "claude", "codex"}
MAX_FILES = 16
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 256 * 1024
SECRET_PATTERNS = (
    re.compile(r"(?:ghp|gho|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}", re.I),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}", re.I),
    re.compile(r"AKIA[A-Z0-9]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"Bearer [A-Za-z0-9._~+/=-]{20,}", re.I),
    re.compile(r"\[REDACTED", re.I),
    re.compile(
        r"(?:password|passwd|secret|token|api[_-]?key|authorization)"
        r"\s*[=:]\s*[\"']?[A-Za-z0-9+/_-]{16,}",
        re.I,
    ),
)
HOME_PATH_RE = re.compile(r"(?:^|[^A-Za-z0-9_])/(?:root|home|Users)/[^\s`'\"<>]+")
# Android (Termux) app-private root: the kernel only lets the app uid and
# root traverse into it, unlike its system-owned 0771 parents (#1390).
TERMUX_APP_ROOT = Path("/data/data/com.termux")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


class SyncError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Config:
    home: Path
    state_dir: Path
    repo: str
    remote: str
    ref: str
    claude_root: Path
    codex_root: Path


@dataclass(frozen=True)
class SkillFile:
    relative: str
    content: bytes
    executable: bool


@dataclass(frozen=True)
class ApprovedSkill:
    audience: str
    name: str
    files: tuple[SkillFile, ...]
    tree_sha256: str
    source_candidate_id: str
    source_tree_sha256: str
    reviewed_by: str | None


@dataclass(frozen=True)
class Operation:
    provider: str
    skill: ApprovedSkill
    target: Path
    action: str


# Actions that must never reach apply_operations: noop (already exact) and
# skip-repo-managed (a repo-managed installation owns the target — the higher
# precedence layer, #1344).
SKIP_ACTIONS = frozenset({"noop", "skip-repo-managed"})


def json_line(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def config(args: argparse.Namespace) -> Config:
    home = Path(os.environ.get("HOME", "/root")).absolute()
    claude_dir = Path(os.environ.get("CCC_CLAUDE_DIR", home / ".claude")).absolute()
    state_dir = Path(
        os.environ.get("CCC_FLEET_SKILLS_STATE_DIR", claude_dir / "state" / "fleet-skills")
    ).absolute()
    repo = os.environ.get("CCC_FLEET_SKILLS_REPO", "jinwon-int/fleet-skills")
    if not REPO_RE.fullmatch(repo):
        raise SyncError("repo_invalid")
    if not SHA_RE.fullmatch(args.ref):
        raise SyncError("exact_commit_required")
    return Config(
        home=home,
        state_dir=state_dir,
        repo=repo,
        remote=os.environ.get("CCC_FLEET_SKILLS_REMOTE", f"https://github.com/{repo}.git"),
        ref=args.ref,
        claude_root=Path(
            os.environ.get("CCC_FLEET_SKILLS_CLAUDE_DIR", claude_dir / "skills")
        ).absolute(),
        codex_root=Path(
            os.environ.get(
                "CCC_FLEET_SKILLS_CODEX_DIR",
                Path(os.environ.get("CODEX_HOME", home / ".codex")) / "skills",
            )
        ).absolute(),
    )


def run(argv: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SyncError("command_failed") from None
    if result.returncode != 0 or len(result.stdout) > 1024 * 1024 or len(result.stderr) > 1024 * 1024:
        raise SyncError("command_failed")
    return result


def private_repo_required(cfg: Config) -> None:
    run(["gh", "auth", "status", "--hostname", "github.com"])
    result = run(["gh", "repo", "view", cfg.repo, "--json", "isPrivate,visibility"])
    try:
        value = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SyncError("github_output_invalid") from None
    if (
        not isinstance(value, dict)
        or value.get("isPrivate") is not True
        or str(value.get("visibility", "")).upper() != "PRIVATE"
    ):
        raise SyncError("target_repo_not_private")


def private_root(path: Path) -> Path:
    """Validated platform-private root where the component walk may start.

    The walk normally starts at the filesystem anchor and requires every
    parent component to be root/euid-owned and not group/other-writable.
    Under Termux that is unsatisfiable by construction: HOME lives under
    /data/data/com.termux, whose /data and /data/data parents are
    system-owned 0771, and the app root's own files/ is 0771 by bootstrap
    default — so no state or skills path could ever pass (#1390). When the
    app root lies on the path and validates as a private root/euid 0700
    directory (the kernel-enforced app sandbox), the walk starts there.
    Everything else keeps the strict anchor walk. The env override exists
    for tests; it must satisfy the same validation, so it is safe by
    construction.
    """
    candidate = TERMUX_APP_ROOT
    if override := os.environ.get("CCC_FLEET_SKILLS_APP_ROOT"):
        candidate = Path(override)
        if not candidate.is_absolute():
            return Path(path.anchor)
    if path != candidate and candidate not in path.parents:
        return Path(path.anchor)
    try:
        metadata = candidate.lstat()
    except OSError:
        return Path(path.anchor)
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or mode & 0o077
    ):
        return Path(path.anchor)
    return candidate


def protected_walk(path: Path, code: str, *, create: bool, private_final: bool) -> None:
    """Validate path's parent components against the ownership walk.

    Components below a validated private root keep the symlink/dir and
    root/euid-ownership checks but skip the group/other-write check: no
    other user can traverse the private root, so those bits are inert
    there. The final component always stays fully strict.
    """
    start = private_root(path)
    sandboxed = start != Path(path.anchor)
    current = start
    for component in path.relative_to(start).parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if not create:
                raise
            current.mkdir(mode=0o700)
            metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise SyncError(code)
        mode = stat.S_IMODE(metadata.st_mode)
        sticky = metadata.st_uid == 0 and bool(mode & stat.S_ISVTX)
        writable_relaxed = sandboxed and current != path
        if metadata.st_uid not in {0, os.geteuid()} or (
            mode & 0o022 and not sticky and not writable_relaxed
        ):
            raise SyncError(code)
        if private_final and current == path and mode & 0o077:
            raise SyncError(code)


def private_dir(path: Path) -> None:
    protected_walk(path, "state_path_unsafe", create=True, private_final=True)


def safe_root(path: Path, *, create: bool) -> None:
    if create:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise SyncError("skills_root_unsafe")
    protected_walk(path, "skills_root_unsafe", create=False, private_final=False)


def scan(payload: bytes) -> str:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise SyncError("source_not_utf8") from None
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        raise SyncError("source_sensitive")
    if HOME_PATH_RE.search(text):
        raise SyncError("source_node_fact")
    for raw in IPV4_RE.findall(text):
        octets = raw.split(".")
        if any(int(part) > 255 for part in octets):
            continue
        if not raw.startswith("127.") and raw != "0.0.0.0":
            raise SyncError("source_node_fact")
    if any(
        raw.lower() not in {"git@github.com", "git@gitlab.com"}
        for raw in EMAIL_RE.findall(text)
    ):
        raise SyncError("source_node_fact")
    return text


def frontmatter(payload: bytes, expected: str) -> None:
    lines = scan(payload).splitlines()
    if not lines or lines[0] != "---":
        raise SyncError("frontmatter_invalid")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise SyncError("frontmatter_invalid") from None
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            raise SyncError("frontmatter_invalid")
        key, value = (part.strip() for part in line.split(":", 1))
        if key in fields or not value:
            raise SyncError("frontmatter_invalid")
        fields[key] = value
    if set(fields) != {"name", "description"} or fields["name"] != expected:
        raise SyncError("frontmatter_invalid")
    if not 20 <= len(fields["description"]) <= 1024 or len(lines[end + 1 :]) < 3:
        raise SyncError("frontmatter_invalid")


def approval(path: Path, name: str) -> tuple[str, str, str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise SyncError("approval_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SyncError("approval_invalid") from None
    # a2a-nexus#2030: reviewed_by is optional (legacy approvals keep it).
    required = {
        "schema_version", "source_candidate_id", "source_tree_sha256",
        "approved_at",
    }
    keys = set(value)
    if (
        not isinstance(value, dict)
        or keys not in (required, required | {"reviewed_by"})
        or value.get("schema_version") != 1
    ):
        raise SyncError("approval_invalid")
    candidate_id = value.get("source_candidate_id")
    source_hash = value.get("source_tree_sha256")
    reviewer = value.get("reviewed_by")
    timestamp = value.get("approved_at")
    if (
        not isinstance(candidate_id, str)
        or not candidate_id.startswith(name + "-")
        or not re.fullmatch(r"[a-z0-9-]+-[0-9a-f]{12}", candidate_id)
        or not isinstance(source_hash, str)
        or not HASH_RE.fullmatch(source_hash)
        or (reviewer is not None and (
            not isinstance(reviewer, str)
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", reviewer)
        ))
        or not isinstance(timestamp, str)
        or not timestamp.endswith("Z")
    ):
        raise SyncError("approval_invalid")
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise SyncError("approval_invalid") from None
    return candidate_id, source_hash, reviewer


def skill_tree(path: Path, audience: str) -> ApprovedSkill:
    name = path.name
    if path.is_symlink() or not path.is_dir() or not NAME_RE.fullmatch(name):
        raise SyncError("approved_layout_invalid")
    candidate_id, source_hash, reviewer = approval(path / "approval.json", name)
    rows: list[SkillFile] = []
    total = 0
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path).as_posix()
        parts = relative.split("/")
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise SyncError("source_symlink")
        if stat.S_ISDIR(metadata.st_mode):
            if parts[0] not in SUPPORT_DIRS:
                raise SyncError("source_path_not_allowed")
            continue
        if relative == "approval.json":
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise SyncError("source_file_unsafe")
        if relative != "SKILL.md" and (
            parts[0] not in SUPPORT_DIRS
            or any(part in {"", ".", ".."} or not COMPONENT_RE.fullmatch(part) for part in parts)
        ):
            raise SyncError("source_path_not_allowed")
        if metadata.st_size > MAX_FILE_BYTES:
            raise SyncError("source_tree_too_large")
        payload = item.read_bytes()
        scan(payload)
        total += len(payload)
        if total > MAX_TOTAL_BYTES or len(rows) >= MAX_FILES:
            raise SyncError("source_tree_too_large")
        rows.append(SkillFile(relative, payload, bool(metadata.st_mode & 0o111)))
    if not rows or rows[0].relative != "SKILL.md":
        raise SyncError("skill_missing")
    frontmatter(rows[0].content, name)
    digest = hashlib.sha256()
    for row in rows:
        digest.update(row.relative.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(row.content).hexdigest().encode())
        digest.update(b"\0")
    return ApprovedSkill(
        audience=audience,
        name=name,
        files=tuple(rows),
        tree_sha256=digest.hexdigest(),
        source_candidate_id=candidate_id,
        source_tree_sha256=source_hash,
        reviewed_by=reviewer,
    )


def approved_skills(checkout: Path) -> list[ApprovedSkill]:
    root = checkout / "approved"
    if root.is_symlink() or not root.is_dir():
        raise SyncError("approved_root_missing")
    skills: list[ApprovedSkill] = []
    names: set[str] = set()
    for audience_path in sorted(path for path in root.iterdir() if path.is_dir()):
        audience = audience_path.name
        if audience not in AUDIENCES or audience_path.is_symlink():
            raise SyncError("approved_layout_invalid")
        for path in sorted(child for child in audience_path.iterdir() if child.is_dir()):
            skill = skill_tree(path, audience)
            if skill.name in names:
                raise SyncError("approved_name_duplicate")
            names.add(skill.name)
            skills.append(skill)
    return skills


def checkout(cfg: Config, parent: Path) -> Path:
    private_repo_required(cfg)
    work = parent / "repo"
    run(["git", "clone", "--quiet", "--no-checkout", cfg.remote, str(work)])
    run(["git", "checkout", "--quiet", "--detach", cfg.ref], cwd=work)
    actual = run(["git", "rev-parse", "HEAD"], cwd=work).stdout.decode("ascii").strip()
    if actual != cfg.ref:
        raise SyncError("commit_mismatch")
    return work


def marker(cfg: Config, provider: str, skill: ApprovedSkill) -> dict[str, object]:
    return {
        "schema_version": 1,
        "manager": "ccc-node-fleet-skills",
        "repo": cfg.repo,
        "commit": cfg.ref,
        "provider": provider,
        "audience": skill.audience,
        "name": skill.name,
        "tree_sha256": skill.tree_sha256,
        "source_candidate_id": skill.source_candidate_id,
        "source_tree_sha256": skill.source_tree_sha256,
        "reviewed_by": skill.reviewed_by,
    }


def existing_marker(path: Path) -> dict[str, Any] | None:
    marker_path = path / ".ccc-fleet-skill.json"
    if not marker_path.exists():
        return None
    try:
        metadata = marker_path.lstat()
        if (
            marker_path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > 16 * 1024
        ):
            raise SyncError("installed_marker_unsafe")
        value = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SyncError("installed_marker_unsafe") from None
    if not isinstance(value, dict) or value.get("manager") != "ccc-node-fleet-skills":
        raise SyncError("installed_marker_invalid")
    return value


def installed_matches(path: Path, skill: ApprovedSkill) -> bool:
    actual: list[tuple[str, bytes, bool]] = []
    try:
        for item in sorted(path.rglob("*")):
            relative = item.relative_to(path).as_posix()
            if relative == ".ccc-fleet-skill.json":
                continue
            metadata = item.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                return False
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE_BYTES:
                return False
            actual.append((relative, item.read_bytes(), bool(metadata.st_mode & 0o111)))
    except OSError:
        return False
    wanted = [(row.relative, row.content, row.executable) for row in skill.files]
    return actual == wanted


def repo_managed_marker(path: Path) -> dict[str, Any] | None:
    """The Codex provisioner's ownership marker, fail-closed on tampering."""
    marker_path = path / ".ccc-node-managed.json"
    if not marker_path.exists():
        return None
    try:
        metadata = marker_path.lstat()
        if (
            marker_path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_size > 16 * 1024
        ):
            raise SyncError("repo_managed_marker_unsafe")
        value = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SyncError("repo_managed_marker_unsafe") from None
    if not isinstance(value, dict) or value.get("manager") != "ccc-node":
        raise SyncError("repo_managed_marker_invalid")
    return value


def repo_managed_names(cfg: Config) -> set[str]:
    """Skill names the repo layer currently owns via setup.sh's manifest.

    setup.sh tracks its Claude-side repo skills in
    ``<claude-dir>/state/repo-skills.manifest`` (no per-directory marker), so
    manifest membership is the ownership signal there. A missing manifest
    simply means the repo layer owns nothing — the fail-safe direction.
    """
    manifest = cfg.claude_root.parent / "state" / "repo-skills.manifest"
    try:
        text = manifest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    if len(text) > 256 * 1024:
        raise SyncError("repo_managed_manifest_unsafe")
    names: set[str] = set()
    for line in text.splitlines():
        name = line.split(" ", 1)[0].strip()
        if name:
            names.add(name)
    return names


def is_repo_managed(cfg: Config, provider: str, target: Path, name: str) -> bool:
    """Whether the target is owned by the higher-precedence repo layer.

    Precedence contract (#1344): repo-managed(setup) > fleet-approved(sync)
    > autosave-owned > user-owned. The repo layer signals ownership through
    the Codex provisioner marker (codex root) or the setup manifest (claude
    root); fleet-sync must skip, never fight it.
    """
    if provider == "claude":
        return name in repo_managed_names(cfg)
    return repo_managed_marker(target) is not None


def operations(cfg: Config, skills: list[ApprovedSkill], *, create_roots: bool) -> list[Operation]:
    roots = {"claude": cfg.claude_root, "codex": cfg.codex_root}
    for root in roots.values():
        safe_root(root, create=create_roots)
    rows: list[Operation] = []
    for skill in skills:
        providers = ("claude", "codex") if skill.audience == "shared" else (skill.audience,)
        for provider in providers:
            target = roots[provider] / skill.name
            if target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_dir():
                    raise SyncError("target_conflict")
                current = existing_marker(target)
                if current is None:
                    if is_repo_managed(cfg, provider, target, skill.name):
                        rows.append(Operation(provider, skill, target, "skip-repo-managed"))
                        continue
                    raise SyncError("target_user_owned")
                wanted = marker(cfg, provider, skill)
                action = "noop" if current == wanted and installed_matches(target, skill) else "update"
            else:
                action = "install"
            rows.append(Operation(provider, skill, target, action))
    return rows


def write_skill(stage: Path, cfg: Config, operation: Operation) -> None:
    stage.chmod(0o700)
    for row in operation.skill.files:
        destination = stage / row.relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.write_bytes(row.content)
        destination.chmod(0o755 if row.executable else 0o644)
    marker_path = stage / ".ccc-fleet-skill.json"
    marker_path.write_text(
        json.dumps(marker(cfg, operation.provider, operation.skill), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    marker_path.chmod(0o600)


def apply_operations(cfg: Config, rows: list[Operation]) -> int:
    changed = [row for row in rows if row.action not in SKIP_ACTIONS]
    if not changed:
        return 0
    backup_root = cfg.state_dir / "backups" / cfg.ref
    private_dir(backup_root)
    staged: dict[tuple[str, str], Path] = {}
    backups: dict[tuple[str, str], Path] = {}
    applied: list[Operation] = []
    try:
        for row in changed:
            stage = Path(tempfile.mkdtemp(prefix=f".ccc-fleet-stage-{row.skill.name}-", dir=row.target.parent))
            write_skill(stage, cfg, row)
            staged[(row.provider, row.skill.name)] = stage
        for row in changed:
            key = (row.provider, row.skill.name)
            backup = backup_root / f"{row.provider}-{row.skill.name}"
            if backup.exists():
                raise SyncError("backup_collision")
            if row.target.exists():
                if row.target.stat().st_dev != backup_root.stat().st_dev:
                    raise SyncError("backup_cross_device")
                os.replace(row.target, backup)
                backups[key] = backup
            os.replace(staged[key], row.target)
            applied.append(row)
    except (OSError, SyncError):
        for row in reversed(changed):
            key = (row.provider, row.skill.name)
            if key in backups and backups[key].exists():
                if row.target.exists():
                    shutil.rmtree(row.target)
                os.replace(backups[key], row.target)
            elif row in applied and row.target.exists():
                shutil.rmtree(row.target)
        raise SyncError("apply_failed") from None
    finally:
        for path in staged.values():
            if path.exists():
                shutil.rmtree(path)
    receipt = cfg.state_dir / "installed.json"
    descriptor, raw = tempfile.mkstemp(prefix=".installed-", dir=cfg.state_dir)
    temporary = Path(raw)
    try:
        os.fchmod(descriptor, 0o600)
        payload = {
            "schema_version": 1,
            "repo": cfg.repo,
            "commit": cfg.ref,
            "skills": [
                {
                    "provider": row.provider,
                    "name": row.skill.name,
                    "audience": row.skill.audience,
                    "tree_sha256": row.skill.tree_sha256,
                }
                for row in rows
            ],
        }
        os.write(descriptor, (json_line(payload) + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, receipt)
    return len(changed)


def execute(cfg: Config, *, apply: bool) -> dict[str, object]:
    private_dir(cfg.state_dir)
    lock_path = cfg.state_dir / "sync.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        metadata = os.fstat(lock_fd)
        if metadata.st_uid != os.geteuid() or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise SyncError("lock_unsafe")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("locked") from None
        with tempfile.TemporaryDirectory(prefix="checkout-", dir=cfg.state_dir) as raw:
            skills = approved_skills(checkout(cfg, Path(raw)))
            rows = operations(cfg, skills, create_roots=apply)
            changed = apply_operations(cfg, rows) if apply else 0
    finally:
        os.close(lock_fd)
    return {
        "ok": True,
        "mode": "apply" if apply else "plan",
        "repo": cfg.repo,
        "commit": cfg.ref,
        "changed": changed,
        "operations": [
            {
                "provider": row.provider,
                "audience": row.skill.audience,
                "name": row.skill.name,
                "action": row.action,
                "tree_sha256": row.skill.tree_sha256,
            }
            for row in rows
        ],
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    sub = value.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply"):
        command = sub.add_parser(name)
        command.add_argument("--ref", required=True, help="exact 40-character commit SHA")
    return value


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        result = execute(config(args), apply=args.command == "apply")
    except SyncError as error:
        result = {"ok": False, "code": error.code}
    except (OSError, TypeError, ValueError, RecursionError):
        result = {"ok": False, "code": "internal_error"}
    print(json_line(result))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
