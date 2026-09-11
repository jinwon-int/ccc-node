"""Fixture builders for the skill lookup tests (#1678).

A synthetic repo (``skills/registry.json`` with a same-name twin pair plus a
deprecated entry) and per-runtime installed roots, so search/read/policy
tests run against a hermetic world instead of the developer's node.
"""

from __future__ import annotations

import json
from pathlib import Path

from telegram_bot.core.skill_lookup import repo_tree_revision

SKILL_BODY = """---
name: web
description: Fixture repo skill for lookup tests
---

Use this fixture skill for lookup tests.
"""


def write_skill(
    root: Path,
    name: str,
    description: str = "Fixture skill used by lookup tests",
) -> Path:
    """One valid installed-style skill dir (frontmatter name == dir name)."""

    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    body = (
        f"---\nname: {name}\ndescription: {description}\n---\n\n"
        "Use this fixture skill for lookup tests.\n"
    )
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return skill_dir


def build_lookup_world(tmp_path: Path) -> dict[str, str]:
    """Create the world and return the env that pins the lookup onto it."""

    repo = tmp_path / "repo"
    write_skill(tmp_path / "home" / ".claude" / "skills", "local-one")
    write_skill(tmp_path / "home" / ".codex" / "skills", "codex-only")
    write_skill(tmp_path / "home" / ".piri" / "agent" / "skills", "piri-only")

    skill_dir = write_skill(repo / "skills", "web", "Fixture repo skill for lookup tests")
    twin_dir = write_skill(
        repo / "codex" / "skills", "web", "Codex twin of the fixture repo skill"
    )
    retired_dir = write_skill(
        repo / "skills", "retired", "Deprecated fixture repo skill"
    )
    registry = {
        "schema_version": 1,
        "skills": [
            {
                "audience": "claude",
                "classification": "compatible",
                "description": "Fixture repo skill for lookup tests",
                "files": 1,
                "managed": False,
                "name": "web",
                "source": "skills/web",
                "status": "active",
                "tree_sha256": repo_tree_revision(skill_dir, repo),
            },
            {
                "audience": "codex",
                "classification": None,
                "description": "Codex twin of the fixture repo skill",
                "files": 1,
                "managed": False,
                "name": "web",
                "source": "codex/skills/web",
                "status": "active",
                "tree_sha256": repo_tree_revision(twin_dir, repo),
            },
            {
                "audience": "claude",
                "classification": None,
                "description": "Deprecated fixture repo skill",
                "files": 1,
                "managed": False,
                "name": "retired",
                "source": "skills/retired",
                "status": "deprecated",
                "tree_sha256": repo_tree_revision(retired_dir, repo),
            },
        ],
    }
    (repo / "skills" / "registry.json").write_text(
        json.dumps(registry, indent=1, sort_keys=True), encoding="utf-8"
    )
    return {
        "CCC_SKILL_LOOKUP_REPO_ROOT": str(repo),
        "CCC_SKILL_LOOKUP_HOME": str(tmp_path / "home"),
        "CCC_NODE_ISOLATION_PROFILE": "fleet",
    }
