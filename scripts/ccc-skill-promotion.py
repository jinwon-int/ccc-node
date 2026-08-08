#!/usr/bin/env python3
"""Promote safe autosave-managed skills through draft GitHub pull requests.

The local autosave pipeline is intentionally node-local.  This helper adds the
separate, opt-in publication boundary: it reclassifies and rescans an installed
skill, snapshots only a bounded allowlist of files, and opens a draft PR against
the ccc-node repository.  It never merges a PR or pushes to the default branch.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any


_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ALLOWED_SUPPORT_DIRS = {"references", "scripts", "templates"}
_MAX_FILES = 16
_MAX_FILE_BYTES = 64 * 1024
_MAX_TOTAL_BYTES = 256 * 1024
_MAX_COMMAND_OUTPUT = 1024 * 1024
_MAX_CANDIDATES_PER_RUN = 64
_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("gh-token", re.compile(r"(?:ghp|gho|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}", re.I)),
    ("api-key", re.compile(r"sk-[A-Za-z0-9_-]{20,}", re.I)),
    ("aws-key", re.compile(r"AKIA[A-Z0-9]{16}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"Bearer [A-Za-z0-9._~+/=-]{20,}", re.I)),
    ("redaction-marker", re.compile(r"\[REDACTED", re.I)),
    (
        "credential-assignment",
        re.compile(
            r"(?:password|passwd|secret|token|api[_-]?key|authorization)"
            r"\s*[=:]\s*[\"']?[A-Za-z0-9+/_-]{16,}",
            re.I,
        ),
    ),
    ("possible-token", re.compile(r"[A-Za-z0-9+/]{40,}")),
)
_HOME_PATH_RE = re.compile(r"(?:^|[^A-Za-z0-9_])/(?:root|home|Users)/[^\s`'\"<>]+")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_CLAUDE_COUPLINGS = (
    re.compile(r"(?:^|[^A-Za-z0-9_-])claude\s+-p(?:\s|$)", re.M),
    re.compile(r"(?:^|[^A-Za-z0-9_])(?:~|\$HOME|\$\{HOME[^}]*\})?/?\.claude/"),
    re.compile(r"\bCLAUDE_[A-Z0-9_]+\b"),
    re.compile(r"\bclaude/(?:hooks|skills)/", re.I),
    re.compile(r"\bclaude\s+mcp\b", re.I),
    re.compile(r"\bAgent tool\b"),
    re.compile(r"\bPreToolUse\b"),
)
_CODEX_COUPLINGS = (
    re.compile(r"(?:^|[^A-Za-z0-9_-])codex\s+exec(?:\s|$)", re.M),
    re.compile(r"(?:^|[^A-Za-z0-9_])(?:~|\$HOME|\$\{HOME[^}]*\})?/?\.codex/"),
    re.compile(r"\bCODEX_[A-Z0-9_]+\b"),
)


class PromotionError(RuntimeError):
    """A redaction-safe, body-free promotion failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Config:
    home: Path
    state_dir: Path
    promotion_state_dir: Path
    ownership_tool: Path
    repo: str
    remote: str
    base: str
    node: str
    providers: tuple[str, ...]
    provider_roots: dict[str, Path]
    max_prs: int
    enabled: bool
    autonomy: str


@dataclass(frozen=True)
class SnapshotFile:
    relative: str
    content: bytes
    executable: bool


