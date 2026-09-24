"""Restricted Matrix frontend for one existing Grok Bot and one owner direct room.

Same deliberately degraded contract as the Telegram frontend (``grok_bot``):
no generic commands, prompt decoration, memory/background workers or session
reset; text only; one turn at a time; only the configured owner, only in a
direct room. The E2EE transport, room gate and event admission come from
``core.matrix.transport`` unchanged — this class is its ``TurnRunner`` and owns
the same Grok startup gates (single local frontend, persisted journal, host
version, idle Bot) the Telegram frontend runs before polling.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import signal
import socket
from typing import Any, Callable

from .agent_runtime import CompletionEvent, ErrorEvent, ResultEvent, SessionRequest
from .grok_protocol import MAX_PROMPT, MAX_REPLY, ProtocolError, check_idle
from .grok_provider import configured_route
from .grok_runtime import GrokRuntime

logger = logging.getLogger(__name__)

TransportFactory = Callable[[Mapping[str, Any], Any], Any]

STATUS_TEXT = (
    "지정된 기존 Grok 대화에 연결되어 있습니다. 텍스트만 지원하며 /new·모델 변경·파일·승인은 "
    "지원하지 않습니다. /stop은 로컬 대기만 중단합니다."
)
TEXT_ONLY = "이 연결은 일반 텍스트만 지원합니다. 기존 Grok 대화를 초기화하거나 다른 세션을 열 수 없습니다."
TOO_LARGE = "입력 크기 제한을 초과했습니다."
BUSY = "기존 요청을 처리 중입니다. 이 입력은 전송하지 않았습니다."
UNCERTAIN = (
    "요청 또는 전달 결과를 확정할 수 없습니다. 기록은 보존했습니다. 다시 입력하면 기록과 대조하며, "
    "불확실한 전송은 자동 재전송하지 않습니다."
)
NOT_ADMITTED = "이 연결은 지정된 방의 허용된 구성원에게만 응답합니다."
BANNER = "Grok Matrix 연결을 시작했습니다. " + STATUS_TEXT
BANNER_KEY = "grok-matrix-startup-banner"

_WITHHELD = "Grok Matrix transport diagnostic (details withheld)"


class _TransportLogFilter(logging.Filter):
    """Matrix/HTTP client records may carry access tokens or bodies; withhold them."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.split(".")[0] in {"nio", "aiohttp", "httpx", "httpcore"}:
            record.msg = _WITHHELD
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


@dataclass(frozen=True)
class _TurnResult:
    """Mirror of ``matrix.transport.TurnResult`` (same field order/defaults)."""

    text: str
    session_id: str | None
    streamed: bool = False
    status: str = "complete"


def _turn_result(text: str) -> Any:
    try:
        from telegram_bot.core.matrix.transport import TurnResult
    except ImportError:
        return _TurnResult(text=text, session_id=None)
    return TurnResult(text=text, session_id=None)


