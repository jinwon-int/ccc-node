from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core.skill_command import (
    SkillCommandResolutionError,
    expand_audience_scoped_skill_command,
)


def _config(tmp_path: Path, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "agent_provider": "claude",
        "execution_profile": "owner-operator",
        "bridge_memory_mode": "audience-scoped",
        "project_root": tmp_path / "project",
        "claude_settings_path": tmp_path / "home" / ".claude" / "settings.json",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _install(config: SimpleNamespace, name: str = "skillsuggest") -> Path:
    root = Path(config.claude_settings_path).parent / "skills"
    skill_dir = root / name
    skill_dir.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    skill_dir.chmod(0o700)
    skill = skill_dir / "SKILL.md"
    skill.write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n# Procedure\nDo the thing.\n",
        encoding="utf-8",
    )
    skill.chmod(0o600)
    return skill


def test_expands_installed_skill_for_audience_scoped_owner_claude(tmp_path: Path):
    config = _config(tmp_path)
    skill = _install(config)

    expanded = expand_audience_scoped_skill_command(
        config, "/skillsuggest review pending"
    )

    assert not expanded.startswith("/")
    assert "explicitly invoked local skill" in expanded
    assert f'"{skill}"' in expanded
    assert '"review pending"' in expanded
    assert "Use the Read tool" in expanded

    multiline = expand_audience_scoped_skill_command(
        config, "/skillsuggest\nreview the queue"
    )
    assert '"review the queue"' in multiline


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_provider", "codex"),
        ("execution_profile", "strict-project"),
        ("bridge_memory_mode", "curated"),
    ],
)
def test_preserves_native_command_outside_target_profile(
    tmp_path: Path, field: str, value: str
):
    config = _config(tmp_path, **{field: value})
    _install(config)

    assert (
        expand_audience_scoped_skill_command(config, "/skillsuggest")
        == "/skillsuggest"
    )


def test_preserves_unknown_and_invalid_commands(tmp_path: Path):
    config = _config(tmp_path)
    _install(config)

    assert expand_audience_scoped_skill_command(config, "/compact") == "/compact"
    assert (
        expand_audience_scoped_skill_command(config, "/../skillsuggest")
        == "/../skillsuggest"
    )
    assert expand_audience_scoped_skill_command(config, "plain text") == "plain text"


def test_rejects_frontmatter_name_mismatch(tmp_path: Path):
    config = _config(tmp_path)
    skill = _install(config)
    skill.write_text(
        "---\nname: another-skill\ndescription: mismatch\n---\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillCommandResolutionError, match="frontmatter"):
        expand_audience_scoped_skill_command(config, "/skillsuggest")


def test_rejects_group_or_world_writable_skill(tmp_path: Path):
    config = _config(tmp_path)
    skill = _install(config)
    skill.chmod(0o622)

    with pytest.raises(SkillCommandResolutionError, match="writable"):
        expand_audience_scoped_skill_command(config, "/skillsuggest")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_rejects_symlinked_skill_file(tmp_path: Path):
    config = _config(tmp_path)
    skill = _install(config)
    target = skill.with_name("trusted.md")
    skill.rename(target)
    skill.symlink_to(target.name)

    with pytest.raises(SkillCommandResolutionError, match="symlink"):
        expand_audience_scoped_skill_command(config, "/skillsuggest")


def test_bot_command_paths_use_skill_aware_expansion():
    source = (
        Path(__file__).resolve().parents[1] / "core" / "bot_commands.py"
    ).read_text(encoding="utf-8")

    command_start = source.index("async def _cmd_command")
    command_end = source.index("async def _exec_slash_command", command_start)
    exec_start = command_end
    exec_end = source.index("async def _cmd_skill", exec_start)

    assert "_skill_aware_slash_message(message, slash_cmd)" in source[
        command_start:command_end
    ]
    assert "user_message=user_message" in source[command_start:command_end]
    assert "_skill_aware_slash_message(message, slash_cmd)" in source[
        exec_start:exec_end
    ]
    assert "user_message=user_message" in source[exec_start:exec_end]