@dataclass(frozen=True)
class Candidate:
    provider: str
    name: str
    skill_sha256: str
    tree_sha256: str
    source_dir: Path
    files: tuple[SnapshotFile, ...]
    description: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_line(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _bounded_int(raw: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(raw if raw is not None else default)
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default


def _safe_node(raw: str) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    value = re.sub(r"-+", "-", value)[:32].rstrip("-")
    return value or "node"


def _read_enabled_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
        if (
            not _path_components_safe(path, final_kind="file")
            or
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > 32
        ):
            return False
        return path.read_text(encoding="utf-8").strip().lower() == "true"
    except (OSError, UnicodeDecodeError):
        return False


def _autonomy_state(env: dict[str, str], state_dir: Path) -> str:
    value = env.get("CCC_AUTONOMY", "")
    if value in {"kill", "killed", "off", "OFF"}:
        return "kill"
    if value in {"dry-run", "dryrun", "dry", "DRY"}:
        return "dry-run"
    for name, state in (("autonomy.kill", "kill"), ("autonomy.dry-run", "dry-run")):
        path = state_dir / name
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return "kill"
        if (
            not _path_components_safe(path, final_kind="file")
            or
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            return "kill"
        return state
    return "active"


def _config(environment: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if environment is None else environment)
    home = Path(env.get("HOME", "/root")).absolute()
    claude_dir = Path(env.get("CCC_CLAUDE_DIR", home / ".claude")).absolute()
    state_dir = Path(env.get("CCC_STATE_DIR", claude_dir / "state")).absolute()
    promotion_state_dir = Path(
        env.get("CCC_SKILL_PROMOTION_STATE_DIR", state_dir / "skill-promotion")
    ).absolute()
    repo = env.get("CCC_SKILL_PROMOTION_REPO", "jinwon-int/ccc-node")
    if not _REPO_RE.fullmatch(repo):
        raise PromotionError("repo_invalid")
    base = env.get("CCC_SKILL_PROMOTION_BASE", "main")
    if not _SAFE_COMPONENT_RE.fullmatch(base):
        raise PromotionError("base_invalid")
    providers_raw = env.get("CCC_SKILL_PROMOTION_PROVIDERS", "claude,codex")
    providers = tuple(dict.fromkeys(part.strip() for part in providers_raw.split(",") if part.strip()))
    if not providers or any(provider not in {"claude", "codex"} for provider in providers):
        raise PromotionError("providers_invalid")
    enabled_raw = env.get("CCC_SKILL_PROMOTION_ENABLED")
    if enabled_raw is None:
        enabled = _read_enabled_file(state_dir / "skill-promotion.enabled")
    elif enabled_raw.lower() in {"1", "true", "yes"}:
        enabled = True
    elif enabled_raw.lower() in {"0", "false", "no"}:
        enabled = False
    else:
        raise PromotionError("enabled_invalid")
    tool_default = claude_dir / "hooks" / "skill-review" / "ownership.py"
    return Config(
        home=home,
        state_dir=state_dir,
        promotion_state_dir=promotion_state_dir,
        ownership_tool=Path(env.get("CCC_SKILL_PROMOTION_OWNERSHIP_TOOL", tool_default)).absolute(),
        repo=repo,
        remote=env.get("CCC_SKILL_PROMOTION_REMOTE", f"https://github.com/{repo}.git"),
        base=base,
        node=_safe_node(env.get("CCC_NODE", env.get("HOSTNAME", "node"))),
        providers=providers,
        provider_roots={
            "claude": Path(
                env.get("CCC_SKILL_PROMOTION_CLAUDE_SKILLS_DIR", claude_dir / "skills")
            ).absolute(),
            "codex": Path(
                env.get(
                    "CCC_SKILL_PROMOTION_CODEX_SKILLS_DIR",
                    Path(env.get("CODEX_HOME", home / ".codex")) / "skills",
                )
            ).absolute(),
        },
        max_prs=_bounded_int(env.get("CCC_SKILL_PROMOTION_MAX_PRS_PER_RUN"), 1, 1, 3),
        enabled=enabled,
        autonomy=_autonomy_state(env, state_dir),
    )


def _private_state_dir(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PromotionError("state_path_unsafe")
        mode = stat.S_IMODE(metadata.st_mode)
        writable_ancestor = bool(mode & 0o022)
        trusted_sticky_ancestor = metadata.st_uid == 0 and bool(mode & stat.S_ISVTX)
        is_final = current == path
        if metadata.st_uid not in {0, os.geteuid()} or (
            writable_ancestor and not trusted_sticky_ancestor
        ) or (is_final and mode & 0o077):
            raise PromotionError("state_path_unsafe")


def _path_components_safe(path: Path, *, final_kind: str) -> bool:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    try:
        for index, component in enumerate(absolute.parts[1:], start=1):
            current /= component
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                return False
            is_final = index == len(absolute.parts) - 1
            if is_final and final_kind == "file":
                if not stat.S_ISREG(metadata.st_mode):
                    return False
            elif not stat.S_ISDIR(metadata.st_mode):
                return False
            mode = stat.S_IMODE(metadata.st_mode)
            writable = bool(mode & 0o022)
            trusted_sticky = metadata.st_uid == 0 and bool(mode & stat.S_ISVTX)
            if metadata.st_uid not in {0, os.geteuid()} or (writable and not trusted_sticky):
                return False
        return True
    except OSError:
        return False


def _safe_tool(path: Path) -> bool:
    try:
        metadata = path.lstat()
        return (
            _path_components_safe(path, final_kind="file")
            and
            stat.S_ISREG(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and metadata.st_uid in {0, os.geteuid()}
            and not stat.S_IMODE(metadata.st_mode) & 0o022
        )
    except OSError:
        return False


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PromotionError("command_failed") from None
    if len(completed.stdout) > _MAX_COMMAND_OUTPUT or len(completed.stderr) > _MAX_COMMAND_OUTPUT:
        raise PromotionError("command_output_too_large")
    if check and completed.returncode != 0:
        raise PromotionError("command_failed")
    return completed


def _ownership_rows(config: Config, provider: str) -> list[dict[str, Any]]:
    if not _safe_tool(config.ownership_tool):
        raise PromotionError("ownership_tool_unsafe")
    root = config.provider_roots[provider]
    if not root.is_dir():
        return []
    if not _path_components_safe(root, final_kind="dir"):
        raise PromotionError("skills_root_unsafe")
    completed = _run(
        [
            sys.executable,
            str(config.ownership_tool),
            "--provider",
            provider,
            "--skills-dir",
            str(root),
            "--state-dir",
            str(config.state_dir),
            "status",
        ]
    )
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PromotionError("ownership_output_invalid") from None
    rows = value.get("skills") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise PromotionError("ownership_output_invalid")
    return [row for row in rows if isinstance(row, dict)]


def _safe_read(path: Path, *, max_bytes: int, exact_mode: int | None = None) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise PromotionError("source_file_unsafe") from None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size > max_bytes
            or (exact_mode is not None and stat.S_IMODE(before.st_mode) != exact_mode)
        ):
            raise PromotionError("source_file_unsafe")
        payload = b""
        while len(payload) <= max_bytes:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        after = os.fstat(descriptor)
        if len(payload) > max_bytes or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise PromotionError("source_changed")
        return payload, after
    finally:
        os.close(descriptor)


def _frontmatter(payload: bytes, expected_name: str) -> str:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise PromotionError("skill_not_utf8") from None
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise PromotionError("skill_frontmatter_invalid")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise PromotionError("skill_frontmatter_invalid") from None
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            raise PromotionError("skill_frontmatter_invalid")
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key in fields or not value:
            raise PromotionError("skill_frontmatter_invalid")
        fields[key] = value
    if set(fields) != {"name", "description"}:
        raise PromotionError("skill_frontmatter_invalid")
    if fields.get("name") != expected_name:
        raise PromotionError("skill_name_mismatch")
    description = fields.get("description", "")
    if not 20 <= len(description) <= 1024 or len(lines[end + 1 :]) < 3:
        raise PromotionError("skill_frontmatter_invalid")
    return description


def _scan_text(payload: bytes) -> None:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise PromotionError("source_not_utf8") from None
    for label, pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(text):
            raise PromotionError(f"secret_{label}")
    if _HOME_PATH_RE.search(text):
        raise PromotionError("node_specific_home_path")
    for raw in _IPV4_RE.findall(text):
        octets = raw.split(".")
        if any(int(part) > 255 for part in octets):
            continue
        if not raw.startswith("127.") and raw != "0.0.0.0":
            raise PromotionError("node_specific_ipv4")
    for raw in _EMAIL_RE.findall(text):
        if raw.lower() not in {"git@github.com", "git@gitlab.com"}:
            raise PromotionError("node_specific_user_at_host")
    if any(pattern.search(text) for pattern in _CLAUDE_COUPLINGS):
        raise PromotionError("runtime_specific_claude")
    if any(pattern.search(text) for pattern in _CODEX_COUPLINGS):
        raise PromotionError("runtime_specific_codex")


def _snapshot(config: Config, provider: str, row: dict[str, Any]) -> Candidate:
    name = row.get("name")
    skill_sha = row.get("skill_sha256")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or not isinstance(skill_sha, str):
        raise PromotionError("ownership_row_invalid")
    if (
        row.get("provider") != provider
        or row.get("classification") != "autosave-managed"
        or row.get("pinned") is not False
        or row.get("autonomous_write_allowed") is not True
    ):
        raise PromotionError("not_autosave_eligible")
    skill_dir = config.provider_roots[provider] / name
    try:
        directory = skill_dir.lstat()
    except OSError:
        raise PromotionError("source_directory_unsafe") from None
    if (
        not _path_components_safe(skill_dir, final_kind="dir")
        or
        stat.S_ISLNK(directory.st_mode)
        or not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != os.geteuid()
        or stat.S_IMODE(directory.st_mode) & 0o022
    ):
        raise PromotionError("source_directory_unsafe")
    marker_payload, _ = _safe_read(skill_dir / ".autosave-meta.json", max_bytes=16 * 1024, exact_mode=0o600)
    try:
        marker = json.loads(marker_payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PromotionError("autosave_marker_invalid") from None
    if (
        not isinstance(marker, dict)
        or marker.get("schema_version") != 2
        or marker.get("manager") != "ccc-node-skill-autosave"
        or marker.get("ownership") != "autosave-managed"
        or marker.get("provider") != provider
        or marker.get("name") != name
        or marker.get("target_id") != row.get("target_id")
        or marker.get("skill_sha256") != skill_sha
        or marker.get("created_by") != "ccc-node"
        or marker.get("rollback_eligible") is not True
        or marker.get("provenance_revision") != row.get("provenance_revision")
    ):
        raise PromotionError("autosave_marker_invalid")
    files: list[SnapshotFile] = []
    total = 0
    for path in sorted(skill_dir.rglob("*")):
        relative = path.relative_to(skill_dir).as_posix()
        parts = relative.split("/")
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise PromotionError("source_symlink")
        if stat.S_ISDIR(metadata.st_mode):
            if parts[0] not in _ALLOWED_SUPPORT_DIRS:
                if relative.startswith("."):
                    continue
                raise PromotionError("source_path_not_allowed")
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
                raise PromotionError("source_directory_unsafe")
            continue
        if relative == ".autosave-meta.json":
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise PromotionError("source_file_unsafe")
        if relative != "SKILL.md" and (
            parts[0] not in _ALLOWED_SUPPORT_DIRS
            or any(part in {"", ".", ".."} or not _SAFE_COMPONENT_RE.fullmatch(part) for part in parts)
        ):
            raise PromotionError("source_path_not_allowed")
        content, opened = _safe_read(path, max_bytes=_MAX_FILE_BYTES)
        total += len(content)
        if total > _MAX_TOTAL_BYTES or len(files) >= _MAX_FILES:
            raise PromotionError("source_tree_too_large")
        _scan_text(content)
        files.append(
            SnapshotFile(
                relative=relative,
                content=content,
                executable=bool(stat.S_IMODE(opened.st_mode) & 0o111),
            )
        )
    if not files or files[0].relative != "SKILL.md":
        raise PromotionError("skill_missing")
    actual_skill_sha = hashlib.sha256(files[0].content).hexdigest()
    if actual_skill_sha != skill_sha:
        raise PromotionError("skill_hash_mismatch")
    description = _frontmatter(files[0].content, name)
    digest = hashlib.sha256()
    for item in files:
        digest.update(item.relative.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(item.content).hexdigest().encode())
        digest.update(b"\0")
    final_directory = skill_dir.lstat()
    if (directory.st_dev, directory.st_ino, directory.st_mtime_ns, directory.st_ctime_ns) != (
        final_directory.st_dev,
        final_directory.st_ino,
        final_directory.st_mtime_ns,
        final_directory.st_ctime_ns,
    ):
        raise PromotionError("source_changed")
    return Candidate(
        provider=provider,
        name=name,
        skill_sha256=skill_sha,
        tree_sha256=digest.hexdigest(),
        source_dir=skill_dir,
        files=tuple(files),
        description=description,
    )


def _discover(config: Config) -> tuple[list[Candidate], list[dict[str, str]]]:
    candidates: list[Candidate] = []
    blocked: list[dict[str, str]] = []
    for provider in config.providers:
        for row in _ownership_rows(config, provider):
            if row.get("classification") != "autosave-managed":
                continue
            name = row.get("name") if isinstance(row.get("name"), str) else "invalid"
            try:
                candidates.append(_snapshot(config, provider, row))
            except PromotionError as error:
                blocked.append({"provider": provider, "name": name, "code": error.code})
    candidates.sort(key=lambda item: (item.name, item.provider, item.tree_sha256))
    return candidates, blocked


def _display_name(name: str) -> str:
    return " ".join(part.capitalize() for part in name.split("-"))[:80]


def _yaml_interface(candidate: Candidate) -> bytes:
    short = " ".join(candidate.description.split())[:100]
    values = {
        "display": _display_name(candidate.name),
        "short": short,
        "prompt": f"Use ${candidate.name} when this reusable procedure matches the task.",
    }
    return (
        "interface:\n"
        f"  display_name: {json.dumps(values['display'], ensure_ascii=False)}\n"
        f"  short_description: {json.dumps(values['short'], ensure_ascii=False)}\n"
        f"  default_prompt: {json.dumps(values['prompt'], ensure_ascii=False)}\n"
    ).encode()


def _description_tokens(value: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", value.lower()) if len(token) >= 3}


def _central_frontmatter(path: Path) -> tuple[str, str] | None:
    try:
        payload = path.read_bytes()
        if len(payload) > _MAX_FILE_BYTES:
            return None
        lines = payload.decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if not lines or lines[0] != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            return None
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    name, description = fields.get("name"), fields.get("description")
    if not name or not description:
        return None
    return name, description


def _central_dedup(root: Path, candidate: Candidate) -> None:
    wanted_name = re.sub(r"[^a-z0-9]", "", candidate.name.lower())
    wanted_tokens = _description_tokens(candidate.description)
    for prefix in ("skills/shared", "claude/skills", "codex/skills"):
        tree = root / prefix
        if not tree.is_dir():
            continue
        for path in sorted(tree.glob("*/SKILL.md")):
            existing = _central_frontmatter(path)
            if existing is None:
                continue
            name, description = existing
            if re.sub(r"[^a-z0-9]", "", name.lower()) == wanted_name:
                raise PromotionError("central_name_exists")
            existing_tokens = _description_tokens(description)
            union = wanted_tokens | existing_tokens
            if len(union) >= 6 and len(wanted_tokens & existing_tokens) * 100 >= len(union) * 60:
                raise PromotionError("central_description_similar")


def _update_catalog(root: Path, candidate: Candidate) -> None:
    path = root / "codex" / "compatibility.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise PromotionError("catalog_invalid") from None
    classifications = data.get("classifications")
    managed = data.get("managed_skills")
    if not isinstance(classifications, list) or not isinstance(managed, list):
        raise PromotionError("catalog_invalid")
    pattern = f"skills/shared/{candidate.name}/**"
    if any(isinstance(item, dict) and item.get("pattern") == pattern for item in classifications):
        raise PromotionError("central_name_exists")
    if any(isinstance(item, dict) and item.get("name") == candidate.name for item in managed):
        raise PromotionError("central_name_exists")
    classifications.append(
        {"pattern": pattern, "compatibility": "adapted", "codex_skill": candidate.name}
    )
    managed.append({"name": candidate.name, "source": f"skills/shared/{candidate.name}"})
    managed.sort(key=lambda item: str(item.get("name", "")) if isinstance(item, dict) else "")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_candidate(root: Path, candidate: Candidate) -> None:
    for prefix in ("skills/shared", "claude/skills", "codex/skills"):
        if (root / prefix / candidate.name).exists():
            raise PromotionError("central_name_exists")
    _central_dedup(root, candidate)
    target = root / "skills" / "shared" / candidate.name
    target.mkdir(parents=True, mode=0o755)
    for item in candidate.files:
        destination = target / item.relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        destination.write_bytes(item.content)
        destination.chmod(0o755 if item.executable else 0o644)
    agent = target / "agents" / "openai.yaml"
    agent.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    agent.write_bytes(_yaml_interface(candidate))
    agent.chmod(0o644)
    _update_catalog(root, candidate)


def _branch(config: Config, candidate: Candidate) -> str:
    return (
        f"skill-promotion/{config.node}/{candidate.name}-"
        f"{candidate.provider}-{candidate.tree_sha256[:12]}"
    )


def _existing_pr(config: Config, branch: str) -> dict[str, str] | None:
    completed = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            config.repo,
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            "1",
            "--json",
            "url,state,isDraft",
        ]
    )
    try:
        rows = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PromotionError("github_output_invalid") from None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise PromotionError("github_output_invalid")
    if not rows:
        return None
    return {
        "url": str(rows[0].get("url", "")),
        "state": str(rows[0].get("state", "UNKNOWN")),
        "draft": str(bool(rows[0].get("isDraft", False))).lower(),
    }


def _remote_branch_oid(config: Config, branch: str) -> str | None:
    completed = _run(
        ["git", "ls-remote", "--exit-code", "--heads", config.remote, f"refs/heads/{branch}"],
        check=False,
    )
    if completed.returncode == 0:
        try:
            oid = completed.stdout.decode("ascii").split()[0]
        except (UnicodeDecodeError, IndexError):
            raise PromotionError("git_remote_probe_failed") from None
        if not re.fullmatch(r"[0-9a-f]{40,64}", oid):
            raise PromotionError("git_remote_probe_failed")
        return oid
    if completed.returncode == 2:
        return None
    raise PromotionError("git_remote_probe_failed")


def _publish(config: Config, candidate: Candidate) -> dict[str, str]:
    branch = _branch(config, candidate)
    _run(["gh", "auth", "status", "--hostname", "github.com"])
    remote_oid = _remote_branch_oid(config, branch)
    existing = _existing_pr(config, branch) if remote_oid is not None else None
    if existing is not None:
        return {"outcome": "existing-pr", "branch": branch, **existing}
    with tempfile.TemporaryDirectory(prefix="skill-promotion-", dir=config.promotion_state_dir) as raw:
        work = Path(raw) / "repo"
        _run(
            [
                "git",
                "clone",
                "--quiet",
                "--depth",
                "1",
                "--branch",
                config.base,
                config.remote,
                str(work),
            ]
        )
        _run(["git", "checkout", "-b", branch], cwd=work)
        _write_candidate(work, candidate)
        _run(
            ["git", "add", f"skills/shared/{candidate.name}", "codex/compatibility.json"],
            cwd=work,
        )
        _run(
            [
                "git",
                "-c",
                f"user.name=ccc-node skill promoter ({config.node})",
                "-c",
                "user.email=ccc-node-skill-promoter@users.noreply.github.com",
                "commit",
                "--quiet",
                "-m",
                f"feat(skills): promote {candidate.name} from {config.node}",
                "-m",
                f"CCC-Skill-Promotion: {candidate.tree_sha256}",
            ],
            cwd=work,
        )
        push_args = ["git", "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"]
        if remote_oid is not None:
            push_args.insert(2, f"--force-with-lease=refs/heads/{branch}:{remote_oid}")
        _run(push_args, cwd=work)
    body = "\n".join(
        [
            "Automated draft promotion of a locally generated autosave-managed skill.",
            "",
            f"- source node: `{config.node}`",
            f"- source provider: `{candidate.provider}`",
            f"- source tree SHA-256: `{candidate.tree_sha256}`",
            "- local gates: ownership, rollback eligibility, bounded files, secret scan, node-fact scan, runtime-neutral scan",
            "- merge policy: human/independent review required; this automation never merges",
        ]
    )
    completed = _run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            config.repo,
            "--base",
            config.base,
            "--head",
            branch,
            "--draft",
            "--title",
            f"feat(skills): promote {candidate.name} from {config.node}",
            "--body",
            body,
        ]
    )
    try:
        url = completed.stdout.decode("utf-8").strip().splitlines()[-1]
    except (UnicodeDecodeError, IndexError):
        raise PromotionError("github_output_invalid") from None
    if not url.startswith("https://github.com/"):
        raise PromotionError("github_output_invalid")
    return {"outcome": "pr-opened", "branch": branch, "url": url, "state": "OPEN", "draft": "true"}


def _append_ledger(config: Config, record: dict[str, object]) -> None:
    path = config.promotion_state_dir / "ledger.jsonl"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.geteuid() or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PromotionError("ledger_unsafe")
        os.write(descriptor, (_json_line(record) + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _status(config: Config) -> dict[str, object]:
    if not config.enabled:
        return {
            "ok": True,
            "mode": "status-read-only",
            "enabled": False,
            "autonomy": config.autonomy,
            "candidates": [],
            "blocked": [],
        }
    candidates, blocked = _discover(config)
    return {
        "ok": True,
        "mode": "status-read-only",
        "enabled": True,
        "autonomy": config.autonomy,
        "repo": config.repo,
        "max_prs_per_run": config.max_prs,
        "candidates": [
            {
                "provider": item.provider,
                "name": item.name,
                "tree_sha256": item.tree_sha256,
                "target": f"skills/shared/{item.name}",
            }
            for item in candidates
        ],
        "blocked": blocked,
    }


def _execute(config: Config, *, dry_run: bool) -> dict[str, object]:
    if not config.enabled:
        return {
            "ok": True,
            "mode": "run",
            "enabled": False,
            "autonomy": config.autonomy,
            "published": [],
            "blocked": [],
            "errors": [],
        }
    if config.autonomy == "kill":
        return {
            "ok": True,
            "mode": "run",
            "enabled": True,
            "autonomy": "kill",
            "status": "autonomy-kill",
            "published": [],
            "blocked": [],
            "errors": [],
        }
    dry_run = dry_run or config.autonomy == "dry-run"
    _private_state_dir(config.promotion_state_dir)
    lock_path = config.promotion_state_dir / "promotion.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        metadata = os.fstat(lock_fd)
        if metadata.st_uid != os.geteuid() or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PromotionError("lock_unsafe")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "ok": True,
                "mode": "run",
                "enabled": True,
                "autonomy": config.autonomy,
                "status": "locked",
                "published": [],
                "blocked": [],
                "errors": [],
            }
        candidates, blocked = _discover(config)
        published: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []
        opened = 0
        selected = candidates[: config.max_prs] if dry_run else candidates[:_MAX_CANDIDATES_PER_RUN]
        for candidate in selected:
            if not dry_run and opened >= config.max_prs:
                break
            if dry_run:
                outcome = {
                    "outcome": "would-open-draft-pr",
                    "branch": _branch(config, candidate),
                    "provider": candidate.provider,
                    "name": candidate.name,
                    "tree_sha256": candidate.tree_sha256,
                }
            else:
                try:
                    outcome = {
                        **_publish(config, candidate),
                        "provider": candidate.provider,
                        "name": candidate.name,
                        "tree_sha256": candidate.tree_sha256,
                    }
                except PromotionError as error:
                    errors.append({"provider": candidate.provider, "name": candidate.name, "code": error.code})
                    continue
            published.append(outcome)
            if outcome["outcome"] == "pr-opened":
                opened += 1
            if not dry_run and outcome["outcome"] == "pr-opened":
                _append_ledger(config, {"ts": _utc_now(), **outcome})
        return {
            "ok": not errors,
            "mode": "dry-run" if dry_run else "run",
            "enabled": True,
            "autonomy": config.autonomy,
            "published": published,
            "blocked": blocked,
            "errors": errors,
        }
    finally:
        os.close(lock_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = _config()
        result = _status(config) if args.command == "status" else _execute(config, dry_run=args.dry_run)
    except PromotionError as error:
        result = {"ok": False, "code": error.code}
    except (OSError, TypeError, ValueError, RecursionError):
        result = {"ok": False, "code": "internal_error"}
    print(_json_line(result))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