class GrokMatrixBot:
    def __init__(self, settings: Any, runtime: GrokRuntime, transport_factory: TransportFactory | None = None):
        self.settings = settings
        self.route = configured_route(settings)
        if (not isinstance(runtime, GrokRuntime)
                or runtime.journal.binding != self.route.journal.binding
                or runtime.journal.root != self.route.journal.root):
            raise ProtocolError("grok_runtime_route_mismatch")
        self.runtime = runtime
        self._transport_factory = transport_factory
        self._config: Mapping[str, Any] | None = None
        self._transport: Any = None
        self.ready = False
        self.session: Any = None
        self.active: asyncio.Task[Any] | None = None
        self._generation = 0
        self._closed = False
        self._poller: socket.socket | None = None

    # -- configuration -------------------------------------------------------

    def config_path(self) -> Path:
        raw = getattr(self.settings, "matrix_config_path", None)
        if not raw:
            raise ProtocolError("grok_matrix_config_required")
        return Path(str(raw)).expanduser()

    def load_config(self) -> Mapping[str, Any]:
        if self._config is None:
            from telegram_bot.core.matrix.state import load_config

            config = load_config(self.config_path())
            # The Grok journal binds exactly one owner conversation. Family
            # rooms/users admit other senders into that conversation, so they
            # are refused before the transport opens unless the operator opted
            # in explicitly (CCC_GROK_MATRIX_FAMILY_ROOMS=1).
            if (config.get("family_rooms") or config.get("family_users")) and not self.family_rooms_enabled:
                raise ProtocolError("grok_matrix_direct_room_only")
            self._config = config
        return self._config

    @property
    def family_rooms_enabled(self) -> bool:
        return bool(getattr(self.settings, "grok_matrix_family_rooms", False))

    @property
    def owner(self) -> str:
        return str(self.load_config()["owner"])

    def validate_runtime_paths(self) -> None:
        # No journal opening/initialization before the transport authenticated.
        configured_route(self.settings)
        if not self.config_path().is_file():
            raise ProtocolError("grok_matrix_config_missing")

    # -- lifecycle -----------------------------------------------------------

    @property
    def runner(self) -> "_GrokMatrixTurnRunner":
        return _GrokMatrixTurnRunner(self)

    def _build_transport(self, config: Mapping[str, Any]) -> Any:
        if self._transport_factory is not None:
            return self._transport_factory(config, self.runner)
        from telegram_bot.core.matrix.transport import MatrixTransport

        return MatrixTransport(dict(config), self.runner)

    def _claim_poller(self) -> None:
        # Same key as the Telegram frontend: one local frontend per existing
        # remote Bot, whichever channel it serves. Other hosts are not
        # coordinated: deployment still requires a verified single frontend.
        binding = self.route.journal.binding
        key = hashlib.sha256((binding.destination + "\n" + binding.agent_id).encode()).hexdigest()
        claim = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            claim.bind("\0ccc-grok-" + key)
        except OSError:
            claim.close()
            raise ProtocolError("grok_local_poller_busy") from None
        self._poller = claim

    def _boot_current(self, generation: int) -> None:
        if self._closed or generation != self._generation:
            raise ProtocolError("grok_startup_retired")

    async def _attach(self) -> None:
        """Grok startup gates, after the Matrix device authenticated and synced."""
        if self._closed or self.ready or self._poller is not None:
            raise ProtocolError("grok_already_initialized")
        generation = self._generation
        self._claim_poller()
        binding = self.route.journal.binding
        self.session = await self.runtime.start_or_resume(
            SessionRequest(binding.working_directory, session_id=binding.session_id))
        self._boot_current(generation)
        health = await self.runtime._call("health")
        self._boot_current(generation)
        check_idle(health, binding.agent_id)
        self.ready = True

    def _post_banner(self, config: Mapping[str, Any], transport: Any) -> None:
        if not getattr(self.settings, "matrix_startup_banner", True):
            return
        for room in config.get("rooms") or ():
            try:
                transport.enqueue_notice(str(room), BANNER, key=BANNER_KEY)
            except Exception:
                logger.warning("Grok Matrix startup banner not queued")

    async def serve(self) -> None:
        """Open the transport, run the Grok gates, serve until the transport returns."""
        config = self.load_config()
        transport = self._build_transport(config)
        self._transport = transport
        initialize = bool(getattr(self.settings, "matrix_initialize", False))
        try:
            await transport.open(initialize=initialize)
            if initialize:
                # First run of a NEW bot device (CCC_MATRIX_INITIALIZE=1), same
                # contract as MatrixBot: keys/pins/room gate only, no journal.
                logger.info("Grok Matrix frontend initialised a new bot device; start again without CCC_MATRIX_INITIALIZE")
                return
            await self._attach()
            self._post_banner(config, transport)
            await transport.run()
        finally:
            self._transport = None
            await self.shutdown()
            await transport.close()

    def request_shutdown(self) -> None:
        """Synchronous signal/control fence, before any transport drain awaits."""
        self._closed = True
        self.ready = False
        self._generation += 1
        if self.active is not None:
            self.active.cancel()

    async def shutdown(self) -> None:
        self.request_shutdown()
        if self.session is not None:
            await self.session.close()
        if self.active is not None and self.active is not asyncio.current_task():
            self.active.cancel()
            await asyncio.gather(self.active, return_exceptions=True)
        if self._poller is not None:
            self._poller.close()
            self._poller = None

    def run(self) -> None:
        """Blocking entry point with the same contract as ``GrokTelegramBot.run()``."""
        log_filter = _TransportLogFilter()
        handlers = tuple(logging.getLogger().handlers)
        for handler in handlers:
            handler.addFilter(log_filter)
        # nio creates its crypto store with the process umask (unsafe-crypto-store otherwise).
        os.umask(0o077)

        async def _main() -> None:
            loop = asyncio.get_running_loop()
            task = asyncio.current_task()
            assert task is not None
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, task.cancel)
                except (NotImplementedError, RuntimeError):  # pragma: no cover - non-POSIX loops
                    pass
            try:
                await self.serve()
            except asyncio.CancelledError:
                logger.info("Grok Matrix frontend stopped")

        try:
            asyncio.run(_main())
        except ProtocolError as exc:
            # Protocol codes carry no secret; the class name is all other errors reveal.
            logger.warning("Grok Matrix frontend failed: %s", exc)
            raise ProtocolError("grok_frontend_failed_state_retained") from None
        except Exception as exc:
            logger.warning("Grok Matrix frontend failed: %s", type(exc).__name__)
            raise ProtocolError("grok_frontend_failed_state_retained") from None
        finally:
            self.request_shutdown()
            if self._poller is not None:
                self._poller.close()
                self._poller = None
            for handler in handlers:
                handler.removeFilter(log_filter)

    # -- turns ---------------------------------------------------------------

    def _admitted(self, job: Mapping[str, Any], room_kind: str) -> bool:
        config = self.load_config()
        event_id = str(job.get("event_id") or "")
        if not self.ready or not event_id.startswith("$") or event_id.startswith("$self-"):
            return False
        sender, room_id = job.get("sender"), job.get("room_id")
        if room_kind == "direct":
            return bool(sender == config["owner"] and room_id in (config.get("rooms") or ()))
        if room_kind == "family" and self.family_rooms_enabled:
            # The transport's mention gate already required the bot to be
            # addressed; the sender/room allowlist is re-checked here because
            # every admitted family prompt enters the owner's one conversation.
            allowed = {config["owner"], *(config.get("family_users") or ())}
            return bool(room_id in (config.get("family_rooms") or ()) and sender in allowed)
        return False

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

    async def run_turn(self, job: Mapping[str, Any], *, sink: Any, session_id: str | None, room_kind: str) -> Any:
        """``TurnRunner.run``: one admitted owner message -> one committed Grok reply."""
        del sink, session_id  # no progress bubbles/approvals; the journal is the session authority
        if not self._admitted(job, room_kind):
            return _turn_result(NOT_ADMITTED)  # no journal/remote access
        body = job.get("body")
        generation = self._generation
        if body in {"/start", "/status"}:
            return _turn_result(STATUS_TEXT)
        if job.get("attachment") or not isinstance(body, str) or not body or body.startswith("/"):
            return _turn_result(TEXT_ONLY)  # #1795: Grok stays text-only; no attachment reaches it
        if len(body.encode("utf-8")) > MAX_PROMPT:
            return _turn_result(TOO_LARGE)
        if self.active is not None:
            return _turn_result(BUSY)
        self.active = asyncio.current_task()
        try:
            text = await self._result(body)
            self._boot_current(generation)  # a retired frontend does not deliver
            return _turn_result(text)
        except asyncio.CancelledError:
            if self.session is not None:
                await self.session.close()
            raise
        except Exception as exc:
            if self.session is not None:
                await self.session.close()
            # ProtocolError codes are categorical and body-free; other types
            # reveal only their class name.
            logger.warning("Grok turn denied, uncertain, or delivery failed; state retained (%s)",
                           exc if isinstance(exc, ProtocolError) else type(exc).__name__)
            return _turn_result(UNCERTAIN)
        finally:
            self.active = None

    async def cancel(self, job: Mapping[str, Any]) -> bool:
        """``TurnRunner.cancel`` (/stop, /cancel): local waiting only, like Telegram."""
        del job
        if self.session is not None:
            await self.session.interrupt()
        if self.active is not None:
            self.active.cancel()
        return True


class _GrokMatrixTurnRunner:
    """The ``TurnRunner`` object handed to the transport (``run``/``cancel``)."""

    def __init__(self, bot: GrokMatrixBot) -> None:
        self._bot = bot

    async def run(self, job: Mapping[str, Any], *, sink: Any, session_id: str | None, room_kind: str) -> Any:
        return await self._bot.run_turn(job, sink=sink, session_id=session_id, room_kind=room_kind)

    async def cancel(self, job: Mapping[str, Any]) -> bool:
        return await self._bot.cancel(job)
