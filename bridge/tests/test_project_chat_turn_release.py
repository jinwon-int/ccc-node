"""Focused tests for the failure and release helpers extracted in #896 PR4.

``_process_agent_message``'s ``except TimeoutError`` / ``except Exception``
bodies now live in ``_handle_turn_timeout`` / ``_handle_turn_exception`` and
its whole ``finally`` in ``_release_turn``. The ``except asyncio.CancelledError``
clause stays inline. These tests call the helpers directly and pin what the
move must not change: the interrupt-before-drop order on a timeout, the
``session_id`` fallback when no session was adopted, the #1721 transport
health mark, and — the one the design comment asked for — that a failing
``_finalize_request_progress`` still lets ``clear_active_turn`` run and the
registry token deactivate, in that order, before the failure propagates.

Module globals are patched through the helper's own ``__globals__`` (the
repo pattern, see ``test_external_wait.py``): under the CI ``telegram_bot``
path shim the module can be imported under two names, and patching the
attribute of the one this file imported does not reach the function.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from telegram_bot.core.codex_app_server import CodexConnectionClosedError
from telegram_bot.core.project_chat_process import ProjectChatProcessMixin
from telegram_bot.core.project_chat_types import _PendingRequest
from telegram_bot.core.request_lifecycle import RequestPhase


class _Session:
    session_id = "sess-1"

    def __init__(self) -> None:
        self.cleared: list[str] = []

    def clear_task_followup_authorization(self) -> None:
        self.cleared.append("followup")

    def clear_task_resume_authorization(self) -> None:
        self.cleared.append("resume")


class _Registry:
    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.deactivated: list[tuple[object, float]] = []

    def deactivate_if_same(self, token: object, *, touch_at: float) -> None:
        self._log.append("deactivate")
        self.deactivated.append((token, touch_at))


class _Host:
    _process_timeout_seconds = 30

    def __init__(self) -> None:
        self.log: list[str] = []
        self._agent_connection_error_reported = False
        self._agent_session_registry = _Registry(self.log)

    async def _interrupt_agent_session(self, session: object) -> None:
        self.log.append("interrupt")

    async def _drop_agent_session(self, key: str, session: object) -> None:
        self.log.append("drop")

    async def _cancel_agent_streaming(self, handler: object, *, context: str) -> None:
        self.log.append(f"cancel:{context}")

    def _external_wait_home(self) -> Path:
        return Path("/nonexistent/external-wait")


def _request() -> _PendingRequest:
    loop = asyncio.get_running_loop()
    return _PendingRequest(
        user_id=1,
        chat_id=2,
        model=None,
        requested_session_id=None,
        permission_callback=None,
        typing_callback=None,
        future=loop.create_future(),
    )


async def _timeout(host: _Host, request: _PendingRequest, session: Any) -> Any:
    return await ProjectChatProcessMixin._handle_turn_timeout(
        host,
        key="1:2",
        session=session,
        session_id="requested-9",
        progress_request=request,
        streaming_handler=None,
    )


async def _exception(host: _Host, request: _PendingRequest, exc: Exception,
                     session: Any) -> Any:
    return await ProjectChatProcessMixin._handle_turn_exception(
        host,
        exc,
        key="1:2",
        session=session,
        session_id="requested-9",
        progress_request=request,
        streaming_handler=None,
    )


async def _release(host: _Host, request: _PendingRequest, *, session: Any,
                   turn_token: Any = None, resume: bool = False,
                   followup: bool = False) -> None:
    await ProjectChatProcessMixin._release_turn(
        host,
        session=session,
        session_id="requested-9",
        turn_token=turn_token,
        loop=asyncio.get_running_loop(),
        user_id=1,
        chat_id=2,
        progress_coordinator=object(),
        progress_handle=object(),
        progress_request=request,
        resume_authorized=resume,
        followup_authorized=followup,
    )


# --- _handle_turn_timeout --------------------------------------------------


def test_timeout_interrupts_before_dropping_then_stops_streaming() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        response = await _timeout(host, request, _Session())
        assert response.success is False
        assert response.error == "Timed out after 30s"
        assert response.session_id == "sess-1"
        assert request.lifecycle.phase is RequestPhase.TIMEOUT
        # A closed session ignores interrupt, so interrupt must come first.
        assert host.log == ["interrupt", "drop", "cancel:handling an agent timeout"]

    asyncio.run(run())


def test_timeout_without_a_session_falls_back_to_the_requested_id() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        response = await _timeout(host, request, None)
        assert response.session_id == "requested-9"
        assert host.log == ["cancel:handling an agent timeout"]

    asyncio.run(run())


def test_timeout_that_lost_the_terminal_race_touches_nothing() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        assert request.lifecycle.try_terminal(RequestPhase.CANCELED, cause="stop").won
        response = await _timeout(host, request, _Session())
        assert response.success is False
        assert host.log == []
        assert request.lifecycle.phase is RequestPhase.CANCELED

    asyncio.run(run())


# --- _handle_turn_exception ------------------------------------------------


def test_exception_claims_failed_drops_and_reports_the_message() -> None:
    async def run() -> None:
        host = _Host()
        request = _request()
        response = await _exception(host, request, RuntimeError("boom"), _Session())
        assert response.success is False
        assert response.content == "❌ Error: boom"
        assert response.error == "boom"
        assert request.lifecycle.phase is RequestPhase.FAILED
        assert host.log == ["drop", "cancel:returning an agent error"]
        assert host._agent_connection_error_reported is False

    asyncio.run(run())


def test_exception_without_text_uses_the_generic_message() -> None:
    async def run() -> None:
        host = _Host()
        response = await _exception(host, _request(), RuntimeError(), None)
        assert response.error == "Agent runtime failed"
        assert response.session_id == "requested-9"

    asyncio.run(run())


def test_codex_connection_closed_marks_transport_health(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[str] = []

    class _Health:
        def record_agent_error(self, error: str) -> None:
            recorded.append(error)

    monkeypatch.setitem(
        ProjectChatProcessMixin._handle_turn_exception.__globals__, "health_reporter", _Health()
    )

    async def run() -> None:
        host = _Host()
        exc = CodexConnectionClosedError("socket closed")
        response = await _exception(host, _request(), exc, _Session())
        assert response.error == "socket closed"
        assert host._agent_connection_error_reported is True
        assert recorded == ["Codex app-server connection failed: socket closed"]

    asyncio.run(run())


# --- _release_turn ---------------------------------------------------------


def _patch_release(monkeypatch: pytest.MonkeyPatch, log: list[str], *,
                   finalize_raises: bool = False) -> list[dict[str, Any]]:
    clears: list[dict[str, Any]] = []
    release_globals = ProjectChatProcessMixin._release_turn.__globals__
    real_clear = release_globals["clear_active_turn"]

    async def fake_finalize(**kwargs: Any) -> None:
        log.append("finalize")
        if finalize_raises:
            raise RuntimeError("finalize failed")

    async def fake_offloaded(fn: Any, /, *args: Any, **kwargs: Any) -> None:
        assert fn is real_clear
        log.append("clear")
        clears.append(dict(kwargs))

    monkeypatch.setitem(release_globals, "_finalize_request_progress", fake_finalize)
    monkeypatch.setitem(release_globals, "_await_offloaded_write", fake_offloaded)
    return clears


def test_release_clears_authorizations_then_finalizes_clears_and_deactivates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        host = _Host()
        clears = _patch_release(monkeypatch, host.log)
        session = _Session()
        token = object()
        await _release(host, _request(), session=session, turn_token=token,
                       resume=True, followup=True)
        assert session.cleared == ["followup", "resume"]
        assert host.log == ["finalize", "clear", "deactivate"]
        assert clears == [{"user_id": 1, "chat_id": 2, "session_id": "sess-1"}]
        assert host._agent_session_registry.deactivated[0][0] is token

    asyncio.run(run())


def test_release_still_clears_and_deactivates_when_finalize_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The nested finally blocks are the whole point of moving P9 as one unit:
    # a finalize failure must not leave a stale active-turn route or a live
    # registry token behind, and must still surface afterwards.
    async def run() -> None:
        host = _Host()
        _patch_release(monkeypatch, host.log, finalize_raises=True)
        with pytest.raises(RuntimeError, match="finalize failed"):
            await _release(host, _request(), session=_Session(), turn_token=object())
        assert host.log == ["finalize", "clear", "deactivate"]

    asyncio.run(run())


def test_release_without_session_or_token_skips_what_it_cannot_touch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        host = _Host()
        clears = _patch_release(monkeypatch, host.log)
        await _release(host, _request(), session=None, turn_token=None,
                       resume=True, followup=True)
        assert host.log == ["finalize", "clear"]
        assert clears == [{"user_id": 1, "chat_id": 2, "session_id": None}]
        assert host._agent_session_registry.deactivated == []

    asyncio.run(run())
