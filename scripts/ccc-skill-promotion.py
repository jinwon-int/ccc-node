#!/usr/bin/env python3
"""Stage generated skills locally and publish private intake draft PRs centrally.

Every node may create an owner-only, content-addressed outbox envelope. Only an
explicitly configured central publisher may collect envelopes over SSH and open
draft PRs in a repository whose GitHub visibility is verified as PRIVATE. Raw
intake is never written to ccc-node, merged, approved, or installed by this tool.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
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
    publisher_enabled: bool
    collect_nodes: tuple[str, ...]
    dispatch_enabled: bool
    broker_url: str
    a2a_nexus_dir: Path
    dispatch_ci_wait_sec: int
    autonomy: str


@dataclass(frozen=True)
class SnapshotFile:
    relative: str
    content: bytes
    executable: bool


@dataclass(frozen=True)
class Candidate:
    node: str
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
    """Sanitize a fleet node identity, or return "" when there is none.

    Returning "" rather than a placeholder is deliberate (#1067). The node name
    is a fleet identity that the collecting publisher matches against the SSH
    alias it dialled, not a machine hostname: guessing one produces envelopes
    that stage cleanly and are then rejected as `remote_node_mismatch`, with the
    failure visible only on the publisher. Callers must refuse to stage instead.
    """
    value = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    return re.sub(r"-+", "-", value)[:32].rstrip("-")


def _read_enabled_file(path: Path, *, trust_root: Path | None = None) -> bool:
    try:
        metadata = path.lstat()
        if (
            not _path_components_safe(path, final_kind="file", trust_root=trust_root)
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


def _private_lines(path: Path, *, max_bytes: int = 4096, trust_root: Path | None = None) -> tuple[str, ...]:
    try:
        metadata = path.lstat()
        if (
            not _path_components_safe(path, final_kind="file", trust_root=trust_root)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > max_bytes
        ):
            raise PromotionError("collector_config_unsafe")
        return tuple(
            line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeDecodeError):
        raise PromotionError("collector_config_unsafe") from None


def _autonomy_state(env: dict[str, str], state_dir: Path, *, trust_root: Path | None = None) -> str:
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
            not _path_components_safe(path, final_kind="file", trust_root=trust_root)
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
    repo = env.get("CCC_SKILL_PROMOTION_REPO", "jinwon-int/fleet-skills")
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
        enabled = _read_enabled_file(state_dir / "skill-promotion.enabled", trust_root=home)
    elif enabled_raw.lower() in {"1", "true", "yes"}:
        enabled = True
    elif enabled_raw.lower() in {"0", "false", "no"}:
        enabled = False
    else:
        raise PromotionError("enabled_invalid")
    publisher_raw = env.get("CCC_SKILL_PROMOTION_PUBLISHER")
    if publisher_raw is None:
        publisher_enabled = _read_enabled_file(state_dir / "skill-promotion.publisher", trust_root=home)
    elif publisher_raw.lower() in {"1", "true", "yes"}:
        publisher_enabled = True
    elif publisher_raw.lower() in {"0", "false", "no"}:
        publisher_enabled = False
    else:
        raise PromotionError("publisher_invalid")
    dispatch_raw = env.get("CCC_SKILL_PROMOTION_DISPATCH")
    if dispatch_raw is None:
        dispatch_enabled = _read_enabled_file(state_dir / "skill-promotion.dispatch", trust_root=home)
    elif dispatch_raw.lower() in {"1", "true", "yes"}:
        dispatch_enabled = True
    elif dispatch_raw.lower() in {"0", "false", "no"}:
        dispatch_enabled = False
    else:
        raise PromotionError("dispatch_invalid")
    collect_raw = env.get("CCC_SKILL_PROMOTION_COLLECT_NODES")
    collect_nodes = (
        tuple(part.strip() for part in collect_raw.split(",") if part.strip())
        if collect_raw is not None
        else _private_lines(state_dir / "skill-promotion.collect-nodes", trust_root=home)
    )
    collect_nodes = tuple(dict.fromkeys(collect_nodes))
    if len(collect_nodes) > 32 or any(
        not _NAME_RE.fullmatch(node) or len(node) > 32 for node in collect_nodes
    ):
        raise PromotionError("collector_nodes_invalid")
    tool_default = claude_dir / "hooks" / "skill-review" / "ownership.py"
    return Config(
        home=home,
        state_dir=state_dir,
        promotion_state_dir=promotion_state_dir,
        ownership_tool=Path(env.get("CCC_SKILL_PROMOTION_OWNERSHIP_TOOL", tool_default)).absolute(),
        repo=repo,
        remote=env.get("CCC_SKILL_PROMOTION_REMOTE", f"https://github.com/{repo}.git"),
        base=base,
        node=_safe_node(env.get("CCC_NODE") or env.get("HOSTNAME") or ""),
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
        publisher_enabled=publisher_enabled,
        collect_nodes=collect_nodes,
        dispatch_enabled=dispatch_enabled,
        broker_url=env.get("CCC_SKILL_PROMOTION_BROKER_URL", "http://127.0.0.1:8787").rstrip("/"),
        a2a_nexus_dir=Path(
            env.get("CCC_SKILL_PROMOTION_A2A_NEXUS_DIR", str(home / "work" / "a2a" / "a2a-nexus"))
        ).absolute(),
        dispatch_ci_wait_sec=_bounded_int(
            env.get("CCC_SKILL_PROMOTION_DISPATCH_CI_WAIT_SEC"), 600, 60, 3600
        ),
        autonomy=_autonomy_state(env, state_dir, trust_root=home),
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


def _component_safe(metadata: os.stat_result) -> bool:
    """One path component: owned by root or us, and not group/other writable.

    The sticky exception covers root-owned shared dirs like /tmp, where the
    sticky bit is what actually prevents a co-tenant from replacing our entry.
    """
    mode = stat.S_IMODE(metadata.st_mode)
    writable = bool(mode & 0o022)
    trusted_sticky = metadata.st_uid == 0 and bool(mode & stat.S_ISVTX)
    return metadata.st_uid in {0, os.geteuid()} and not (writable and not trusted_sticky)


def _anchored_walk(absolute: Path, trust_root: Path) -> tuple[Path, tuple[str, ...]] | None:
    """(start, components) to walk from ``trust_root``, or None to walk from /.

    None means "this anchor does not apply" — the path is not inside the root,
    or the root itself is a symlink, not a directory, or fails the component
    rules. Callers then fall back to the unanchored walk, so a bad or
    inapplicable root can only ever be stricter, never weaker.
    """
    start = trust_root.absolute()
    try:
        components = absolute.relative_to(start).parts
        root_metadata = start.lstat()
    except (ValueError, OSError):
        return None
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        return None
    if not _component_safe(root_metadata):
        return None
    return start, components


def _path_components_safe(
    path: Path, *, final_kind: str, trust_root: Path | None = None
) -> bool:
    """Refuse a path that any untrusted directory on the way to it could swap.

    With no ``trust_root`` every component from the filesystem root is checked.
    That is correct on a normal Linux node and stays the default.

    ``trust_root`` anchors the walk instead: the root itself is checked with the
    same rules, then only the components BELOW it (#1069). Android/Termux needs
    this — ``/data`` and ``/data/data`` are ``771 system(1000)``, platform-owned
    and unchangeable, so a walk from ``/`` rejects every correctly-provisioned
    Termux path. daegyo and gongyung could not even read their own enabled flag:
    ``_read_enabled_file`` runs the same check, so the node reported
    ``enabled:false`` with nothing anywhere saying why, and 20 intake candidates
    were structurally excluded.

    Anchoring is sound there because ``/data/data/<pkg>`` IS the OS-enforced
    per-app uid boundary: components above it are not a hole another app can
    reach through, so examining them adds no protection. It is only sound for a
    root the operator controls — pass the harness home, never a caller-supplied
    or world-writable directory.

    The anchor only ever RELAXES, and only for paths inside a root that itself
    passed the same checks. A path outside the root, or a root that fails them,
    falls back to the full walk from ``/`` — i.e. exactly today's behaviour.
    That matters because ``CCC_STATE_DIR`` may legitimately point outside
    ``$HOME``; refusing those would silently break a valid configuration, which
    is the same failure this fix exists to remove.
    """
    absolute = path.absolute()
    try:
        start = Path(absolute.anchor)
        components = absolute.parts[1:]
        if trust_root is not None:
            anchored = _anchored_walk(absolute, trust_root)
            if anchored is not None:
                start, components = anchored
                if not components:
                    return final_kind == "dir"
        current = start
        for index, component in enumerate(components, start=1):
            current /= component
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                return False
            is_final = index == len(components)
            if is_final and final_kind == "file":
                if not stat.S_ISREG(metadata.st_mode):
                    return False
            elif not stat.S_ISDIR(metadata.st_mode):
                return False
            if not _component_safe(metadata):
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
    timeout: int = 120,
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
            timeout=timeout,
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
    if not _path_components_safe(root, final_kind="dir", trust_root=config.home):
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


# Security gates remain linear so each rejection precedes content export.
def _snapshot(config: Config, provider: str, row: dict[str, Any]) -> Candidate:  # noqa: C901
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
        not _path_components_safe(skill_dir, final_kind="dir", trust_root=config.home)
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
        node=config.node,
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
    for prefix in ("approved/shared", "approved/claude", "approved/codex"):
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


def _candidate_id(candidate: Candidate) -> str:
    return f"{candidate.name}-{candidate.tree_sha256[:12]}"


def _manifest(candidate: Candidate, *, created_at: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "candidate_id": _candidate_id(candidate),
        "node": candidate.node,
        "provider": candidate.provider,
        "name": candidate.name,
        "skill_sha256": candidate.skill_sha256,
        "tree_sha256": candidate.tree_sha256,
        "created_at": created_at,
        "files": [
            {
                "path": item.relative,
                "sha256": hashlib.sha256(item.content).hexdigest(),
                "size": len(item.content),
                "executable": item.executable,
            }
            for item in candidate.files
        ],
    }


def _write_candidate(root: Path, candidate: Candidate, *, created_at: str) -> Path:
    for prefix in ("approved/shared", "approved/claude", "approved/codex"):
        if (root / prefix / candidate.name).exists():
            raise PromotionError("central_name_exists")
    _central_dedup(root, candidate)
    target = (
        root / "intake" / candidate.node / candidate.provider / _candidate_id(candidate)
    )
    if target.exists():
        raise PromotionError("intake_path_exists")
    skill_target = target / "skill"
    skill_target.mkdir(parents=True, mode=0o755)
    manifest = target / "manifest.json"
    manifest.write_text(
        json.dumps(_manifest(candidate, created_at=created_at), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    manifest.chmod(0o644)
    for item in candidate.files:
        destination = skill_target / item.relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        destination.write_bytes(item.content)
        destination.chmod(0o755 if item.executable else 0o644)
    return target


def _branch(config: Config, candidate: Candidate) -> str:
    return (
        f"skill-intake/{candidate.node}/{candidate.name}-"
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


def _require_private_repo(config: Config) -> None:
    completed = _run(
        ["gh", "repo", "view", config.repo, "--json", "isPrivate,visibility"]
    )
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PromotionError("github_output_invalid") from None
    if not isinstance(value, dict) or value.get("isPrivate") is not True:
        raise PromotionError("target_repo_not_private")
    if str(value.get("visibility", "")).upper() != "PRIVATE":
        raise PromotionError("target_repo_not_private")


def _publish(
    config: Config, candidate: Candidate, *, created_at: str
) -> dict[str, str]:
    branch = _branch(config, candidate)
    _run(["gh", "auth", "status", "--hostname", "github.com"])
    _require_private_repo(config)
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
        target = _write_candidate(work, candidate, created_at=created_at)
        _run(["git", "add", str(target.relative_to(work))], cwd=work)
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
                f"intake: stage {candidate.name} from {candidate.node}",
                "-m",
                f"CCC-Skill-Intake: {candidate.tree_sha256}",
            ],
            cwd=work,
        )
        push_args = ["git", "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"]
        if remote_oid is not None:
            push_args.insert(2, f"--force-with-lease=refs/heads/{branch}:{remote_oid}")
        _run(push_args, cwd=work)
    body = "\n".join(
        [
            "Automated private intake of a locally generated autosave-managed skill.",
            "",
            f"- source node: `{candidate.node}`",
            f"- source provider: `{candidate.provider}`",
            f"- source tree SHA-256: `{candidate.tree_sha256}`",
            "- local gates: ownership, rollback eligibility, bounded files, secret scan, node-fact scan, runtime-neutral scan",
            "- merge policy: **DO NOT MERGE this intake PR**; rebuild a sanitized `approved/*` PR from current `main`",
            "- publication policy: private review only; public release requires a separate explicit decision",
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
            f"intake: {candidate.name} from {candidate.node}/{candidate.provider}",
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
    return {
        "outcome": "pr-opened",
        "branch": branch,
        "url": url,
        "state": "OPEN",
        "draft": "true",
    }


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


def _transport_id(candidate: Candidate) -> str:
    return f"{candidate.node}-{candidate.provider}-{_candidate_id(candidate)}"


def _envelope(candidate: Candidate, *, created_at: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "transport_id": _transport_id(candidate),
        "created_at": created_at,
        "node": candidate.node,
        "provider": candidate.provider,
        "name": candidate.name,
        "description": candidate.description,
        "skill_sha256": candidate.skill_sha256,
        "tree_sha256": candidate.tree_sha256,
        "files": [
            {
                "path": item.relative,
                "content_b64": base64.b64encode(item.content).decode("ascii"),
                "executable": item.executable,
            }
            for item in candidate.files
        ],
    }


# Keep the complete wire-schema and content verification in one fail-closed path.
def _candidate_from_envelope(  # noqa: C901
    value: object,
) -> tuple[Candidate, str, str]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "transport_id",
        "created_at",
        "node",
        "provider",
        "name",
        "description",
        "skill_sha256",
        "tree_sha256",
        "files",
    }:
        raise PromotionError("envelope_schema_invalid")
    node = value.get("node")
    provider = value.get("provider")
    name = value.get("name")
    description = value.get("description")
    skill_sha = value.get("skill_sha256")
    tree_sha = value.get("tree_sha256")
    created_at = value.get("created_at")
    transport_id = value.get("transport_id")
    if not isinstance(node, str) or not _NAME_RE.fullmatch(node) or len(node) > 32:
        raise PromotionError("envelope_node_invalid")
    if provider not in {"claude", "codex"}:
        raise PromotionError("envelope_provider_invalid")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or len(name) > 80:
        raise PromotionError("envelope_name_invalid")
    if not isinstance(description, str) or not 20 <= len(description) <= 1024:
        raise PromotionError("envelope_description_invalid")
    if not isinstance(skill_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", skill_sha):
        raise PromotionError("envelope_hash_invalid")
    if not isinstance(tree_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", tree_sha):
        raise PromotionError("envelope_hash_invalid")
    if not isinstance(created_at, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", created_at
    ):
        raise PromotionError("envelope_time_invalid")
    expected_transport = f"{node}-{provider}-{name}-{tree_sha[:12]}"
    if transport_id != expected_transport or not _SAFE_COMPONENT_RE.fullmatch(expected_transport):
        raise PromotionError("envelope_transport_invalid")
    raw_files = value.get("files")
    if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= _MAX_FILES:
        raise PromotionError("envelope_files_invalid")
    files: list[SnapshotFile] = []
    total = 0
    seen: set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, dict) or set(raw) != {"path", "content_b64", "executable"}:
            raise PromotionError("envelope_files_invalid")
        relative = raw.get("path")
        encoded = raw.get("content_b64")
        executable = raw.get("executable")
        if not isinstance(relative, str) or relative in seen:
            raise PromotionError("envelope_path_invalid")
        parts = relative.split("/")
        if relative != "SKILL.md" and (
            parts[0] not in _ALLOWED_SUPPORT_DIRS
            or any(part in {"", ".", ".."} or not _SAFE_COMPONENT_RE.fullmatch(part) for part in parts)
        ):
            raise PromotionError("envelope_path_invalid")
        if not isinstance(encoded, str) or not isinstance(executable, bool):
            raise PromotionError("envelope_files_invalid")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise PromotionError("envelope_base64_invalid") from None
        if len(content) > _MAX_FILE_BYTES:
            raise PromotionError("envelope_tree_too_large")
        total += len(content)
        if total > _MAX_TOTAL_BYTES:
            raise PromotionError("envelope_tree_too_large")
        _scan_text(content)
        seen.add(relative)
        files.append(SnapshotFile(relative=relative, content=content, executable=executable))
    files.sort(key=lambda item: item.relative)
    if not files or files[0].relative != "SKILL.md":
        raise PromotionError("envelope_skill_missing")
    actual_description = _frontmatter(files[0].content, name)
    if actual_description != description or hashlib.sha256(files[0].content).hexdigest() != skill_sha:
        raise PromotionError("envelope_skill_mismatch")
    digest = hashlib.sha256()
    for item in files:
        digest.update(item.relative.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(item.content).hexdigest().encode())
        digest.update(b"\0")
    if digest.hexdigest() != tree_sha:
        raise PromotionError("envelope_tree_mismatch")
    return (
        Candidate(
            node=node,
            provider=provider,
            name=name,
            skill_sha256=skill_sha,
            tree_sha256=tree_sha,
            source_dir=Path("/private-intake-envelope"),
            files=tuple(files),
            description=description,
        ),
        created_at,
        expected_transport,
    )


def _state_payload(path: Path, *, trust_root: Path | None = None) -> dict[str, object]:
    if not _path_components_safe(path, final_kind="file", trust_root=trust_root):
        raise PromotionError("outbox_file_unsafe")
    payload, _ = _safe_read(path, max_bytes=_MAX_TOTAL_BYTES * 2, exact_mode=0o600)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PromotionError("envelope_json_invalid") from None
    if not isinstance(value, dict):
        raise PromotionError("envelope_json_invalid")
    return value


def _stage_candidate(config: Config, candidate: Candidate) -> dict[str, str]:
    outbox = config.promotion_state_dir / "outbox"
    sent = config.promotion_state_dir / "sent"
    _private_state_dir(outbox)
    _private_state_dir(sent)
    transport_id = _transport_id(candidate)
    destination = outbox / f"{transport_id}.json"
    sent_path = sent / destination.name
    if sent_path.exists():
        _candidate_from_envelope(_state_payload(sent_path, trust_root=config.home))
        return {"outcome": "already-published", "transport_id": transport_id}
    if destination.exists():
        existing, _, existing_id = _candidate_from_envelope(_state_payload(destination, trust_root=config.home))
        if existing.tree_sha256 != candidate.tree_sha256 or existing_id != transport_id:
            raise PromotionError("outbox_collision")
        return {"outcome": "already-staged", "transport_id": transport_id}
    value = _envelope(candidate, created_at=_utc_now())
    descriptor, raw = tempfile.mkstemp(prefix=".candidate-", dir=outbox)
    temporary = Path(raw)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, (_json_line(value) + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, destination)
        directory_fd = os.open(outbox, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    _append_ledger(
        config,
        {
            "ts": _utc_now(),
            "outcome": "staged",
            "transport_id": transport_id,
            "node": candidate.node,
            "provider": candidate.provider,
            "name": candidate.name,
            "tree_sha256": candidate.tree_sha256,
        },
    )
    return {"outcome": "staged", "transport_id": transport_id}


def _pending_envelopes(config: Config, *, limit: int) -> list[dict[str, object]]:
    outbox = config.promotion_state_dir / "outbox"
    sent = config.promotion_state_dir / "sent"
    if not outbox.exists():
        return []
    if not _path_components_safe(outbox, final_kind="dir", trust_root=config.home):
        raise PromotionError("outbox_path_unsafe")
    rows: list[dict[str, object]] = []
    for path in sorted(outbox.glob("*.json")):
        if len(rows) >= limit:
            break
        if (sent / path.name).exists():
            continue
        value = _state_payload(path, trust_root=config.home)
        _, _, transport_id = _candidate_from_envelope(value)
        if path.name != f"{transport_id}.json":
            raise PromotionError("outbox_filename_mismatch")
        rows.append(value)
    return rows


def _ack_local(config: Config, transport_id: str) -> bool:
    if not _SAFE_COMPONENT_RE.fullmatch(transport_id):
        raise PromotionError("ack_id_invalid")
    outbox = config.promotion_state_dir / "outbox"
    sent = config.promotion_state_dir / "sent"
    _private_state_dir(outbox)
    _private_state_dir(sent)
    source = outbox / f"{transport_id}.json"
    destination = sent / source.name
    if destination.exists():
        _candidate_from_envelope(_state_payload(destination, trust_root=config.home))
        return True
    if not source.exists():
        return False
    _, _, actual_id = _candidate_from_envelope(_state_payload(source, trust_root=config.home))
    if actual_id != transport_id:
        raise PromotionError("ack_id_mismatch")
    os.replace(source, destination)
    directory_fd = os.open(sent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return True


def _remote_command(node: str, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return _run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            node,
            "python3",
            "~/.claude/hooks/ccc-skill-promotion.py",
            *arguments,
        ]
    )


def _remote_envelopes(node: str, *, limit: int) -> list[dict[str, object]]:
    completed = _remote_command(node, ["export", "--limit", str(limit)])
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PromotionError("remote_output_invalid") from None
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise PromotionError("remote_export_failed")
    rows = value.get("envelopes")
    if not isinstance(rows, list) or len(rows) > limit:
        raise PromotionError("remote_output_invalid")
    return rows


def _remote_ack(node: str, transport_id: str) -> None:
    completed = _remote_command(node, ["ack", transport_id])
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PromotionError("remote_output_invalid") from None
    if not isinstance(value, dict) or value.get("ok") is not True or value.get("acked") is not True:
        raise PromotionError("remote_ack_failed")


def _status(config: Config) -> dict[str, object]:
    if not config.enabled:
        return {
            "ok": True,
            "mode": "status-read-only",
            "enabled": False,
            "publisher_enabled": config.publisher_enabled,
            "autonomy": config.autonomy,
            "candidates": [],
            "blocked": [],
        }
    candidates, blocked = _discover(config)
    return {
        "ok": True,
        "mode": "status-read-only",
        "enabled": True,
        "publisher_enabled": config.publisher_enabled,
        "autonomy": config.autonomy,
        "repo": config.repo,
        "max_prs_per_run": config.max_prs,
        "collect_nodes": list(config.collect_nodes),
        "candidates": [
            {
                "node": item.node,
                "provider": item.provider,
                "name": item.name,
                "tree_sha256": item.tree_sha256,
                "target": f"local-outbox/{_transport_id(item)}.json",
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
            "staged": [],
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
            "staged": [],
            "blocked": [],
            "errors": [],
        }
    # Refuse to stage without a fleet identity (#1067). `bash -lc`, which is what
    # the autosave cron uses, exports neither CCC_NODE nor HOSTNAME, so this used
    # to fall back to a placeholder: every envelope staged "successfully" and the
    # publisher then rejected all of them as `remote_node_mismatch`. The node saw
    # ok/staged; only the publisher saw the error. Fail here, where it shows.
    if not config.node:
        raise PromotionError("node_identity_unresolved")
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
                "staged": [],
                "blocked": [],
                "errors": [],
            }
        candidates, blocked = _discover(config)
        staged: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []
        selected = candidates[:_MAX_CANDIDATES_PER_RUN]
        for candidate in selected:
            if dry_run:
                outcome = {
                    "outcome": "would-stage-private-outbox",
                    "transport_id": _transport_id(candidate),
                    "provider": candidate.provider,
                    "name": candidate.name,
                    "tree_sha256": candidate.tree_sha256,
                }
            else:
                try:
                    outcome = {
                        **_stage_candidate(config, candidate),
                        "provider": candidate.provider,
                        "name": candidate.name,
                        "tree_sha256": candidate.tree_sha256,
                    }
                except PromotionError as error:
                    errors.append({"provider": candidate.provider, "name": candidate.name, "code": error.code})
                    continue
            staged.append(outcome)
        return {
            "ok": not errors,
            "mode": "dry-run" if dry_run else "run",
            "enabled": True,
            "autonomy": config.autonomy,
            "staged": staged,
            "blocked": blocked,
            "errors": errors,
        }
    finally:
        os.close(lock_fd)


def _export_result(config: Config, *, limit: int) -> dict[str, object]:
    _private_state_dir(config.promotion_state_dir)
    rows = _pending_envelopes(config, limit=limit)
    return {
        "ok": True,
        "mode": "export-read-only",
        "node": config.node,
        "envelopes": rows,
    }


def _ack_result(config: Config, transport_id: str) -> dict[str, object]:
    return {
        "ok": True,
        "mode": "ack",
        "transport_id": transport_id,
        "acked": _ack_local(config, transport_id),
    }


# --- Phase C: A2A intake review dispatch (a2a-nexus#2007) --------------------
# The central publisher dispatches exactly one skills-intake-review task for
# every intake PR it opens or finds open. Fleet rules carried over from the
# manual Phase A round (2026-08-29, fleet-skills#18): the edge secret only
# ever lives in the publisher's local environment (never GH secrets), task ids
# are unique per round (a2a-nexus#2010), a handler crash is a crash — never a
# verdict — and dispatch failure never closes a PR. Verdicts stay with the
# a2a-receipts workflow; there is no auto-close and the human `reviewed_by`
# promotion gate is untouched.

_INTAKE_LANE = "skills_intake_review"
_INTAKE_RUBRIC_VERSION = "2026-08-28.2"  # must match docs/skills-intake-review.md
_DISPATCH_DOC_START = "## Worker procedure"
_DISPATCH_DOC_END = "## Receipt projection"

_INTAKE_VERDICT_SCHEMA: dict[str, object] = {
    "verdict": "approve | revise | reject",
    "findings": [
        {
            "severity": "info|minor|major|blocker",
            "area": "safety|spec|triggering|disclosure|quality|claims|duplication|utility",
            "note": "...",
        }
    ],
    "evidence": [{"kind": "grep|url|diff", "detail": "..."}],
    "model": "<runtime model id>",
    "reviewer_node": "<your node id>",
    "head_sha": "<40-char full head sha>",
    "rubric_version": _INTAKE_RUBRIC_VERSION,
    "note": "any major/blocker finding MUST carry a machine re-verifiable evidence entry; emit the verdict JSON only",
}


def _worker_procedure_from_docs(nexus_dir: Path) -> str:
    """Extract the worker procedure section from the a2a-nexus intake spec."""
    try:
        text = (nexus_dir / "docs" / "skills-intake-review.md").read_text(encoding="utf-8")
    except OSError as error:
        raise PromotionError("dispatch_docs_unreadable") from error
    start = text.find(_DISPATCH_DOC_START)
    end = text.find(_DISPATCH_DOC_END)
    if start < 0 or end < 0 or end <= start:
        raise PromotionError("dispatch_docs_section_missing")
    procedure = text[start:end].strip()
    if len(procedure) < 256:
        raise PromotionError("dispatch_docs_section_missing")
    return procedure


def _broker_id(config: Config, secret: str) -> str:
    try:
        completed = _run(
            ["curl", "-fsS", "-H", f"x-a2a-edge-secret: {secret}", f"{config.broker_url}/health"]
        )
        health = json.loads(completed.stdout.decode("utf-8"))
    except (PromotionError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PromotionError("dispatch_broker_unreachable") from error
    broker_id = health.get("brokerId") if isinstance(health, dict) else None
    if not isinstance(broker_id, str) or not broker_id.strip():
        raise PromotionError("dispatch_broker_id_missing")
    return broker_id.strip()


def _broker_online_worker_ids(config: Config, secret: str) -> set[str]:
    try:
        completed = _run(
            ["curl", "-fsS", "-H", f"x-a2a-edge-secret: {secret}", f"{config.broker_url}/workers"]
        )
        payload = json.loads(completed.stdout.decode("utf-8"))
    except (PromotionError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PromotionError("dispatch_broker_unreachable") from error
    items = payload.get("items") if isinstance(payload, dict) else None
    online: set[str] = set()
    if isinstance(items, list):
        for row in items:
            node_id = row.get("nodeId") if isinstance(row, dict) else None
            if isinstance(node_id, str) and node_id.strip():
                online.add(node_id.strip())
    return online


def _dispatch_target_worker(config: Config, author_node: str, secret: str) -> str:
    """Pick a reviewer that is actually homed on the target broker: keyring
    workers minus the author, intersected with the broker's online workers.
    A keyring worker registered on the OTHER broker would 404 at task creation
    (#2011 rollout, 2026-08-29: daegyo is T2-homed while seoseo dispatches on
    T1). The broker also enforces author disqualification independently."""
    online = _broker_online_worker_ids(config, secret)
    completed = _run(
        ["gh", "api", f"repos/{config.repo}/contents/refs/a2a-public-keyring.json?ref={config.base}"]
    )
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
        keyring = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PromotionError("dispatch_keyring_invalid") from error
    keys = keyring.get("keys") if isinstance(keyring, dict) else None
    workers: list[str] = []
    if isinstance(keys, dict):
        for key_id in sorted(keys):
            match = re.fullmatch(r"worker:([a-z0-9_-]{2,32}):g\d+:v\d+", key_id)
            if match and match.group(1) != author_node and match.group(1) in online:
                workers.append(match.group(1))
    if not workers:
        raise PromotionError("dispatch_no_reviewer_online")
    return workers[0]


def _branch_head_sha(config: Config, branch: str) -> str:
    completed = _run(["gh", "api", f"repos/{config.repo}/commits/{branch}", "--jq", ".sha"])
    sha = completed.stdout.decode("utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise PromotionError("dispatch_head_invalid")
    return sha


def _wait_for_skills_check(config: Config, head: str, timeout_sec: int) -> bool:
    """Block until the CI `skills` check on this exact head completes. The
    machine gate is a precondition of dispatch: no green, no dispatch."""
    deadline = time.monotonic() + timeout_sec
    while True:
        try:
            completed = _run(
                [
                    "gh", "api", f"repos/{config.repo}/commits/{head}/check-runs",
                    "--jq", '[.check_runs[] | select(.name == "skills")][0] | "\\(.status)/\\(.conclusion // "")"',
                ]
            )
            state = completed.stdout.decode("utf-8").strip()
        except PromotionError:
            state = ""
        if state.startswith("completed/"):
            return state == "completed/success"
        if time.monotonic() >= deadline:
            return False
        time.sleep(15)


def _build_dispatch_manifest(  # noqa: C901
    config: Config,
    candidate: Candidate,
    *,
    pr_number: str,
    branch: str,
    head: str,
    reviewer: str,
    broker_id: str,
    procedure: str,
    inventory: list[dict[str, str]],
    now: str,
) -> dict[str, object]:
    skill_files: list[dict[str, str]] = []
    for item in candidate.files:
        try:
            skill_files.append({"path": item.relative, "content": item.content.decode("utf-8")})
        except UnicodeDecodeError as error:
            raise PromotionError("dispatch_content_invalid") from error
    round_id = f"{_INTAKE_LANE}-pr{pr_number}-auto-{head[:8]}-{now}"
    task_id = f"{_INTAKE_LANE}-pr{pr_number}-{candidate.node}-{now}"
    return {
        "roundId": round_id,
        "brokerUrl": config.broker_url,
        "requester": {"id": config.node, "role": "operator"},
        "defaults": {"intent": _INTAKE_LANE},
        "lanes": [
            {
                "id": task_id,
                "target": {"id": reviewer, "role": "analyst"},
                "intent": _INTAKE_LANE,
                "message": (
                    f"skills-intake-reviewer procedure invoked: review fleet-skills intake "
                    f"PR #{pr_number} ({candidate.name}, author node {candidate.node}) per "
                    f"skills.skill-intake-review.v1 (rubric {_INTAKE_RUBRIC_VERSION}). Apply "
                    "rubric areas A-H in order and return ONLY the verdict JSON. Bind your "
                    f"output to skillName={candidate.name}, "
                    f"sourceTreeSha256={candidate.tree_sha256}, headPrefix={head[:8]}."
                ),
                "payload": {
                    "schema": "skills.skill-intake-review.v1",
                    "rubricVersion": _INTAKE_RUBRIC_VERSION,
                    "skillName": candidate.name,
                    "provenance": {
                        "author_node": candidate.node,
                        "intake_pr": int(pr_number),
                        "branch": branch,
                        "head_sha": head,
                        "source_tree_sha256": candidate.tree_sha256,
                    },
                    "machineGate": {
                        "secret_scan": "pass",
                        "node_facts": "pass",
                        "structure": "pass",
                        "dedup": "n/a",
                        "codex_compat": "n/a",
                        "claims": "n/a",
                    },
                    "skillFiles": skill_files,
                    "inventorySnapshot": inventory,
                    "verdictSchema": _INTAKE_VERDICT_SCHEMA,
                    "workerProcedure": procedure,
                    "review": {"required": True, "authorWorkerId": candidate.node},
                    "originBrokerId": broker_id,
                    "brokerOfRecordId": broker_id,
                    "operatorFacingOwner": "local",
                },
            }
        ],
    }


def _inventory_snapshot(config: Config, *, limit: int = 64) -> list[dict[str, str]]:
    """Read-only name/audience/description snapshot of approved skills — the
    reviewer's rubric-G duplication surface, identical to the manual round."""
    completed = _run(
        ["gh", "api", f"repos/{config.repo}/git/trees/{config.base}?recursive=1", "--jq",
         '[.tree[].path | select(test("^approved/[^/]+/[^/]+/SKILL.md$"))] | .[:' + str(limit) + "]"]
    )
    try:
        paths = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PromotionError("dispatch_keyring_invalid") from error
    if not isinstance(paths, list):
        raise PromotionError("dispatch_keyring_invalid")
    snapshot: list[dict[str, str]] = []
    for path in paths[:limit]:
        item = _run(["gh", "api", f"repos/{config.repo}/contents/{path}?ref={config.base}"])
        try:
            body = base64.b64decode(json.loads(item.stdout.decode("utf-8"))["content"]).decode("utf-8", "replace")
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PromotionError("dispatch_keyring_invalid") from error
        name_match = re.search(r"^name:\s*(.+)$", body, re.M)
        desc_match = re.search(r"^description:\s*(.+)$", body, re.M)
        snapshot.append(
            {
                "name": name_match.group(1).strip() if name_match else Path(path).parts[-2],
                "audience": path.split("/")[1],
                "description": (desc_match.group(1).strip()[:200] if desc_match else ""),
            }
        )
    return snapshot


def _dispatch_intake_review(  # noqa: C901
    config: Config,
    candidate: Candidate,
    outcome: dict[str, object],
    *,
    transport_id: str,
) -> dict[str, object]:
    """Best-effort fail-safe: never raises, never closes the PR. Skips are
    recorded so the next collect cycle can retry non-terminal failures."""
    url = outcome.get("url")
    branch = outcome.get("branch")
    if not isinstance(url, str) or not url:
        return {"outcome": "dispatch-skipped", "code": "dispatch_no_pr_url"}
    if not isinstance(branch, str) or not branch:
        return {"outcome": "dispatch-skipped", "code": "dispatch_no_branch"}
    pr_match = re.search(r"/pull/(\d+)$", url.rstrip("/"))
    if pr_match is None:
        return {"outcome": "dispatch-skipped", "code": "dispatch_no_pr_number"}
    pr_number = pr_match.group(1)
    nexus_script = config.a2a_nexus_dir / "scripts" / "a2a-dispatch-round.mjs"
    if not nexus_script.is_file():
        return {"outcome": "dispatch-skipped", "code": "dispatch_nexus_missing"}
    secret = os.environ.get("A2A_EDGE_SECRET", "")
    if not secret:
        return {"outcome": "dispatch-skipped", "code": "dispatch_secret_missing"}
    try:
        head = _branch_head_sha(config, branch)
    except PromotionError as error:
        return {"outcome": "dispatch-skipped", "code": error.code}
    # Idempotency: one dispatch per transport_id + head, crash-safe via ledger.
    try:
        ledger_text = (config.promotion_state_dir / "ledger.jsonl").read_text(encoding="utf-8")
        for line in ledger_text.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(record, dict)
                and record.get("kind") == "a2a-dispatch"
                and record.get("transport_id") == transport_id
                and record.get("head_sha") == head
            ):
                return {"outcome": "dispatch-skipped", "code": "dispatch_already"}
    except OSError:
        pass
    try:
        if not _wait_for_skills_check(config, head, config.dispatch_ci_wait_sec):
            return {"outcome": "dispatch-skipped", "code": "dispatch_ci_not_green"}
        procedure = _worker_procedure_from_docs(config.a2a_nexus_dir)
        reviewer = _dispatch_target_worker(config, candidate.node, secret)
        broker_id = _broker_id(config, secret)
        inventory = _inventory_snapshot(config)
        now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        manifest = _build_dispatch_manifest(
            config, candidate,
            pr_number=pr_number, branch=branch, head=head, reviewer=reviewer,
            broker_id=broker_id, procedure=procedure, inventory=inventory, now=now,
        )
    except PromotionError as error:
        return {"outcome": "dispatch-skipped", "code": error.code}
    descriptor, manifest_path = tempfile.mkstemp(
        prefix="dispatch-manifest-", suffix=".json", dir=config.promotion_state_dir
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False)
        env = dict(os.environ)
        env["A2A_EDGE_SECRET"] = secret
        try:
            completed = _run(
                ["node", str(nexus_script), "--manifest", str(manifest_path), "--verify", "--json"],
                env=env,
                timeout=300,
            )
            result = json.loads(completed.stdout.decode("utf-8"))
        except (PromotionError, UnicodeDecodeError, json.JSONDecodeError) as error:
            return {"outcome": "dispatch-skipped", "code": "dispatch_round_failed", "detail": str(error)[:120]}
    finally:
        try:
            os.unlink(manifest_path)
        except OSError:
            pass
    rows = result.get("results") if isinstance(result, dict) else None
    created = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    dispatched_task = created.get("taskId") if created else None
    if not isinstance(dispatched_task, str) or not dispatched_task:
        return {"outcome": "dispatch-skipped", "code": "dispatch_round_failed"}
    lane = manifest["lanes"][0]
    task_id = lane["id"]
    _append_ledger(
        config,
        {
            "ts": _utc_now(),
            "kind": "a2a-dispatch",
            "transport_id": transport_id,
            "head_sha": head,
            "round_id": manifest["roundId"],
            "task_id": task_id,
            "dispatched_task": dispatched_task,
            "reviewer_node": reviewer,
            "broker_id": broker_id,
            "pr_url": url,
        },
    )
    try:
        _run(
            [
                "gh", "pr", "comment", pr_number, "--repo", config.repo,
                "--body",
                f"A2A intake review dispatched automatically (Phase C): task `{dispatched_task}` "
                f"→ reviewer `{reviewer}` (round `{manifest['roundId']}`, broker `{broker_id}`). "
                "The signed receipt will project `a2a/receipts` on this head; human `reviewed_by` "
                "sign-off remains required per policies/REVIEW.md.",
            ]
        )
    except PromotionError:
        pass
    return {
        "outcome": "dispatched",
        "task_id": task_id,
        "dispatched_task": dispatched_task,
        "reviewer_node": reviewer,
        "round_id": manifest["roundId"],
    }


# Collection deliberately sequences visibility, remote reads, publication, and ack.
def _collect(config: Config, *, dry_run: bool) -> dict[str, object]:  # noqa: C901
    if not config.publisher_enabled:
        return {
            "ok": True,
            "mode": "collect",
            "publisher_enabled": False,
            "autonomy": config.autonomy,
            "published": [],
            "errors": [],
        }
    if config.autonomy == "kill":
        return {
            "ok": True,
            "mode": "collect",
            "publisher_enabled": True,
            "autonomy": "kill",
            "status": "autonomy-kill",
            "published": [],
            "errors": [],
        }
    dry_run = dry_run or config.autonomy == "dry-run"
    _private_state_dir(config.promotion_state_dir)
    _run(["gh", "auth", "status", "--hostname", "github.com"])
    _require_private_repo(config)
    errors: list[dict[str, str]] = []
    collected: list[tuple[Candidate, str, str, str]] = []
    seen: set[str] = set()

    def accept(value: object, source: str, expected_node: str | None) -> None:
        candidate, created_at, transport_id = _candidate_from_envelope(value)
        if expected_node is not None and candidate.node != expected_node:
            raise PromotionError("remote_node_mismatch")
        if transport_id in seen:
            return
        seen.add(transport_id)
        collected.append((candidate, created_at, transport_id, source))

    for value in _pending_envelopes(config, limit=_MAX_CANDIDATES_PER_RUN):
        accept(value, "local", config.node)
    for node in config.collect_nodes:
        if node == config.node or len(collected) >= _MAX_CANDIDATES_PER_RUN:
            continue
        try:
            remaining = min(config.max_prs, _MAX_CANDIDATES_PER_RUN - len(collected))
            for value in _remote_envelopes(node, limit=remaining):
                accept(value, node, node)
        except PromotionError as error:
            errors.append({"source": node, "code": error.code})

    published: list[dict[str, str]] = []
    opened = 0
    for candidate, created_at, transport_id, source in collected:
        if opened >= config.max_prs:
            break
        if dry_run:
            outcome = {
                "outcome": "would-open-private-intake-pr",
                "branch": _branch(config, candidate),
            }
        else:
            try:
                outcome = _publish(config, candidate, created_at=created_at)
            except PromotionError as error:
                errors.append(
                    {"source": source, "name": candidate.name, "code": error.code}
                )
                continue
        row = {
            **outcome,
            "source": source,
            "node": candidate.node,
            "provider": candidate.provider,
            "name": candidate.name,
            "tree_sha256": candidate.tree_sha256,
            "transport_id": transport_id,
        }
        published.append(row)
        if outcome["outcome"] == "pr-opened":
            opened += 1
        if not dry_run and outcome["outcome"] in {"pr-opened", "existing-pr"}:
            try:
                if source == "local":
                    if not _ack_local(config, transport_id):
                        raise PromotionError("local_ack_failed")
                else:
                    _remote_ack(source, transport_id)
                _append_ledger(config, {"ts": _utc_now(), **row})
            except PromotionError as error:
                errors.append(
                    {"source": source, "name": candidate.name, "code": error.code}
                )
            if config.dispatch_enabled:
                row["dispatch"] = _dispatch_intake_review(
                    config, candidate, outcome, transport_id=transport_id
                )
    return {
        "ok": not errors,
        "mode": "collect-dry-run" if dry_run else "collect",
        "publisher_enabled": True,
        "autonomy": config.autonomy,
        "repo": config.repo,
        "published": published,
        "errors": errors,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--dry-run", action="store_true")
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--limit", type=int, default=3, choices=range(1, 4))
    ack_parser = subparsers.add_parser("ack")
    ack_parser.add_argument("transport_id")
    collect_parser = subparsers.add_parser("collect")
    collect_parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = _config()
        if args.command == "status":
            result = _status(config)
        elif args.command == "run":
            result = _execute(config, dry_run=args.dry_run)
        elif args.command == "export":
            result = _export_result(config, limit=args.limit)
        elif args.command == "ack":
            result = _ack_result(config, args.transport_id)
        else:
            result = _collect(config, dry_run=args.dry_run)
    except PromotionError as error:
        result = {"ok": False, "code": error.code}
    except (OSError, TypeError, ValueError, RecursionError):
        result = {"ok": False, "code": "internal_error"}
    print(_json_line(result))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
