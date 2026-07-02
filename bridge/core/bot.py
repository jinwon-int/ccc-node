# ruff: noqa: E402
import asyncio
import json
import logging
import os
import re
import signal
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path as FilePath
from typing import Any, Dict, Iterable, List, Optional, Tuple
from datetime import datetime, timezone

import telegram.error
from telegram import (
    Update,
    Message,
    User,
    Chat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllChatAdministrators,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.request import BaseRequest, HTTPXRequest
from telegram_bot.utils.config import config
from telegram_bot.session.manager import session_manager
from telegram_bot.core.push_notifier import PushNotifier
from telegram_bot.core.session_isolation import apply_subprocess_session_isolation
from telegram_bot.core import ui
from telegram_bot.core import paths as path_scope
from telegram_bot.core.task_queue import UserTaskQueue
from telegram_bot.core.project_chat import (
    project_chat_handler,
    ChatResponse,
)
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny
from telegram_bot.utils.chat_logger import log_debug
from telegram_bot.utils.tg_format import wrap_markdown_tables
from telegram_bot.utils.tg_robust import send_with_retry
from telegram_bot.utils import tg_md
from telegram_bot.utils import tg_readable
from telegram_bot.utils import tg_entities
from telegram_bot.utils.audio_processor import AudioProcessor
from telegram_bot.utils.transcription import (
    VolcengineFileFastTranscriber,
    WhisperTranscriber,
)
from telegram_bot.utils.tts import MacOSTtsSynthesizer
from telegram_bot.utils.tos_uploader import VolcengineTOSUploader
from telegram_bot.utils.health import health_reporter

logger = logging.getLogger(__name__)
STALE_MESSAGE_SECONDS = 20 * 60  # 20 minutes


from telegram_bot.core.bot_shared import (
    _PollingRestart,
    enforce_access_control,
)
from telegram_bot.core import bot_commands as _bot_commands_module
from telegram_bot.core.bot_commands import BotCommandMixin
from telegram_bot.core import bot_voice as _bot_voice_module
from telegram_bot.core.bot_voice import BotVoiceMixin


_EXTRACTED_MODULES = (_bot_commands_module, _bot_voice_module)


def _sync_extracted_modules() -> None:
    """Keep extracted mixin modules aligned with monkeypatched bot globals."""
    for module in _EXTRACTED_MODULES:
        module.config = config
        module.project_chat_handler = project_chat_handler
        module.session_manager = session_manager


class _BotModule(types.ModuleType):
    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name in {"config", "project_chat_handler", "session_manager"}:
            _sync_extracted_modules()


_sync_extracted_modules()
sys.modules[__name__].__class__ = _BotModule


class TelegramBot(BotCommandMixin, BotVoiceMixin):

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

    async def _on_ready(self, application: Application):
        """Called after application.initialize() — sets up commands and cleanup."""
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._image_dir.mkdir(parents=True, exist_ok=True)
        removed = await self._cleanup_stale_audio_files(
            self._audio_dir, max_age_seconds=self._STALE_AUDIO_SECONDS
        )
        removed_images = await self._cleanup_stale_audio_files(
            self._image_dir, max_age_seconds=self._STALE_AUDIO_SECONDS
        )
        if removed:
            logger.info("Startup audio cleanup removed %s stale file(s)", removed)
        if removed_images:
            logger.info("Startup image cleanup removed %s stale file(s)", removed_images)
        await self._set_bot_commands()
        logger.info("Bot initialization complete")

    def build(self):
        """Build the application (no post_init — lifecycle managed manually)."""
        self.application = (
            Application.builder()
            .token(config.telegram_bot_token)
            .concurrent_updates(True)
            .get_updates_request(self._build_get_updates_request())
            .request(self._build_default_request())
            .build()
        )
        self._setup_handlers()
        self.application.add_error_handler(self._error_handler)

    def _build_default_request(self) -> BaseRequest:
        """Build default request for all non-getUpdates API calls."""
        proxy_url = (
            os.environ.get("PROXY_URL")
            or os.environ.get("https_proxy")
            or os.environ.get("http_proxy")
        )
        return HTTPXRequest(
            connection_pool_size=8,
            pool_timeout=3.0,
            read_timeout=10.0,
            write_timeout=10.0,
            connect_timeout=5.0,
            proxy=proxy_url,
            http_version="1.1",
        )

    def _build_get_updates_request(self) -> BaseRequest:
        """Build dedicated request for getUpdates polling."""
        proxy_url = (
            os.environ.get("PROXY_URL")
            or os.environ.get("https_proxy")
            or os.environ.get("http_proxy")
        )
        return HTTPXRequest(
            connection_pool_size=4,  # Increased from 2 to handle long polling
            pool_timeout=5.0,
            read_timeout=35.0,
            write_timeout=10.0,
            connect_timeout=5.0,
            proxy=proxy_url,
            http_version="1.1",
        )

    _MIN_UPTIME = 30  # seconds — polling exits faster → count as crash
    _MAX_RAPID_CRASHES = 5

    def run(self):
        """Run the bot with in-process polling restart capability."""
        enforce_access_control(config)
        exit_reason = "Bot stopped"
        try:
            health_reporter.initialize_process()
            health_reporter.mark_starting("initializing bot")
            asyncio.run(self._run_async())
        except KeyboardInterrupt:
            exit_reason = "Stopped by signal"
            raise
        except SystemExit as exc:
            if exc.code not in (None, 0):
                exit_reason = str(exc.code)
            raise
        except Exception:
            exit_reason = "Unexpected error in bot run loop"
            logger.exception("Unexpected error in bot run loop")
            raise
        finally:
            health_reporter.mark_unavailable(exit_reason)
            health_reporter.cleanup_runtime_files()

    def _probe_claude_readiness(self) -> tuple[bool, str]:
        cli_path = (
            str(config.claude_cli_path)
            if config.claude_cli_path
            else shutil.which("claude") or ""
        )
        if not cli_path:
            return False, "claude command not found"

        try:
            proc = subprocess.run(
                [cli_path, "auth", "status", "--json"],
                text=True,
                capture_output=True,
                timeout=float(os.getenv("CLAUDE_AUTH_STATUS_TIMEOUT", "15")),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, "claude auth status timed out"
        except Exception as exc:
            return False, f"claude auth status failed: {exc}"

        raw = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            preview = raw.replace("\n", " ")[:200]
            return (
                False,
                f"invalid claude auth response (cli={cli_path}, exit={proc.returncode}): {preview}",
            )

        if data.get("loggedIn") is True:
            return True, ""

        return False, "claude authentication unavailable"

    async def _run_async(self):
        """Async entry: manage Application lifecycle and polling restart loop."""
        # Isolate child claude/bash/pytest process trees into their own session so a
        # SIGTERM/SIGINT from work the bot itself launched cannot propagate back and
        # stop the bot (see core/session_isolation.py).
        apply_subprocess_session_isolation()
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        rapid_crash_count = 0

        while not stop_event.is_set():
            if not self.application:
                self.build()

            logger.info("Starting...")
            start_time = time.time()
            health_reporter.mark_starting("initializing telegram polling")

            try:
                await self.application.initialize()
            except telegram.error.InvalidToken:
                message = (
                    "Invalid Telegram Bot Token. "
                    "Please check TELEGRAM_BOT_TOKEN in your .env file.\n"
                    "   Get a valid token from @BotFather on Telegram."
                )
                health_reporter.record_telegram_error(message, consecutive_failures=1)
                raise SystemExit(message)
            except telegram.error.Conflict:
                message = (
                    "Another bot instance is already running with the same token.\n"
                    "   Use --stop to stop it first, or check for duplicate processes."
                )
                health_reporter.record_telegram_error(message, consecutive_failures=1)
                raise SystemExit(message)
            except telegram.error.TimedOut as e:
                # PoolTimeout is converted to TimedOut, need force cleanup
                health_reporter.record_telegram_error(
                    f"telegram timeout error: {e}",
                    consecutive_failures=1,
                )
                logger.warning(
                    "TimedOut error during initialization (likely PoolTimeout): %s, retrying...",
                    e,
                )
                # Force cleanup to release leaked connections from pool
                await self._graceful_shutdown(force=True)
                await asyncio.sleep(5)
                continue
            except telegram.error.NetworkError as e:
                health_reporter.record_telegram_error(
                    f"telegram startup error: {e}",
                    consecutive_failures=1,
                )
                logger.warning(
                    "Network error during initialization: %s, retrying...", e
                )
                # Force cleanup to release leaked connections from pool
                await self._graceful_shutdown(force=True)
                await asyncio.sleep(5)
                continue

            await self._on_ready(self.application)

            watchdog_task = None
            push_task = None
            try:
                await self.application.start()
                await self.application.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,
                )

                logger.info("Bot is running")
                health_reporter.record_telegram_ok()
                claude_ready, claude_reason = self._probe_claude_readiness()
                if claude_ready:
                    health_reporter.record_claude_ok()
                else:
                    health_reporter.record_claude_error(claude_reason)

                watchdog_task = asyncio.create_task(self._polling_watchdog(stop_event))
                push_task = asyncio.create_task(
                    self._push_notifier.run(self.application, stop_event)
                )

                await self._wait_for_polling_exit(stop_event)

            except _PollingRestart:
                health_reporter.mark_starting(
                    "restarting polling after connection loss"
                )
                uptime = time.time() - start_time
                if uptime < self._MIN_UPTIME:
                    rapid_crash_count += 1
                    if rapid_crash_count >= self._MAX_RAPID_CRASHES:
                        raise SystemExit(
                            f"Polling restarted {self._MAX_RAPID_CRASHES} times within "
                            f"{self._MIN_UPTIME}s each. Giving up."
                        )
                    logger.warning(
                        "Polling restart after only %.1fs (crash %d/%d)",
                        uptime,
                        rapid_crash_count,
                        self._MAX_RAPID_CRASHES,
                    )
                else:
                    rapid_crash_count = 0

                logger.warning("Polling restart triggered, restarting...")
                continue
            except telegram.error.TimedOut as e:
                # PoolTimeout is converted to TimedOut, need force cleanup
                health_reporter.record_telegram_error(
                    f"telegram timeout error: {e}",
                    consecutive_failures=1,
                )
                logger.warning(
                    "TimedOut error during runtime (likely PoolTimeout): %s", e
                )
                # Force cleanup to release leaked connections from pool
                await self._graceful_shutdown(force=True)
                continue
            except telegram.error.NetworkError as e:
                health_reporter.record_telegram_error(
                    f"telegram runtime error: {e}",
                    consecutive_failures=1,
                )
                logger.warning("Network error during startup: %s", e)
                # Force cleanup to release leaked connections from pool
                await self._graceful_shutdown(force=True)
                continue
            except telegram.error.Forbidden as e:
                message = (
                    f"Bot token was revoked or bot is blocked: {e}\n"
                    "   Create a new token via @BotFather on Telegram."
                )
                health_reporter.record_telegram_error(message, consecutive_failures=1)
                raise SystemExit(message)
            finally:
                for _task in (watchdog_task, push_task):
                    if _task and not _task.done():
                        _task.cancel()
                        try:
                            await _task
                        except (asyncio.CancelledError, _PollingRestart):
                            pass
                await self._graceful_shutdown()

        logger.info("Bot stopped")

    async def _polling_watchdog(self, stop_event: asyncio.Event):
        """Monitor Telegram API reachability; restart polling if hung."""
        consecutive_failures = 0

        while not stop_event.is_set():
            await asyncio.sleep(self._WATCHDOG_INTERVAL)

            updater = self.application.updater if self.application else None
            if not self.application or not updater or not updater.running:
                continue

            try:
                await asyncio.wait_for(self.application.bot.get_me(), timeout=10)
                if consecutive_failures > 0:
                    logger.info(
                        "Telegram API reachable again after %d failure(s)",
                        consecutive_failures,
                    )
                consecutive_failures = 0
                health_reporter.record_telegram_ok()
            except Exception as e:
                consecutive_failures += 1
                total_down = consecutive_failures * self._WATCHDOG_INTERVAL
                health_reporter.record_telegram_error(
                    str(e), consecutive_failures=consecutive_failures
                )
                logger.warning("Telegram API unreachable (%ds): %s", total_down, e)

                if total_down >= self._NETWORK_FAILURE_THRESHOLD:
                    logger.warning(
                        "Network down for %ds, restarting polling...",
                        total_down,
                    )
                    try:
                        await asyncio.wait_for(updater.stop(), timeout=15)
                    except asyncio.TimeoutError:
                        logger.error("updater.stop() timed out, forcing process exit")
                        os._exit(1)
                    raise _PollingRestart()

    async def _wait_for_polling_exit(self, stop_event: asyncio.Event):
        """Block until stop signal or polling exits unexpectedly."""
        while not stop_event.is_set():
            if (
                self.application
                and self.application.updater
                and not self.application.updater.running
            ):
                logger.warning("Polling exited unexpectedly, triggering restart")
                raise _PollingRestart()
            await asyncio.sleep(1)

    async def _graceful_shutdown(self, force: bool = False):
        """Tear down the current Application so the next loop iteration is clean.

        Args:
            force: If True, skip graceful stop and immediately cleanup.
                   Use when connection pool is exhausted or timed out.
        """
        if not self.application:
            return

        try:
            if force:
                logger.warning("Force shutdown requested, skipping graceful stop")
            else:
                # Give graceful shutdown 5 seconds max
                await asyncio.wait_for(
                    self._do_graceful_stop(),
                    timeout=5.0,
                )
        except asyncio.TimeoutError:
            logger.warning("Graceful shutdown timed out after 5s, forcing cleanup")
        except Exception:
            logger.exception("Error during graceful shutdown")
        finally:
            # Always clear the reference so next build() creates fresh connections
            self.application = None

    async def _do_graceful_stop(self):
        """Actual graceful shutdown logic with proper resource cleanup."""
        if self.application.updater and self.application.updater.running:
            await self.application.updater.stop()
        if self.application.running:
            await self.application.stop()
        await self.application.shutdown()

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


    async def _handle_text_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle text messages - use project chat or answer pending questions"""
        if not await self._check_access(update):
            return
        message = self._require_message(update)
        if not message.text:
            return

        user_id = self._require_user(update).id
        chat = self._require_chat(update)
        conversation_key = self._conversation_key(user_id, chat.id)
        text = message.text
        session = await session_manager.get_session(conversation_key)

        # Check resume selection (user replies with a number)
        resume_list = session.get("resume_list")
        if resume_list and text.strip().isdigit():
            log_debug(user_id, "user", text)
            idx = int(text.strip()) - 1
            if 0 <= idx < len(resume_list):
                sid, msg = resume_list[idx]
                session["session_id"] = sid
                session["new_session"] = False
                session.pop("resume_list", None)
                await session_manager.update_session(conversation_key, session)
                self._runtime_active_sessions.add(conversation_key)
                reply = f"✅ Switched to session: {msg}"
                await message.reply_text(reply)
                log_debug(user_id, "bot", reply)
                # Send last assistant message as progress summary
                last_msg = project_chat_handler.get_session_last_assistant_message(sid)
                if last_msg:
                    progress = f"📋 {last_msg}"
                    await message.reply_text(progress)
                    log_debug(user_id, "bot", progress)
                return
            else:
                reply = "❌ Invalid number, please try again."
                await message.reply_text(reply)
                log_debug(user_id, "bot", reply)
                return

        # Clear resume list if user sends non-number
        if resume_list:
            session.pop("resume_list", None)
            await session_manager.update_session(conversation_key, session)

        # Capture explicit outside-path approval/denial from user replies.
        await self._maybe_capture_outside_approval(user_id, text, chat.id)

        # Check if there's a pending question
        pending = await session_manager.get_pending_question(conversation_key)
        if pending:
            log_debug(user_id, "user", f"[answer] {text}")
            await session_manager.clear_pending_question(conversation_key)
            reply = f"✅ Answer received: {text}\n\nContinuing..."
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)
            return

        async def run_task():
            await self._process_user_message_text(update, user_id, text)

        async def on_overflow():
            reply = "⏳ Processing previous messages, please wait or send /stop to terminate."
            await message.reply_text(reply)
            log_debug(user_id, "bot", reply)

        await self._enqueue_user_task(conversation_key, run_task, on_overflow)
    # Match both absolute (/foo/bar.png) and relative (foo/bar.png) file paths
    _FILE_PATH_RE = re.compile(
        r"(/?(?:[\w.@-]+/)+[\w.@-]+\.(?:png|jpg|jpeg|gif|webp|mp4|mp3|pdf|zip))",
        re.IGNORECASE,
    )
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

    def _resolve_paths(self, content: str) -> List[FilePath]:
        """Extract file paths from text and resolve relative ones against PROJECT_ROOT."""
        from telegram_bot.core.project_chat import PROJECT_ROOT

        paths = []
        seen = set()
        for m in self._FILE_PATH_RE.findall(content):
            p = FilePath(m.strip())
            if not p.is_absolute():
                p = PROJECT_ROOT / p
            p = p.resolve()
            if p not in seen and p.is_file() and p.stat().st_size < 10 * 1024 * 1024:
                seen.add(p)
                paths.append(p)
        return paths

    def _split_paths_by_scope(
        self, paths: List[FilePath]
    ) -> Tuple[List[FilePath], List[FilePath]]:
        return path_scope.split_paths_by_scope(paths, self._project_root())

    def _extract_options(self, text: str) -> List[str]:
        """Extract numbered options from text like '1. xxx\n2. xxx'."""
        return ui.extract_options(text)

    def _build_option_keyboard(
        self, options: List[str]
    ) -> Optional[InlineKeyboardMarkup]:
        """Build inline keyboard from extracted options."""
        return ui.build_option_keyboard(options)

    def _build_history_keyboard(
        self, messages: List[Dict[str, Any]], page: int = 0, page_size: int = 10
    ) -> InlineKeyboardMarkup:
        """Build inline keyboard for message history selection."""
        return ui.build_history_keyboard(messages, page, page_size)

    @staticmethod
    def _format_relative_time(timestamp: str) -> str:
        """Format timestamp as relative time (see ui.format_relative_time)."""
        return ui.format_relative_time(timestamp)

    def _build_revert_mode_keyboard(self, msg_index: int) -> InlineKeyboardMarkup:
        """Build inline keyboard for revert mode selection."""
        return ui.build_revert_mode_keyboard(msg_index)

    async def _send_file_paths(self, chat_id: int, paths: List[FilePath]) -> None:
        app = self._require_application()
        bot = app.bot
        logger.debug(f"_send_file_paths: sending {len(paths)} files to chat {chat_id}")
        for p in paths:
            try:
                logger.debug(f"Sending file: {p} (suffix: {p.suffix.lower()})")
                if p.suffix.lower() in self._IMAGE_EXTS:
                    with open(p, "rb") as f:
                        await bot.send_photo(chat_id, photo=f)
                    logger.info(f"Sent photo: {p}")
                else:
                    with open(p, "rb") as f:
                        await bot.send_document(chat_id, document=f)
                    logger.info(f"Sent document: {p}")
            except Exception as e:
                logger.warning(f"Failed to send file {p}: {e}")

    async def _prompt_outside_file_confirmation(
        self, chat_id: int, user_id: int, paths: List[FilePath]
    ) -> None:
        session = await session_manager.get_session(user_id)
        session["pending_external_files"] = [str(p) for p in paths]
        await session_manager.update_session(user_id, session)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Send external files", callback_data="extsend:allow"
                    )
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="extsend:deny")],
            ]
        )
        app = self._require_application()
        await app.bot.send_message(
            chat_id,
            "File paths outside PROJECT_ROOT detected. Confirmation required before sending.",
            reply_markup=kb,
        )

    @staticmethod
    def _split_text(text: str, limit: int = 4000) -> List[str]:
        """Split text into chunks no longer than limit (see ui.split_text)."""
        return ui.split_text(text, limit)

    async def _deliver_markdown(self, content: str, op, base_parse_mode: str = "Markdown"):
        """Split *content* and send each chunk via ``op(text, parse_mode)``.

        ``op`` is a callable ``(text, parse_mode|None) -> awaitable`` returning a
        fresh awaitable each call. Markdown is rendered to Telegram **MarkdownV2**
        (GFM tables -> aligned code blocks, special chars escaped) when the
        telegramify renderer is available; on the rare per-chunk parse error we
        fall back to clean plain text for that chunk only. ``HTML`` callers keep
        HTML; if the renderer is unavailable we use the legacy
        ``wrap_markdown_tables`` + base-parse-mode path.
        """
        # Per-bubble size: non-streaming replies split into the same digestible
        # messages as the streaming path (CCC_TELEGRAM_MAX_BUBBLE_CHARS), bounded
        # by the Telegram hard limit.
        limit = max(
            200,
            min(
                int(getattr(config, "telegram_max_bubble_chars", 4000)),
                tg_md.TELEGRAM_LIMIT,
            ),
        )

        # HTML callers (e.g. /skills listing) keep their existing behavior.
        if base_parse_mode == "HTML":
            for part in self._split_text(wrap_markdown_tables(content), limit):
                try:
                    await send_with_retry(lambda p=part: op(p, "HTML"))
                except telegram.error.BadRequest:
                    await send_with_retry(lambda p=part: op(p, None))
            return

        # Normalize layout for mobile readability (loose-spacing etc.) before the
        # MarkdownV2 conversion — mirrors the streaming finalize path so the
        # non-streaming delivery path (the default since live streaming is
        # opt-in, see CCC_TELEGRAM_STREAMING) renders identically. Shared helper
        # keeps both paths from drifting. Content-preserving, idempotent,
        # fail-open.
        render_text = tg_readable.render_for_delivery(
            content,
            enabled=getattr(config, "enable_readable_renderer", False),
            loose=getattr(config, "enable_loose_spacing", False),
            spacing=getattr(config, "spacing_lines", 1),
        )

        # Entity path (opt-in via CCC_TELEGRAM_ENTITY_RENDERER, default on):
        # send (text + MessageEntity[]) without parse_mode, avoiding MarkdownV2
        # escape failures. Mirrors the streaming finalize path so both delivery
        # paths render identically. Fail-open: to_entity_chunks returns None when
        # the renderer is unavailable (-> MarkdownV2 below), and each chunk
        # degrades to plain text on the rare BadRequest (per-message, so no
        # duplication on partial failure).
        if getattr(config, "enable_entity_renderer", False):
            entity_chunks = tg_entities.to_entity_chunks(render_text, limit)
            if entity_chunks:
                for text, entities in entity_chunks:
                    try:
                        await send_with_retry(
                            lambda t=text, e=entities: op(t, None, e or None)
                        )
                    except telegram.error.BadRequest:
                        await send_with_retry(lambda t=text: op(t, None))
                return

        if tg_md.available():
            # Convert the whole message to MarkdownV2 first, THEN split on
            # entity-safe boundaries with split_markdownv2. Splitting the raw
            # markdown before conversion is unsafe: MarkdownV2 escaping expands
            # the text (~1.2x, more for tables/symbol-dense content), so a
            # sub-limit raw chunk can exceed TELEGRAM_LIMIT once escaped and was
            # silently dropped to plain text (all formatting lost). Per-part
            # plain fallback only on the rare BadRequest.
            md2 = tg_md.to_markdownv2(render_text)
            if md2 is not None:
                for part in tg_md.split_markdownv2(md2, limit):
                    try:
                        await send_with_retry(lambda p=part: op(p, "MarkdownV2"))
                    except telegram.error.BadRequest:
                        await send_with_retry(lambda p=part: op(p, None))
                return
            # conversion unavailable/failed -> legacy path below

        # Legacy fallback: telegramify unavailable -> wrap tables + base parse mode.
        for part in self._split_text(wrap_markdown_tables(render_text), limit):
            try:
                await send_with_retry(lambda p=part: op(p, base_parse_mode))
            except telegram.error.BadRequest:
                await send_with_retry(lambda p=part: op(p, None))

    async def _reply_smart(
        self,
        message,
        content: str,
        parse_mode: str = "Markdown",
        force_options: bool = False,
        streamed: bool = False,
    ):
        """Reply with text (splitting if needed), send referenced files, and add option buttons."""
        # Skip text sending if already streamed
        if not streamed:
            await self._deliver_markdown(
                content,
                lambda t, pm=None, ents=None: message.reply_text(
                    t, parse_mode=pm, entities=ents
                ),
                base_parse_mode=parse_mode,
            )

        await self._send_content_artifacts(message, content, force_options)

    async def _send_smart(
        self,
        chat_id: int,
        content: str,
        user_id: Optional[int] = None,
        force_options: bool = False,
        streamed: bool = False,
    ):
        """Send text to chat_id (splitting if needed) with file and option detection."""
        app = self._require_application()
        bot = app.bot

        # Skip text sending if already streamed
        if not streamed:
            await self._deliver_markdown(
                content,
                lambda t, pm=None, ents=None: bot.send_message(
                    chat_id, t, parse_mode=pm, entities=ents
                ),
            )

        resolved_paths = self._resolve_paths(content)
        in_root_paths, _ = self._split_paths_by_scope(resolved_paths)
        await self._send_file_paths(chat_id, in_root_paths)
        # Inline option buttons are opt-in (CCC_TELEGRAM_OPTION_BUTTONS); default
        # off, so numbered options stay as text and the user types their choice.
        if force_options and getattr(config, "enable_option_buttons", False):
            options = self._extract_options(content)
            kb = self._build_option_keyboard(options)
            if kb:
                await bot.send_message(chat_id, "Please select:", reply_markup=kb)

    async def _handle_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle callback queries from inline keyboards"""
        if not await self._check_access(update):
            return

        query = self._require_callback_query(update)
        await query.answer()

        user_id = self._require_user(update).id
        chat = self._require_chat(update)
        app = self._require_application()
        data = query.data
        if data is None:
            return

        if data.startswith("extsend:"):
            session = await session_manager.get_session(user_id)
            pending = session.get("pending_external_files", [])
            session.pop("pending_external_files", None)
            await session_manager.update_session(user_id, session)

            if data == "extsend:deny":
                await query.edit_message_text("❌ External file sending cancelled.")
                return

            if not pending:
                await query.edit_message_text("ℹ️ No pending external files.")
                return

            await query.edit_message_text("✅ Confirmed. Sending external files...")
            paths: List[FilePath] = []
            for raw in pending:
                p = FilePath(raw)
                try:
                    resolved = p.resolve(strict=False)
                    if (
                        resolved.is_file()
                        and resolved.stat().st_size < 10 * 1024 * 1024
                    ):
                        paths.append(resolved)
                except Exception:
                    continue
            await self._send_file_paths(chat.id, paths)
            return

        # Handle permission request buttons
        # Handle numbered option buttons (from Claude's text-based choices)
        if data.startswith("opt:"):
            choice = data.split(":", 1)[1]
            await query.edit_message_text(f"✅ Selected: {choice}")
            # Send choice back to Claude as a new message
            chat_id = chat.id
            await self._maybe_capture_outside_approval(user_id, choice)

            async def run_task():
                session = await session_manager.get_session(user_id)
                await app.bot.send_chat_action(chat_id, action="typing")
                try:
                    response = await project_chat_handler.process_message(
                        user_message=choice,
                        user_id=user_id,
                        chat_id=chat_id,
                        session_id=self._effective_session_id(user_id, session),
                        model=session.get("model"),
                        permission_callback=self._permission_callback,
                        typing_callback=lambda: app.bot.send_chat_action(
                            chat_id, action="typing"
                        ),
                        status_callback=self._make_status_callback(app.bot, chat_id),
                        bot=app.bot,
                    )
                    await self._save_session_id(user_id, response)
                    await self._send_smart(
                        chat_id,
                        response.content,
                        user_id=user_id,
                        force_options=response.has_options,
                        streamed=response.streamed,
                    )
                except Exception as e:
                    logger.error(f"Option reply failed: {e}", exc_info=True)
                    await app.bot.send_message(chat_id, f"❌ Processing failed: {e}")

            async def on_overflow():
                await app.bot.send_message(
                    chat_id,
                    "⏳ Processing previous messages, please wait or send /stop to terminate.",
                )

            conversation_key = self._conversation_key(user_id, chat_id)
            await self._enqueue_user_task(conversation_key, run_task, on_overflow)
            return

        # Handle revert callbacks
        if data.startswith("revert:"):
            await self._handle_revert_callback(update, context, data)
            return

        # Handle model selection
        if data.startswith("model:"):
            model_name = data.split(":", 1)[1]
            log_debug(user_id, "callback", f"model:{model_name}")
            session = await session_manager.get_session(user_id)
            session["model"] = model_name
            await session_manager.update_session(user_id, session)
            label = dict(self.MODELS).get(model_name, model_name)
            logger.info(
                f"User {user_id}: model set to {model_name!r} via inline keyboard"
            )
            reply = f"✅ Model switched to: {label}"
            await query.edit_message_text(reply)
            log_debug(user_id, "bot", reply)
            return

        # Check if there's a pending question
        pending = await session_manager.get_pending_question(user_id)
        if pending:
            await session_manager.clear_pending_question(user_id)
            await query.edit_message_text(f"✅ Selected: {data}\n\nContinuing...")

    async def _set_bot_commands(self):
        """Set bot commands menu"""
        commands = [
            BotCommand("new", "New session"),
            BotCommand("stop", "Stop execution"),
            BotCommand("model", "Switch model"),
            BotCommand("resume", "Resume session"),
            BotCommand("history", "View message history"),
            BotCommand("revert", "Revert conversation"),
            BotCommand("skills", "List skills"),
            BotCommand("skill", "Run skill"),
            BotCommand("command", "Run command"),
        ]
        for scope in (
            BotCommandScopeAllPrivateChats(),
            BotCommandScopeAllGroupChats(),
            BotCommandScopeAllChatAdministrators(),
        ):
            app = self._require_application()
            await app.bot.set_my_commands(commands, scope=scope)
        logger.info("Bot commands set")


bot = TelegramBot()
