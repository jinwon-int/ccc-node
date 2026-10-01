"""Telegram turn paths outside the main message flow refresh the wait status (#2088).

Stage 1 (#2081) wired ``_sync_external_wait_status`` after the reply on the
normal text/voice path, the external-wait resume and continuation replies, the
monitor and ``/cancelwait``. The skills prompt, ``/task_resume``, the slash
``run_task`` closures and the numbered-option callback also end a turn with a
reply but did not refresh the route: a CI wait registered there showed up
only after the next normal turn or the monitor's terminal transition.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from telegram_bot.core.bot_wait_status import refresh_wait_status
from test_danso_recovery import make_app, opt_update, setup_bot, setup_opt_bot
from test_session_provider import make_update


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _response() -> SimpleNamespace:
    return SimpleNamespace(success=True, content="done", session_id="new-sid", has_options=False, streamed=False)


def _track(bot: Any, order: list[Any]) -> None:
    """Record the reply and the status sync in call order."""

    async def reply(*args: Any, **kwargs: Any) -> None:
        order.append("reply")

    async def sync(user_id: int, chat_id: int) -> None:
        order.append(("sync", user_id, chat_id))

    bot._reply_smart = AsyncMock(side_effect=reply)
    bot._send_smart = AsyncMock(side_effect=reply)
    bot._sync_external_wait_status = AsyncMock(side_effect=sync)


async def _command_bot(tmp_path: Any) -> tuple[Any, list[Any]]:
    bot, manager, handler = await setup_bot(tmp_path)
    handler.process_message = AsyncMock(return_value=_response())
    bot.application = make_app()
    bot._permission_callback = AsyncMock()
    bot._make_interim_reply_callback = Mock(return_value=None)
    bot._switch_provider_if_needed = AsyncMock(return_value=(await manager.get_session("7:9"), False))
    bot._effective_session_id = lambda key, current: current.get("session_id")
    bot._skill_aware_slash_message = AsyncMock(side_effect=lambda message, cmd: cmd)
    order: list[Any] = []
    _track(bot, order)
    return bot, order


@pytest.mark.anyio
async def test_skills_prompt_refreshes_after_its_reply(tmp_path: Any) -> None:
    bot, order = await _command_bot(tmp_path)
    await bot._cmd_skills(make_update(user_id=7, chat_id=9), SimpleNamespace(bot=bot.application.bot))
    assert order == ["reply", ("sync", 7, 9)]


@pytest.mark.anyio
async def test_command_run_task_refreshes_after_its_reply(tmp_path: Any) -> None:
    bot, order = await _command_bot(tmp_path)
    await bot._cmd_command(make_update(user_id=7, chat_id=9, text="/command commit"), SimpleNamespace(args=[]))
    assert order == ["reply", ("sync", 7, 9)]


@pytest.mark.anyio
async def test_slash_skill_run_task_refreshes_after_its_reply(tmp_path: Any) -> None:
    bot, order = await _command_bot(tmp_path)
    await bot._exec_slash_command(make_update(user_id=7, chat_id=9), "/commit")
    assert order == ["reply", ("sync", 7, 9)]


@pytest.mark.anyio
async def test_task_resume_refreshes_after_its_reply(tmp_path: Any) -> None:
    bot, order = await _command_bot(tmp_path)
    bot._tasks = SimpleNamespace(active=Mock(return_value=None))
    await bot._cmd_task_resume(make_update(user_id=7, chat_id=9), SimpleNamespace(args=[]))
    assert order[:2] == ["reply", ("sync", 7, 9)]


@pytest.mark.anyio
async def test_option_callback_refreshes_after_its_reply(tmp_path: Any) -> None:
    bot, _manager, _handler = await setup_opt_bot(tmp_path, _response())
    order: list[Any] = []
    _track(bot, order)
    await bot._handle_callback(opt_update(), SimpleNamespace(application=bot.application))
    assert order == ["reply", ("sync", 7, 9)]


@pytest.mark.anyio
async def test_refresh_helper_is_a_fail_open_noop_without_the_sync() -> None:
    await refresh_wait_status(SimpleNamespace(), 7, 9)
    failing = SimpleNamespace(_sync_external_wait_status=AsyncMock(side_effect=RuntimeError("down")))
    await refresh_wait_status(failing, 7, 9)
    failing._sync_external_wait_status.assert_awaited_once_with(7, 9)
