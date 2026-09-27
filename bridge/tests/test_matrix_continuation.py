"""Matrix frontend: yield-and-continue and dead-session recovery are wired (#1825).

``MatrixBot`` does not inherit ``BotLifecycleMixin``, so on Matrix a
``continuation_cli register`` answered ``ok`` and wrote the record while no
monitor ever read it, and terminal Claude task notices left by a dead session
were never delivered. These tests pin the Matrix wiring: the shared
``ContinuationMonitor`` over this frontend's own queue, the bundle run as a
durable self-job whose outcome feeds the monitor's failure guard, /stop and
/continue semantics, and the shared dead-session scanner delivering through
the room outbox.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import types
from typing import Any

import pytest

from telegram_bot.core import continuation_cli
from telegram_bot.core.continuation import (
    MAX_CONSECUTIVE_FAILURES,
    STATE_CANCELLED,
    STATE_CAP_HOLD,
    STATE_DONE,
    STATE_FAILED,
    STATE_PENDING,
)
from telegram_bot.core.dead_session_recovery import MARKER_KEY
from telegram_bot.core.matrix.bot import SELF_JOB_CONTINUATION, MatrixBot
from telegram_bot.core.project_chat_types import ChatResponse
from telegram_bot.core.usage_meter import MODE_AUTONOMOUS, MODE_INTERACTIVE
from test_matrix_bot import (
    DM_ROOM,
    OWNER,
    FakeProjectChat,
    FakeSessionManager,
    FakeSink,
    FakeTransport,
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
    """Same shape as test_matrix_bot's fixture (pytest keys fixtures by attribute name)."""

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


class SelfJobTransport(FakeTransport):
    """Records self-jobs; with ``bot`` set, runs each one as its room turn."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.self_jobs: list[tuple[str, str, str]] = []
        self.bot: MatrixBot | None = None
        self.turns: list[asyncio.Task[Any]] = []

    def enqueue_self_job(self, room_id: str, body: str, *, key: str, sender: str | None = None) -> str:
        self.self_jobs.append((room_id, body, key))
        event_id = "$self-" + key
        if self.bot is not None:
            job = _job(body, sender=sender or OWNER, room=room_id, event_id=event_id)
            self.turns.append(
                asyncio.ensure_future(
                    self.bot.run_turn(job, sink=FakeSink(), session_id=None, room_kind="direct")
                )
            )
        return event_id


def _bot(tmp_path: Path, **overrides: Any) -> tuple[MatrixBot, FakeProjectChat, FakeSessionManager]:
    settings = _settings(tmp_path, **overrides)
    chat = FakeProjectChat()
    manager = FakeSessionManager(provider=settings.agent_provider)
    bot = MatrixBot(settings, project_chat=chat, session_manager=manager, clock=None)
    return bot, chat, manager


async def _attached(tmp_path: Path, *, run_jobs: bool = False) -> tuple[MatrixBot, FakeProjectChat, SelfJobTransport, int, int]:
    """A bot with a transport and the owner's DM mapped (one real turn seeds it)."""

    bot, chat, manager = _bot(tmp_path)
    transport = SelfJobTransport({}, bot.runner)
    bot._transport = transport
    _resume_existing_session(bot, manager)
    await bot.run_turn(_job("hi"), sink=FakeSink(), session_id=None, room_kind="direct")
    chat.calls.clear()
    if run_jobs:
        transport.bot = bot
    user_id = bot.ids.user_id(OWNER)
    chat_id = bot.ids.chat_id(DM_ROOM, OWNER, direct=True)
    return bot, chat, transport, user_id, chat_id


def _register(bot: MatrixBot, user_id: int, chat_id: int, prompt: str = "next bundle: finish step 2") -> str:
    cid, _replaced = bot._continuation_queue().register(
        user_id=user_id, chat_id=chat_id, session_id="s-0", prompt=prompt
    )
    return cid


def _state(bot: MatrixBot, cid: str) -> str | None:
    record = bot._continuation_queue().get(cid)
    return None if record is None else record.get("state")


# --- construction ---------------------------------------------------------------


async def test_monitor_is_off_when_the_env_flag_is_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matrix_config: dict[str, Any]
) -> None:
    monkeypatch.setenv("CCC_CONTINUATION_ENABLED", "0")
    bot, _chat, _manager = _bot(tmp_path)
    assert bot._build_continuation_monitor() is None


