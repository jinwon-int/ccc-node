# ruff: noqa: E402
# mypy: disable-error-code=attr-defined

import asyncio
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sys_modules_isolation import ModuleFakesGuard

_sys_modules_guard = ModuleFakesGuard(__name__).begin()


config_module = types.ModuleType("telegram_bot.utils.config")
config_module.config = SimpleNamespace(
    telegram_bot_token="test-token",
    allowed_user_ids=[],
    claude_settings_path=Path("/tmp/settings.json"),
    max_voice_duration=300,
    bot_data_dir=Path("/tmp/telegram-bot-data"),
    transcription_provider="whisper",
    openai_api_key="test-key",
    openai_base_url=None,
    whisper_model="whisper-1",
    ffmpeg_path="ffmpeg",
    volcengine_app_id="test-app-id",
    volcengine_token="test-token",
    volcengine_access_key="test-ak",
    volcengine_secret_access_key="test-sk",
    volcengine_tos_bucket_name="voice-stage",
    volcengine_tos_endpoint="https://tos-cn-shanghai.volces.com",
    volcengine_tos_region="cn-shanghai",
    volcengine_tos_signed_url_ttl_seconds=900,
    volcengine_cluster="volcengine_streaming_common",
    volcengine_resource_id="volc.bigasr.auc",
    volcengine_model_name="bigmodel",
    volcengine_submit_endpoint="https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit",
    volcengine_query_endpoint="https://openspeech.bytedance.com/api/v3/auc/bigmodel/query",
    volcengine_timeout_seconds=20.0,
    volcengine_max_retries=3,
    volcengine_initial_backoff=1.0,
    volcengine_poll_interval_seconds=2.0,
    volcengine_max_poll_seconds=300.0,
    draft_update_min_chars=20,
    draft_update_interval=0.1,
)


session_module = types.ModuleType("telegram_bot.session.manager")


class _SessionManager:
    async def get_session(self, user_id):
        del user_id
        return {}

    async def update_session(self, user_id, data):
        del user_id, data
        return None

    async def get_pending_question(self, user_id):
        del user_id
        return None

    async def clear_pending_question(self, user_id):
        del user_id
        return None

    async def clear_approve_all(self, user_id):
        del user_id
        return None


session_module.session_manager = _SessionManager()


project_chat_module = types.ModuleType("telegram_bot.core.project_chat")


class _ChatResponse:
    def __init__(self, content="", session_id=None, has_options=False, streamed=False):
        self.content = content
        self.session_id = session_id
        self.has_options = has_options
        self.streamed = streamed


class _ProjectChatHandler:
    async def process_message(self, **kwargs):
        del kwargs
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

# The manual restore above covers the modules this file knows it replaced; the
# guard additionally reverts anything transitively imported under the fakes.
_sys_modules_guard.finish()


def _telegram_bot_class():
    chat_logger = sys.modules.get("telegram_bot.utils.chat_logger")
    if chat_logger is not None and not callable(getattr(chat_logger, "log_debug", None)):
        sys.modules.pop("telegram_bot.utils.chat_logger", None)
    from telegram_bot.core.bot import TelegramBot

    return TelegramBot


def _make_bot(**kwargs):
    return _telegram_bot_class()(**kwargs)


class VoiceHandlerHelperTests(unittest.IsolatedAsyncioTestCase):
    def test_resolve_voice_extension(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        self.assertEqual(bot._resolve_voice_extension("audio/ogg"), "ogg")
        self.assertEqual(bot._resolve_voice_extension("audio/amr"), "amr")
        self.assertEqual(bot._resolve_voice_extension(None), "ogg")

    def test_build_voice_file_name(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        name = bot._build_voice_file_name(user_id=42, extension="ogg")
        self.assertTrue(name.startswith("42_"))
        self.assertTrue(name.endswith(".ogg"))

    async def test_cancel_user_voice_tasks(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )

        async def sleeper():
            await asyncio.sleep(60)

        task = asyncio.create_task(sleeper())
        bot._track_voice_task(99, task)

        cancelled = await bot._cancel_user_voice_tasks(99)
        self.assertEqual(cancelled, 1)
        self.assertTrue(task.cancelled())

    async def test_cleanup_stale_audio_files(self):
        with TemporaryDirectory() as td:
            audio_dir = Path(td)
            stale = audio_dir / "stale.ogg"
            fresh = audio_dir / "fresh.ogg"
            stale.write_bytes(b"OggS")
            fresh.write_bytes(b"OggS")
            stale.touch()
            fresh.touch()

            bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
            removed = await bot._cleanup_stale_audio_files(audio_dir, max_age_seconds=0)
            self.assertGreaterEqual(removed, 1)

    async def test_build_telegram_file_url_supports_relative_path(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        bot.application = SimpleNamespace(
            bot=SimpleNamespace(
                get_file=AsyncMock(
                    return_value=SimpleNamespace(file_path="voice/file_10.oga")
                )
            )
        )

        url = await bot._build_telegram_file_url("voice-file-id")
        self.assertEqual(
            url,
            "https://api.telegram.org/file/bottest-token/voice/file_10.oga",
        )

    async def test_build_telegram_file_url_supports_absolute_url(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        absolute = "https://api.telegram.org/file/bot123456:ABC/voice/file_10.oga"
        bot.application = SimpleNamespace(
            bot=SimpleNamespace(
                get_file=AsyncMock(return_value=SimpleNamespace(file_path=absolute))
            )
        )

        url = await bot._build_telegram_file_url("voice-file-id")
        self.assertEqual(url, absolute)

    def test_redact_telegram_file_url_masks_all_bot_tokens(self):
        bot = _make_bot(
            settings=config_module.config,
            session_manager=session_module.session_manager,
            project_chat=project_chat_module.project_chat_handler,
        )
        source = "https://api.telegram.org/file/botA/https://api.telegram.org/file/botB/voice/file_10.oga"
        redacted = bot._redact_telegram_file_url(source)
        self.assertEqual(
            redacted,
            "https://api.telegram.org/file/bot***REDACTED***/https://api.telegram.org/file/bot***REDACTED***/voice/file_10.oga",
        )


if __name__ == "__main__":
    unittest.main()
