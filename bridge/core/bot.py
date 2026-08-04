# ruff: noqa: E402
import asyncio
import contextvars
import hashlib
import json
import logging
import os
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
from telegram_bot.core.project_chat_state import (
    FollowupNotificationCapacityError,
    FollowupQueueCorruptionError,
    PersistentFollowupQueue,
    QueuedFollowup,
)
from telegram_bot.core.heartbeat import format_duration
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


@dataclass(frozen=True, slots=True)
class _FollowupUpdateEnvelope:
    handler: str
    update: Update


_FOLLOWUP_UPDATE: contextvars.ContextVar[_FollowupUpdateEnvelope | None] = (
    contextvars.ContextVar("ccc_bridge_followup_update", default=None)
)
_FOLLOWUP_REPLAY: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "ccc_bridge_followup_replay",
    default=False,
)


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
        self._followup_queue = PersistentFollowupQueue(
            settings.bot_data_dir / "followup-queue.json",
            per_chat_cap=int(getattr(settings, "followup_queue_cap", 32)),
            failure_notification_cap=int(
                getattr(
                    settings,
                    "followup_failure_notification_cap",
                    32,
                )
            ),
        )
        self._followup_admission_locks: Dict[str, asyncio.Lock] = {}
        self._followup_idle_events: Dict[str, asyncio.Event] = {}
        self._followup_live_counts: Dict[str, int] = {}
        self._followup_workers: Dict[str, asyncio.Task[None]] = {}
        self._followup_worker_items: Dict[str, QueuedFollowup] = {}
        self._followup_worker_disabled: set[str] = set()
        self._followup_notice_retry_tasks: Dict[str, asyncio.Task[None]] = {}
        self._followup_disable_notice_tasks: Dict[str, asyncio.Task[None]] = {}
        self._followup_workers_stopping = False
        self._followup_queue_enabled = True
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
    _FOLLOWUP_MAX_ATTEMPTS = 3



    def _conversation_key(self, user_id: int, chat_id: Optional[int] = None) -> Any:
        """Storage/queue key for one Telegram conversation.

        The default isolates each sender/chat pair. Operators may explicitly
        share inside each group or, with the broader shared-all opt-in, across
        every DM and group after access control has accepted the sender.
        """
        cfg = getattr(self, "_config", None)
        scope = getattr(cfg, "telegram_session_scope", "per-user-chat")
        return storage_key(scope, user_id, chat_id)

    @staticmethod
    def _followup_queue_key(conversation_key: Any) -> str:
        """Type-tag a SessionStore key for stable durable queue lookup."""

        if not isinstance(conversation_key, (int, str)):
            raise TypeError("unsupported conversation queue key")
        return json.dumps(
            [type(conversation_key).__name__, conversation_key],
            ensure_ascii=True,
            separators=(",", ":"),
        )

    def _followup_idle_event(self, queue_key: str) -> asyncio.Event:
        event = self._followup_idle_events.get(queue_key)
        if event is None:
            event = asyncio.Event()
            if self._followup_live_counts.get(queue_key, 0) == 0:
                event.set()
            self._followup_idle_events[queue_key] = event
        return event

    async def _with_followup_update(
        self,
        *,
        handler: str,
        update: Update,
        context: Any,
        callback: Any,
    ) -> None:
        token = _FOLLOWUP_UPDATE.set(_FollowupUpdateEnvelope(handler, update))
        try:
            await callback(update, context)
        finally:
            _FOLLOWUP_UPDATE.reset(token)

    async def _handle_followup_text_update(self, update: Update, context: Any) -> None:
        await self._with_followup_update(
            handler="text",
            update=update,
            context=context,
            callback=self._handle_text_message,
        )

    async def _handle_followup_voice_update(self, update: Update, context: Any) -> None:
        await self._with_followup_update(
            handler="voice",
            update=update,
            context=context,
            callback=self._handle_voice_message,
        )

    async def _handle_followup_photo_update(self, update: Update, context: Any) -> None:
        await self._with_followup_update(
            handler="photo",
            update=update,
            context=context,
            callback=self._handle_photo_message,
        )

    async def _handle_followup_sticker_update(self, update: Update, context: Any) -> None:
        await self._with_followup_update(
            handler="sticker",
            update=update,
            context=context,
            callback=self._handle_sticker_message,
        )

    async def _handle_followup_document_update(
        self, update: Update, context: Any
    ) -> None:
        await self._with_followup_update(
            handler="document",
            update=update,
            context=context,
            callback=self._handle_document_message,
        )

    @staticmethod
    def _serialize_followup_update(update: Update) -> str:
        serialized = update.to_json()
        decoded = json.loads(serialized)
        if not isinstance(decoded, dict):
            raise ValueError("Telegram update serialization is not an object")
        return json.dumps(decoded, ensure_ascii=False, separators=(",", ":"))

    async def _reply_followup_status(
        self,
        update: Update,
        text: str,
        *,
        user_id: int | None = None,
    ) -> None:
        message = self._require_message(update)
        try:
            await message.reply_text(text)
        except Exception:
            logger.warning(
                "Follow-up queue status delivery failed for user %s",
                user_id if user_id is not None else "unknown",
                exc_info=True,
            )
        else:
            if user_id is not None:
                log_debug(user_id, "bot", text)

    def _followup_busy_seconds(self, update: Update) -> float | None:
        user = update.effective_user
        chat = update.effective_chat
        probe = getattr(self._project_chat, "busy_for_seconds", None)
        if user is None or chat is None or not callable(probe):
            return None
        try:
            return probe(
                user.id,
                chat.id,
                asyncio.get_running_loop().time(),
            )
        except Exception:
            logger.warning("Follow-up busy probe failed", exc_info=True)
            return None

    async def _persist_followup(
        self,
        *,
        queue_key: str,
        envelope: _FollowupUpdateEnvelope,
        busy_seconds: float | None,
    ) -> bool:
        user = envelope.update.effective_user
        user_id = user.id if user is not None else None
        if not self._followup_queue_enabled:
            await self._reply_followup_status(
                envelope.update,
                "❌ The saved follow-up queue is unavailable, so this message "
                "was not queued. Please contact the operator.",
                user_id=user_id,
            )
            return False
        if queue_key in self._followup_worker_disabled:
            await self._reply_followup_status(
                envelope.update,
                "❌ Saved follow-up processing is paused for this conversation, "
                "so this message was not queued. Please contact the operator.",
                user_id=user_id,
            )
            return False
        cap = self._followup_queue.per_chat_cap
        try:
            update_json = self._serialize_followup_update(envelope.update)
            queued, position = self._followup_queue.enqueue(
                conversation_key=queue_key,
                handler=envelope.handler,
                update_json=update_json,
                enqueued_at=time.time(),
            )
        except Exception:
            logger.exception("Follow-up update could not be persisted")
            reply = (
                "❌ I could not save this follow-up, so it was not queued. "
                "Please retry after the current work finishes."
            )
            await self._reply_followup_status(
                envelope.update,
                reply,
                user_id=user_id,
            )
            return False

        if queued is None:
            reply = (
                f"⚠️ Follow-up queue is full ({cap} messages). "
                "This message was not queued; please retry after earlier work "
                "finishes or send /stop to clear the queue."
            )
            await self._reply_followup_status(
                envelope.update,
                reply,
                user_id=user_id,
            )
            return False

        self._start_followup_worker(queue_key)
        threshold = float(
            getattr(self._config, "busy_notice_min_elapsed_seconds", 10.0)
        )
        notice_enabled = bool(
            getattr(self._config, "busy_notice_enabled", True)
        )
        if (
            notice_enabled
            and busy_seconds is not None
            and busy_seconds >= threshold
        ):
            reply = (
                "⏳ Still working on the previous message "
                f"({format_duration(busy_seconds)} elapsed). "
                f"This message is saved in queue position {position} and will "
                "be handled in arrival order."
            )
        else:
            reply = (
                f"⏳ This message is saved in queue position {position} and "
                "will be handled in arrival order."
            )
        await self._reply_followup_status(
            envelope.update,
            reply,
            user_id=user_id,
        )
        return True

    def _mark_followup_live(self, queue_key: str) -> None:
        self._followup_live_counts[queue_key] = (
            self._followup_live_counts.get(queue_key, 0) + 1
        )
        self._followup_idle_event(queue_key).clear()

    def _unmark_followup_live(self, queue_key: str) -> None:
        remaining = self._followup_live_counts.get(queue_key, 0) - 1
        if remaining > 0:
            self._followup_live_counts[queue_key] = remaining
            return
        self._followup_live_counts.pop(queue_key, None)
        self._followup_idle_event(queue_key).set()
        self._start_followup_worker(queue_key)

    async def _enqueue_user_task(
        self,
        user_id: Any,
        run_task: Any,
        on_overflow: Any,
    ) -> bool:
        """Persist ordinary follow-ups before the legacy in-memory run queue."""

        if _FOLLOWUP_REPLAY.get():
            await run_task()
            return True

        queue_key = self._followup_queue_key(user_id)
        admission_lock = self._followup_admission_locks.setdefault(
            queue_key, asyncio.Lock()
        )
        envelope = _FOLLOWUP_UPDATE.get()
        async with admission_lock:
            busy_seconds = (
                self._followup_busy_seconds(envelope.update)
                if envelope is not None
                else None
            )
            try:
                durable_depth = (
                    self._followup_queue.depth(queue_key)
                    if self._followup_queue_enabled
                    else 0
                )
            except FollowupQueueCorruptionError:
                if envelope is None:
                    raise
                logger.exception("Follow-up queue validation failed")
                await self._reply_followup_status(
                    envelope.update,
                    "❌ I could not verify the saved follow-up queue, so this "
                    "message was not queued. Please contact the operator.",
                    user_id=(
                        envelope.update.effective_user.id
                        if envelope.update.effective_user is not None
                        else None
                    ),
                )
                return False

            occupied = (
                self._followup_live_counts.get(queue_key, 0) > 0
                or durable_depth > 0
                or busy_seconds is not None
            )
            if (
                envelope is not None
                and occupied
                and self._followup_queue_enabled
            ):
                return await self._persist_followup(
                    queue_key=queue_key,
                    envelope=envelope,
                    busy_seconds=busy_seconds,
                )

            self._mark_followup_live(queue_key)

            async def tracked_run_task() -> None:
                try:
                    await run_task()
                finally:
                    self._unmark_followup_live(queue_key)

            accepted = await self._tasks.enqueue(
                user_id,
                tracked_run_task,
                on_overflow,
            )
            if not accepted:
                self._unmark_followup_live(queue_key)
            return accepted

    async def _dispatch_queued_followup(self, item: QueuedFollowup) -> None:
        payload = json.loads(item.update_json)
        message_payload = payload.get("message")
        if isinstance(message_payload, dict):
            # The stale-update check protects the polling boundary. This update
            # was already accepted and durably owned by the bridge, so refresh
            # only its transport timestamp before running every live gate again.
            message_payload["date"] = int(time.time())
        app = self._require_application()
        update = Update.de_json(payload, app.bot)
        if update is None:
            raise FollowupQueueCorruptionError(
                "Telegram deserializer returned no durable follow-up update"
            )
        handlers = {
            "document": self._handle_document_message,
            "photo": self._handle_photo_message,
            "sticker": self._handle_sticker_message,
            "text": self._handle_text_message,
            "voice": self._handle_voice_message,
        }
        callback = handlers.get(item.handler)
        if callback is None:
            raise FollowupQueueCorruptionError(
                "durable follow-up queue handler is invalid"
            )
        replay_token = _FOLLOWUP_REPLAY.set(True)
        try:
            # Re-enter the original handler: access, approval, pending-question,
            # and owner gates are evaluated against current state.
            await callback(update, None)
        finally:
            _FOLLOWUP_REPLAY.reset(replay_token)

    def _followup_retry_delays(self) -> tuple[float, ...]:
        configured = getattr(
            self._config,
            "followup_retry_backoff_seconds",
            (1.0, 5.0, 30.0),
        )
        return tuple(float(delay) for delay in configured)

    @staticmethod
    def _followup_notice_kwargs(
        item: QueuedFollowup, text: str
    ) -> dict[str, Any]:
        payload = json.loads(item.update_json)
        message_payload = payload.get("message")
        if not isinstance(message_payload, dict):
            raise ValueError("queued update has no message")
        chat_payload = message_payload.get("chat")
        if not isinstance(chat_payload, dict) or "id" not in chat_payload:
            raise ValueError("queued update has no chat")
        send_kwargs: dict[str, Any] = {
            "chat_id": chat_payload["id"],
            "text": text,
        }
        message_id = message_payload.get("message_id")
        if isinstance(message_id, int):
            send_kwargs["reply_to_message_id"] = message_id
        return send_kwargs

    async def _notify_failed_followup(self, item: QueuedFollowup) -> bool:
        """Try once to tell the sender that one durable item was discarded."""

        text = (
            f"❌ This saved {item.handler} follow-up could not be processed "
            f"after {self._FOLLOWUP_MAX_ATTEMPTS} attempts and was removed "
            "from the queue. Later follow-ups will continue."
        )
        try:
            await self._require_application().bot.send_message(
                **self._followup_notice_kwargs(item, text)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "Failed to notify sender about discarded durable follow-up %s",
                item.item_id,
                exc_info=True,
            )
            return False
        return True

    async def _notify_followup_worker_disabled(
        self, queue_key: str, item: QueuedFollowup | None
    ) -> bool:
        if item is None:
            logger.critical(
                "Cannot notify the sender for disabled durable follow-up "
                "worker %s because no queued update could be read",
                queue_key,
            )
            return False
        text = (
            "⚠️ Saved follow-up processing is paused for this conversation "
            "after repeated worker or queue-storage failures. Your queued "
            "message was retained; please contact the operator."
        )
        try:
            await self._require_application().bot.send_message(
                **self._followup_notice_kwargs(item, text)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "Failed to notify sender that durable follow-up worker %s "
                "was disabled",
                queue_key,
                exc_info=True,
            )
            return False
        return True

    def _followup_notice_retry_delay(self) -> float:
        delays = self._followup_retry_delays()
        return delays[-1] if delays else 30.0

    def _schedule_followup_worker_disabled_notice_retry(
        self,
        queue_key: str,
        item: QueuedFollowup,
    ) -> None:
        """Retry one failed pause notice once, without reviving the worker."""

        current = self._followup_disable_notice_tasks.get(queue_key)
        if current is not None and not current.done():
            return

        async def retry() -> None:
            try:
                await asyncio.sleep(self._followup_notice_retry_delay())
                if (
                    self._followup_workers_stopping
                    or queue_key not in self._followup_worker_disabled
                ):
                    return
                if not await self._notify_followup_worker_disabled(
                    queue_key,
                    item,
                ):
                    logger.critical(
                        "Durable follow-up worker %s pause notification "
                        "still failed after one paced retry",
                        queue_key,
                    )
            finally:
                task = asyncio.current_task()
                if self._followup_disable_notice_tasks.get(queue_key) is task:
                    self._followup_disable_notice_tasks.pop(queue_key, None)

        self._followup_disable_notice_tasks[queue_key] = asyncio.create_task(
            retry(),
            name=f"followup-disable-notice:{queue_key}",
        )

    async def _disable_followup_worker(
        self,
        queue_key: str,
        *,
        reason: str,
        item: QueuedFollowup | None = None,
    ) -> None:
        if queue_key in self._followup_worker_disabled:
            return
        self._followup_worker_disabled.add(queue_key)
        logger.critical(
            "DURABLE FOLLOW-UP WORKER DISABLED for %s: %s; queued items retained",
            queue_key,
            reason,
            exc_info=True,
        )
        candidate = item or self._followup_worker_items.get(queue_key)
        if candidate is None:
            try:
                candidate = self._followup_queue.peek(queue_key)
            except Exception:
                logger.exception(
                    "Failed to read queued update while disabling worker %s",
                    queue_key,
                )
        notified = await self._notify_followup_worker_disabled(
            queue_key,
            candidate,
        )
        if not notified and candidate is not None:
            self._schedule_followup_worker_disabled_notice_retry(
                queue_key,
                candidate,
            )

    async def _deliver_failed_followup_notification(
        self, item: QueuedFollowup
    ) -> bool:
        """Retry one durable outcome notice and retain it until delivered."""

        delays = self._followup_retry_delays()
        for attempt in range(len(delays) + 1):
            if await self._notify_failed_followup(item):
                try:
                    acknowledged = (
                        self._followup_queue.acknowledge_failure_notification(
                            item.item_id
                        )
                    )
                except OSError:
                    await self._disable_followup_worker(
                        item.conversation_key,
                        reason=(
                            "failure-notification acknowledgement could not "
                            "be persisted"
                        ),
                        item=item,
                    )
                    return False
                if not acknowledged:
                    logger.error(
                        "Delivered durable follow-up failure notification %s "
                        "disappeared before acknowledgement",
                        item.item_id,
                    )
                return True
            if attempt < len(delays):
                await asyncio.sleep(delays[attempt])
        logger.critical(
            "Durable follow-up failure notification %s remains pending after "
            "%s delivery attempts",
            item.item_id,
            len(delays) + 1,
        )
        return False

    async def _deliver_pending_followup_notifications(
        self, queue_key: str
    ) -> bool:
        while not self._followup_workers_stopping:
            item = self._followup_queue.peek_failure_notification(queue_key)
            if item is None:
                return True
            if not await self._deliver_failed_followup_notification(item):
                return queue_key not in self._followup_worker_disabled
        return False

    async def _discard_failed_followup(self, item: QueuedFollowup) -> bool:
        logger.error(
            "Discarding durable follow-up %s after %s failed attempts",
            item.item_id,
            item.retry_count,
        )
        try:
            notification = self._followup_queue.stage_failure_notification(
                item.item_id
            )
            acknowledged = self._followup_queue.acknowledge(item.item_id)
        except FollowupNotificationCapacityError:
            await self._disable_followup_worker(
                item.conversation_key,
                reason=(
                    "failure-notification cap reached; newest failed item "
                    "and all earlier promised receipts retained"
                ),
                item=item,
            )
            return False
        except OSError:
            await self._disable_followup_worker(
                item.conversation_key,
                reason="discard acknowledgement could not be persisted",
                item=item,
            )
            return False
        if notification is None:
            logger.error(
                "Failed durable follow-up %s disappeared before discard",
                item.item_id,
            )
            return True
        if not acknowledged:
            logger.error(
                "Failed durable follow-up %s disappeared before discard "
                "acknowledgement",
                item.item_id,
            )
        # Removal is durable before any user-visible discard notification. If
        # Telegram remains unavailable, the staged notice survives for startup
        # or a later conversation worker to surface.
        await self._deliver_failed_followup_notification(notification)
        return item.conversation_key not in self._followup_worker_disabled

    async def _run_followup_worker(self, queue_key: str) -> None:
        if not await self._deliver_pending_followup_notifications(queue_key):
            return
        while not self._followup_workers_stopping:
            await self._followup_idle_event(queue_key).wait()
            item = self._followup_queue.peek(queue_key)
            if item is None:
                self._followup_worker_items.pop(queue_key, None)
                return
            self._followup_worker_items[queue_key] = item
            if item.retry_count >= self._FOLLOWUP_MAX_ATTEMPTS:
                if not await self._discard_failed_followup(item):
                    return
                continue
            try:
                # Deliberately do not impose a dispatch timeout. Legitimate
                # agent turns can be long, and cancelling one risks concurrent
                # replay if the provider ignores cancellation. /stop remains
                # the explicit operator/user escape hatch for a hung turn.
                await self._dispatch_queued_followup(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                try:
                    failed = self._followup_queue.record_failure(item.item_id)
                except OSError:
                    await self._disable_followup_worker(
                        queue_key,
                        reason="dispatch failure count could not be persisted",
                        item=item,
                    )
                    return
                if failed is None:
                    logger.error(
                        "Failed durable follow-up %s disappeared before retry",
                        item.item_id,
                        exc_info=True,
                    )
                    continue
                logger.warning(
                    "Durable follow-up %s failed attempt %s/%s",
                    failed.item_id,
                    failed.retry_count,
                    self._FOLLOWUP_MAX_ATTEMPTS,
                    exc_info=True,
                )
                if failed.retry_count >= self._FOLLOWUP_MAX_ATTEMPTS:
                    if not await self._discard_failed_followup(failed):
                        return
                else:
                    delays = self._followup_retry_delays()
                    delay = delays[min(failed.retry_count - 1, len(delays) - 1)]
                    await asyncio.sleep(delay)
                continue
            try:
                acknowledged = self._followup_queue.acknowledge(item.item_id)
            except OSError:
                await self._disable_followup_worker(
                    queue_key,
                    reason="processed follow-up acknowledgement could not be persisted",
                    item=item,
                )
                return
            if not acknowledged:
                raise FollowupQueueCorruptionError(
                    "processed follow-up disappeared before acknowledgement"
                )
            self._followup_worker_items.pop(queue_key, None)

    async def _supervise_followup_worker(self, queue_key: str) -> None:
        restart_cap = int(
            getattr(self._config, "followup_worker_restart_cap", 3)
        )
        base_delay = float(
            getattr(
                self._config,
                "followup_worker_restart_backoff_seconds",
                1.0,
            )
        )
        same_head_restarts = 0
        last_failed_head_id: int | None = None
        next_delay = base_delay
        while not self._followup_workers_stopping:
            try:
                await self._run_followup_worker(queue_key)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                failed_item = self._followup_worker_items.get(queue_key)
                failed_head_id = (
                    failed_item.item_id if failed_item is not None else None
                )
                made_progress = (
                    last_failed_head_id is not None
                    and failed_head_id is not None
                    and failed_head_id != last_failed_head_id
                )
                if made_progress:
                    # Progress is a durable head-ID change between unexpected
                    # failures. It resets only the disable threshold. Backoff
                    # remains independent so an error recurring on every item
                    # cannot oscillate at the base delay forever.
                    same_head_restarts = 0
                if same_head_restarts >= restart_cap:
                    await self._disable_followup_worker(
                        queue_key,
                        reason=(
                            "same-head worker restart cap "
                            f"({restart_cap}) exceeded at item "
                            f"{failed_head_id}"
                        ),
                    )
                    return
                delay = min(next_delay, 300.0)
                next_delay = min(max(base_delay, delay * 2.0), 300.0)
                same_head_restarts += 1
                last_failed_head_id = failed_head_id
                logger.exception(
                    "Durable follow-up worker %s failed at item %s; "
                    "same-head restart %s/%s in %.2fs%s",
                    queue_key,
                    failed_head_id,
                    same_head_restarts,
                    restart_cap,
                    delay,
                    " after durable head progress" if made_progress else "",
                )
                await asyncio.sleep(delay)

    def _cancel_followup_notice_retry(
        self, queue_key: str
    ) -> asyncio.Task[None] | None:
        task = self._followup_notice_retry_tasks.pop(queue_key, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        return task

    def _schedule_followup_notice_retry(self, queue_key: str) -> None:
        """Start a notice-only worker after a wall-clock pacing delay."""

        if (
            self._followup_workers_stopping
            or queue_key in self._followup_worker_disabled
        ):
            return
        current = self._followup_notice_retry_tasks.get(queue_key)
        if current is not None and not current.done():
            return

        async def retry() -> None:
            try:
                await asyncio.sleep(self._followup_notice_retry_delay())
                if not self._followup_workers_stopping:
                    self._start_followup_worker(queue_key)
            finally:
                task = asyncio.current_task()
                if self._followup_notice_retry_tasks.get(queue_key) is task:
                    self._followup_notice_retry_tasks.pop(queue_key, None)

        self._followup_notice_retry_tasks[queue_key] = asyncio.create_task(
            retry(),
            name=f"followup-notice-retry:{queue_key}",
        )

    def _start_followup_worker(self, queue_key: str) -> None:
        if (
            self._followup_workers_stopping
            or not self._followup_queue_enabled
            or queue_key in self._followup_worker_disabled
        ):
            return
        current = self._followup_workers.get(queue_key)
        if current is not None and not current.done():
            return
        self._cancel_followup_notice_retry(queue_key)
        task = asyncio.create_task(
            self._supervise_followup_worker(queue_key),
            name=f"followup-queue:{queue_key}",
        )
        self._followup_workers[queue_key] = task

        def done(completed: asyncio.Task[None]) -> None:
            if self._followup_workers.get(queue_key) is completed:
                self._followup_workers.pop(queue_key, None)
            try:
                completed.result()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.critical(
                    "Durable follow-up supervisor stopped unexpectedly; "
                    "queued item retained",
                    exc_info=True,
                )
                return
            if self._followup_workers_stopping:
                return
            try:
                pending = self._followup_queue.peek(queue_key) is not None
                pending_notice = (
                    self._followup_queue.peek_failure_notification(queue_key)
                    is not None
                )
            except Exception:
                logger.exception("Follow-up queue recheck failed")
                return
            if pending:
                self._start_followup_worker(queue_key)
            elif pending_notice:
                # Never restart a notice-only worker inline here. A Telegram
                # outage makes delivery return normally with the receipt still
                # retained; immediate restart at this trap site creates an
                # unpaced send storm.
                self._schedule_followup_notice_retry(queue_key)

        task.add_done_callback(done)

    async def _stop_followup_workers(self) -> None:
        self._followup_workers_stopping = True
        workers = tuple(self._followup_workers.values())
        auxiliary_tasks = tuple(self._followup_notice_retry_tasks.values()) + tuple(
            self._followup_disable_notice_tasks.values()
        )
        for task in workers + auxiliary_tasks:
            task.cancel()
        if workers or auxiliary_tasks:
            await asyncio.gather(
                *workers,
                *auxiliary_tasks,
                return_exceptions=True,
            )
        self._followup_workers.clear()
        self._followup_worker_items.clear()
        self._followup_notice_retry_tasks.clear()
        self._followup_disable_notice_tasks.clear()

    async def _clear_user_queue(self, user_id: Any) -> tuple[int, int, int]:
        """Clear volatile work, durable work, and retained outcome receipts."""

        volatile_cleared = self._tasks.clear(user_id)
        queue_key = self._followup_queue_key(user_id)
        durable_items_cleared = 0
        failure_notifications_cleared = 0
        if self._followup_queue_enabled:
            result = self._followup_queue.clear(queue_key)
            durable_items_cleared = result.queued_items
            failure_notifications_cleared = result.failure_notifications
        worker = self._followup_workers.get(queue_key)
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        notice_retry = self._cancel_followup_notice_retry(queue_key)
        disable_notice_retry = self._followup_disable_notice_tasks.pop(
            queue_key,
            None,
        )
        if disable_notice_retry is not None:
            disable_notice_retry.cancel()
        background = tuple(
            task
            for task in (notice_retry, disable_notice_retry)
            if task is not None
        )
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        if self._followup_workers.get(queue_key) is worker:
            self._followup_workers.pop(queue_key, None)
        self._followup_worker_items.pop(queue_key, None)
        self._followup_worker_disabled.discard(queue_key)
        if self._followup_queue_enabled:
            try:
                pending = self._followup_queue.peek(queue_key) is not None
            except Exception:
                logger.exception("Follow-up queue recheck after /stop failed")
            else:
                if pending:
                    self._start_followup_worker(queue_key)
        return (
            volatile_cleared,
            durable_items_cleared,
            failure_notifications_cleared,
        )

    async def _on_ready(self, application: Application) -> None:
        await super()._on_ready(application)
        self._followup_workers_stopping = False
        self._followup_worker_disabled.clear()
        self._followup_worker_items.clear()
        self._followup_notice_retry_tasks.clear()
        self._followup_disable_notice_tasks.clear()
        try:
            self._followup_queue.initialize()
            queue_keys = self._followup_queue.conversation_keys()
        except Exception:
            self._followup_queue_enabled = False
            logger.critical(
                "DURABLE FOLLOW-UP QUEUE DISABLED: startup validation failed; "
                "the original queue file is preserved at %s and the bridge "
                "will continue without durable follow-up queuing",
                self._followup_queue.path,
                exc_info=True,
            )
            return
        self._followup_queue_enabled = True
        for queue_key in queue_keys:
            self._start_followup_worker(queue_key)

    async def _do_graceful_stop(self) -> None:
        await self._stop_followup_workers()
        await super()._do_graceful_stop()

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
        if provider not in {"claude", "codex", "crush", "piri"}:
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
        """Align provider and memory route before reusing a persisted session.

        The deny/invalidate pair is security-relevant: pending and previously
        granted Codex approvals belong to the departing provider/route and must
        not survive into the new one.  A session created before audience-scoped
        mode has no trustworthy route label, so it is reset instead of being
        resumed under whichever Telegram surface happens to reference it.
        """
        aligned, switched = await self._align_active_provider(
            session_key,
            session,
            user_id=user_id,
            chat_id=chat_id,
        )
        route_reset = False
        from telegram_bot.core.memory_audience import resolve_memory_audience

        audience = resolve_memory_audience(
            self._config,
            user_id=user_id,
            chat_id=chat_id,
        )
        if audience is not None and aligned.get("session_id"):
            stored_route = (
                aligned.get("distill_memory_audience"),
                aligned.get("distill_memory_scope"),
            )
            expected_route = (audience.kind, audience.scope)
            if stored_route != expected_route:
                await self._session_manager.patch_session(
                    session_key,
                    updates={"session_id": None, "new_session": False},
                    remove_fields={
                        "resume_list",
                        "distill_memory_audience",
                        "distill_memory_scope",
                    },
                )
                aligned.update(session_id=None, new_session=False)
                aligned.pop("resume_list", None)
                aligned.pop("distill_memory_audience", None)
                aligned.pop("distill_memory_scope", None)
                route_reset = True
                logger.warning(
                    "Reset persisted %s session without a matching memory route",
                    aligned.get("provider", self._active_provider()),
                )
        if switched or route_reset:
            self._deny_codex_approvals(user_id, chat_id)
            self._invalidate_codex_approvals(user_id, chat_id)
            self._runtime_active_sessions.discard(session_key)
        return aligned, switched or route_reset

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
        if self._active_provider() in {"codex", "piri"}:
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
        provider_label = {
            "claude": "Claude Code",
            "codex": "Codex",
            "crush": "Crush",
            "piri": "Piri",
        }.get(provider, provider.title())
        # Banner model label: explicit /model choice first, then the operator
        # display label, then the env-routed model (Claude path only), so the
        # notice reflects the real backend instead of a bare "default".
        display_model = model or os.environ.get("CCC_MODEL_LABEL", "").strip()
        if not display_model and provider == "claude":
            display_model = os.environ.get("ANTHROPIC_MODEL", "").strip()
        if not display_model and provider == "crush":
            display_model = os.environ.get("CCC_CRUSH_MODEL", "").strip()
        if not display_model:
            display_model = "default"
        lines = [
            f"◐ CCC session started ({reason}). Conversation history is on a fresh {provider_label} stream.",
            "Use /resume to browse and restore a previous session.",
            "",
            f"◆ Model: {display_model}",
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
        self.application.add_handler(CommandHandler("waits", self._cmd_waits))
        self.application.add_handler(CommandHandler("cancelwait", self._cmd_cancelwait))
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
        self.application.add_handler(CommandHandler("restart", self._cmd_restart))
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
            MessageHandler(filters.VOICE, self._handle_followup_voice_update),
            group=2,
        )
        self.application.add_handler(
            MessageHandler(
                filters.PHOTO | filters.Document.IMAGE,
                self._handle_followup_photo_update,
            ),
            group=2,
        )
        self.application.add_handler(
            MessageHandler(
                filters.Sticker.ALL,
                self._handle_followup_sticker_update,
            ),
            group=2,
        )
        self.application.add_handler(
            MessageHandler(
                filters.Document.ALL & ~filters.Document.IMAGE,
                self._handle_followup_document_update,
            ),
            group=2,
        )
        self.application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._handle_followup_text_update,
            ),
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
        busy_probe = getattr(self._project_chat, "busy_for_seconds", None)
        if bool(getattr(self._config, "busy_notice_enabled", True)) and callable(
            busy_probe
        ):
            busy_seconds = busy_probe(
                user_id,
                chat.id,
                asyncio.get_running_loop().time(),
            )
            threshold = float(
                getattr(self._config, "busy_notice_min_elapsed_seconds", 10.0)
            )
            if busy_seconds is not None and busy_seconds >= threshold:
                reply = (
                    "⏳ Still working on the previous message "
                    f"({format_duration(busy_seconds)} elapsed). "
                    "I will handle this message after it finishes."
                )
                try:
                    await message.reply_text(reply)
                except Exception:
                    logger.warning(
                        "Busy notice delivery failed for user %s",
                        user_id,
                        exc_info=True,
                    )
                else:
                    log_debug(user_id, "bot", reply)

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
