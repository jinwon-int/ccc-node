# ruff: noqa: E402
import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, cast
from datetime import datetime, timezone

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
from telegram_bot.utils.chat_logger import log_debug
from telegram_bot.core import session_resume
from telegram_bot.core.push_notifier import PushNotifier
from telegram_bot.core.task_queue import UserTaskQueue
from telegram_bot.core.project_chat import ChatResponse
from telegram_bot.core.session_scope import legacy_storage_keys, storage_key
from telegram_bot.memory.distill_types import DistillJob, DistillTrigger
from telegram_bot.utils.audio_processor import AudioProcessor
from telegram_bot.utils.transcription import (
    VolcengineFileFastTranscriber,
    WhisperTranscriber,
)
from telegram_bot.utils.tts import MacOSTtsSynthesizer
from telegram_bot.utils.tos_uploader import VolcengineTOSUploader

logger = logging.getLogger(__name__)
STALE_MESSAGE_SECONDS = 20 * 60  # 20 minutes


@dataclass(slots=True)
class _DistillCheckpointProgress:
    thread_id: str
    started_at: float
    turns: int = 0
    byte_count: int = 0
    last_turn_marker: str | None = None
    pending_discriminator: str | None = None


from telegram_bot.core.bot_shared import _PollingRestart, enforce_access_control  # noqa: F401
from telegram_bot.core.bot_status import BotStatusMixin
from telegram_bot.core.bot_access import BotAccessMixin
from telegram_bot.core.bot_lifecycle import BotLifecycleMixin
from telegram_bot.core.bot_commands import BotCommandMixin
from telegram_bot.core.bot_delivery import BotDeliveryMixin
from telegram_bot.core.bot_voice import BotVoiceMixin
from telegram_bot.core.bot_approvals import BotApprovalMixin
from telegram_bot.core.bot_callbacks import BotCallbackMixin


