"""Body-free skill names for the status line and hande ``result.skills`` (#2833).

A skill reaches a Claude session two ways, the same two the node's
``claude/hooks/skill-usage-log.sh`` ledger counts (#1347): the ``Skill`` tool
(``{"skill": "gh-pr-flow", "args": …}``) and a plain ``Read`` of
``…/skills/<name>/SKILL.md`` — how bridge-resolved and file-invoked skills
actually load. :func:`skill_name` returns the skill's name for either, or
``None``.

Only the name leaves this function, and only when it is identifier-shaped
(:data:`_SKILL_NAME_RE`): the ``Skill`` arguments and the rest of the path are
never returned, so the name is safe for a status line and a ``result`` frame.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

#: Identifier-shaped skill names only (plugin skills use ``plugin:skill``).
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
#: ``…/skills/[.system/]<name>/SKILL.md`` (the ``core/codex_skill_usage.py`` shape).
_SKILL_FILE_RE = re.compile(r"(?:^|/)skills/(?:\.system/)?([^/]+)/SKILL\.md$")
#: Skills kept per turn for the ``result`` frame.
MAX_TURN_SKILLS = 8


def _valid(name: str) -> str | None:
    return name if _SKILL_NAME_RE.match(name) else None


def skill_name(tool_name: Any, tool_input: Any) -> str | None:
    """The skill a ``Skill`` call or a ``SKILL.md`` ``Read`` loads, else ``None``."""

    if not isinstance(tool_input, dict):
        return None
    if tool_name == "Skill":
        raw = tool_input.get("skill") or tool_input.get("command")
        if not isinstance(raw, str):
            return None
        words = raw.strip().lstrip("/").split(maxsplit=1)
        return _valid(words[0]) if words else None
    if tool_name == "Read":
        path = tool_input.get("file_path") or tool_input.get("path")
        if not isinstance(path, str):
            return None
        match = _SKILL_FILE_RE.search(path.strip())
        return _valid(match.group(1)) if match else None
    return None


def bounded_skills(names: Iterable[Any]) -> tuple[str, ...]:
    """Valid names in first-seen order, deduplicated, at most :data:`MAX_TURN_SKILLS`."""

    seen: list[str] = []
    for name in names:
        if isinstance(name, str) and _valid(name) and name not in seen:
            seen.append(name)
            if len(seen) >= MAX_TURN_SKILLS:
                break
    return tuple(seen)
