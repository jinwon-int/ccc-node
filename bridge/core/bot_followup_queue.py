import asyncio
import contextvars
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, cast

from telegram import Message, Update
from telegram.ext import Application

from telegram_bot.core.heartbeat import format_duration
from telegram_bot.core.project_chat_state import (
    FollowupNotificationCapacityError,
    FollowupQueueCorruptionError,
    PersistentFollowupQueue,
    QueuedFollowup,
)
from telegram_bot.core.task_queue import UserTaskQueue
from telegram_bot.utils.chat_logger import log_debug

logger = logging.getLogger(__name__)


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


class BotFollowupQueueMixin:
    """Durable per-conversation follow-up admission and worker orchestration."""

    _config: Any
    _project_chat: Any
    _tasks: UserTaskQueue
    application: Optional[Application]
    _require_application: Callable[[], Application]
    _require_message: Callable[[Update], Message]
    _handle_document_message: Callable[[Update, Any], Awaitable[None]]
    _handle_photo_message: Callable[[Update, Any], Awaitable[None]]
    _handle_sticker_message: Callable[[Update, Any], Awaitable[None]]
    _handle_text_message: Callable[[Update, Any], Awaitable[None]]
    _handle_voice_message: Callable[[Update, Any], Awaitable[None]]

    _FOLLOWUP_MAX_ATTEMPTS = 3

    def _initialize_followup_queue(self, settings: Any) -> None:
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
        await cast(Any, super())._on_ready(application)
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
        await cast(Any, super())._do_graceful_stop()
