"""Production-import contract for the owner-operated bridge profile."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_PROBE = r"""
import asyncio
import json
import os
from pathlib import Path

import telegram_bot
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from telegram_bot.utils.config import config
from telegram_bot.core.bot import enforce_access_control


enforce_access_control(config)

from telegram_bot.core import project_chat

repo_root = Path(os.environ["CCC_CONTRACT_REPO_ROOT"]).resolve()
candidate_imports = all(
    Path(module.__file__).resolve().is_relative_to(repo_root)
    for module in (telegram_bot, project_chat)
)


class FakeSDKClient:
    last_options = None

    def __init__(self, options):
        type(self).last_options = options

    async def connect(self):
        return None


def close_task(coro):
    coro.close()
    return object()


project_chat.ClaudeSDKClient = FakeSDKClient
project_chat.asyncio.create_task = close_task
asyncio.run(project_chat.ProjectChatHandler()._create_user_stream(42, None))
options = FakeSDKClient.last_options
assert options is not None
transport = SubprocessCLITransport(prompt="", options=options)
transport._cli_path = "/bin/true"
cli_args = transport._build_command()
print(
    json.dumps(
        {
            "allowed_owner_ids": config.allowed_user_ids,
            "execution_profile": project_chat.EXECUTION_PROFILE,
            "bash_policy": project_chat.BASH_POLICY,
            "candidate_imports": candidate_imports,
            "require_allowlist": config.require_allowlist,
            "sandbox": options.sandbox,
            "setting_sources": options.setting_sources,
            "bash_allowed": "Bash" in options.allowed_tools,
            "bash_disallowed": "Bash" in options.disallowed_tools,
            "pretool_hook": "PreToolUse" in options.hooks,
            "normal_setting_sources_cli": (
                "--setting-sources=user,project,local" in cli_args
            ),
        },
        sort_keys=True,
    )
)
"""


def _probe_owner_profile(tmp_path: Path, bash_policy: str) -> dict:
    project_root = tmp_path / "project"
    data_dir = project_root / ".telegram_bot"
    data_dir.mkdir(parents=True)
    project_root.chmod(0o700)
    data_dir.chmod(0o700)
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    home_dir.chmod(0o700)
    env_file = data_dir / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=123456:test\n"
        "ALLOWED_USER_IDS=42\n"
        "CCC_REQUIRE_ALLOWLIST=true\n"
        "CCC_BRIDGE_EXECUTION_PROFILE=owner-operator\n"
        f"CCC_BRIDGE_BASH_POLICY={bash_policy}\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ.get("PATH", ""),
        "PROJECT_ROOT": str(project_root),
        "CCC_CONTRACT_REPO_ROOT": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(REPO_ROOT / ".github" / "pythonpath"),
    }
    result = subprocess.run(
        [sys.executable, "-c", PRODUCTION_PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("bash_policy", "bash_allowed", "bash_disallowed", "pretool_hook"),
    [
        ("auto-approve", True, False, False),
        ("approve-each", False, False, True),
        ("disabled", False, True, False),
    ],
)
def test_production_owner_profile_preserves_host_scope_across_bash_policies(
    tmp_path: Path,
    bash_policy: str,
    bash_allowed: bool,
    bash_disallowed: bool,
    pretool_hook: bool,
):
    observed = _probe_owner_profile(tmp_path, bash_policy)

    assert observed == {
        "allowed_owner_ids": [42],
        "bash_allowed": bash_allowed,
        "bash_disallowed": bash_disallowed,
        "bash_policy": bash_policy,
        "candidate_imports": True,
        "execution_profile": "owner-operator",
        "normal_setting_sources_cli": True,
        "pretool_hook": pretool_hook,
        "require_allowlist": True,
        "sandbox": None,
        "setting_sources": ["user", "project", "local"],
    }
