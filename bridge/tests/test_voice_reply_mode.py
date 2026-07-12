# ruff: noqa: E402
# mypy: disable-error-code=attr-defined

import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


config_module = types.ModuleType("telegram_bot.utils.config")
config_module.config = SimpleNamespace(
    telegram_bot_token="test-token",
    allowed_user_ids=[],
    claude_settings_path=Path("/tmp/settings.json"),
    max_voice_duration=300,
    bot_data_dir=Path("/tmp/telegram-bot-data"),
    project_root=Path("/tmp"),
    transcription_provider="whisper",
    openai_api_key="test-key",
    openai_base_url=None,
    whisper_model="whisper-1",
    ffmpeg_path="ffmpeg",
    voice_reply_persona="Tingting",
    draft_update_min_chars=20,
    draft_update_interval=0.1,
    auto_new_session_after_hours=24.0,
)


session_module = types.ModuleType("telegram_bot.session.manager")


class _SessionManager:
    def __init__(self):
        self._sessions = {}

    async def get_session(self, user_id):
        return dict(self._sessions.get(user_id, {"reply_mode": "text"}))

    async def update_session(self, user_id, data):
        session = dict(self._sessions.get(user_id, {"reply_mode": "text"}))
        session.update(data)
        self._sessions[user_id] = session
        return None

    async def patch_session(self, user_id, *, updates=None, remove_fields=()):
        session = dict(self._sessions.get(user_id, {"reply_mode": "text"}))
        session.update(dict(updates or {}))
        for field in remove_fields:
            session.pop(field, None)
        self._sessions[user_id] = session

    async def patch_session_if(
        self, user_id, *, expected, updates=None, remove_fields=()
    ):
        session = dict(self._sessions.get(user_id, {"reply_mode": "text"}))
        if any(session.get(key) != value for key, value in expected.items()):
            return False
        await self.patch_session(
            user_id, updates=updates, remove_fields=remove_fields
        )
        return True

    async def get_pending_question(self, user_id):
        del user_id
        return None

    async def clear_pending_question(self, user_id):
        del user_id
        return None

    async def should_start_new_session(self, user_id, now=None):
        del now
        session = await self.get_session(user_id)
        return bool(session.get("force_auto_new_session"))

    async def set_last_user_message_at(self, user_id, at):
        await self.update_session(user_id, {"last_user_message_at": at.isoformat()})


session_module.session_manager = _SessionManager()


project_chat_module = types.ModuleType("telegram_bot.core.project_chat")


class _ChatResponse:
    def __init__(self, content="", session_id=None, has_options=False, streamed=False):
        self.content = content
        self.session_id = session_id
        self.has_options = has_options
        self.streamed = streamed


class _ProjectChatHandler:
    def __init__(self):
        self.responses = [_ChatResponse(content="ok")]

    async def process_message(self, **kwargs):
        del kwargs
        if self.responses:
            return self.responses.pop(0)
        return _ChatResponse(content="ok")

    async def stop(self, user_id):
        del user_id
        return False

    async def cancel_user_streaming(self, user_id):
        del user_id
        return False

    def list_sessions(self, limit=10):
        del limit
        return []

    def get_session_last_assistant_message(self, session_id):
        del session_id
        return None


_MISSING_MODULE = object()
_original_project_chat = sys.modules.get("telegram_bot.core.project_chat", _MISSING_MODULE)
_original_chat_logger = sys.modules.get("telegram_bot.utils.chat_logger", _MISSING_MODULE)

project_chat_module.project_chat_handler = _ProjectChatHandler()
project_chat_module.ChatResponse = _ChatResponse
project_chat_module.PROJECT_ROOT = Path("/tmp")
project_chat_module.CONVERSATIONS_DIR = Path("/tmp/conversations")
sys.modules["telegram_bot.core.project_chat"] = project_chat_module

chat_logger_module = types.ModuleType("telegram_bot.utils.chat_logger")
chat_logger_module.log_debug = lambda *args, **kwargs: None
sys.modules["telegram_bot.utils.chat_logger"] = chat_logger_module