class TelegramBot(
    BotLifecycleMixin,
    BotStatusMixin,
    BotAccessMixin,
    BotCommandMixin,
    BotDeliveryMixin,
    BotCallbackMixin,
    BotVoiceMixin,
    BotApprovalMixin,
):

    def __init__(
        self,
        *,
        settings: Any,
        session_manager: Any,
        project_chat: Any,
        distill_journal: Any = None,
        distill_snapshot_worker: Any = None,
        distill_extraction_worker: Any = None,
        distill_local_sink_worker: Any = None,
        memory_promoter: Any = None,
        distill_wiki_sink_worker: Any = None,
        distill_honcho_sink_worker: Any = None,
        skill_candidate_collector_worker: Any = None,
        application_builder_factory: Any = None,
        clock: Any = None,
    ):
        self._config = settings
        self._session_manager = session_manager
        self._project_chat = project_chat
        self._distill_journal = distill_journal
        self._distill_snapshot_worker = distill_snapshot_worker
        # Budget-gated distill extraction worker composed by build_context;
        # retained by the running application so #465's scheduling phase
        # drives this exact gated instance (#388).
        self._distill_extraction_worker = distill_extraction_worker
        self._distill_local_sink_worker = distill_local_sink_worker
        self._memory_promoter = memory_promoter
        self._distill_wiki_sink_worker = distill_wiki_sink_worker
        self._distill_honcho_sink_worker = distill_honcho_sink_worker
        self._skill_candidate_collector_worker = skill_candidate_collector_worker
        self._application_builder_factory = (
            application_builder_factory or Application.builder
        )
        self._clock = clock or time
        self.application: Optional[Application] = None
        # ccc-node owner-only push notifier (disabled unless config.push_enabled).
        self._push_notifier = PushNotifier(settings)
        # Only sessions created/resumed in current runtime are auto-resumed.
        self._runtime_active_sessions: set[Any] = set()
        self._distill_checkpoint_progress: Dict[Any, _DistillCheckpointProgress] = {}
        self._distill_checkpoint_locks: Dict[Any, asyncio.Lock] = {}
        # Serialize first-use legacy seeding per destination. Telegram may
        # dispatch updates from different chats concurrently in shared scopes.
        self._scope_migration_locks: Dict[Any, asyncio.Lock] = {}
        self._user_voice_tasks: Dict[Any, set[asyncio.Task]] = {}
        # Per-user bounded run queue + active-task tracking (priority stop/revert).
        self._tasks = UserTaskQueue(self._MAX_INFLIGHT_MESSAGES)
        self._audio_dir = settings.bot_data_dir / "audio"
        self._image_dir = settings.bot_data_dir / "images"
        self._document_dir = settings.bot_data_dir / "uploads"
        self._audio_processor = AudioProcessor(ffmpeg_path=settings.ffmpeg_path)
        self._whisper_transcriber: Optional[WhisperTranscriber] = None
        self._volcengine_transcriber: Optional[VolcengineFileFastTranscriber] = None
        self._volcengine_tos_uploader: Optional[VolcengineTOSUploader] = None
        self._tts_synthesizer: Optional[MacOSTtsSynthesizer] = None
        self._initialize_codex_approvals()


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
    _SHUTDOWN_DISTILL_MAX_SESSIONS = 128
    _SHUTDOWN_DISTILL_TIMEOUT_SECONDS = 2.0



    def _conversation_key(self, user_id: int, chat_id: Optional[int] = None) -> Any:
        """Storage/queue key for one Telegram conversation.

        The default isolates each sender/chat pair. Operators may explicitly
        share inside each group or, with the broader shared-all opt-in, across
        every DM and group after access control has accepted the sender.
        """
        cfg = getattr(self, "_config", None)
        scope = getattr(cfg, "telegram_session_scope", "per-user-chat")
        return storage_key(scope, user_id, chat_id)

    async def _seed_scoped_session_from_legacy(
        self,
        conversation_key: Any,
        user_id: int,
        chat_id: int,
        current_session: Dict[str, Any],
    ) -> None:
        """Seed a new scoped row once from the first request's legacy row."""

        if conversation_key == user_id:
            return
        locks = getattr(self, "_scope_migration_locks", None)
        if locks is None:
            locks = self._scope_migration_locks = {}
        migration_lock = locks.setdefault(conversation_key, asyncio.Lock())
        async with migration_lock:
            latest_session = await self._session_manager.get_session(conversation_key)
            current_session.clear()
            current_session.update(latest_session)
            if not set(latest_session).issubset({"reply_mode", "provider"}):
                return

            legacy_session = None
            legacy_key = None
            for candidate in legacy_storage_keys(
                getattr(self._config, "telegram_session_scope", "per-user-chat"),
                user_id,
                chat_id,
            ):
                candidate_session = await self._session_manager.get_session(candidate)
                if candidate_session and candidate_session != latest_session:
                    legacy_session = candidate_session
                    legacy_key = candidate
                    break
            if legacy_session is None:
                return
            migration_fields = {
                "session_id",
                "model",
                "effort",
                "provider",
                "reply_mode",
                "last_user_message_at",
                "force_auto_new_session",
            }
            migrated = {
                key: value
                for key, value in legacy_session.items()
                if key in migration_fields
            }
            if migrated:
                await self._session_manager.patch_session(
                    conversation_key, updates=migrated
                )
                current_session.update(migrated)
                logger.info(
                    "conversation_scope_migrated source=%s destination=%s fields=%s",
                    legacy_key,
                    conversation_key,
                    sorted(migrated),
                )
            if legacy_key in self._runtime_active_sessions:
                self._runtime_active_sessions.add(conversation_key)

    def _active_provider(self) -> str:
        provider = str(getattr(self._config, "agent_provider", "claude")).strip().lower()
        if provider not in {"claude", "codex"}:
            raise ValueError(f"Unsupported agent provider: {provider!r}")
        return provider

    async def _enqueue_previous_codex_session(
        self,
        session: dict[str, Any],
        trigger: DistillTrigger,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        discriminator: str | None = None,
    ) -> DistillJob | None:
        provider = str(session.get("provider", "claude")).strip().lower()
        thread_id = session.get("session_id")
        if provider != "codex" or not isinstance(thread_id, str) or not thread_id:
            return None
        journal = getattr(self, "_distill_journal", None)
        if journal is None:
            return None
        memory_audience = None
        memory_scope = None
        if user_id is not None and chat_id is not None:
            from telegram_bot.core.memory_audience import resolve_memory_audience

            audience = resolve_memory_audience(
                self._config,
                user_id=user_id,
                chat_id=chat_id,
            )
            if audience is not None:
                memory_audience = audience.kind
                memory_scope = audience.scope
        else:
            stored_audience = session.get("distill_memory_audience")
            stored_scope = session.get("distill_memory_scope")
            if isinstance(stored_audience, str) and isinstance(stored_scope, str):
                memory_audience = stored_audience
                memory_scope = stored_scope
        enqueue_kwargs = {
            "provider": "codex",
            "thread_id": thread_id,
            "trigger": trigger,
            "memory_audience": memory_audience,
            "memory_scope": memory_scope,
        }
        if discriminator is not None:
            enqueue_kwargs["discriminator"] = discriminator
        return await asyncio.to_thread(
            journal.enqueue_once,
            **enqueue_kwargs,
        )

    async def _align_active_provider(
        self,
        session_key: Any,
        session=None,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
    ):
        """Durably capture a departing Codex thread before provider state resets."""
        if session is None:
            session = await self._session_manager.get_session(session_key)
        provider = str(session.get("provider", "claude")).strip().lower()
        active_provider = self._active_provider()
        session["provider"] = provider
        if provider == active_provider:
            return session, False
        await self._enqueue_previous_codex_session(
            session,
            DistillTrigger.PROVIDER_SWITCH,
            user_id=user_id,
            chat_id=chat_id,
        )
        align = getattr(self._session_manager, "align_active_provider", None)
        if callable(align):
            return await cast(Any, align)(session_key)
        await self._session_manager.patch_session(
            session_key,
            updates={
                "provider": active_provider,
                "session_id": None,
                "new_session": True,
            },
            remove_fields={"model"},
        )
        session.update(provider=active_provider, session_id=None, new_session=True)
        session.pop("model", None)
        return session, True

    async def _switch_provider_if_needed(
        self, session_key: Any, user_id: int, chat_id: int, session=None
    ):
        """Align the session provider and reset approval state on a switch.

        The deny/invalidate pair is security-relevant: pending and previously
        granted Codex approvals belong to the departing provider and must not
        survive into the new one. Keeping the reset inside this helper means a
        call site cannot align the provider and forget the reset.
        """
        aligned, switched = await self._align_active_provider(
            session_key,
            session,
            user_id=user_id,
            chat_id=chat_id,
        )
        if switched:
            self._deny_codex_approvals(user_id, chat_id)
            self._invalidate_codex_approvals(user_id, chat_id)
            self._runtime_active_sessions.discard(session_key)
        return aligned, switched

    async def _reset_for_auto_new_session(
        self,
        session_key: Any,
        session: dict[str, Any],
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
    ) -> None:
        await self._enqueue_previous_codex_session(
            session,
            DistillTrigger.AUTO_NEW,
            user_id=user_id,
            chat_id=chat_id,
        )
        await self._session_manager.patch_session(
            session_key,
            updates={"session_id": None, "new_session": False},
        )
        session["session_id"] = None
        session["new_session"] = False
        self._runtime_active_sessions.discard(session_key)

    async def _session_provider(self, session_key: Any) -> str:
        get_provider = getattr(self._session_manager, "get_session_provider", None)
        if callable(get_provider):
            return await get_provider(session_key)
        session = await self._session_manager.get_session(session_key)
        return str(session.get("provider", "claude")).strip().lower()

    async def _save_session_id(
        self,
        session_key: Any,
        response: ChatResponse,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        request_text: str = "",
        turn_marker: str | None = None,
    ):
        if getattr(response, "success", True) and response.session_id:
            updates = {
                "provider": self._active_provider(),
                "session_id": response.session_id,
            }
            remove_fields: set[str] = set()
            if user_id is not None and chat_id is not None:
                from telegram_bot.core.memory_audience import resolve_memory_audience

                audience = resolve_memory_audience(
                    self._config,
                    user_id=user_id,
                    chat_id=chat_id,
                )
                if audience is None:
                    remove_fields.update(
                        {"distill_memory_audience", "distill_memory_scope"}
                    )
                else:
                    updates.update(
                        {
                            "distill_memory_audience": audience.kind,
                            "distill_memory_scope": audience.scope,
                        }
                    )
            await self._session_manager.patch_session(
                session_key,
                updates=updates,
                remove_fields=remove_fields,
            )
            self._runtime_active_sessions.add(session_key)
            try:
                await self._record_codex_checkpoint(
                    session_key,
                    response,
                    request_text=request_text,
                    turn_marker=turn_marker,
                    user_id=user_id,
                    chat_id=chat_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Codex checkpoint accounting failed error=%s",
                    type(error).__name__,
                )

    def _distill_checkpoint_gates(self) -> tuple[int, int, int]:
        return (
            int(getattr(self._config, "codex_distill_checkpoint_turns", 0) or 0),
            int(getattr(self._config, "codex_distill_checkpoint_bytes", 0) or 0),
            int(
                getattr(self._config, "codex_distill_checkpoint_age_seconds", 0)
                or 0
            ),
        )

    @staticmethod
    def _distill_checkpoint_reached(
        progress: _DistillCheckpointProgress,
        gates: tuple[int, int, int],
        *,
        now: float,
    ) -> bool:
        turn_gate, byte_gate, age_gate = gates
        elapsed = max(0.0, now - progress.started_at)
        return (
            (turn_gate > 0 and progress.turns >= turn_gate)
            or (byte_gate > 0 and progress.byte_count >= byte_gate)
            or (age_gate > 0 and elapsed >= age_gate)
        )

    @staticmethod
    def _update_distill_checkpoint_progress(
        progress_by_key: Dict[Any, _DistillCheckpointProgress],
        session_key: Any,
        *,
        thread_id: str,
        marker_hash: str,
        turn_bytes: int,
        now: float,
    ) -> _DistillCheckpointProgress:
        progress = progress_by_key.get(session_key)
        if progress is None or progress.thread_id != thread_id:
            progress = _DistillCheckpointProgress(thread_id, now)
            progress_by_key[session_key] = progress
        if progress.last_turn_marker != marker_hash:
            progress.turns += 1
            progress.byte_count += turn_bytes
            progress.last_turn_marker = marker_hash
        return progress

    async def _record_codex_checkpoint(
        self,
        session_key: Any,
        response: ChatResponse,
        *,
        request_text: str,
        turn_marker: str | None,
        user_id: int | None,
        chat_id: int | None,
    ) -> None:
        """Count completed turns and durably enqueue the first reached gate."""
        if self._active_provider() != "codex":
            return
        if getattr(self, "_distill_journal", None) is None:
            return
        gates = self._distill_checkpoint_gates()
        if all(gate <= 0 for gate in gates):
            return
        thread_id = response.session_id
        if not isinstance(thread_id, str) or not thread_id:
            return

        marker = turn_marker
        if not isinstance(marker, str) or not marker:
            content = response.content if isinstance(response.content, str) else ""
            marker = hashlib.sha256(
                f"{request_text}\0{content}".encode("utf-8")
            ).hexdigest()
        marker_hash = hashlib.sha256(marker.encode("utf-8")).hexdigest()

        progress_by_key = getattr(self, "_distill_checkpoint_progress", None)
        if progress_by_key is None:
            progress_by_key = self._distill_checkpoint_progress = {}
        locks = getattr(self, "_distill_checkpoint_locks", None)
        if locks is None:
            locks = self._distill_checkpoint_locks = {}
        lock = locks.setdefault(session_key, asyncio.Lock())

        async with lock:
            now = float(self._clock.time())
            response_text = (
                response.content if isinstance(response.content, str) else ""
            )
            progress = self._update_distill_checkpoint_progress(
                progress_by_key,
                session_key,
                thread_id=thread_id,
                marker_hash=marker_hash,
                turn_bytes=(
                    len(request_text.encode("utf-8"))
                    + len(response_text.encode("utf-8"))
                ),
                now=now,
            )

            if progress.pending_discriminator is None:
                if not self._distill_checkpoint_reached(
                    progress,
                    gates,
                    now=now,
                ):
                    return
                digest = hashlib.sha256(
                    f"{thread_id}\0{marker_hash}".encode("utf-8")
                ).hexdigest()
                progress.pending_discriminator = f"checkpoint-turn-v1-{digest}"

            try:
                session = await self._session_manager.get_session(session_key)
                if (
                    session.get("provider") != "codex"
                    or session.get("session_id") != thread_id
                ):
                    progress_by_key.pop(session_key, None)
                    return
                job = await self._enqueue_previous_codex_session(
                    session,
                    DistillTrigger.CHECKPOINT,
                    user_id=user_id,
                    chat_id=chat_id,
                    discriminator=progress.pending_discriminator,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Codex checkpoint journal enqueue failed error=%s",
                    type(error).__name__,
                )
                return

            if job is not None:
                progress_by_key[session_key] = _DistillCheckpointProgress(
                    thread_id=thread_id,
                    started_at=now,
                    last_turn_marker=marker_hash,
                )

    @staticmethod
    def _shutdown_distill_discriminator(session: dict[str, Any]) -> str:
        marker = session.get("last_user_message_at")
        thread_id = session.get("session_id")
        digest = hashlib.sha256(
            f"{thread_id}\0{marker or 'unknown-turn'}".encode("utf-8")
        ).hexdigest()
        return f"shutdown-turn-v1-{digest}"

    async def _enqueue_shutdown_distills(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        """Bound shutdown work to durable journal writes; never call a provider."""
        if getattr(self, "_distill_journal", None) is None:
            return

        active_keys = sorted(
            tuple(getattr(self, "_runtime_active_sessions", ())),
            key=str,
        )
        limit = self._SHUTDOWN_DISTILL_MAX_SESSIONS
        selected_keys = active_keys[:limit]
        if len(active_keys) > limit:
            logger.warning(
                "Codex shutdown distill queue capped at %d active sessions",
                limit,
            )

        async def enqueue_selected() -> None:
            for session_key in selected_keys:
                try:
                    session = await self._session_manager.get_session(session_key)
                    await self._enqueue_previous_codex_session(
                        session,
                        DistillTrigger.SHUTDOWN,
                        discriminator=self._shutdown_distill_discriminator(session),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.warning(
                        "Codex shutdown distill queue entry failed error=%s",
                        type(error).__name__,
                    )

        timeout = (
            self._SHUTDOWN_DISTILL_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )
        try:
            await asyncio.wait_for(enqueue_selected(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Codex shutdown distill queue timed out after %.2fs",
                timeout,
            )

    def _effective_session_id(self, session_key: Any, session: dict) -> Optional[str]:
        """Return a session_id that is safe to auto-resume.

        Sessions touched in the current runtime always resume. After a bridge
        restart the runtime set is empty; to avoid conversation memory loss,
        a persisted session_id is still resumed when its SDK transcript exists
        on disk (opt-out: CCC_RESUME_PERSISTED_SESSIONS=false). A persisted id
        without a transcript is still ignored (stale/foreign session data).
        """
        session_id = session.get("session_id")
        if not session_id:
            return None
        if session.get("provider", "claude") != self._active_provider():
            return None
        if session_key in self._runtime_active_sessions:
            return session_id
        if self._active_provider() == "codex":
            self._runtime_active_sessions.add(session_key)
            return session_id
        if session_resume.resume_persisted_enabled() and session_resume.persisted_transcript_exists(
            self._sdk_conversations_dir(), session_id
        ):
            logger.info(
                f"Resuming persisted session_id for conversation {session_key} after restart"
            )
            self._runtime_active_sessions.add(session_key)
            return session_id
        logger.info(
            f"Ignoring persisted session_id for conversation {session_key} (not active in current runtime)"
        )
        return None

    def _sdk_conversations_dir(self):
        """Return the SDK history directory owned by the injected handler."""
        return self._project_chat.conversations_dir

    @staticmethod
    def _session_start_notice_text(
        *,
        reason: str,
        model: Optional[str],
        provider: str = "claude",
        previous_session_id: Optional[str] = None,
    ) -> str:
        provider_label = "Claude Code" if provider == "claude" else "Codex"
        lines = [
            f"◐ CCC session started ({reason}). Conversation history is on a fresh {provider_label} stream.",
            "Use /resume to browse and restore a previous session.",
            "",
            f"◆ Model: {model or 'default'}",
            f"◆ Provider: {provider_label}",
            "◆ Context: new stream",
        ]
        if previous_session_id:
            lines.append(f"◆ Previous session: {previous_session_id[:8]}… (not resumed)")
        return "\n".join(lines)

    @staticmethod
    def _session_start_reason(
        *,
        new_session: bool,
        auto_new_session: bool,
        stale_session_id: Optional[str],
    ) -> str:
        if auto_new_session:
            return "automatic reset"
        if new_session:
            return "/new requested"
        if stale_session_id:
            return "previous session was not resumable"
        return "no active session"

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
        self.application.add_handler(CommandHandler("usage", self._cmd_usage))
        self.application.add_handler(CommandHandler("skills", self._cmd_skills))
        self.application.add_handler(CommandHandler("new", self._cmd_new))
        self.application.add_handler(CommandHandler("distill", self._cmd_distill))
        self.application.add_handler(
            CommandHandler("memory_promote", self._cmd_memory_promote)
        )
        self.application.add_handler(CommandHandler("model", self._cmd_model))
        self.application.add_handler(CommandHandler("effort", self._cmd_effort))
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
            MessageHandler(
                filters.Document.ALL & ~filters.Document.IMAGE,
                self._handle_document_message,
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

    def _own_bot_id(self) -> Optional[int]:
        """This bot's numeric user id, or None if unavailable.

        Used to detect replies to the bot's own messages. Accessing
        ``bot.id`` before the bot is initialized raises, so guard broadly and
        fall back to None. Reply-context trust classification then remains
        fail-closed instead of treating an arbitrary bot as this bot.
        """
        try:
            app = self.application
            if app is None:
                return None
            return getattr(app.bot, "id", None)
        except Exception:
            return None


























    async def _process_user_message_text(  # noqa: C901 -- #348 baseline hotspot
        self,
        update: Update,
        user_id: int,
        text: str,
        message_source: str = "text",
        voice_input_preview: Optional[str] = None,
        sensitive_log_event: Optional[str] = None,
    ) -> None:
        message = self._require_message(update)
        chat = self._require_chat(update)
        app = self._require_application()
        conversation_key = self._conversation_key(user_id, chat.id)
        current_session = await self._session_manager.get_session(conversation_key)
        await self._seed_scoped_session_from_legacy(
            conversation_key, user_id, chat.id, current_session
        )
        current_session, provider_switched = await self._switch_provider_if_needed(
            conversation_key, user_id, chat.id, current_session
        )
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
            await self._session_manager.update_session(
                conversation_key, {"reply_mode": next_reply_mode}
            )
        else:
            current_session["reply_mode"] = current_reply_mode
        try:
            await message.chat.send_action(action="typing")
        except Exception:
            pass

        try:
            # Capture stale session_id before it may be cleared by auto_new_session.
            # Used below to inject recent conversation history when a new session starts.
            stale_session_id = current_session.get("session_id")

            requested_new_session = bool(current_session.get("new_session"))
            new_session = False
            if requested_new_session:
                new_session = await self._session_manager.patch_session_if(
                    conversation_key,
                    expected={"new_session": True},
                    updates={"new_session": False},
                )
                current_session["new_session"] = False
            auto_new_session = await self._session_manager.should_start_new_session(
                conversation_key, now=message_timestamp
            )
            if auto_new_session:
                await self._reset_for_auto_new_session(
                    conversation_key,
                    current_session,
                    user_id=user_id,
                    chat_id=chat.id,
                )
                new_session = True

            await self._session_manager.set_last_user_message_at(conversation_key, message_timestamp)

            effective_sid = self._effective_session_id(conversation_key, current_session)
            if effective_sid is None:
                notice = self._session_start_notice_text(
                    reason=self._session_start_reason(
                        new_session=new_session,
                        auto_new_session=auto_new_session,
                        stale_session_id=stale_session_id,
                    ),
                    model=current_session.get("model"),
                    provider=current_session["provider"],
                    previous_session_id=stale_session_id,
                )
                await message.reply_text(notice)
                log_debug(user_id, "bot", notice)

            # History injection: when the effective session_id is None (new session due to
            # bridge restart, session expiry, or auto-rotation) but we have a previous
            # session, prepend the recent exchanges so context is not lost.
            send_text = text
            if (
                effective_sid is None
                and stale_session_id
                and current_session["provider"] == "claude"
            ):
                try:
                    recent = self._project_chat.get_recent_messages(stale_session_id, limit=6)
                    if recent:
                        lines = []
                        for m in recent:
                            label = "사용자" if m["role"] == "user" else "어시스턴트"
                            snippet = m["content"][:400].replace("\n", " ")
                            lines.append(f"{label}: {snippet}")
                        history_block = "\n".join(lines)
                        send_text = (
                            f"[이전 대화 맥락 — 세션 전환으로 자동 주입됨]\n"
                            f"{history_block}\n\n"
                            f"[현재 메시지]\n{text}"
                        )
                        if sensitive_log_event:
                            logger.info(
                                "History injection applied for sensitive input event=%s",
                                sensitive_log_event,
                            )
                        else:
                            logger.info(
                                f"History injection: {len(recent)} msgs from session "
                                f"{stale_session_id[:8]}... prepended for user {user_id}"
                            )
                except Exception as _hist_err:
                    if sensitive_log_event:
                        logger.warning(
                            "History injection failed for sensitive input event=%s error=%s",
                            sensitive_log_event,
                            type(_hist_err).__name__,
                        )
                    else:
                        logger.warning(
                            f"History injection failed, sending without context: {_hist_err}"
                        )

            enable_streaming_text = next_reply_mode != "voice"
            response = await self._project_chat.process_message(
                user_message=send_text,
                user_id=user_id,
                chat_id=chat.id,
                message_id=message.message_id,
                session_id=effective_sid,
                model=current_session.get("model"),
                effort=current_session.get("effort"),
                approval_policy=self._codex_approval_policy(),
                approvals_reviewer=self._codex_approvals_reviewer(),
                sandbox_policy=self._codex_sandbox_policy(),
                new_session=new_session,
                permission_callback=self._permission_callback,
                approval_callback=self._codex_approval_callback,
                typing_callback=lambda: message.chat.send_action(action="typing"),
                status_callback=self._make_status_callback(app.bot, chat.id),
                bot=app.bot if enable_streaming_text else None,
                notification_bot=app.bot,
                interim_message_callback=(
                    self._make_interim_reply_callback(message)
                    if enable_streaming_text
                    else None
                ),
                sensitive_log_event=sensitive_log_event,
            )
            await self._save_session_id(
                conversation_key,
                response,
                user_id=user_id,
                chat_id=chat.id,
                request_text=send_text,
                turn_marker=f"telegram-message:{message.message_id}",
            )
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
            if sensitive_log_event:
                logger.debug(
                    "Sensitive message processing cancelled event=%s",
                    sensitive_log_event,
                )
            else:
                logger.debug(f"Message processing cancelled for user {user_id}")
            raise
        except Exception as e:
            if sensitive_log_event:
                logger.error(
                    "Sensitive project chat error event=%s error=%s",
                    sensitive_log_event,
                    type(e).__name__,
                )
            else:
                logger.error(f"Error in project chat: {e}", exc_info=True)
            await message.reply_text(
                "❌ Sorry, an error occurred while processing your message.\n"
                f"Error: {str(e)}\n\n"
                "Please try again later."
            )


    # Extensions the bot auto-sends when the agent's reply references a real
    # in-root file. Deliverable document/data/archive/media families; source
    # code and executables are intentionally excluded so an ordinary coding turn
    # ("I edited src/app.py") does not push every touched file to Telegram. A
    # matched path must still pass _resolve_paths' is_file()/size/scope gate
    # before anything is sent, so a false-positive token that is not a real file
    # is harmless.
    _SENDABLE_FILE_EXTENSIONS = (
        # documents
        "pdf", "txt", "md", "markdown", "rtf", "doc", "docx", "odt", "tex", "epub",
        # data / markup
        "csv", "tsv", "json", "jsonl", "ndjson", "xml", "yaml", "yml", "ics", "log",
        # spreadsheets / presentations
        "xls", "xlsx", "ods", "ppt", "pptx", "odp",
        # archives
        "zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar",
        # images
        "png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif", "svg", "heic",
        # audio
        "mp3", "wav", "ogg", "oga", "m4a", "flac", "aac", "opus", "amr",
        # video
        "mp4", "mov", "webm", "mkv", "avi", "m4v",
    )
    # Match both absolute (/foo/bar.pdf) and relative (foo/bar.pdf) file paths.
    # A directory separator is required (reduces prose false-positives), and the
    # trailing (?![A-Za-z0-9]) makes the extension alternation order-independent
    # and stops partial matches (e.g. ".json" is not clipped to ".js").
    _FILE_PATH_RE = re.compile(
        r"(/?(?:[\w.@-]+/)+[\w.@-]+\.(?:"
        + "|".join(_SENDABLE_FILE_EXTENSIONS)
        + r"))(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
