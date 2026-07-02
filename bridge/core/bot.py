# ruff: noqa: E402
import asyncio
import logging
import re
import sys
import types
from pathlib import Path as FilePath
from typing import Any, Dict, Iterable, List, Optional
from datetime import datetime, timezone

import telegram.error
from telegram import (
    Update,
    Message,
    User,
    Chat,
    CallbackQuery,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.request import HTTPXRequest  # noqa: F401 - compatibility for tests/patches
from telegram_bot.utils.config import config
from telegram_bot.session.manager import session_manager
from telegram_bot.core.push_notifier import PushNotifier
from telegram_bot.core import paths as path_scope
from telegram_bot.core.task_queue import UserTaskQueue
from telegram_bot.core.project_chat import (
    project_chat_handler,
    ChatResponse,
)
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny
from telegram_bot.utils.audio_processor import AudioProcessor
from telegram_bot.utils.transcription import (
    VolcengineFileFastTranscriber,
    WhisperTranscriber,
)
from telegram_bot.utils.tts import MacOSTtsSynthesizer
from telegram_bot.utils.tos_uploader import VolcengineTOSUploader

logger = logging.getLogger(__name__)
STALE_MESSAGE_SECONDS = 20 * 60  # 20 minutes


from telegram_bot.core.bot_shared import _PollingRestart, enforce_access_control  # noqa: F401
from telegram_bot.core import bot_lifecycle as _bot_lifecycle_module
from telegram_bot.core.bot_lifecycle import BotLifecycleMixin
from telegram_bot.core import bot_commands as _bot_commands_module
from telegram_bot.core.bot_commands import BotCommandMixin
from telegram_bot.core import bot_delivery as _bot_delivery_module
from telegram_bot.core.bot_delivery import BotDeliveryMixin
from telegram_bot.core import bot_voice as _bot_voice_module
from telegram_bot.core.bot_voice import BotVoiceMixin


_EXTRACTED_MODULES = (
    _bot_lifecycle_module,
    _bot_commands_module,
    _bot_delivery_module,
    _bot_voice_module,
)


def _sync_extracted_modules() -> None:
    """Keep extracted mixin modules aligned with monkeypatched bot globals."""
    for module in _EXTRACTED_MODULES:
        module.config = config
        module.project_chat_handler = project_chat_handler
        module.session_manager = session_manager
        module.Application = Application
        module.HTTPXRequest = HTTPXRequest
        module.enforce_access_control = enforce_access_control


class _BotModule(types.ModuleType):
    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name in {
            "config",
            "project_chat_handler",
            "session_manager",
            "Application",
            "HTTPXRequest",
            "enforce_access_control",
        }:
            _sync_extracted_modules()


_sync_extracted_modules()
sys.modules[__name__].__class__ = _BotModule


class TelegramBot(BotLifecycleMixin, BotCommandMixin, BotDeliveryMixin, BotVoiceMixin):

    def __init__(self):
        self._config = config
        self.application: Optional[Application] = None
        # ccc-node owner-only push notifier (disabled unless config.push_enabled).
        self._push_notifier = PushNotifier()
        # Only sessions created/resumed in current runtime are auto-resumed.
        self._runtime_active_sessions: set[Any] = set()
        self._user_voice_tasks: Dict[Any, set[asyncio.Task]] = {}
        # Per-user bounded run queue + active-task tracking (priority stop/revert).
        self._tasks = UserTaskQueue(self._MAX_INFLIGHT_MESSAGES)
        self._audio_dir = config.bot_data_dir / "audio"
        self._image_dir = config.bot_data_dir / "images"
        self._audio_processor = AudioProcessor(ffmpeg_path=config.ffmpeg_path)
        self._whisper_transcriber: Optional[WhisperTranscriber] = None
        self._volcengine_transcriber: Optional[VolcengineFileFastTranscriber] = None
        self._volcengine_tos_uploader: Optional[VolcengineTOSUploader] = None
        self._tts_synthesizer: Optional[MacOSTtsSynthesizer] = None

    def _make_status_callback(self, bot: Any, chat_id: int):
        """Build a fail-open send/edit/delete callback for task heartbeat messages."""

        async def status_callback(text: Optional[str], message_id: Optional[int] = None) -> Optional[int]:
            try:
                if text is None:
                    if message_id is not None and getattr(config, "heartbeat_delete_on_done", True):
                        await bot.delete_message(chat_id=chat_id, message_id=message_id)
                    return None
                if message_id is not None:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=text,
                        )
                    except telegram.error.BadRequest as exc:
                        if "message is not modified" not in str(exc).lower():
                            raise
                    return message_id
                sent = await bot.send_message(chat_id=chat_id, text=text)
                value = getattr(sent, "message_id", None)
                return value if isinstance(value, int) else None
            except Exception as exc:
                logger.warning("Heartbeat status callback failed: %s", type(exc).__name__)
                return message_id

        return status_callback

    # Available models for /model command (aliases, CLI resolves via env vars)
    MODELS = [
        ("sonnet", "Claude Sonnet"),
        ("opus", "Claude Opus"),
        ("haiku", "Claude Haiku"),
    ]
    _ALLOW_OUTSIDE_ONCE_TOKEN = "ALLOW_OUTSIDE_ONCE"
    _DENY_OUTSIDE_TOKEN = "DENY_OUTSIDE"
    _MAX_INFLIGHT_MESSAGES = 3
    _STALE_AUDIO_SECONDS = 24 * 60 * 60
    _WATCHDOG_INTERVAL = 60
    _NETWORK_FAILURE_THRESHOLD = 300  # 5 min of consecutive failures → force exit


    def _check_user_access(self, user_id: int) -> bool:
        """Check if user has permission to use the bot"""
        if not config.allowed_user_ids:
            return True  # Allow all users if not configured
        return user_id in config.allowed_user_ids

    async def _check_access(self, update: Update) -> bool:
        """Check if user has permission to use this bot

        Returns:
            bool: True if user has permission, False otherwise
        """
        # Drop stale messages (> 20 min old)
        msg = update.message or update.callback_query and update.callback_query.message
        if msg and msg.date:
            age = (datetime.now(timezone.utc) - msg.date).total_seconds()
            if age > STALE_MESSAGE_SECONDS:
                logger.debug(
                    f"Dropping stale message ({age:.0f}s old) from {update.effective_user}"
                )
                return False

        user = update.effective_user
        if not user:
            return False

        # Check if user is in the allowed list
        if not self._check_user_access(user.id):
            # Send different rejection messages based on update type
            if update.message:
                if update.message.voice:
                    await update.message.reply_text(
                        "⛔ You don't have permission to send voice messages to this bot.\n"
                        "Please contact the admin for access."
                    )
                else:
                    await update.message.reply_text(
                        "⛔ Sorry, you don't have permission to use this bot.\n"
                        "Please contact the admin for access."
                    )
            elif update.callback_query:
                await update.callback_query.answer(
                    "⛔ No permission to use this feature", show_alert=True
                )
            return False
        return True

    @staticmethod
    def _is_priority_command(text: str) -> bool:
        """Check if a command should be processed with priority (bypass queue).

        Priority commands are processed immediately without queue limit checks.
        Currently /stop and /revert are priority commands.
        """
        return text.strip() in ("/stop", "/revert")

    @staticmethod
    def _project_root() -> FilePath:
        from telegram_bot.core.project_chat import PROJECT_ROOT

        return PROJECT_ROOT

    @staticmethod
    def _is_within_project_root(path: FilePath) -> bool:
        return path_scope.is_within_project_root(path, TelegramBot._project_root())

    @staticmethod
    def _resolve_candidate_path(raw_path: str) -> FilePath:
        return path_scope.resolve_candidate_path(raw_path, TelegramBot._project_root())

    @staticmethod
    def _iter_strings(value: Any) -> Iterable[str]:
        return path_scope.iter_strings(value)

    @staticmethod
    def _extract_paths_from_command(command: str) -> List[str]:
        return path_scope.extract_paths_from_command(command)

    def _extract_path_candidates(self, tool_name: str, tool_input: Any) -> List[str]:
        return path_scope.extract_path_candidates(tool_name, tool_input)

    def _extract_outside_paths(self, tool_name: str, tool_input: Any) -> List[str]:
        return path_scope.extract_outside_paths(
            tool_name, tool_input, project_root=self._project_root()
        )

    async def _consume_outside_approval_once(self, user_id: int, chat_id: Optional[int] = None) -> bool:
        session_key = self._conversation_key(user_id, chat_id)
        session = await session_manager.get_session(session_key)
        if not session.get("outside_path_approved_once"):
            return False
        session["outside_path_approved_once"] = False
        session.pop("pending_outside_paths", None)
        await session_manager.update_session(session_key, session)
        return True

    async def _maybe_capture_outside_approval(self, user_id: int, text: str, chat_id: Optional[int] = None) -> None:
        session_key = self._conversation_key(user_id, chat_id)
        session = await session_manager.get_session(session_key)
        pending = session.get("pending_outside_paths")
        if not pending:
            return

        normalized = text.strip().lower()
        allow = self._ALLOW_OUTSIDE_ONCE_TOKEN.lower() in normalized or normalized in {
            "1",
            "allow",
            "yes",
            "y",
        }
        deny = self._DENY_OUTSIDE_TOKEN.lower() in normalized or normalized in {
            "2",
            "deny",
            "no",
            "n",
        }

        if allow:
            session["outside_path_approved_once"] = True
            session.pop("pending_outside_paths", None)
            await session_manager.update_session(session_key, session)
        elif deny:
            session["outside_path_approved_once"] = False
            session.pop("pending_outside_paths", None)
            await session_manager.update_session(session_key, session)

    async def _permission_callback(
        self, chat_id: int, user_id: int, tool_name: str, tool_input: Any
    ):
        """Handle tool permission requests.

        All interactive requests are denied so Claude falls back to numbered
        text options, which the bot detects and presents as inline keyboard buttons.
        """
        if tool_name == "AskUserQuestion":
            return PermissionResultDeny(
                message=(
                    "AskUserQuestion is not available in this environment. "
                    "IMPORTANT: Do NOT mention this to the user. Do NOT say the tool is unavailable or that user didn't select. "
                    "Instead, you MUST output the question and options in this EXACT format:\n\n"
                    "[Question text here]\n\n"
                    "1. [First option]\n"
                    "2. [Second option]\n"
                    "3. [Third option]\n\n"
                    "Example:\n"
                    "确认发布内容：\n"
                    "[show the content here with absolute file paths like /Users/.../image.png]\n\n"
                    "1. 去发布\n"
                    "2. 重新生成\n"
                    "3. 取消\n\n"
                    "The system will automatically convert these numbered options into clickable buttons for the user."
                )
            )

        outside_paths = self._extract_outside_paths(tool_name, tool_input)
        if outside_paths:
            session_key = self._conversation_key(user_id, chat_id)
            if await self._consume_outside_approval_once(user_id, chat_id):
                return PermissionResultAllow()

            session = await session_manager.get_session(session_key)
            session["pending_outside_paths"] = outside_paths[:5]
            await session_manager.update_session(session_key, session)

            preview = "\n".join(f"- {path}" for path in outside_paths[:5])
            return PermissionResultDeny(
                message=(
                    "Detected access to paths outside PROJECT_ROOT. Requires confirmation before proceeding.\n"
                    f"{preview}\n"
                    "Please output the following two options to the user and wait for a reply:\n"
                    f"1. {self._ALLOW_OUTSIDE_ONCE_TOKEN} (Allow this external path access)\n"
                    f"2. {self._DENY_OUTSIDE_TOKEN} (Deny)"
                )
            )

        return PermissionResultAllow()

    @staticmethod
    def _conversation_key(user_id: int, chat_id: Optional[int] = None) -> Any:
        """Storage/queue key for one Telegram conversation.

        Private chats and groups can contain the same Telegram user. Session and
        queue state must therefore include chat_id; otherwise answers/session IDs
        can bleed between DM and group conversations.
        """
        if chat_id is None or chat_id == user_id:
            return user_id
        return f"{user_id}:{chat_id}"

    async def _save_session_id(self, session_key: Any, response: ChatResponse):
        if response.session_id:
            session = await session_manager.get_session(session_key)
            session["session_id"] = response.session_id
            await session_manager.update_session(session_key, session)
            self._runtime_active_sessions.add(session_key)

    def _effective_session_id(self, session_key: Any, session: dict) -> Optional[str]:
        """Prevent cross-process auto-resume from persisted session data."""
        session_id = session.get("session_id")
        if not session_id:
            return None
        if session_key not in self._runtime_active_sessions:
            logger.info(
                f"Ignoring persisted session_id for conversation {session_key} (not active in current runtime)"
            )
            return None
        return session_id

    @staticmethod
    def _message_timestamp_utc(message: Message) -> datetime:
        message_date = getattr(message, "date", None)
        if message_date is None:
            return datetime.now(timezone.utc)
        if message_date.tzinfo is None:
            return message_date.replace(tzinfo=timezone.utc)
        return message_date.astimezone(timezone.utc)

    def _setup_handlers(self):
        # Command handlers
        self.application.add_handler(CommandHandler("start", self._cmd_start))
        self.application.add_handler(CommandHandler("skills", self._cmd_skills))
        self.application.add_handler(CommandHandler("new", self._cmd_new))
        self.application.add_handler(CommandHandler("model", self._cmd_model))
        self.application.add_handler(CommandHandler("resume", self._cmd_resume))
        self.application.add_handler(CommandHandler("stop", self._cmd_stop))
        self.application.add_handler(CommandHandler("history", self._cmd_history))
        self.application.add_handler(CommandHandler("revert", self._cmd_revert))
        self.application.add_handler(CommandHandler("command", self._cmd_command))
        self.application.add_handler(CommandHandler("skill", self._cmd_skill))

        # Skill command handler - catches all /commands
        self.application.add_handler(
            MessageHandler(filters.COMMAND, self._handle_skill_command), group=1
        )

        # Text/message handlers - for answers to questions
        self.application.add_handler(
            MessageHandler(filters.VOICE, self._handle_voice_message), group=2
        )
        self.application.add_handler(
            MessageHandler(
                filters.PHOTO | filters.Document.IMAGE,
                self._handle_photo_message,
            ),
            group=2,
        )
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text_message),
            group=2,
        )

        # Callback query handler - for inline keyboards
        self.application.add_handler(CallbackQueryHandler(self._handle_callback))

    @staticmethod
    def _require_user(update: Update) -> User:
        user = update.effective_user
        if user is None:
            raise RuntimeError("Telegram update is missing effective_user.")
        return user

    @staticmethod
    def _require_message(update: Update) -> Message:
        message = update.message
        if message is None:
            raise RuntimeError("Telegram update is missing message.")
        return message

    @staticmethod
    def _require_chat(update: Update) -> Chat:
        chat = update.effective_chat
        if chat is None:
            raise RuntimeError("Telegram update is missing effective_chat.")
        return chat

    @staticmethod
    def _require_callback_query(update: Update) -> CallbackQuery:
        query = update.callback_query
        if query is None:
            raise RuntimeError("Telegram update is missing callback_query.")
        return query

    def _require_application(self) -> Application:
        app = self.application
        if app is None:
            raise RuntimeError("Telegram application is not initialized.")
        return app


























    async def _process_user_message_text(
        self,
        update: Update,
        user_id: int,
        text: str,
        message_source: str = "text",
        voice_input_preview: Optional[str] = None,
    ) -> None:
        message = self._require_message(update)
        chat = self._require_chat(update)
        app = self._require_application()
        conversation_key = self._conversation_key(user_id, chat.id)
        current_session = await session_manager.get_session(conversation_key)
        if conversation_key != user_id:
            # One-time compatibility migration: older bridge versions stored all
            # Telegram chat state under bare user_id. If the scoped session only
            # has defaults, seed it from legacy state, then keep future updates
            # chat-scoped.
            legacy_session = await session_manager.get_session(user_id)
            scoped_is_default = set(current_session.keys()).issubset({"reply_mode"})
            if legacy_session and scoped_is_default and legacy_session != current_session:
                current_session = dict(legacy_session)
                await session_manager.update_session(conversation_key, current_session)
                if user_id in self._runtime_active_sessions:
                    self._runtime_active_sessions.add(conversation_key)
        current_reply_mode = self._normalize_reply_mode(
            current_session.get("reply_mode")
        )
        message_timestamp = self._message_timestamp_utc(message)
        next_reply_mode = self._resolve_next_reply_mode(
            current_mode=current_reply_mode,
            message_source=message_source,
            user_text=text,
        )
        if current_reply_mode != next_reply_mode:
            current_session["reply_mode"] = next_reply_mode
            await session_manager.update_session(
                conversation_key, {"reply_mode": next_reply_mode}
            )
        else:
            current_session["reply_mode"] = current_reply_mode
        try:
            await message.chat.send_action(action="typing")
        except Exception:
            pass

        try:
            new_session = current_session.pop("new_session", False)
            auto_new_session = await session_manager.should_start_new_session(
                conversation_key, now=message_timestamp
            )
            if auto_new_session:
                current_session["session_id"] = None
                self._runtime_active_sessions.discard(conversation_key)
                new_session = True
            if new_session:
                await session_manager.update_session(conversation_key, current_session)

            await session_manager.set_last_user_message_at(conversation_key, message_timestamp)

            enable_streaming_text = next_reply_mode != "voice"
            response = await project_chat_handler.process_message(
                user_message=text,
                user_id=user_id,
                chat_id=chat.id,
                message_id=message.message_id,
                session_id=self._effective_session_id(conversation_key, current_session),
                model=current_session.get("model"),
                new_session=new_session,
                permission_callback=self._permission_callback,
                typing_callback=lambda: message.chat.send_action(action="typing"),
                status_callback=self._make_status_callback(app.bot, chat.id),
                bot=app.bot if enable_streaming_text else None,
            )
            await self._save_session_id(conversation_key, response)
            await self._send_reply_by_mode(
                message=message,
                user_id=user_id,
                content=response.content,
                parse_mode="Markdown",
                force_options=response.has_options,
                streamed=response.streamed,
                reply_mode=next_reply_mode,
                voice_input_preview=voice_input_preview,
            )
        except asyncio.CancelledError:
            # Task was cancelled by /stop command - silently exit
            # The /stop handler will send the user response
            logger.debug(f"Message processing cancelled for user {user_id}")
            raise
        except Exception as e:
            logger.error(f"Error in project chat: {e}", exc_info=True)
            await message.reply_text(
                "❌ Sorry, an error occurred while processing your message.\n"
                f"Error: {str(e)}\n\n"
                "Please try again later."
            )


    # Match both absolute (/foo/bar.png) and relative (foo/bar.png) file paths
    _FILE_PATH_RE = re.compile(
        r"(/?(?:[\w.@-]+/)+[\w.@-]+\.(?:png|jpg|jpeg|gif|webp|mp4|mp3|pdf|zip))",
        re.IGNORECASE,
    )
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

















bot = TelegramBot()
