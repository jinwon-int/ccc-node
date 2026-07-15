from __future__ import annotations

import json
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from telegram_bot.core.curated_memory import build_curated_memory_settings
from telegram_bot.core import project_chat
from telegram_bot.core.bot import TelegramBot
from telegram_bot.core.session_scope import (
    legacy_storage_keys,
    storage_key,
    stream_key,
)


def test_default_scope_isolates_sender_chat_pairs() -> None:
    assert storage_key("per-user-chat", 7, 7) == 7
    assert storage_key("per-user-chat", 7, -100) == "7:-100"
    assert storage_key("per-user-chat", 8, -100) == "8:-100"
    assert stream_key("per-user-chat", 7, -100) == (7, -100)


def test_shared_groups_keep_dms_and_groups_isolated() -> None:
    assert storage_key("shared-groups", 7, 7) == 7
    assert storage_key("shared-groups", 8, 8) == 8
    assert storage_key("shared-groups", 7, -100) == "0:-100"
    assert storage_key("shared-groups", 8, -100) == "0:-100"
    assert storage_key("shared-groups", 7, -200) == "0:-200"
    assert stream_key("shared-groups", 7, -100) == (0, -100)
    assert stream_key("shared-groups", 8, -100) == (0, -100)
    assert stream_key("shared-groups", 7, -200) == (0, -200)


def test_shared_group_migration_prefers_same_sender_same_group() -> None:
    assert legacy_storage_keys("shared-groups", 7, -100) == ("7:-100", 7)
    assert legacy_storage_keys("per-user-chat", 7, -100) == (7,)


def _settings(tmp_path: Path, mode: str):
    return SimpleNamespace(
        bridge_memory_mode=mode,
        claude_settings_path=tmp_path / ".claude" / "settings.json",
        hook_policy_environment=lambda: {
            "CCC_NODE_ISOLATION_PROFILE": "external",
            "CCC_WIKI_MEMORY_ENABLED": "0",
            "CCC_MEMORY_USER_LABEL": "Etter",
            "CCC_MEMORY_ASSISTANT_LABEL": "Karellen",
        },
    )


def test_curated_memory_settings_are_off_by_default(tmp_path: Path) -> None:
    assert build_curated_memory_settings(_settings(tmp_path, "off")) is None


def test_curated_memory_settings_load_only_memory_lifecycle(tmp_path: Path) -> None:
    raw = build_curated_memory_settings(_settings(tmp_path, "curated"))
    assert raw is not None
    payload = json.loads(raw)
    assert payload["env"]["CCC_NODE_ISOLATION_PROFILE"] == "external"
    assert payload["env"]["CCC_WIKI_MEMORY_ENABLED"] == "0"
    assert set(payload["hooks"]) == {
        "SessionStart",
        "PreCompact",
        "PostCompact",
        "SessionEnd",
    }
    commands = [
        hook["command"]
        for matchers in payload["hooks"].values()
        for matcher in matchers
        for hook in matcher["hooks"]
    ]
    assert any("load-memory.sh SessionStart" in command for command in commands)
    assert any("checkpoint.sh PreCompact" in command for command in commands)
    assert any("distill.sh precompact" in command for command in commands)
    assert any("load-memory.sh PostCompact" in command for command in commands)
    assert any("distill.sh sessionend" in command for command in commands)
    assert all("guard.py" not in command for command in commands)
    assert all("skill-review" not in command for command in commands)


class _FakeSDKClient:
    last_options = None

    def __init__(self, options):
        type(self).last_options = options

    async def connect(self):
        return None


def _handler_settings(tmp_path: Path):
    return SimpleNamespace(
        project_root=tmp_path,
        execution_profile="disabled",
        bash_policy="disabled",
        allowed_user_ids=[1, 2, 3, 4],
        require_allowlist=True,
        claude_cli_path=None,
        enable_streaming=False,
        enable_partial_streaming=False,
        bot_data_dir=None,
        task_ledger_path=None,
        telegram_session_scope="shared-groups",
        image_context_guard=True,
        bridge_memory_mode="curated",
        claude_settings_path=tmp_path / ".claude" / "settings.json",
        hook_policy_environment=lambda: {
            "CCC_NODE_ISOLATION_PROFILE": "external",
            "CCC_WIKI_MEMORY_ENABLED": "0",
            "CCC_MEMORY_USER_LABEL": "Etter",
            "CCC_MEMORY_ASSISTANT_LABEL": "Karellen",
        },
    )


