"""Read the same rule as Claude; verify optional-style failure boundaries."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from telegram_bot.utils.report_style import read_report_style

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def style_home(tmp_path):
    claude = tmp_path / ".claude"
    (claude / "state").mkdir(parents=True)
    (claude / "hooks/lib").mkdir(parents=True)
    shutil.copyfile(REPO / "claude/hooks/lib/report-style-ste.txt",
                    claude / "hooks/lib/report-style-ste.txt")
    shutil.copyfile(REPO / "bridge/utils/report_style.py", claude / "hooks/ccc_report_style.py")
    return claude


@pytest.mark.parametrize("note", ["", "\nend: fixture\nignored", "x" * 400, "가" * 200])
def test_claude_and_python_emit_identical_rules(style_home, note):
    (style_home / "state/report-style-canary.flag").write_text(note)
    env = {"PATH": os.defpath, "CCC_CLAUDE_DIR": str(style_home)}
    result = subprocess.run(["bash", str(REPO / "claude/hooks/report-style-canary.sh")],
                            env=env, text=True, capture_output=True, check=True)
    assert read_report_style(style_home, environ=env) == json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    if note.startswith("가"):
        assert "가" * 160 in read_report_style(style_home, environ=env)
        assert "가" * 161 not in read_report_style(style_home, environ=env)


@pytest.mark.parametrize("bad", ["missing", "symlink", "directory", "fifo", "writable", "large", "invalid"])
def test_optional_flag_failures_are_quiet(style_home, bad):
    flag = style_home / "state/report-style-canary.flag"
    if bad == "symlink":
        flag.symlink_to(style_home / "hooks/lib/report-style-ste.txt")
    elif bad == "directory":
        flag.mkdir()
    elif bad == "fifo":
        os.mkfifo(flag)
    elif bad != "missing":
        flag.write_bytes(b"\xff" if bad == "invalid" else b"x" * (4097 if bad == "large" else 1))
        if bad == "writable":
            flag.chmod(0o666)
    assert read_report_style(style_home, environ={}) == ""


@pytest.mark.parametrize("bad", ["missing", "blank", "invalid", "large", "symlink", "fifo"])
def test_optional_rule_failures_are_quiet(style_home, bad):
    (style_home / "state/report-style-canary.flag").touch()
    rule = style_home / "hooks/lib/report-style-ste.txt"
    rule.unlink()
    if bad == "symlink":
        rule.symlink_to(style_home / "state/report-style-canary.flag")
    elif bad == "fifo":
        os.mkfifo(rule)
    elif bad != "missing":
        rule.write_bytes(b"\xff" if bad == "invalid" else b" " * (16385 if bad == "large" else 4))
    assert read_report_style(style_home, environ={}) == ""


def test_distill_guard_and_hook_override(style_home, tmp_path):
    (style_home / "state/report-style-canary.flag").touch()
    assert read_report_style(style_home, environ={"CLAUDE_DISTILL_INFLIGHT": "1"}) == ""
    assert read_report_style(style_home, environ={"CCC_HOOK_DIR": str(tmp_path / "absent")}) == ""
