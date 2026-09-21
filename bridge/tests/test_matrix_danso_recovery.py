"""Matrix frontend: Danso long-task commands and recovery offers (#1895 PR-A).

The Telegram ``DansoRecoveryMixin`` discipline is reused unchanged; these tests
pin the Matrix channel ports around it: numbered text menus over the outbox,
owner-only choices, typed ``1/2/3`` answered inside the served turn, and a
restart scan that *offers* but never dispatches.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from telegram_bot.core.bot_danso_recovery import OFFER, RECOVERY_TEXT_MENU
from telegram_bot.core.danso_worker import TASK_RESUME_CONTROL
from telegram_bot.core.matrix.bot import MatrixBot
from telegram_bot.core.project_chat_types import ChatResponse
from test_danso_recovery import snapshot
from test_matrix_bot import DM_ROOM, KID, OWNER, FakeProjectChat, FakeSink, FormattedTransport, _job, _settings
from test_matrix_bot import matrix_config as _matrix_config_fixture  # noqa: F401 - registers the fixture
from test_session_provider import make_manager

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class RecoveryProjectChat(FakeProjectChat):
    def __init__(self, state: str = "paused", allowed: bool = True) -> None:
        super().__init__()
        self._memory_route = "matrix"
        self.inspect_danso_recovery = AsyncMock(return_value=snapshot(state, allowed))
        self.request_danso_task_pause = AsyncMock(return_value="requested")


async def _danso_bot(
    tmp_path: Path, *, state: str = "paused", allowed: bool = True, stored_session: str | None = "sid"
) -> tuple[MatrixBot, RecoveryProjectChat, Any, FormattedTransport]:
    settings = _settings(
        tmp_path, agent_provider="danso", danso_long_task_enabled=True, bash_policy="auto-approve"
    )
    chat = RecoveryProjectChat(state, allowed)
    manager = make_manager(tmp_path, "danso")
    bot = MatrixBot(settings, project_chat=chat, session_manager=manager, clock=SimpleNamespace(time=lambda: 1_700_000_000.0))
    transport = FormattedTransport({}, bot.runner)
    bot._transport = transport
    user_id, chat_id, _room = bot._job_identity(_job("hello"), "direct")  # registers the DM room mapping
    if stored_session:
        await manager.patch_session(
            bot._conversation_key(user_id, chat_id),
            updates={"provider": "danso", "session_id": stored_session},
        )
        bot._runtime_active_sessions.add(bot._conversation_key(user_id, chat_id))
    return bot, chat, manager, transport


async def _turn(bot: MatrixBot, body: str, *, sender: str = OWNER) -> Any:
    return await bot.runner.run(_job(body, sender=sender), sink=FakeSink(), session_id=None, room_kind="direct")


def _session(bot: MatrixBot, manager: Any, sender: str = OWNER):
    user_id, chat_id, _ = bot._job_identity(_job("x", sender=sender), "direct")
    return manager.get_session(bot._conversation_key(user_id, chat_id))


@pytest.mark.anyio
async def test_task_pause_maps_native_status_to_reply(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager, _transport = await _danso_bot(tmp_path)
    result = await _turn(bot, "/task_pause")
    chat.request_danso_task_pause.assert_awaited_once()
    assert result.text.startswith("⏸ Graceful pause requested")
    chat.request_danso_task_pause.return_value = "not_active"
    assert (await _turn(bot, "/task_pause")).text.startswith("ℹ️ No active Danso long task")
    assert (await _turn(bot, "/task_pause now")).text == "Usage: /task_pause"


@pytest.mark.anyio
async def test_task_commands_refuse_without_long_task_mode(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager, _transport = await _danso_bot(tmp_path)
    bot._settings.danso_long_task_enabled = False
    for command in ("/task_pause", "/task_recover", "/task_resume"):
        assert (await _turn(bot, command)).text == "❌ Danso long-task mode is disabled."
    chat.request_danso_task_pause.assert_not_awaited()
    assert chat.calls == []


@pytest.mark.anyio
async def test_task_recover_offers_numbered_text_menu_over_outbox(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager, transport = await _danso_bot(tmp_path)
    result = await _turn(bot, "/task_recover")
    assert result.text == ""  # the offer itself was delivered as a notice
    assert len(transport.notices) == 1
    room, text = transport.notices[0]
    assert room == DM_ROOM
    assert "중단 상태: 일시 정지" in text and RECOVERY_TEXT_MENU in text
    assert (await _session(bot, manager))[OFFER]["token"]
    assert chat.calls == []  # offering never dispatches


@pytest.mark.anyio
async def test_typed_one_resumes_through_explicit_path_inside_the_turn(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager, transport = await _danso_bot(tmp_path)
    await _turn(bot, "/task_recover")
    chat.response = ChatResponse(content="resumed answer", session_id="sid")
    result = await _turn(bot, "1")
    assert result.streamed is True and result.text == ""
    assert len(chat.calls) == 1
    call = chat.calls[0]
    assert call["user_message"] == TASK_RESUME_CONTROL
    assert call["resume_task"] is True and call["new_session"] is False and call["session_id"] == "sid"
    assert callable(call["dispatch_guard"]) and call["dispatch_guard"]()
    assert OFFER not in await _session(bot, manager)  # one-shot claim consumed
    assert any("resumed answer" in body for _room, body, _html in transport.formatted)
    # A second "1" has nothing to claim and is an ordinary message again.
    await _turn(bot, "1")
    assert len(chat.calls) == 2 and chat.calls[1]["user_message"] == "1"


@pytest.mark.anyio
async def test_typed_one_without_resume_allowed_starts_evidence_first_new_session(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager, _transport = await _danso_bot(tmp_path, state="pending_provider", allowed=False)
    await _turn(bot, "/task_recover")
    await _turn(bot, "1")
    call = chat.calls[0]
    assert call["new_session"] is True and call["session_id"] is None
    assert "resume_task" not in call and "First inspect" in call["user_message"]


@pytest.mark.anyio
async def test_typed_two_and_three_never_call_the_provider(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager, transport = await _danso_bot(tmp_path)
    await _turn(bot, "/task_recover")
    await _turn(bot, "3")
    assert chat.calls == [] and any("중단 상태" in body for _r, body, _h in transport.formatted)
    await _turn(bot, "2")
    assert chat.calls == []
    session = await _session(bot, manager)
    assert session.get("session_id") is None and session.get("new_session") is True and OFFER not in session


@pytest.mark.anyio
async def test_non_owner_cannot_see_or_answer_offers(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager, transport = await _danso_bot(tmp_path)
    await _turn(bot, "/task_recover")  # owner creates the offer
    assert len(transport.notices) == 1
    assert (await _turn(bot, "/task_recover", sender=KID)).text.startswith("현재 복구할 작업이 없거나")
    assert len(transport.notices) == 1
    await _turn(bot, "1", sender=KID)  # falls through to an ordinary message for the kid's own conversation
    assert chat.calls and chat.calls[-1]["user_message"] == "1"
    assert (await _session(bot, manager))[OFFER]["token"]  # owner's offer untouched


@pytest.mark.anyio
async def test_failed_turn_is_followed_by_the_offer_after_the_failure_text(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager, transport = await _danso_bot(tmp_path, state="failed", allowed=False)
    chat.response = ChatResponse(content="❌ Processing failed: boom", success=False, session_id="sid")
    result = await _turn(bot, "do the thing")
    assert result.status == "error"
    assert transport.formatted and "boom" in transport.formatted[0][1]
    assert len(transport.notices) == 1 and RECOVERY_TEXT_MENU in transport.notices[0][1]
    assert (await _session(bot, manager))[OFFER]["token"]


@pytest.mark.anyio
async def test_task_resume_dispatches_explicit_resume_and_reports_missing_task(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager, transport = await _danso_bot(tmp_path)
    chat.response = ChatResponse(content="continued", session_id="sid")
    result = await _turn(bot, "/task_resume")
    assert chat.calls[0]["user_message"] == TASK_RESUME_CONTROL and chat.calls[0]["resume_task"] is True
    assert result.streamed is True and any("continued" in b for _r, b, _h in transport.formatted)
    fresh, chat2, _m, _t = await _danso_bot(tmp_path / "fresh", stored_session=None)
    assert (await _turn(fresh, "/task_resume")).text.startswith("📭 No paused Danso long task")
    assert chat2.calls == []


@pytest.mark.anyio
async def test_startup_scan_offers_but_never_dispatches(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager, transport = await _danso_bot(tmp_path)
    await bot._startup_danso_recovery_scan()
    assert len(transport.notices) == 1 and RECOVERY_TEXT_MENU in transport.notices[0][1]
    assert chat.calls == []
    assert (await _session(bot, manager))[OFFER]["token"]
    await bot._startup_danso_recovery_scan()  # unchanged journal: deduplicated
    assert len(transport.notices) == 1


@pytest.mark.anyio
async def test_claude_provider_never_scans_or_offers(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager, transport = await _danso_bot(tmp_path)
    bot._settings.agent_provider = "claude"
    await bot._startup_danso_recovery_scan()
    chat.response = ChatResponse(content="❌ failed", success=False, session_id="s2")
    await _turn(bot, "hi")
    assert transport.notices == []
