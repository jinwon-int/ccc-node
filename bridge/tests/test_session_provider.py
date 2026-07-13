"""Provider-aware session persistence and command behavior."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from telegram_bot.core.project_chat_types import ChatResponse
from telegram_bot.core.agent_runtime import (
    ModelInfo,
    SessionHistory,
    SessionHistoryMessage,
    SessionSummary,
)
from telegram_bot.session.manager import SessionManager
from telegram_bot.session.store import SessionStore


class Message:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.message_id = 1
        self.reply_to_message = None
        self.replies: list[tuple[str, dict]] = []
        self.chat = SimpleNamespace(send_action=AsyncMock())

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append((text, kwargs))


def make_update(*, user_id: int = 7, chat_id: int = 9, text: str = ""):
    message = Message(text)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        message=message,
        callback_query=None,
    )


def make_manager(tmp_path: Path, provider: str) -> SessionManager:
    store = SessionStore(tmp_path / "sessions.json")
    manager = SessionManager(
        store,
        SimpleNamespace(agent_provider=provider, auto_new_session_after_hours=None),
    )
    manager.initialize()
    return manager


def telegram_bot_class():
    chat_logger = sys.modules.get("telegram_bot.utils.chat_logger")
    if chat_logger is not None and not callable(getattr(chat_logger, "log_debug", None)):
        sys.modules.pop("telegram_bot.utils.chat_logger", None)
        sys.modules.pop("telegram_bot.core.bot", None)
    from telegram_bot.core.bot import TelegramBot

    return TelegramBot


def bare_bot(manager: SessionManager, *, provider: str, project_chat=None) -> Any:
    TelegramBot = telegram_bot_class()
    bot = TelegramBot.__new__(TelegramBot)
    bot._session_manager = manager
    bot._config = SimpleNamespace(
        agent_provider=provider,
        claude_settings_path=Path("/path/that/must/not/be/read"),
    )
    bot._project_chat = project_chat or SimpleNamespace()
    bot._runtime_active_sessions = set()
    bot._clock = SimpleNamespace(time=lambda: 1000.0)
    bot._check_access = AsyncMock(return_value=True)
    return bot


@pytest.mark.parametrize(
    ("bash_policy", "approval", "reviewer", "sandbox"),
    [
        ("approve-each", "untrusted", None, None),
        (
            "auto-approve",
            "never",
            None,
            {"type": "workspaceWrite", "networkAccess": False},
        ),
        (
            "auto-review",
            "on-request",
            "auto_review",
            {"type": "workspaceWrite", "networkAccess": False},
        ),
        ("disabled", "untrusted", None, None),
    ],
)
def test_codex_execution_policy_follows_bridge_bash_policy(
    tmp_path: Path,
    bash_policy: str,
    approval: str,
    reviewer: str | None,
    sandbox: dict[str, object] | None,
) -> None:
    bot = bare_bot(make_manager(tmp_path, "codex"), provider="codex")
    bot._config.bash_policy = bash_policy
    bot._config.execution_profile = "owner-operator"
    bot._config.allowed_user_ids = [7]
    bot._config.require_allowlist = True

    assert bot._codex_approval_policy() == approval
    assert bot._codex_approvals_reviewer() == reviewer
    assert bot._codex_sandbox_policy() == sandbox


@pytest.mark.anyio
async def test_legacy_provider_defaults_to_claude_without_bulk_migration(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, "codex")
    await manager.store.set(7, {"session_id": "legacy", "reply_mode": "voice"})

    session = await manager.get_session(7)

    assert session["provider"] == "claude"
    assert "provider" not in (await manager.store.get(7))


@pytest.mark.anyio
async def test_new_default_session_uses_active_provider(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, "codex")

    session = await manager.get_session(7)

    assert session == {"provider": "codex", "reply_mode": "text"}
    assert (await manager.store.get(7))["provider"] == "codex"


@pytest.mark.anyio
async def test_provider_switch_reset_is_atomic_and_exactly_conversation_scoped(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, "codex")
    await manager.store.set(
        "7:9",
        {
            "provider": "claude",
            "session_id": "claude-9",
            "model": "opus",
            "effort": "high",
            "reply_mode": "voice",
            "metadata": {"keep": True},
        },
    )
    await manager.store.set(
        "7:10",
        {"provider": "claude", "session_id": "claude-10", "model": "sonnet"},
    )

    session, switched = await manager.align_active_provider("7:9")

    assert switched is True
    assert session == {
        "provider": "codex",
        "session_id": None,
        "new_session": True,
        "reply_mode": "voice",
        "metadata": {"keep": True},
    }
    assert await manager.store.get("7:10") == {
        "provider": "claude",
        "session_id": "claude-10",
        "model": "sonnet",
    }


@pytest.mark.anyio
async def test_resume_provider_mismatch_rejects_without_mutation(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, "codex")
    original = {"session_id": "legacy-claude", "model": "opus"}
    await manager.store.set("7:9", original)
    project_chat = SimpleNamespace(list_sessions=Mock(side_effect=AssertionError("must not list")))
    bot = bare_bot(manager, provider="codex", project_chat=project_chat)
    update = make_update()

    await bot._cmd_resume(update, SimpleNamespace(args=[]))

    assert await manager.store.get("7:9") == original
    assert "provider mismatch" in update.message.replies[0][0].lower()


@pytest.mark.anyio
async def test_history_uses_conversation_scope_and_labels_legacy_claude(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, "claude")
    await manager.store.set("7:9", {"session_id": "right"})
    await manager.store.set("7:10", {"provider": "codex", "session_id": "wrong"})
    project_chat = SimpleNamespace(
        get_recent_messages=Mock(
            return_value=[
                {"role": "assistant", "content": "hello", "timestamp": "2026-01-01T00:00:00Z"}
            ]
        )
    )
    bot = bare_bot(manager, provider="claude", project_chat=project_chat)
    update = make_update(chat_id=9)

    await bot._cmd_history(update, SimpleNamespace(args=[]))

    project_chat.get_recent_messages.assert_called_once_with("right", limit=5)
    assert "Provider: claude" in update.message.replies[0][0]


@pytest.mark.anyio
async def test_codex_resume_uses_runtime_and_persists_canonical_provider_entries(
    tmp_path: Path,
) -> None:
    manager = make_manager(tmp_path, "codex")
    project_chat = SimpleNamespace(
        list_runtime_sessions=AsyncMock(return_value=(
            SessionSummary(
                "codex-1", title="Thread title", preview="hello", updated_at=900.0,
                cwd="/workspace", model="o3",
            ),
        )),
        list_sessions=Mock(side_effect=AssertionError("must not read Claude transcripts")),
    )
    bot = bare_bot(manager, provider="codex", project_chat=project_chat)
    update = make_update()

    await bot._cmd_resume(update, SimpleNamespace(args=[]))

    project_chat.list_runtime_sessions.assert_awaited_once_with(limit=10)
    session = await manager.get_session("7:9")
    assert session["resume_list"] == [["codex-1", "Thread title", "codex"]]
    assert "[codex" in update.message.replies[0][0].lower()


@pytest.mark.anyio
async def test_codex_history_never_reads_claude_transcript_store(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, "codex")
    await manager.store.set(
        "7:9", {"provider": "codex", "session_id": "codex-thread"}
    )
    project_chat = SimpleNamespace(
        get_recent_messages=Mock(side_effect=AssertionError("must not read Claude history")),
        read_runtime_session=AsyncMock(return_value=SessionHistory(
            "codex-thread",
            (
                SessionHistoryMessage("user", "hello", "2026-01-01T00:00:00Z"),
                SessionHistoryMessage("assistant", "world", "2026-01-01T00:01:00Z"),
            ),
        )),
    )
    bot = bare_bot(manager, provider="codex", project_chat=project_chat)
    update = make_update(chat_id=9)

    await bot._cmd_history(update, SimpleNamespace(args=[]))

    project_chat.get_recent_messages.assert_not_called()
    project_chat.read_runtime_session.assert_awaited_once_with("codex-thread", limit=5)
    reply = update.message.replies[0][0]
    assert "Provider: codex" in reply
    assert "hello" in reply
    assert "world" in reply


@pytest.mark.anyio
async def test_codex_browsing_empty_and_error_states_are_safe(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, "codex")
    await manager.patch_session(
        "7:9", updates={"provider": "codex", "session_id": "codex-thread"}
    )
    project_chat = SimpleNamespace(
        list_runtime_sessions=AsyncMock(side_effect=RuntimeError("raw payload secret")),
        read_runtime_session=AsyncMock(return_value=SessionHistory("codex-thread", ())),
        list_runtime_models=AsyncMock(return_value=()),
    )
    bot = bare_bot(manager, provider="codex", project_chat=project_chat)

    resume = make_update()
    await bot._cmd_resume(resume, SimpleNamespace(args=[]))
    assert resume.message.replies[0][0] == "⚠️ Codex session history is unavailable."
    assert "raw payload secret" not in resume.message.replies[0][0]

    history = make_update()
    await bot._cmd_history(history, SimpleNamespace(args=[]))
    assert history.message.replies[0][0] == "📭 No history available for this session."

    model = make_update()
    await bot._cmd_model(model, SimpleNamespace(args=[]))
    assert model.message.replies[0][0] == (
        "📭 No Codex models are available. Use /model <codex-model>."
    )


@pytest.mark.anyio
async def test_claude_model_command_keeps_alias_keyboard_and_settings_default(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    manager = make_manager(tmp_path, "claude")
    bot = bare_bot(manager, provider="claude")
    bot._config.claude_settings_path = settings_path
    update = make_update()

    await bot._cmd_model(update, SimpleNamespace(args=[]))

    text, kwargs = update.message.replies[0]
    assert text == "🤖 Select Claude Code model:"
    labels = [row[0].text for row in kwargs["reply_markup"].inline_keyboard]
    assert "Claude Opus (current)" in labels


@pytest.mark.anyio
async def test_codex_new_preserves_explicit_model_without_reading_claude_settings(
    tmp_path: Path,
) -> None:
    manager = make_manager(tmp_path, "codex")
    await manager.patch_session(
        "7:9", updates={"provider": "codex", "model": "codex-explicit"}
    )
    bot = bare_bot(manager, provider="codex")
    bot._cancel_user_voice_tasks = AsyncMock(return_value=0)
    bot._cancel_user_streaming = AsyncMock(return_value=False)
    update = make_update()

    with patch("builtins.open") as open_file:
        await bot._cmd_new(update, SimpleNamespace(args=[]))

    open_file.assert_not_called()
    session = await manager.get_session("7:9")
    assert session["provider"] == "codex"
    assert session["session_id"] is None
    assert session["model"] == "codex-explicit"
    assert session["new_session"] is True


@pytest.mark.anyio
async def test_codex_model_command_does_not_read_claude_settings_and_persists_raw_value(
    tmp_path: Path,
) -> None:
    manager = make_manager(tmp_path, "codex")
    await manager.store.set(
        "7:9", {"session_id": "legacy-claude", "model": "opus"}
    )
    project_chat = SimpleNamespace(
        list_runtime_models=AsyncMock(return_value=(
            ModelInfo("o3", "O3"),
            ModelInfo("codex/custom", "Codex Custom"),
        ))
    )
    bot = bare_bot(manager, provider="codex", project_chat=project_chat)
    update = make_update()

    await bot._cmd_model(update, SimpleNamespace(args=[]))
    assert update.message.replies[0][0] == "🤖 Select Codex model:"
    keyboard = update.message.replies[0][1]["reply_markup"].inline_keyboard
    assert [row[0].text for row in keyboard] == ["O3", "Codex Custom"]
    assert [row[0].callback_data for row in keyboard] == [
        "model:codex:o3",
        "model:codex:codex/custom",
    ]

    explicit = make_update()
    await bot._cmd_model(explicit, SimpleNamespace(args=["o3/custom:raw"]))
    session = await manager.get_session("7:9")
    assert session["provider"] == "codex"
    assert session["model"] == "o3/custom:raw"
    assert session["session_id"] is None
    assert session["new_session"] is True
    assert explicit.message.replies[0][0] == "✅ Switched to o3/custom:raw"


@pytest.mark.anyio
async def test_successful_codex_response_persists_provider_and_errors_do_not_overwrite(
    tmp_path: Path,
) -> None:
    manager = make_manager(tmp_path, "codex")
    bot = bare_bot(manager, provider="codex")

    await bot._save_session_id("7:9", ChatResponse("ok", session_id="codex-1"))
    await bot._save_session_id(
        "7:9", ChatResponse("failed", success=False, session_id="incompatible", error="failed")
    )

    session = await manager.get_session("7:9")
    assert session["provider"] == "codex"
    assert session["session_id"] == "codex-1"


@pytest.mark.anyio
async def test_codex_model_callback_is_provider_scoped_and_conversation_safe(
    tmp_path: Path,
) -> None:
    manager = make_manager(tmp_path, "codex")
    await manager.store.set(
        "7:9", {"provider": "codex", "session_id": "thread-9", "model": "old"}
    )
    await manager.store.set(
        "7:10", {"provider": "codex", "session_id": "thread-10", "model": "other"}
    )
    bot = bare_bot(manager, provider="codex")
    bot.application = SimpleNamespace(bot=object())
    query = SimpleNamespace(
        data="model:codex:o3/raw",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=9),
        message=None,
        callback_query=query,
    )

    await bot._handle_callback(update, SimpleNamespace())

    selected = await manager.get_session("7:9")
    other = await manager.get_session("7:10")
    assert selected["model"] == "o3/raw"
    assert selected["session_id"] == "thread-9"
    assert other["model"] == "other"
    query.edit_message_text.assert_awaited_once_with("✅ Model switched to: o3/raw")

    query.edit_message_text.reset_mock()
    query.data = "model:claude:opus"
    await bot._handle_callback(update, SimpleNamespace())
    assert "Provider mismatch" in query.edit_message_text.await_args.args[0]
    assert (await manager.get_session("7:9"))["model"] == "o3/raw"


@pytest.mark.anyio
async def test_successful_resume_selection_persists_compatible_provider(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, "claude")
    await manager.patch_session(
        "7:9", updates={"resume_list": [["claude-1", "summary", "claude"]]}
    )
    project_chat = SimpleNamespace(get_session_last_assistant_message=Mock(return_value=None))
    bot = bare_bot(manager, provider="claude", project_chat=project_chat)
    update = make_update(text="1")

    await bot._handle_text_message(update, SimpleNamespace(args=[]))

    session = await manager.get_session("7:9")
    assert session["provider"] == "claude"
    assert session["session_id"] == "claude-1"
    assert "resume_list" not in session


@pytest.mark.anyio
async def test_codex_resume_selection_never_reads_claude_progress_and_is_chat_scoped(
    tmp_path: Path,
) -> None:
    manager = make_manager(tmp_path, "codex")
    await manager.patch_session(
        "7:9", updates={"resume_list": [["codex-1", "summary", "codex"]]}
    )
    await manager.patch_session(
        "7:10", updates={"session_id": "codex-other", "provider": "codex"}
    )
    project_chat = SimpleNamespace(
        get_session_last_assistant_message=Mock(
            side_effect=AssertionError("must not read Claude transcript")
        )
    )
    bot = bare_bot(manager, provider="codex", project_chat=project_chat)
    update = make_update(text="1")

    await bot._handle_text_message(update, SimpleNamespace(args=[]))

    project_chat.get_session_last_assistant_message.assert_not_called()
    assert (await manager.get_session("7:9"))["session_id"] == "codex-1"
    assert (await manager.get_session("7:10"))["session_id"] == "codex-other"


def _effort_models():
    return (
        ModelInfo(
            "o3",
            "O3",
            default_reasoning_effort="medium",
            supported_reasoning_efforts=("low", "medium", "high"),
            is_default=True,
        ),
    )


def test_effort_command_handler_is_registered(tmp_path: Path) -> None:
    bot = bare_bot(make_manager(tmp_path, "codex"), provider="codex")
    bot.application = SimpleNamespace(add_handler=Mock())

    bot._setup_handlers()

    handlers = [call.args[0] for call in bot.application.add_handler.call_args_list]
    commands = {
        command
        for handler in handlers
        for command in getattr(handler, "commands", frozenset())
    }
    assert "effort" in commands


@pytest.mark.anyio
async def test_effort_command_is_published_in_bot_menu(tmp_path: Path) -> None:
    bot = bare_bot(make_manager(tmp_path, "codex"), provider="codex")
    set_my_commands = AsyncMock()
    bot.application = SimpleNamespace(bot=SimpleNamespace(set_my_commands=set_my_commands))

    await bot._set_bot_commands()

    assert set_my_commands.await_count == 3
    for call in set_my_commands.await_args_list:
        assert "effort" in {command.command for command in call.args[0]}


@pytest.mark.anyio
async def test_codex_effort_picker_uses_catalog_order_and_metadata(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, "codex")
    await manager.store.set(
        "7:9",
        {
            "provider": "codex",
            "session_id": "thread-9",
            "model": "o3",
            "effort": "high",
        },
    )
    project_chat = SimpleNamespace(list_runtime_models=AsyncMock(return_value=_effort_models()))
    bot = bare_bot(manager, provider="codex", project_chat=project_chat)
    update = make_update()

    await bot._cmd_effort(update, SimpleNamespace(args=[]))

    text, kwargs = update.message.replies[0]
    assert text == "🧠 Select reasoning effort for O3:\nCurrent: high · Model default: medium"
    keyboard = kwargs["reply_markup"].inline_keyboard
    assert [row[0].text for row in keyboard] == [
        "low",
        "medium (model default)",
        "high (current)",
        "Use model default",
    ]
    assert [row[0].callback_data for row in keyboard] == [
        "effort:codex:low",
        "effort:codex:medium",
        "effort:codex:high",
        "effort:codex:default",
    ]


@pytest.mark.anyio
async def test_codex_effort_argument_and_default_are_conversation_scoped(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, "codex")
    await manager.store.set(
        "7:9", {"provider": "codex", "session_id": "thread-9", "model": "o3"}
    )
    await manager.store.set(
        "7:10", {"provider": "codex", "session_id": "thread-10", "model": "o3"}
    )
    project_chat = SimpleNamespace(list_runtime_models=AsyncMock(return_value=_effort_models()))
    bot = bare_bot(manager, provider="codex", project_chat=project_chat)

    selected = make_update()
    await bot._cmd_effort(selected, SimpleNamespace(args=["high"]))
    assert (await manager.get_session("7:9"))["effort"] == "high"
    assert "effort" not in await manager.get_session("7:10")

    reset = make_update()
    await bot._cmd_effort(reset, SimpleNamespace(args=["default"]))
    session = await manager.get_session("7:9")
    assert "effort" not in session
    assert session["session_id"] == "thread-9"
    assert reset.message.replies[0][0] == (
        "✅ Reasoning effort reset to model default (medium) for O3"
    )


@pytest.mark.anyio
async def test_codex_effort_rejects_unknown_selected_model(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, "codex")
    await manager.store.set(
        "7:9",
        {"provider": "codex", "session_id": "thread-9", "model": "custom/raw"},
    )
    project_chat = SimpleNamespace(list_runtime_models=AsyncMock(return_value=_effort_models()))
    bot = bare_bot(manager, provider="codex", project_chat=project_chat)
    update = make_update()

    await bot._cmd_effort(update, SimpleNamespace(args=["high"]))

    assert "effort" not in await manager.get_session("7:9")
    assert update.message.replies[0][0] == (
        "📭 The selected Codex model does not advertise reasoning effort options."
    )

    await manager.patch_session("7:9", updates={"effort": "high"})
    reset = make_update()
    await bot._cmd_effort(reset, SimpleNamespace(args=["default"]))
    assert "effort" not in await manager.get_session("7:9")
    assert reset.message.replies[0][0] == "✅ Reasoning effort reset to provider default"


@pytest.mark.anyio
async def test_codex_model_change_resets_incompatible_effort(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, "codex")
    await manager.store.set(
        "7:9",
        {
            "provider": "codex",
            "session_id": "thread-9",
            "model": "o3",
            "effort": "high",
        },
    )
    models = (
        ModelInfo(
            "o3-mini",
            "O3 Mini",
            default_reasoning_effort="medium",
            supported_reasoning_efforts=("low", "medium"),
        ),
    )
    project_chat = SimpleNamespace(list_runtime_models=AsyncMock(return_value=models))
    bot = bare_bot(manager, provider="codex", project_chat=project_chat)
    update = make_update()

    await bot._cmd_model(update, SimpleNamespace(args=["o3-mini"]))

    session = await manager.get_session("7:9")
    assert session["model"] == "o3-mini"
    assert "effort" not in session
    assert update.message.replies[0][0] == (
        "✅ Switched to o3-mini\n"
        "ℹ️ Reasoning effort high is unsupported; reset to model default (medium)."
    )


@pytest.mark.anyio
async def test_codex_effort_callback_is_conversation_safe(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, "codex")
    await manager.store.set(
        "7:9", {"provider": "claude", "session_id": "claude-9", "model": "opus"}
    )
    await manager.store.set(
        "7:10", {"provider": "codex", "session_id": "thread-10", "model": "o3"}
    )
    project_chat = SimpleNamespace(list_runtime_models=AsyncMock(return_value=_effort_models()))
    bot = bare_bot(manager, provider="codex", project_chat=project_chat)
    bot.application = SimpleNamespace(bot=object())
    query = SimpleNamespace(
        data="effort:codex:high",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=9),
        message=None,
        callback_query=query,
    )

    await bot._handle_callback(update, SimpleNamespace())

    session = await manager.get_session("7:9")
    assert session["provider"] == "codex"
    assert session["session_id"] is None
    assert "model" not in session
    assert session["effort"] == "high"
    assert "effort" not in await manager.get_session("7:10")
    query.edit_message_text.assert_awaited_once_with(
        "✅ Reasoning effort set to high for O3"
    )


@pytest.mark.anyio
async def test_codex_effort_aligns_stale_provider_before_selection(tmp_path: Path) -> None:
    manager = make_manager(tmp_path, "codex")
    await manager.store.set(
        "7:9", {"provider": "claude", "session_id": "claude-9", "model": "opus"}
    )
    project_chat = SimpleNamespace(list_runtime_models=AsyncMock(return_value=_effort_models()))
    bot = bare_bot(manager, provider="codex", project_chat=project_chat)
    update = make_update()

    await bot._cmd_effort(update, SimpleNamespace(args=["high"]))

    session = await manager.get_session("7:9")
    assert session["provider"] == "codex"
    assert session["session_id"] is None
    assert "model" not in session
    assert session["effort"] == "high"
