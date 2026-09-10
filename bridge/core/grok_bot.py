"""Restricted Telegram frontend for one existing Grok Bot and one owner DM.

No generic commands, project prompt decoration, memory/background workers or
session reset paths are installed. This is intentionally a degraded frontend
over the same AgentRuntime contract, not a second implementation of Grok RPC.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import hashlib
import logging
import signal
import socket
from typing import Any

from telegram import Update
from telegram.ext import Application, TypeHandler

from .agent_runtime import CompletionEvent, ErrorEvent, ResultEvent, SessionRequest
from .grok_protocol import MAX_PROMPT, MAX_REPLY, ProtocolError, check_idle
from .grok_provider import configured_route
from .grok_runtime import GrokRuntime

logger = logging.getLogger(__name__)


class _TransportLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.split(".")[0] in {"telegram", "httpx", "httpcore"}:
            record.msg = "Grok Telegram transport diagnostic (details withheld)"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


class GrokTelegramBot:
    def __init__(self, settings: Any, runtime: GrokRuntime, builder: Any = None):
        self.settings = settings
        self.route = configured_route(settings)
        if (not isinstance(runtime, GrokRuntime)
                or runtime.journal.binding != self.route.journal.binding
                or runtime.journal.root != self.route.journal.root):
            raise ProtocolError("grok_runtime_route_mismatch")
        self.runtime = runtime
        self.builder = builder or Application.builder
        self.application: Any = None
        self.ready = False
        self.session: Any = None
        self.active: asyncio.Task[Any] | None = None
        self._generation = 0
        self._closed = False
        self._initializing = False
        self._stop: asyncio.Event | None = None
        self._poller: socket.socket | None = None
        self._seen: set[int] = set()  # bounded process-only update replay guard

    def validate_runtime_paths(self) -> None:
        # No journal opening/initialization before authenticated getMe.
        configured_route(self.settings)

    def _claim_poller(self) -> None:
        # Linux-only like the journal. One local process per existing remote
        # Bot, even with a second journal/Telegram token. Other hosts are not
        # coordinated: deployment still requires a verified single poller.
        binding = self.route.journal.binding
        key = hashlib.sha256((binding.destination + "\n" + binding.agent_id).encode()).hexdigest()
        claim = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            claim.bind("\0ccc-grok-" + key)
        except OSError:
            claim.close()
            raise ProtocolError("grok_local_poller_busy") from None
        self._poller = claim

    async def initialize(self, application: Any) -> None:
        if self._closed or self._initializing or self.ready or self._poller is not None:
            raise ProtocolError("grok_already_initialized")
        self._initializing = True
        generation = self._generation
        try:
            # Recheck after every external wait: shutdown is terminal and a
            # late startup must not reclaim a socket or access protected state.
            identity = await application.bot.get_me()
            self._boot_current(generation)
            if identity.id != self.route.telegram_bot_id or identity.is_bot is not True:
                raise ProtocolError("grok_telegram_identity_mismatch")
            webhook = await application.bot.get_webhook_info()
            self._boot_current(generation)
            if webhook.url:
                raise ProtocolError("grok_existing_webhook_denied")
            self._claim_poller()
            binding = self.route.journal.binding
            self.session = await self.runtime.start_or_resume(
                SessionRequest(binding.working_directory, session_id=binding.session_id))
            self._boot_current(generation)
            health = await self.runtime._call("health")
            self._boot_current(generation)
            check_idle(health, binding.agent_id)
            self.application = application
            self.ready = True
        except BaseException:
            await self.shutdown(application)
            raise
        finally:
            self._initializing = False

    def _boot_current(self, generation: int) -> None:
        if self._closed or generation != self._generation:
            raise ProtocolError("grok_startup_retired")

    def request_shutdown(self) -> None:
        """Synchronous signal/control fence, before any framework drain awaits."""
        self._closed = True
        self.ready = False
        self._generation += 1
        if self.active is not None:
            self.active.cancel()
        if self._stop is not None:
            self._stop.set()

    async def shutdown(self, application: Any) -> None:
        del application
        self.request_shutdown()
        if self.session is not None:
            await self.session.close()
        if self.active is not None and self.active is not asyncio.current_task():
            self.active.cancel()
            await asyncio.gather(self.active, return_exceptions=True)
        if self._poller is not None:
            self._poller.close()
            self._poller = None

    def _admitted(self, update: Update, context: Any) -> bool:
        message, user, chat = update.message, update.effective_user, update.effective_chat
        return bool(
            self.ready and context.bot.id == self.route.telegram_bot_id
            and message is not None and update.edited_message is None
            and update.callback_query is None and user is not None and not user.is_bot
            and user.id == self.route.owner_id and chat is not None
            and chat.type == "private" and chat.id == self.route.owner_id
            and not message.is_topic_message and message.message_thread_id is None
            and message.sender_chat is None and message.forward_origin is None
        )

    async def _reply(self, context: Any, text: str, generation: int) -> None:
        for offset in range(0, len(text), 2000):  # <=4000 UTF16 units
            if not self.ready or generation != self._generation:
                return
            await context.bot.send_message(
                chat_id=self.route.owner_id, text=text[offset:offset + 2000],
                parse_mode=None, disable_web_page_preview=True)

    async def _result(self, text: str) -> str:
        binding = self.route.journal.binding
        self.session = await self.runtime.start_or_resume(
            SessionRequest(binding.working_directory, session_id=binding.session_id))
        result = None
        completed = False
        async for event in self.session.send_turn(text):
            if isinstance(event, ErrorEvent):
                raise ProtocolError("grok_turn_denied_or_uncertain")
            if isinstance(event, ResultEvent):
                if result is not None or completed:
                    raise ProtocolError("grok_invalid_result")
                result = event.result
            if isinstance(event, CompletionEvent):
                if completed or result is None:
                    raise ProtocolError("grok_invalid_completion")
                completed = True
        if (not completed or not isinstance(result, Mapping) or set(result) != {"text"}
                or not isinstance(result["text"], str)
                or not 0 < len(result["text"].encode("utf-8")) <= MAX_REPLY):
            raise ProtocolError("grok_invalid_result")
        return result["text"]

    async def handle(self, update: Update, context: Any) -> None:
        if not self._admitted(update, context):
            return  # no journal/remote access and no disclosure to other rooms
        if update.update_id in self._seen:
            return
        if len(self._seen) >= 1024:
            # Do not evict old update IDs and silently weaken process dedup.
            self.ready = False
            return
        self._seen.add(update.update_id)
        message = update.message
        assert message is not None
        text = message.text
        generation = self._generation
        if text == "/stop":
            self._generation += 1
            if self.session is not None:
                await self.session.interrupt()
            if self.active is not None:
                self.active.cancel()
            await self._reply(context, "로컬 응답 대기를 중단했습니다. Grok 원격 실행 중단은 보장되지 않습니다.", self._generation)
            return
        if text in {"/start", "/status"}:
            await self._reply(context, "지정된 기존 Grok 대화에 연결되어 있습니다. 텍스트만 지원하며 /new·모델 변경·파일·승인은 지원하지 않습니다. /stop은 로컬 대기만 중단합니다.", generation)
            return
        if not isinstance(text, str) or not text or text.startswith("/"):
            await self._reply(context, "이 연결은 일반 텍스트만 지원합니다. 기존 Grok 대화를 초기화하거나 다른 세션을 열 수 없습니다.", generation)
            return
        if len(text.encode("utf-8")) > MAX_PROMPT:
            await self._reply(context, "입력 크기 제한을 초과했습니다.", generation)
            return
        if self.active is not None:
            await self._reply(context, "기존 요청을 처리 중입니다. 이 입력은 전송하지 않았습니다.", generation)
            return
        self.active = asyncio.current_task()
        try:
            result = await self._result(text)
            await self._reply(context, result, generation)
        except asyncio.CancelledError:
            if self.session is not None:
                await self.session.close()
            raise
        except Exception:
            if self.session is not None:
                await self.session.close()
            logger.warning("Grok turn denied, uncertain, or delivery failed; state retained")
            await self._reply(context, "요청 또는 전달 결과를 확정할 수 없습니다. 기록은 보존했습니다. 다시 입력하면 기록과 대조하며, 불확실한 전송은 자동 재전송하지 않습니다.", generation)
        finally:
            self.active = None

    async def error(self, update: object, context: Any) -> None:
        del update, context  # never log Update, token URL, exception or bodies
        logger.warning("Grok Telegram callback failed; no automatic replay")

    async def _serve(self) -> None:
        # Explicit lifecycle avoids run_polling's post_shutdown ordering:
        # Application.stop waits for callbacks, so a late hook cannot cancel
        # those callbacks before they publish results. Retire synchronously.
        loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        previous = {}
        app = self.application
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.getsignal(signum)
                loop.add_signal_handler(signum, self.request_shutdown)
            await app.initialize()
            await self.initialize(app)
            await app.start()
            if not self._closed:
                await app.updater.start_polling(allowed_updates=["message"], drop_pending_updates=False)
            await self._stop.wait()
        finally:
            self.request_shutdown()
            try:
                await self.shutdown(app)
                if app.updater.running:
                    await app.updater.stop()
                if app.running:
                    await app.stop()
                await app.shutdown()
            finally:
                for signum, handler in previous.items():
                    loop.remove_signal_handler(signum)
                    signal.signal(signum, handler)

    def run(self) -> None:
        # Updater/bootstrap exceptions can include HTTP URLs or message data;
        # apply before framework getMe/polling, including DEBUG deployments.
        log_filter = _TransportLogFilter()
        handlers = tuple(logging.getLogger().handlers)
        for handler in handlers:
            handler.addFilter(log_filter)
        try:
            self.application = (self.builder().token(self.settings.telegram_bot_token)
                                .concurrent_updates(8).build())
            self.application.add_handler(TypeHandler(Update, self.handle))
            self.application.add_error_handler(self.error)
            asyncio.run(self._serve())
        except Exception:
            raise ProtocolError("grok_frontend_failed_state_retained") from None
        finally:
            self.request_shutdown()
            if self._poller is not None:
                self._poller.close()
                self._poller = None
            for handler in handlers:
                handler.removeFilter(log_filter)
