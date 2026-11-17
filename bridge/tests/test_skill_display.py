"""Skill usage display (#2833): status-line label, per-turn names, hande ``result.skills``."""

from __future__ import annotations

from typing import Any

import pytest

from telegram_bot.core.agent_runtime import ToolStartedEvent
from telegram_bot.core.hande.state import HandeConfigError, validate_config
from telegram_bot.core.heartbeat import tool_label
from telegram_bot.core.project_chat_turn_state import TurnEventState
from telegram_bot.core.skill_display import MAX_TURN_SKILLS, bounded_skills, skill_name


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("Skill", {"skill": "gh-pr-flow"}, "gh-pr-flow"),
        ("Skill", {"skill": "/wiki-record", "args": "LOG body"}, "wiki-record"),
        ("Skill", {"skill": "code-review high 123"}, "code-review"),
        ("Skill", {"skill": "plugin:skill"}, "plugin:skill"),
        ("Skill", {"command": "/doctor --fix"}, "doctor"),
        ("Read", {"file_path": "/root/.claude/skills/wiki-record/SKILL.md"}, "wiki-record"),
        ("Read", {"file_path": "/home/u/.codex/skills/.system/gh-ci-wait/SKILL.md"}, "gh-ci-wait"),
        ("Read", {"file_path": "skills/a2a-claim/SKILL.md"}, "a2a-claim"),
        # Not a skill load.
        ("Read", {"file_path": "/root/.claude/skills/wiki-record/README.md"}, None),
        ("Read", {"file_path": "/tmp/SKILL.md"}, None),
        ("Read", {"file_path": "/x/skills/has space/SKILL.md"}, None),
        ("Skill", {"skill": ""}, None),
        ("Skill", {"skill": "../../etc/passwd"}, None),
        ("Skill", {"skill": "x" * 65}, None),
        ("Skill", {"skill": 7}, None),
        ("Bash", {"command": "cat skills/x/SKILL.md"}, None),
        ("Skill", "gh-pr-flow", None),
    ],
)
def test_skill_name(tool: str, arguments: Any, expected: str | None) -> None:
    assert skill_name(tool, arguments) == expected


def test_bounded_skills_dedupes_in_order_and_caps() -> None:
    names = ["b", "a", "b", "bad name", 3, *[f"s{i}" for i in range(20)]]
    result = bounded_skills(names)
    assert result[:2] == ("b", "a")
    assert len(result) == MAX_TURN_SKILLS
    assert "bad name" not in result


def test_status_label_names_the_skill() -> None:
    assert tool_label("Skill", {"skill": "gh-pr-flow", "args": "secret-ish"}) == "📘 gh-pr-flow"
    assert tool_label("Read", {"file_path": "/r/.claude/skills/wiki-record/SKILL.md"}) == "📘 wiki-record"
    # Other tools keep their labels.
    assert tool_label("Read", {"file_path": "/tmp/a.py"}) == "Read: /tmp/a.py"
    assert tool_label("Skill", {}) == "Skill"


def test_turn_state_collects_skills_first_seen_bounded() -> None:
    state = TurnEventState()
    events = [
        ToolStartedEvent("t1", "Skill", {"skill": "gh-pr-flow"}),
        ToolStartedEvent("t2", "Bash", {"command": "ls"}),
        ToolStartedEvent("t3", "Read", {"file_path": "/r/skills/wiki-record/SKILL.md"}),
        ToolStartedEvent("t4", "Skill", {"skill": "gh-pr-flow"}),
    ]
    for event in events:
        state.observe(event)
    assert state.skills_used == ["gh-pr-flow", "wiki-record"]
    assert state.active_tools["t1"] == "📘 gh-pr-flow"

    many = TurnEventState()
    for index in range(MAX_TURN_SKILLS + 3):
        many.observe(ToolStartedEvent(f"s{index}", "Skill", {"skill": f"skill-{index}"}))
    assert len(many.skills_used) == MAX_TURN_SKILLS


def test_hande_config_skill_frames_knob() -> None:
    base = {"account": "@n:hande", "owner": "@owner:hande", "rooms": ["agent"]}
    assert validate_config(dict(base))["skill_frames"] is False
    assert validate_config({**base, "skill_frames": True})["skill_frames"] is True
    with pytest.raises(HandeConfigError):
        validate_config({**base, "skill_frames": "yes"})