def test_project_chat_shares_group_stream_and_lock_keys(tmp_path: Path) -> None:
    handler = project_chat.ProjectChatHandler(
        settings=_handler_settings(tmp_path), sdk_client_factory=_FakeSDKClient
    )
    assert handler._stream_key(1, -100) == (0, -100)
    assert handler._stream_key(2, -100) == (0, -100)
    assert handler._stream_key(1, -200) == (0, -200)
    assert handler._stream_key(1, 1) == (1, 1)
    assert handler._get_conversation_lock(1, -100) is handler._get_conversation_lock(
        2, -100
    )


def test_first_sender_deterministically_seeds_shared_group_session() -> None:
    class Manager:
        def __init__(self):
            self.sessions = {
                "0:-100": {"provider": "claude", "reply_mode": "text"},
                "1:-100": {
                    "provider": "claude",
                    "reply_mode": "text",
                    "session_id": "sender-one",
                    "model": "opus",
                },
                "2:-100": {
                    "provider": "claude",
                    "reply_mode": "text",
                    "session_id": "sender-two",
                },
            }

        async def get_session(self, key):
            return dict(self.sessions.get(key, {"provider": "claude", "reply_mode": "text"}))

        async def patch_session(self, key, *, updates):
            self.sessions.setdefault(key, {}).update(updates)

    async def exercise():
        manager = Manager()
        bot = TelegramBot.__new__(TelegramBot)
        bot._config = SimpleNamespace(telegram_session_scope="shared-groups")
        bot._session_manager = manager
        bot._runtime_active_sessions = {"1:-100"}
        group = await manager.get_session("0:-100")
        await bot._seed_scoped_session_from_legacy("0:-100", 1, -100, group)
        assert manager.sessions["0:-100"]["session_id"] == "sender-one"
        assert manager.sessions["0:-100"]["model"] == "opus"
        assert "0:-100" in bot._runtime_active_sessions

        # Once seeded, another sender cannot overwrite the group session.
        persisted = await manager.get_session("0:-100")
        await bot._seed_scoped_session_from_legacy("0:-100", 2, -100, persisted)
        assert manager.sessions["0:-100"]["session_id"] == "sender-one"

    asyncio.run(exercise())


def test_non_owner_curated_settings_and_image_read_dedupe(tmp_path: Path) -> None:
    script = r'''
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telegram_bot.core import project_chat
from telegram_bot.core.project_chat_types import _PendingRequest

root = Path(os.environ["PROBE_ROOT"])
image = root / "same.jpg"
image.write_bytes(b"first")

class FakeSDKClient:
    last_options = None
    def __init__(self, options):
        type(self).last_options = options
    async def connect(self):
        return None

settings = SimpleNamespace(
    project_root=root,
    execution_profile="disabled",
    bash_policy="disabled",
    allowed_user_ids=[1, 2, 3, 4],
    require_allowlist=True,
    claude_cli_path=None,
    enable_streaming=False,
    enable_partial_streaming=False,
    bot_data_dir=None,
    task_ledger_path=None,
    telegram_session_scope="shared-groups",
    image_context_guard=True,
    bridge_memory_mode="curated",
    claude_settings_path=root / ".claude" / "settings.json",
    hook_policy_environment=lambda: {
        "CCC_NODE_ISOLATION_PROFILE": "external",
        "CCC_WIKI_MEMORY_ENABLED": "0",
        "CCC_MEMORY_USER_LABEL": "Etter",
        "CCC_MEMORY_ASSISTANT_LABEL": "Karellen",
    },
)

async def exercise():
    handler = project_chat.ProjectChatHandler(
        settings=settings, sdk_client_factory=FakeSDKClient
    )
    def close_task(coro):
        coro.close()
        return object()
    with patch.object(project_chat.asyncio, "create_task", side_effect=close_task):
        state = await handler._create_user_stream(1, None)
    options = FakeSDKClient.last_options
    assert options.setting_sources == []
    assert json.loads(options.settings)["hooks"]["PreCompact"]
    callback = options.hooks["PreToolUse"][0].hooks[0]
    request = _PendingRequest(
        user_id=1,
        chat_id=-100,
        model=None,
        requested_session_id=None,
        permission_callback=None,
        typing_callback=None,
        future=asyncio.get_running_loop().create_future(),
    )
    state.pending.append(request)
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": str(image)},
    }
    assert await callback(payload, "tool-1", None) == {}
    denied = await callback(payload, "tool-2", None)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    image.write_bytes(b"changed-and-larger")
    assert await callback(payload, "tool-3", None) == {}

asyncio.run(exercise())
print("CURATED_IMAGE_GUARD_OK")
'''
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": os.environ.get("PATH", ""),
            "PROJECT_ROOT": str(tmp_path),
            "TELEGRAM_BOT_TOKEN": "123456:test",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(repo_root / ".github" / "pythonpath"),
            "PROBE_ROOT": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CURATED_IMAGE_GUARD_OK"