async def test_monitor_reads_the_queue_the_agent_cli_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matrix_config: dict[str, Any]
) -> None:
    """The silent drop: the CLI home and the monitor's queue must be one directory."""

    monkeypatch.delenv("CCC_CONTINUATION_HOME", raising=False)
    monkeypatch.setenv("CCC_CONTINUATION_DAILY_CAP", "7")
    bot, _chat, _manager = _bot(tmp_path)
    # settings_memory exports CCC_EXTERNAL_WAIT_HOME = bot_data_dir/external-wait
    # into the agent's environment; the CLI resolves its queue next to it.
    monkeypatch.setenv("CCC_EXTERNAL_WAIT_HOME", str(tmp_path / "data" / "external-wait"))
    monitor = bot._build_continuation_monitor()
    assert monitor is not None
    assert monitor._queue._path.parent == continuation_cli._home()
    assert monitor._queue._path.parent == tmp_path / "data" / "continuation"
    assert monitor._active_turns_path.parent == tmp_path / "data" / "external-wait"
    assert monitor._daily_cap == 7


# --- runner ----------------------------------------------------------------------


async def test_runner_enqueues_a_self_job_and_returns_the_turn_outcome(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, _chat, transport, user_id, chat_id = await _attached(tmp_path)
    record = {"continuation_id": "c1", "user_id": user_id, "chat_id": chat_id}

    task = asyncio.ensure_future(bot._run_continuation(record, "[external_event] go"))
    for _ in range(5):
        await asyncio.sleep(0)
    (room, body, key), = transport.self_jobs
    assert room == DM_ROOM and key == f"{SELF_JOB_CONTINUATION}:c1"
    payload = json.loads(body)
    assert payload == {
        "kind": SELF_JOB_CONTINUATION, "v": 1, "continuation_id": "c1",
        "user_id": user_id, "prompt": "[external_event] go",
    }
    assert not task.done()  # waits for the turn, not just the enqueue
    bot._settle_continuation("c1", True)
    assert await task is True
    assert bot._continuation_waiters == {}


async def test_runner_reports_a_failed_turn(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _chat, _transport, user_id, chat_id = await _attached(tmp_path)
    task = asyncio.ensure_future(
        bot._run_continuation({"continuation_id": "c2", "user_id": user_id, "chat_id": chat_id}, "p")
    )
    for _ in range(5):
        await asyncio.sleep(0)
    bot._settle_continuation("c2", False)
    assert await task is False


async def test_runner_gives_up_after_the_turn_ceiling(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _chat, _transport, user_id, chat_id = await _attached(tmp_path)
    monkeypatch.setattr(bot, "_continuation_wait_seconds", lambda: 0.01)
    record = {"continuation_id": "c3", "user_id": user_id, "chat_id": chat_id}
    assert await bot._run_continuation(record, "p") is False
    assert bot._continuation_waiters == {}


async def test_runner_returns_false_without_a_transport_or_room(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, _chat, _manager = _bot(tmp_path)
    assert await bot._run_continuation({"continuation_id": "c", "user_id": 1, "chat_id": 1}, "p") is False
    bot._transport = SelfJobTransport({}, bot.runner)
    assert await bot._run_continuation({"continuation_id": "c", "user_id": 1, "chat_id": 999}, "p") is False
    assert bot._transport.self_jobs == []


async def test_runner_refuses_an_unadmitted_requester(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _chat, transport, _user_id, chat_id = await _attached(tmp_path)
    stranger = bot.ids.user_id("@stranger:example.org")
    for user in (stranger, None, True):
        record = {"continuation_id": "c", "user_id": user, "chat_id": chat_id}
        assert await bot._run_continuation(record, "p") is False
    assert transport.self_jobs == []


# --- self-job ---------------------------------------------------------------------


async def test_self_job_runs_the_bundle_as_an_autonomous_room_turn(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, chat, _transport, user_id, chat_id = await _attached(tmp_path)
    cid = _register(bot, user_id, chat_id)
    assert bot._continuation_queue().mark_running(cid)
    body = json.dumps({"kind": SELF_JOB_CONTINUATION, "v": 1, "continuation_id": cid,
                       "user_id": user_id, "prompt": "bundle prompt"})
    result = await bot.run_turn(
        _job(body, event_id=f"$self-{cid}"), sink=FakeSink(), session_id=None, room_kind="direct"
    )
    assert result.text == "answer"
    (call,) = chat.calls
    assert call["user_message"] == "bundle prompt"
    assert call["usage_mode"] == MODE_AUTONOMOUS
    # No runner waiting (e.g. after a restart): the self-job records the outcome.
    assert _state(bot, cid) == STATE_DONE


async def test_ordinary_turns_stay_interactive(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _transport, _user_id, _chat_id = await _attached(tmp_path)
    await bot.run_turn(_job("hello", event_id="$e2"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert chat.calls[-1]["usage_mode"] == MODE_INTERACTIVE


async def test_failed_bundle_turns_count_toward_the_failure_guard(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, chat, _transport, user_id, chat_id = await _attached(tmp_path)
    chat.response = ChatResponse(content="", success=False, error="boom")
    queue = bot._continuation_queue()
    for index in range(MAX_CONSECUTIVE_FAILURES):
        cid = _register(bot, user_id, chat_id)
        assert queue.mark_running(cid)
        body = json.dumps({"kind": SELF_JOB_CONTINUATION, "v": 1, "continuation_id": cid,
                           "user_id": user_id, "prompt": "p"})
        await bot.run_turn(
            _job(body, event_id=f"$self-f{index}"), sink=FakeSink(), session_id=None, room_kind="direct"
        )
        assert _state(bot, cid) == STATE_FAILED
    counter = queue.counter_for(user_id, chat_id)
    assert counter["consecutive_failures"] == MAX_CONSECUTIVE_FAILURES


async def test_a_cancelled_bundle_whose_self_job_was_queued_never_runs(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, chat, _transport, user_id, chat_id = await _attached(tmp_path)
    cid = _register(bot, user_id, chat_id)
    assert bot._continuation_queue().mark_running(cid)
    assert await bot.stop_idle({"room_id": DM_ROOM, "sender": OWNER}) is True
    body = json.dumps({"kind": SELF_JOB_CONTINUATION, "v": 1, "continuation_id": cid,
                       "user_id": user_id, "prompt": "p"})
    await bot.run_turn(_job(body, event_id="$self-x"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert chat.calls == []
    assert _state(bot, cid) == STATE_CANCELLED


async def test_self_job_without_a_requester_is_refused(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _transport, user_id, chat_id = await _attached(tmp_path)
    cid = _register(bot, user_id, chat_id)
    assert bot._continuation_queue().mark_running(cid)
    body = json.dumps({"kind": SELF_JOB_CONTINUATION, "v": 1, "continuation_id": cid, "prompt": "p"})
    await bot.run_turn(_job(body, event_id="$self-y"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert chat.calls == []
    assert _state(bot, cid) == STATE_FAILED


# --- end to end through the shared monitor -------------------------------------------


async def test_monitor_tick_runs_a_registered_bundle_to_done(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, chat, transport, user_id, chat_id = await _attached(tmp_path, run_jobs=True)
    cid = _register(bot, user_id, chat_id, prompt="continue the migration")
    monitor = bot._build_continuation_monitor()
    assert monitor is not None

    await monitor._tick()

    assert _state(bot, cid) == STATE_DONE
    (call,) = chat.calls
    assert call["user_message"].startswith(f"[external_event: continuation_queue id={cid}]")
    assert call["user_message"].endswith("continue the migration")
    assert call["usage_mode"] == MODE_AUTONOMOUS
    await asyncio.gather(*transport.turns)


async def test_monitor_parks_the_chain_after_repeated_failures(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, chat, transport, user_id, chat_id = await _attached(tmp_path, run_jobs=True)
    chat.response = ChatResponse(content="", success=False, error="boom")
    monitor = bot._build_continuation_monitor()
    assert monitor is not None
    for _ in range(MAX_CONSECUTIVE_FAILURES):
        _register(bot, user_id, chat_id)
        await monitor._tick()
    parked = _register(bot, user_id, chat_id)
    transport.notices.clear()
    await monitor._tick()
    assert _state(bot, parked) == STATE_CAP_HOLD
    assert any("Auto-continue stopped" in text for _room, text in transport.notices)
    await asyncio.gather(*transport.turns)


# --- /stop and /continue -----------------------------------------------------------------


async def test_idle_stop_cancels_queued_continuations(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _chat, _transport, user_id, chat_id = await _attached(tmp_path)
    assert await bot.stop_idle({"room_id": DM_ROOM, "sender": OWNER}) is False  # nothing queued
    cid = _register(bot, user_id, chat_id)
    assert await bot.stop_idle({"room_id": DM_ROOM, "sender": OWNER}) is True
    assert _state(bot, cid) == STATE_CANCELLED


async def test_stop_during_a_turn_cancels_the_running_bundle_not_as_a_failure(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, _chat, _transport, user_id, chat_id = await _attached(tmp_path)
    cid = _register(bot, user_id, chat_id)
    queue = bot._continuation_queue()
    assert queue.mark_running(cid)
    assert await bot.runner.cancel(_job("/stop")) is True
    assert _state(bot, cid) == STATE_CANCELLED
    bot._settle_continuation(cid, False)  # the cancelled turn ends later
    assert _state(bot, cid) == STATE_CANCELLED
    assert queue.counter_for(user_id, chat_id)["consecutive_failures"] == 0


async def test_cmd_stop_reports_cancelled_continuations(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _transport, user_id, chat_id = await _attached(tmp_path)
    chat.stop_result = False
    _register(bot, user_id, chat_id)
    assert await bot._cmd_stop(user_id=user_id, chat_id=chat_id) == "⏹️ Cancelled 1 queued continuation(s)"
    assert await bot._cmd_stop(user_id=user_id, chat_id=chat_id) == "ℹ️ Nothing running"


async def test_continue_rearms_a_cap_held_bundle(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, _chat, _transport, user_id, chat_id = await _attached(tmp_path)
    result = await bot.run_turn(_job("/continue", event_id="$c0"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.text == "ℹ️ No continuations waiting on the daily cap."
    cid = _register(bot, user_id, chat_id)
    assert bot._continuation_queue().mark_cap_hold(cid)
    result = await bot.run_turn(_job("/continue", event_id="$c1"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert result.text.startswith("▶️ Resumed 1 queued continuation(s)")
    assert _state(bot, cid) == STATE_PENDING


# --- dead-session recovery -------------------------------------------------------------------


class _RecoverySessions:
    """The session-store surface the shared scanner uses."""

    def __init__(self, rows: dict[Any, dict[str, Any]]) -> None:
        self.rows = rows

    async def list_sessions(self) -> dict[Any, dict[str, Any]]:
        return {key: dict(value) for key, value in self.rows.items()}

    async def get_session(self, key: Any) -> dict[str, Any]:
        return dict(self.rows[key])

    async def update_session(self, key: Any, updates: dict[str, Any]) -> None:
        self.rows[key].update(updates)


def _transcript(root: Path, session_id: str) -> None:
    notice = (
        "<task-notification><task-id>task-1</task-id><status>completed</status>"
        "<summary>background build finished</summary></task-notification>"
    )
    row = {
        "type": "queue-operation", "operation": "enqueue", "sessionId": session_id,
        "timestamp": "2026-09-27T00:00:00Z", "content": notice,
    }
    (root / f"{session_id}.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")


async def test_startup_recovery_delivers_dead_session_notices_to_the_room(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, chat, _manager = _bot(tmp_path, agent_provider="claude")
    transport = SelfJobTransport({}, bot.runner)
    bot._transport = transport
    user_id = bot.ids.user_id(OWNER)
    bot._direct_room_map().remember(OWNER, DM_ROOM)
    conversations = tmp_path / "conversations"
    conversations.mkdir()
    _transcript(conversations, "sess-1")
    chat.conversations_dir = conversations
    chat._get_conversation_lock = lambda _user, _chat: asyncio.Lock()  # type: ignore[attr-defined]
    sessions = _RecoverySessions({user_id: {"provider": "claude", "session_id": "sess-1"}})
    bot._session_manager = sessions

    await bot._startup_dead_session_recovery()
    await bot._startup_dead_session_recovery()  # marker: delivered once

    (room, text), = transport.notices
    assert room == DM_ROOM
    assert "background build finished" in text
    assert len(sessions.rows[user_id][MARKER_KEY]) == 1


async def test_startup_recovery_is_fail_open(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    bot, chat, _manager = _bot(tmp_path)
    chat.conversations_dir = tmp_path  # a scanner error must not stop the frontend

    class Broken:
        async def list_sessions(self) -> Any:
            raise RuntimeError("store down")

    bot._session_manager = Broken()
    await bot._startup_dead_session_recovery()


async def test_serve_runs_recovery_and_the_continuation_monitor(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _chat, _manager = _bot(tmp_path)
    seen: list[str] = []

    async def startup() -> None:
        seen.append("startup-recovery")

    async def periodic(stop: asyncio.Event) -> None:
        seen.append("periodic-recovery")
        await stop.wait()

    class Monitor:
        async def run(self, stop: asyncio.Event) -> None:
            seen.append("continuation")
            await asyncio.Event().wait()  # like a runner awaiting a turn: only cancel ends it

    monkeypatch.setattr(bot, "_startup_dead_session_recovery", startup)
    monkeypatch.setattr(bot, "_periodic_dead_session_recovery", periodic)
    monkeypatch.setattr(bot, "_build_continuation_monitor", lambda: Monitor())

    async def script(_transport: FakeTransport) -> None:
        for _ in range(5):
            await asyncio.sleep(0)

    bot._transport_factory = lambda config, runner: FakeTransport(config, runner, script=script)
    await asyncio.wait_for(bot.serve(), timeout=5)
    assert seen[0] == "startup-recovery"
    assert {"periodic-recovery", "continuation"} <= set(seen)
