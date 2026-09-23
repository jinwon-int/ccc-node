"""Matrix frontend: durable external waits are watched, not just recorded (#1934).

The Telegram lifecycle builds ``ExternalWaitMonitor``; ``MatrixBot`` never
did, so a Matrix ``gh-ci-wait`` registration answered ``ok``, wrote the
record — and nothing ever polled it. These tests pin the Matrix wiring:
the shared monitor built against this frontend's own registry home, the
resume handed to the waiting room as a durable self-job (the #1895
mechanism), and the self-job turn running the prompt as an ordinary turn.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import types
from typing import Any

import pytest

from telegram_bot.core.matrix.bot import SELF_JOB_EXTERNAL_WAIT_RESUME, MatrixBot
from telegram_bot.core.project_chat_types import ChatResponse
from test_matrix_bot import (
    DM_ROOM,
    OWNER,
    FakeProjectChat,
    FakeSessionManager,
    FakeSink,
    _job,
    _resume_existing_session,
    _settings,
)

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
        "family_users": [OWNER],
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


class SelfJobTransport:
    """Minimal seam for the resume path: records enqueue_self_job calls."""

    def __init__(self) -> None:
        self.self_jobs: list[tuple[str, str, str]] = []

    def enqueue_self_job(self, room_id: str, body: str, *, key: str) -> str:
        self.self_jobs.append((room_id, body, key))
        return "$self-" + key


def _bot(tmp_path: Path, **overrides: Any) -> tuple[MatrixBot, FakeProjectChat, FakeSessionManager]:
    settings = _settings(tmp_path, **overrides)
    chat = FakeProjectChat()
    manager = FakeSessionManager(provider=settings.agent_provider)
    bot = MatrixBot(settings, project_chat=chat, session_manager=manager, clock=None)
    return bot, chat, manager


def _owner_ids(bot: MatrixBot) -> tuple[int, int]:
    user_id = bot.ids.user_id(OWNER)
    chat_id = bot.ids.chat_id(DM_ROOM, OWNER, direct=True)
    return user_id, chat_id


# --- monitor construction -----------------------------------------------------


@pytest.mark.anyio
async def test_monitor_is_off_when_the_env_flag_is_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matrix_config: dict[str, Any]
) -> None:
    monkeypatch.setenv("CCC_EXTERNAL_WAIT_ENABLED", "0")
    bot, _chat, _manager = _bot(tmp_path)
    assert bot._build_external_wait_monitor() is None


@pytest.mark.anyio
async def test_monitor_defaults_on_and_uses_this_frontends_registry_home(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, _chat, _manager = _bot(tmp_path)
    monitor = bot._build_external_wait_monitor()
    assert monitor is not None
    # The agent-side CLI writes under CCC_EXTERNAL_WAIT_HOME =
    # bot_data_dir/external-wait; the monitor must read the same directory.
    assert str(monitor._registry._path) == str(tmp_path / "data" / "external-wait" / "waits.json")


# --- resumer seam ---------------------------------------------------------------


@pytest.mark.anyio
async def test_resume_is_enqueued_as_a_durable_self_job_in_the_waiting_room(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, _chat, _manager = _bot(tmp_path)
    transport = SelfJobTransport()
    bot._transport = transport
    # One real turn first: that is what registers the chat_id -> room mapping
    # the resume path resolves against (same seeding as the watchdog test).
    await bot.run_turn(_job("hi"), sink=FakeSink(), session_id=None, room_kind="direct")
    transport.self_jobs.clear()
    _user_id, chat_id = _owner_ids(bot)

    ok = await bot._enqueue_external_wait_resume(
        {"chat_id": chat_id, "wait_id": "w-abc"}, "CI finished green; continue with the merge"
    )
    assert ok is True
    room, body, key = transport.self_jobs[0]
    assert room == DM_ROOM
    assert key == f"{SELF_JOB_EXTERNAL_WAIT_RESUME}:w-abc"
    assert json.loads(body) == {
        "kind": SELF_JOB_EXTERNAL_WAIT_RESUME,
        "v": 1,
        "wait_id": "w-abc",
        "prompt": "CI finished green; continue with the merge",
    }


@pytest.mark.anyio
async def test_resume_returns_false_without_a_transport_or_room(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, _chat, _manager = _bot(tmp_path)
    _user_id, chat_id = _owner_ids(bot)

    bot._transport = None
    assert await bot._enqueue_external_wait_resume({"chat_id": chat_id, "wait_id": "w"}, "p") is False

    bot._transport = SelfJobTransport()
    assert await bot._enqueue_external_wait_resume({"chat_id": 987654321, "wait_id": "w"}, "p") is False


@pytest.mark.anyio
async def test_session_lookup_resolves_the_conversations_current_session(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, _chat, manager = _bot(tmp_path)
    user_id, chat_id = _owner_ids(bot)
    _resume_existing_session(bot, manager, "s-0")
    assert await bot._external_wait_session_lookup(user_id, chat_id) == "s-0"
    assert await bot._external_wait_session_lookup(user_id, 987654321) is None


# --- the self-job turn ------------------------------------------------------------


@pytest.mark.anyio
async def test_external_wait_self_job_runs_the_prompt_as_an_ordinary_turn(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, chat, manager = _bot(tmp_path)
    _resume_existing_session(bot, manager, "s-0")
    chat.response = ChatResponse(content="resumed answer", session_id="s-0")
    body = json.dumps(
        {"kind": SELF_JOB_EXTERNAL_WAIT_RESUME, "v": 1, "wait_id": "w1", "prompt": "CI green; merge #1931"}
    )

    result = await bot.run_turn(
        _job(body, sender=OWNER, room=DM_ROOM, event_id="$self-ext1"),
        sink=FakeSink(),
        session_id=None,
        room_kind="direct",
    )

    assert result.text == "resumed answer"
    assert len(chat.calls) == 1
    assert chat.calls[0]["user_message"] == "CI green; merge #1931"
    assert chat.calls[0]["session_id"] == "s-0"


@pytest.mark.anyio
async def test_external_wait_self_job_ignores_a_blank_prompt_and_other_kinds(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, chat, _manager = _bot(tmp_path)

    blank = json.dumps({"kind": SELF_JOB_EXTERNAL_WAIT_RESUME, "v": 1, "wait_id": "w", "prompt": "   "})
    await bot.run_turn(
        _job(blank, sender=OWNER, room=DM_ROOM, event_id="$self-a"),
        sink=FakeSink(), session_id=None, room_kind="direct",
    )
    assert chat.calls == []

    other = json.dumps({"kind": "someone-elses-job", "v": 1})
    await bot.run_turn(
        _job(other, sender=OWNER, room=DM_ROOM, event_id="$self-b"),
        sink=FakeSink(), session_id=None, room_kind="direct",
    )
    assert chat.calls == []
