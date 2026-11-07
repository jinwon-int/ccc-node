"""#2712: owner lanes without a settings chain append the node output style; string prompts no longer replace."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from telegram_bot.core import claude_runtime
from telegram_bot.core.claude_runtime import ClaudeRuntime
from telegram_bot.core.output_style import read_output_style
from telegram_bot.core.system_prompt import append_system_prompt, appended_text

from test_claude_runtime_options import _build, _settings

STYLE = "---\nname: ccc-report\ndescription: test\n---\n# Style body\n- **[답변 필요]** 블록으로 묻는다.\n"


def _claude_dir(tmp_path: Path, *, style: str | None = STYLE, name: object = "ccc-report") -> Path:
    claude = tmp_path / ".claude"
    (claude / "output-styles").mkdir(parents=True, exist_ok=True)
    (claude / "settings.json").write_text(json.dumps({"outputStyle": name}), encoding="utf-8")
    if style is not None:
        (claude / "output-styles" / "ccc-report.md").write_text(style, encoding="utf-8")
    return claude


def test_append_keeps_the_claude_code_preset() -> None:
    options = SimpleNamespace(system_prompt=None)
    append_system_prompt(options, "first")
    append_system_prompt(options, "second")
    append_system_prompt(options, "")
    assert options.system_prompt == {"type": "preset", "preset": "claude_code", "append": "first\n\nsecond"}


def test_append_folds_a_legacy_plain_string_into_the_preset() -> None:
    options = SimpleNamespace(system_prompt="legacy routing")
    append_system_prompt(options, "style")
    assert options.system_prompt["type"] == "preset"
    assert appended_text(options) == "legacy routing\n\nstyle"


def test_append_leaves_a_custom_prompt_alone() -> None:
    custom = {"type": "custom", "prompt": "chosen on purpose"}
    options = SimpleNamespace(system_prompt=dict(custom))
    append_system_prompt(options, "style")
    assert options.system_prompt == custom


def test_read_output_style_strips_front_matter_and_names_the_style(tmp_path: Path) -> None:
    text = read_output_style(_claude_dir(tmp_path))
    assert text.startswith("# Output Style: ccc-report\n# Style body")
    assert "[답변 필요]" in text and "description: test" not in text


def test_read_output_style_is_quietly_empty_when_unconfigured_or_unsafe(tmp_path: Path) -> None:
    assert read_output_style(tmp_path / "missing") == ""
    assert read_output_style(_claude_dir(tmp_path / "a", name="default")) == ""
    assert read_output_style(_claude_dir(tmp_path / "b", name="../etc/passwd")) == ""
    assert read_output_style(_claude_dir(tmp_path / "c", name=7)) == ""
    assert read_output_style(_claude_dir(tmp_path / "d", style=None)) == ""
    claude = _claude_dir(tmp_path / "e")
    os.chmod(claude / "output-styles" / "ccc-report.md", 0o666)
    assert read_output_style(claude) == ""  # group/world-writable is refused
    claude = _claude_dir(tmp_path / "f", style="x" * (40 * 1024))
    assert read_output_style(claude) == ""  # oversized


def test_unrestricted_owner_lane_appends_the_node_output_style(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(claude_runtime, "running_as_root", lambda: False)
    _claude_dir(tmp_path)
    runtime = ClaudeRuntime(settings=_settings(tmp_path, execution_profile="owner-operator", claude_unrestricted=True))
    options = _build(runtime, tmp_path)
    assert options.setting_sources == []
    assert options.system_prompt["type"] == "preset" and options.system_prompt["preset"] == "claude_code"
    assert appended_text(options).startswith("# Output Style: ccc-report")
    assert "[답변 필요]" in appended_text(options)


def test_governed_owner_lane_leaves_the_style_to_the_settings_chain(tmp_path: Path) -> None:
    _claude_dir(tmp_path)
    runtime = ClaudeRuntime(settings=_settings(tmp_path, execution_profile="owner-operator"))
    options = _build(runtime, tmp_path)
    assert options.setting_sources == ["user", "project", "local"]
    assert "Output Style" not in appended_text(options)


def test_strict_project_lane_never_gets_the_owner_style(tmp_path: Path) -> None:
    _claude_dir(tmp_path)
    options = _build(ClaudeRuntime(settings=_settings(tmp_path, execution_profile="strict-project")), tmp_path)
    assert options.setting_sources == []
    assert "Output Style" not in appended_text(options)
