"""Selective local-skill invocation for isolated Claude bridge sessions.

Audience-scoped Claude sessions deliberately set ``setting_sources=[]`` so the
SDK cannot register host hooks or unscoped memory settings.  That also hides
otherwise trusted personal skills from Claude Code's slash-command registry.
This module restores only an explicitly invoked, owner-owned ``SKILL.md``; it
does not re-enable any filesystem settings source.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any


_SKILL_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_MAX_SKILL_BYTES = 128 * 1024


class SkillCommandResolutionError(ValueError):
    """The named local skill exists but failed fail-closed validation."""


def _audience_scoped_owner_claude(config: Any) -> bool:
    return (
        str(getattr(config, "agent_provider", "")).strip().lower() == "claude"
        and str(getattr(config, "execution_profile", "")).strip().lower()
        == "owner-operator"
        and str(getattr(config, "bridge_memory_mode", "")).strip().lower()
        == "audience-scoped"
    )


def _parse_slash_command(slash_command: str) -> tuple[str, str] | None:
    if not isinstance(slash_command, str) or not slash_command.startswith("/"):
        return None
    parts = slash_command[1:].split(maxsplit=1)
    if not parts or not _SKILL_NAME_RE.fullmatch(parts[0]):
        return None
    return parts[0], parts[1] if len(parts) == 2 else ""


def _skill_roots(config: Any) -> tuple[Path, ...]:
    roots: list[Path] = []
    project_root = getattr(config, "project_root", None)
    if project_root is not None:
        roots.append(Path(project_root).expanduser() / ".claude" / "skills")
    settings_path = getattr(config, "claude_settings_path", None)
    if settings_path is not None:
        roots.append(Path(settings_path).expanduser().parent / "skills")

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = os.path.abspath(os.fspath(root))
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return tuple(unique)


def _frontmatter_name(text: str) -> str | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:65]:
        if line.strip() == "---":
            break
        if line.startswith("name:"):
            value = line.partition(":")[2].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return None


def _validated_skill_file(root: Path, name: str) -> Path | None:
    try:
        if root.is_symlink():
            raise SkillCommandResolutionError("skill root must not be a symlink")
        root_resolved = root.resolve(strict=True)
        root_stat = root_resolved.stat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != os.geteuid()
        or root_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise SkillCommandResolutionError("skill root permissions are unsafe")
    candidate_dir = root_resolved / name
    candidate = candidate_dir / "SKILL.md"
    if not candidate_dir.exists() and not candidate.exists():
        return None
    try:
        if candidate_dir.is_symlink() or candidate.is_symlink():
            raise SkillCommandResolutionError("skill path must not be a symlink")
        directory_stat = candidate_dir.stat()
        file_stat = candidate.stat()
        candidate_resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise SkillCommandResolutionError("skill installation is incomplete") from exc

    if not candidate_resolved.is_relative_to(root_resolved):
        raise SkillCommandResolutionError("skill path escapes its trusted root")
    if not stat.S_ISDIR(directory_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise SkillCommandResolutionError("skill installation type is invalid")
    if directory_stat.st_uid != os.geteuid() or file_stat.st_uid != os.geteuid():
        raise SkillCommandResolutionError("skill installation owner is invalid")
    if (directory_stat.st_mode | file_stat.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
        raise SkillCommandResolutionError("skill installation is writable by another user")
    if file_stat.st_size <= 0 or file_stat.st_size > _MAX_SKILL_BYTES:
        raise SkillCommandResolutionError("skill file size is invalid")

    try:
        payload = candidate_resolved.read_bytes()
        text = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillCommandResolutionError("skill file is not safe UTF-8 text") from exc
    if len(payload) != file_stat.st_size or "\x00" in text:
        raise SkillCommandResolutionError("skill file content is invalid")
    if _frontmatter_name(text) != name:
        raise SkillCommandResolutionError("skill frontmatter name does not match")
    return candidate_resolved


def expand_audience_scoped_skill_command(config: Any, slash_command: str) -> str:
    """Expand one installed skill command without enabling host setting sources.

    Non-skill commands and every non-target execution profile are returned
    byte-for-byte unchanged so native Claude slash commands retain their
    existing behavior.
    """

    if not _audience_scoped_owner_claude(config):
        return slash_command
    parsed = _parse_slash_command(slash_command)
    if parsed is None:
        return slash_command
    name, arguments = parsed

    for root in _skill_roots(config):
        skill_file = _validated_skill_file(root, name)
        if skill_file is None:
            continue
        skill_dir = skill_file.parent
        return (
            "The bridge resolved the operator's command as an explicitly invoked "
            "local skill. Use the Read tool to read the SKILL.md file completely "
            "before acting, follow its instructions for this turn, and resolve its "
            "relative references from the skill directory. Do not reinterpret the "
            "original slash command as a Claude Code built-in.\n"
            f"Skill name (JSON): {json.dumps(name, ensure_ascii=False)}\n"
            f"SKILL.md path (JSON): {json.dumps(os.fspath(skill_file), ensure_ascii=False)}\n"
            f"Skill directory (JSON): {json.dumps(os.fspath(skill_dir), ensure_ascii=False)}\n"
            "Operator arguments as a plain user-supplied JSON string: "
            f"{json.dumps(arguments, ensure_ascii=False)}"
        )
    return slash_command