_RUNTIME_MODULE_NAMES = (
    "telegram_bot.core.bot",
    "telegram_bot.core.bot_lifecycle",
    "telegram_bot.core.bot_status",
    "telegram_bot.core.bot_access",
    "telegram_bot.core.bot_commands",
    "telegram_bot.core.bot_delivery",
    "telegram_bot.core.bot_voice",
)
_original_runtime_modules = {
    name: sys.modules.get(name, _MISSING_MODULE) for name in _RUNTIME_MODULE_NAMES
}

try:
    from telegram_bot.utils.tts import VoicePersonaNotAvailableError
finally:
    if _original_project_chat is _MISSING_MODULE:
        sys.modules.pop("telegram_bot.core.project_chat", None)
    else:
        sys.modules["telegram_bot.core.project_chat"] = _original_project_chat
    if _original_chat_logger is _MISSING_MODULE:
        sys.modules.pop("telegram_bot.utils.chat_logger", None)
    else:
        sys.modules["telegram_bot.utils.chat_logger"] = _original_chat_logger
    for _module_name, _original_module in _original_runtime_modules.items():
        if _original_module is _MISSING_MODULE:
            sys.modules.pop(_module_name, None)
        else:
            assert isinstance(_original_module, types.ModuleType)
            sys.modules[_module_name] = _original_module

def _telegram_bot_class():
    chat_logger = sys.modules.get("telegram_bot.utils.chat_logger")
    if chat_logger is not None and not callable(getattr(chat_logger, "log_debug", None)):
        sys.modules.pop("telegram_bot.utils.chat_logger", None)
    from telegram_bot.core.bot import TelegramBot

    return TelegramBot


def _make_bot(**kwargs):
    return _telegram_bot_class()(**kwargs)


def _build_text_update(user_id: int, text: str):
    message = SimpleNamespace(
        text=text,
        message_id=1,
        date=datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc),
        chat=SimpleNamespace(send_action=AsyncMock(), id=1001),
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        message=message,
        callback_query=None,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=1001),
    )


class VoiceReplyModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        TelegramBot = _telegram_bot_class()

        session_module.session_manager._sessions.clear()
        # Voice delivery is gated behind macOS (`say`). These tests mock the actual
        # synthesis (`_send_voice_message`) and only exercise the platform-independent
        # orchestration logic, so force the macOS gate on regardless of host platform
        # to keep coverage on Linux CI runners.
        macos_patcher = patch.object(
            TelegramBot, "_is_macos", staticmethod(lambda: True)
        )
        macos_patcher.start()
        self.addCleanup(macos_patcher.stop)

    def test_voice_message_switches_to_voice_mode(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        mode = bot._resolve_next_reply_mode(
            current_mode="text",
            message_source="voice",
            user_text="任意内容",
        )
        self.assertEqual(mode, "voice")

    def test_text_message_switches_to_text_mode(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        mode = bot._resolve_next_reply_mode(
            current_mode="voice",
            message_source="text",
            user_text="任意内容",
        )
        self.assertEqual(mode, "text")

    def test_voice_message_keeps_voice_mode(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        mode = bot._resolve_next_reply_mode(
            current_mode="voice",
            message_source="voice",
            user_text="任意内容",
        )
        self.assertEqual(mode, "voice")

    def test_delivery_strategy_thresholds(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        self.assertEqual(bot._get_voice_delivery_strategy("短文本"), "voice_only")
        self.assertEqual(bot._get_voice_delivery_strategy("a" * 301), "voice_and_text")
        self.assertEqual(bot._get_voice_delivery_strategy("中" * 1001), "text_only")
        english_long = "word " * 1001
        self.assertEqual(bot._get_voice_delivery_strategy(english_long), "text_only")

    async def test_voice_mode_falls_back_to_text_when_synthesis_fails(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        message = _build_text_update(11, "hello").message
        bot._send_voice_message = AsyncMock(side_effect=RuntimeError("tts failed"))
        bot._reply_smart = AsyncMock()

        await bot._send_reply_by_mode(
            message=message,
            user_id=11,
            content="short reply",
            parse_mode="Markdown",
            force_options=False,
            streamed=False,
            reply_mode="voice",
        )
        bot._reply_smart.assert_awaited_once()

    async def test_voice_mode_returns_friendly_message_when_persona_missing(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        message = _build_text_update(11, "hello").message
        bot._send_voice_message = AsyncMock(
            side_effect=VoicePersonaNotAvailableError(
                persona="Yue (Premium)",
                available_voices=["Tingting", "Yue (Premium)", "Samantha"],
            )
        )
        bot._reply_smart = AsyncMock()

        await bot._send_reply_by_mode(
            message=message,
            user_id=11,
            content="short reply",
            parse_mode="Markdown",
            force_options=False,
            streamed=False,
            reply_mode="voice",
        )

        self.assertTrue(message.reply_text.await_count >= 1)
        first_message_text = message.reply_text.await_args_list[0].args[0]
        self.assertIn("VOICE_REPLY_PERSONA", first_message_text)
        self.assertIn("say -v ?", first_message_text)
        self.assertIn("第一列", first_message_text)
        bot._reply_smart.assert_awaited_once()

    async def test_text_reply_merges_voice_preview_into_single_message(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        message = _build_text_update(11, "hello").message
        bot._reply_smart = AsyncMock()

        await bot._send_reply_by_mode(
            message=message,
            user_id=11,
            content="final reply",
            parse_mode="Markdown",
            force_options=False,
            streamed=False,
            reply_mode="text",
            voice_input_preview="🎤 Voice: raw transcript",
        )

        bot._reply_smart.assert_awaited_once()
        merged_content = bot._reply_smart.await_args.args[1]
        self.assertEqual(merged_content, "🎤 Voice: raw transcript\n\nfinal reply")

    async def test_voice_only_reply_sends_preview_before_voice(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        message = _build_text_update(11, "hello").message
        send_order = []

        async def record_voice(*args, **kwargs):
            del args, kwargs
            send_order.append("voice")

        async def record_preview(*args, **kwargs):
            del args, kwargs
            send_order.append("preview")

        async def record_artifacts(*args, **kwargs):
            del args, kwargs
            send_order.append("artifacts")

        bot._send_voice_message = AsyncMock(side_effect=record_voice)
        bot._reply_smart = AsyncMock()
        message.reply_text = AsyncMock(side_effect=record_preview)
        bot._send_content_artifacts = AsyncMock(side_effect=record_artifacts)

        await bot._send_reply_by_mode(
            message=message,
            user_id=11,
            content="short reply",
            parse_mode="Markdown",
            force_options=False,
            streamed=False,
            reply_mode="voice",
            voice_input_preview="🎤 Voice: raw transcript",
        )

        self.assertEqual(send_order, ["preview", "voice", "artifacts"])
        bot._reply_smart.assert_not_awaited()
        message.reply_text.assert_awaited_once_with("🎤 Voice: raw transcript")

    async def test_voice_only_preview_not_duplicated_when_synthesis_fails(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        message = _build_text_update(11, "hello").message
        bot._send_voice_message = AsyncMock(side_effect=RuntimeError("tts failed"))
        bot._reply_smart = AsyncMock()

        await bot._send_reply_by_mode(
            message=message,
            user_id=11,
            content="short reply",
            parse_mode="Markdown",
            force_options=False,
            streamed=False,
            reply_mode="voice",
            voice_input_preview="🎤 Voice: raw transcript",
        )

        message.reply_text.assert_awaited_once_with("🎤 Voice: raw transcript")
        fallback_content = bot._reply_smart.await_args.args[1]
        self.assertEqual(fallback_content, "short reply")

    async def test_voice_mode_persists_after_long_text_fallback(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        bot.application = SimpleNamespace(bot=SimpleNamespace())
        bot._save_session_id = AsyncMock()
        bot._send_reply_by_mode = AsyncMock()

        project_chat_module.project_chat_handler.responses = [
            _ChatResponse(content="中" * 1001),
            _ChatResponse(content="short reply"),
        ]
        await session_module.session_manager.update_session(22, {"reply_mode": "voice"})

        update_1 = _build_text_update(22, "第一个语音请求")
        update_2 = _build_text_update(22, "第二个语音请求")

        await bot._process_user_message_text(
            update_1, 22, "第一个语音请求", message_source="voice"
        )
        await bot._process_user_message_text(
            update_2, 22, "第二个语音请求", message_source="voice"
        )

        first_call = bot._send_reply_by_mode.await_args_list[0].kwargs
        second_call = bot._send_reply_by_mode.await_args_list[1].kwargs
        self.assertEqual(first_call["reply_mode"], "voice")
        self.assertEqual(second_call["reply_mode"], "voice")
        session = await session_module.session_manager.get_session(22)
        self.assertEqual(session.get("reply_mode"), "voice")

    async def test_voice_mode_disables_streaming_text_forwarding(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        app_bot = SimpleNamespace()
        bot.application = SimpleNamespace(bot=app_bot)
        bot._save_session_id = AsyncMock()
        bot._send_reply_by_mode = AsyncMock()
        process_mock = AsyncMock(return_value=_ChatResponse(content="short reply"))
        project_chat_module.project_chat_handler.process_message = process_mock
        await session_module.session_manager.update_session(33, {"reply_mode": "voice"})

        update = _build_text_update(33, "来自语音转写")
        await bot._process_user_message_text(
            update, 33, "来自语音转写", message_source="voice"
        )

        kwargs = process_mock.await_args.kwargs
        self.assertIsNone(kwargs["bot"])

    async def test_expired_text_message_starts_new_session_automatically(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        bot.application = SimpleNamespace(bot=SimpleNamespace())
        bot._save_session_id = AsyncMock()
        bot._send_reply_by_mode = AsyncMock()
        process_mock = AsyncMock(return_value=_ChatResponse(content="ok"))
        project_chat_module.project_chat_handler.process_message = process_mock
        await session_module.session_manager.update_session(
            44,
            {
                "reply_mode": "text",
                "session_id": "existing-session",
                "force_auto_new_session": True,
            },
        )
        bot._runtime_active_sessions.add(44)

        update = _build_text_update(44, "隔天继续聊")
        await bot._process_user_message_text(update, 44, "隔天继续聊")

        kwargs = process_mock.await_args.kwargs
        self.assertTrue(kwargs["new_session"])
        self.assertIsNone(kwargs["session_id"])
        session = await session_module.session_manager.get_session(bot._conversation_key(44, 1001))
        self.assertIn("last_user_message_at", session)

    async def test_expired_voice_message_starts_new_session_automatically(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        bot.application = SimpleNamespace(bot=SimpleNamespace())
        bot._save_session_id = AsyncMock()
        bot._send_reply_by_mode = AsyncMock()
        process_mock = AsyncMock(return_value=_ChatResponse(content="ok"))
        project_chat_module.project_chat_handler.process_message = process_mock
        await session_module.session_manager.update_session(
            55,
            {
                "reply_mode": "voice",
                "session_id": "voice-session",
                "force_auto_new_session": True,
            },
        )
        bot._runtime_active_sessions.add(55)

        update = _build_text_update(55, "来自语音")
        await bot._process_user_message_text(
            update, 55, "来自语音", message_source="voice"
        )

        kwargs = process_mock.await_args.kwargs
        self.assertTrue(kwargs["new_session"])
        self.assertIsNone(kwargs["session_id"])


class OptionButtonsTests(unittest.IsolatedAsyncioTestCase):
    _CONTENT = "어떤 걸 원하세요?\n\n1. 첫째\n2. 둘째"

    async def test_option_buttons_suppressed_by_default(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        bot._send_file_paths = AsyncMock()
        message = SimpleNamespace(chat=SimpleNamespace(id=1), reply_text=AsyncMock())

        # Stub config has no enable_option_buttons -> default off.
        await bot._send_content_artifacts(message, self._CONTENT, force_options=True)

        message.reply_text.assert_not_called()  # numbered options stay as text

    async def test_option_buttons_shown_when_enabled(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        bot._send_file_paths = AsyncMock()
        message = SimpleNamespace(chat=SimpleNamespace(id=1), reply_text=AsyncMock())

        config_module.config.enable_option_buttons = True
        try:
            await bot._send_content_artifacts(
                message, self._CONTENT, force_options=True
            )
        finally:
            config_module.config.enable_option_buttons = False

        message.reply_text.assert_called_once()
        self.assertIn("reply_markup", message.reply_text.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
