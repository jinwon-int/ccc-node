"""Matrix frontend: Danso long-task commands and recovery offers (#1895 PR-A).

The Telegram ``DansoRecoveryMixin`` discipline is reused unchanged; these tests
pin the Matrix channel ports around it: numbered text menus over the outbox,
owner-only choices, typed ``1/2/3`` answered inside the served turn, and a
restart scan that *offers* but never dispatches.
"""
from __future__ import annotations

from pathlib import Path
import sys
import types
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from telegram_bot.core.bot_danso_recovery import AUTO_RESUME_NOTICE, OFFER, RECOVERY_TEXT_MENU
from telegram_bot.core.danso_worker import TASK_RESUME_CONTROL
from telegram_bot.core.matrix.bot import MatrixBot
from telegram_bot.core.project_chat_types import ChatResponse
from test_danso_recovery import snapshot
from test_matrix_bot import DM_ROOM, KID, OWNER, FakeProjectChat, FakeSink, FormattedTransport, _job, _settings
from test_session_provider import make_manager

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def matrix_config(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Same shape as test_matrix_bot's fixture; defined here because pytest keys fixtures on the attribute name."""

    config = {
        "homeserver": "https://matrix.example.org",
        "account": "@bridge:example.org",
        "device_id": "DEV",
        "owner": OWNER,
        "rooms": [DM_ROOM],
        "family_rooms": [],
        "family_users": [KID, OWNER],
        "not_before_ms": 0,
        "loaded_from": [],
    }
    module = types.ModuleType("telegram_bot.core.matrix.state")

    def load_config(path: Path) -> dict[str, Any]:
        config["loaded_from"].append(Path(path))
        return dict(config)

    module.load_config = load_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram_bot.core.matrix.state", module)
    return config




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
    # Falls through to an ordinary message for the kid's own conversation —
    # which Danso on owner-operator refuses outright (#1955): no agent run.
    from telegram_bot.core.matrix.bot import NON_OWNER_TURN_REFUSED

    calls_before = len(chat.calls)
    assert (await _turn(bot, "1", sender=KID)).text == NON_OWNER_TURN_REFUSED
    assert len(chat.calls) == calls_before
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


# ---- PR-A2: restart automatic resume runs as a transport self-job

class SelfJobTransport(FormattedTransport):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.self_jobs: list[tuple[str, str, str]] = []

    def enqueue_self_job(self, room_id: str, body: str, *, key: str) -> str:
        self.self_jobs.append((room_id, body, key))
        return "$self-" + key


async def _auto_bot(tmp_path: Path, **kw: Any):
    bot, chat, manager, _transport = await _danso_bot(tmp_path, **kw)
    bot._settings.danso_recovery_auto_resume = True
    transport = SelfJobTransport({}, bot.runner)
    bot._transport = transport
    return bot, chat, manager, transport


async def _run_self_job(bot: MatrixBot, transport: SelfJobTransport) -> Any:
    room, body, key = transport.self_jobs[-1]
    job = _job(body, room=room, event_id="$self-" + key)
    return await bot.runner.run(job, sink=FakeSink(), session_id=None, room_kind="direct")


@pytest.mark.anyio
async def test_restart_scan_queues_a_self_job_instead_of_dispatching(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager, transport = await _auto_bot(tmp_path)
    await bot._startup_danso_recovery_scan()
    assert chat.calls == [] and transport.notices == []  # no dispatch, no menu
    assert len(transport.self_jobs) == 1
    room, body, key = transport.self_jobs[0]
    assert room == DM_ROOM and key.startswith("danso-auto-resume:")
    assert '"kind": "danso-auto-resume"' in body
    session = await _session(bot, manager)
    assert session[OFFER]["token"] and "danso_recovery_notified" not in session
    await bot._startup_danso_recovery_scan()  # idempotent key while the job is pending
    assert transport.self_jobs[1][2] == key and chat.calls == []


@pytest.mark.anyio
async def test_self_job_turn_performs_the_automatic_resume_with_the_room_sink(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager, transport = await _auto_bot(tmp_path)
    await bot._startup_danso_recovery_scan()
    chat.response = ChatResponse(content="resumed", session_id="sid")
    result = await _run_self_job(bot, transport)
    assert result.streamed is True
    assert len(chat.calls) == 1
    call = chat.calls[0]
    assert call["user_message"] == TASK_RESUME_CONTROL and call["resume_task"] is True and call["session_id"] == "sid"
    assert call["dispatch_guard"]()
    session = await _session(bot, manager)
    assert session["danso_recovery_auto_resumed"] == snapshot("paused", True).fingerprint
    assert OFFER not in session
    assert any(AUTO_RESUME_NOTICE.split("{")[0] in text for _room, text in transport.notices)
    assert any("resumed" in body for _r, body, _h in transport.formatted)
    # Unchanged journal after the attempt: the next scan offers the menu, never a second self-job.
    await bot._startup_danso_recovery_scan()
    assert len(transport.self_jobs) == 1
    assert any(RECOVERY_TEXT_MENU in text for _room, text in transport.notices)


@pytest.mark.anyio
async def test_self_job_retries_once_after_a_stale_lock_refusal(tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    bot, chat, manager, transport = await _auto_bot(tmp_path)
    bot._settings.danso_recovery_auto_resume_retry_delay_seconds = 5
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
    monkeypatch.setattr("telegram_bot.core.bot_danso_recovery.asyncio.sleep", fake_sleep)
    await bot._startup_danso_recovery_scan()
    failure = ChatResponse(content="❌ Processing failed: session", success=False, session_id="sid", failure_code="danso_session")
    responses = iter([failure, ChatResponse(content="continued", session_id="sid")])

    async def process_message(**kwargs: Any) -> ChatResponse:
        chat.calls.append(kwargs)
        return next(responses)
    chat.process_message = process_message  # type: ignore[method-assign]
    await _run_self_job(bot, transport)
    assert len(chat.calls) == 2 and slept == [5]
    assert any("continued" in body for _r, body, _h in transport.formatted)
    assert OFFER not in await _session(bot, manager)


@pytest.mark.anyio
@pytest.mark.parametrize("why", ["not_resumable", "opt_in_off"])
async def test_scan_offers_menu_when_auto_resume_is_not_eligible(tmp_path: Path, matrix_config: dict[str, Any], why: str) -> None:
    kw = {"state": "pending_provider", "allowed": False} if why == "not_resumable" else {}
    bot, chat, manager, transport = await _auto_bot(tmp_path, **kw)
    if why == "opt_in_off":
        bot._settings.danso_recovery_auto_resume = False
    await bot._startup_danso_recovery_scan()
    assert transport.self_jobs == [] and chat.calls == []
    assert len(transport.notices) == 1 and RECOVERY_TEXT_MENU in transport.notices[0][1]


@pytest.mark.anyio
async def test_foreign_or_malformed_self_jobs_never_dispatch(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, manager, transport = await _auto_bot(tmp_path)
    await bot._startup_danso_recovery_scan()
    room, body, key = transport.self_jobs[0]
    kid_job = _job(body, room=room, sender=KID, event_id="$self-" + key)
    result = await bot.runner.run(kid_job, sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.streamed is True and chat.calls == []
    bad = _job('{"kind":"something-else"}', room=room, event_id="$self-other")
    await bot.runner.run(bad, sink=FakeSink(), session_id=None, room_kind="direct")
    assert chat.calls == []
    assert (await _session(bot, manager))[OFFER]["token"]  # owner's offer still pending
