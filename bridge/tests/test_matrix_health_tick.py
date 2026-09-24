"""Matrix frontend health.json reporting (#1895 follow-up): bound to the Matrix
data dir, marked at startup/shutdown, and refreshed by a workload tick so an
idle frontend no longer looks dead to fleet freshness checks."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from telegram_bot.core.matrix import bot as matrix_bot
from test_matrix_bot import FakeProjectChat, FakeTransport, _attach, _bot
from test_matrix_bot import matrix_config as _shared  # noqa: F401 - fixture registration below

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def matrix_config(_shared: dict[str, Any]) -> dict[str, Any]:  # noqa: F811 - re-exposed under its own name
    return _shared


class RecordingReporter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str):
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))
        return record

    def names(self) -> list[str]:
        return [name for name, _a, _k in self.calls]


class SignalTransport(FakeTransport):
    """A transport that exposes #1820 health signals (fresh sync by default)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.signals: dict[str, Any] = fresh_signals()

    def health_signals(self, now: float | None = None) -> dict[str, Any]:
        return dict(self.signals)


def fresh_signals(**overrides: Any) -> dict[str, Any]:
    signals: dict[str, Any] = {
        "last_sync_at": 1.0,
        "sync_age_s": 5.0,
        "receive_failures": 0,
        "receive_error": "",
        "send_failures": 0,
        "send_error": "",
        "outbox_pending": 0,
        "outbox_head_age_s": None,
    }
    signals.update(overrides)
    return signals


class WorkloadProjectChat(FakeProjectChat):
    def workload_snapshot(self, now: float) -> tuple[int, float]:
        return 1, 3.5

    def waiting_for_turn_snapshot(self) -> int:
        return 2


@pytest.mark.anyio
async def test_serve_binds_marks_and_ticks_health(tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    reporter = RecordingReporter()
    monkeypatch.setattr(matrix_bot, "health_reporter", reporter)
    monkeypatch.setattr(matrix_bot, "_HEALTH_INTERVAL_S", 0.01)
    bot, _chat, _manager = _bot(tmp_path)
    bot._project_chat = WorkloadProjectChat()
    holder = await _attach(bot, SignalTransport)

    async def body(transport: FakeTransport) -> None:
        await asyncio.sleep(0.08)  # several ticks
    holder["body"] = body
    await bot.serve()

    names = reporter.names()
    assert names[:4] == ["bind", "initialize_process", "mark_starting", "record_agent_ok"]
    bind_args, bind_kwargs = reporter.calls[0][1], reporter.calls[0][2]
    assert bind_args[0] == bot._data_dir() and bind_kwargs["agent_provider"] == "codex"
    ticks = [c for c in reporter.calls if c[0] == "record_workload"]
    assert len(ticks) >= 2
    assert ticks[0][1] == (1, 3.5) and ticks[0][2] == {"waiting_for_turn": 2}
    assert names.count("record_telegram_ok") >= 2
    assert names[-1] == "mark_unavailable"


@pytest.mark.anyio
async def test_health_tick_survives_reporter_errors_and_missing_snapshots(tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingReporter(RecordingReporter):
        def __getattr__(self, name: str):
            if name == "record_workload":
                def boom(*a: Any, **k: Any) -> None:
                    self.calls.append(("record_workload", a, k))
                    raise RuntimeError("disk full")
                return boom
            return super().__getattr__(name)

    reporter = ExplodingReporter()
    monkeypatch.setattr(matrix_bot, "health_reporter", reporter)
    monkeypatch.setattr(matrix_bot, "_HEALTH_INTERVAL_S", 0.01)
    bot, _chat, _manager = _bot(tmp_path)  # FakeProjectChat has no workload_snapshot
    holder = await _attach(bot)

    async def body(transport: FakeTransport) -> None:
        await asyncio.sleep(0.05)
    holder["body"] = body
    await bot.serve()  # a failing tick never stops the transport leg

    ticks = [c for c in reporter.calls if c[0] == "record_workload"]
    assert len(ticks) >= 2 and ticks[0][1] == (0, 0.0)  # idle: no active transport job
    assert holder["transport"].events == ["open", "run", "close"]
    assert reporter.names()[-1] == "mark_unavailable"


# --- #1820: transport verdict from real signals ------------------------------


def _verdict_bot(tmp_path: Path, signals: dict[str, Any] | None) -> Any:
    bot, _chat, _manager = _bot(tmp_path)
    transport = SignalTransport({}, None)
    if signals is None:
        transport = FakeTransport({}, None)  # no health_signals at all
    else:
        transport.signals = signals
    bot._transport = transport
    bot._health_started = matrix_bot.time.monotonic()
    return bot


def _telegram_calls(reporter: RecordingReporter) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
    return [c for c in reporter.calls if c[0].startswith("record_telegram")]


def test_fresh_sync_records_telegram_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reporter = RecordingReporter()
    monkeypatch.setattr(matrix_bot, "health_reporter", reporter)
    bot = _verdict_bot(tmp_path, fresh_signals())
    bot._record_transport_health()
    assert _telegram_calls(reporter) == [("record_telegram_ok", (), {})]


def test_stale_sync_records_telegram_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reporter = RecordingReporter()
    monkeypatch.setattr(matrix_bot, "health_reporter", reporter)
    bot = _verdict_bot(tmp_path, fresh_signals(sync_age_s=matrix_bot._SYNC_STALE_S + 30))
    bot._record_transport_health()
    [(name, args, kwargs)] = _telegram_calls(reporter)
    assert name == "record_telegram_error"
    assert args[0] == "matrix sync stale for 120s" and kwargs == {"consecutive_failures": 1}


def test_receive_network_retry_records_error_with_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reporter = RecordingReporter()
    monkeypatch.setattr(matrix_bot, "health_reporter", reporter)
    bot = _verdict_bot(tmp_path, fresh_signals(receive_failures=3, receive_error="matrix-temporary-error"))
    bot._record_transport_health()
    [(name, args, kwargs)] = _telegram_calls(reporter)
    assert name == "record_telegram_error"
    assert args[0] == "matrix sync retrying (matrix-temporary-error)"
    assert kwargs == {"consecutive_failures": 3}


def test_stuck_outbox_records_degraded_reason_with_send_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reporter = RecordingReporter()
    monkeypatch.setattr(matrix_bot, "health_reporter", reporter)
    bot = _verdict_bot(
        tmp_path,
        fresh_signals(
            outbox_pending=2, outbox_head_age_s=300.0, send_failures=4, send_error="group-key-share-incomplete"
        ),
    )
    bot._record_transport_health()
    [(name, args, kwargs)] = _telegram_calls(reporter)
    assert name == "record_telegram_error"
    assert args[0] == "matrix outbox stuck for 300s (2 pending); send retrying (group-key-share-incomplete)"
    assert kwargs == {"consecutive_failures": 4}
    # A young head (normal delivery in flight) is still healthy.
    reporter.calls.clear()
    bot._transport.signals = fresh_signals(outbox_pending=1, outbox_head_age_s=5.0)
    bot._record_transport_health()
    assert [c[0] for c in _telegram_calls(reporter)] == ["record_telegram_ok"]


def test_no_sync_yet_waits_then_errors_after_grace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reporter = RecordingReporter()
    monkeypatch.setattr(matrix_bot, "health_reporter", reporter)
    bot = _verdict_bot(tmp_path, fresh_signals(last_sync_at=None, sync_age_s=None))
    bot._record_transport_health()
    assert _telegram_calls(reporter) == []  # still "starting", never a blind ok
    bot._health_started -= matrix_bot._SYNC_STARTUP_GRACE_S + 1
    bot._record_transport_health()
    [(name, args, _kwargs)] = _telegram_calls(reporter)
    assert name == "record_telegram_error" and args[0] == "matrix sync not completed since start"


def test_transport_without_signals_gets_no_automatic_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reporter = RecordingReporter()
    monkeypatch.setattr(matrix_bot, "health_reporter", reporter)
    bot = _verdict_bot(tmp_path, None)
    bot._record_transport_health()
    assert _telegram_calls(reporter) == []


# --- #1820: per-turn agent marks ---------------------------------------------


def _bound_reporter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from telegram_bot.utils.health import DeferredHealthReporter

    reporter = DeferredHealthReporter()
    reporter.bind(tmp_path / "data", agent_provider="codex")
    monkeypatch.setattr(matrix_bot, "health_reporter", reporter)
    return reporter


def _agent(tmp_path: Path) -> dict[str, Any]:
    import json

    return json.loads((tmp_path / "data" / "health.json").read_text())["agent"]


@pytest.mark.anyio
async def test_completed_turn_updates_agent_last_ok_at(tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from test_matrix_bot import FakeSink, _job

    reporter = _bound_reporter(tmp_path, monkeypatch)
    reporter.record_agent_error("synthetic earlier failure")
    bot, _chat, _manager = _bot(tmp_path)
    bot._health_active = True
    await bot.run_turn(_job("hello"), sink=FakeSink(), session_id=None, room_kind="direct")
    agent = _agent(tmp_path)
    assert agent["state"] == "healthy" and agent["last_ok_at"] and agent["last_error"] == ""


@pytest.mark.anyio
async def test_failed_turn_records_agent_error(tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from telegram_bot.core.project_chat_types import ChatResponse
    from test_matrix_bot import FakeSink, _job

    _bound_reporter(tmp_path, monkeypatch)
    bot, chat, _manager = _bot(tmp_path)
    bot._health_active = True
    chat.response = ChatResponse(
        content="x", success=False, error="request timed out", failure_class="admission-timeout/none"
    )
    await bot.run_turn(_job("hello"), sink=FakeSink(), session_id=None, room_kind="direct")
    agent = _agent(tmp_path)
    assert agent["state"] == "degraded"
    assert agent["last_error"] == "agent turn failed: admission-timeout/none / request timed out"


@pytest.mark.anyio
async def test_non_agent_outcomes_and_inactive_reporter_leave_agent_untouched(tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from telegram_bot.core.project_chat_types import ChatResponse
    from test_matrix_bot import FakeSink, _job

    reporter = RecordingReporter()
    monkeypatch.setattr(matrix_bot, "health_reporter", reporter)
    bot, chat, _manager = _bot(tmp_path)
    bot._health_active = True
    for response in (
        ChatResponse(content="x", success=False, error="bridge_draining"),
        ChatResponse(content="x", success=False, error="coalesced_turn", failure_class="coalesced-turn"),
        ChatResponse(content="x", success=False, error="paused", failure_class="danso_task_paused"),
        ChatResponse(content="Recovery selection expired; use /task_recover.", success=False),
    ):
        chat.response = response
        await bot.run_turn(_job("hello"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert not [c for c in reporter.calls if c[0].startswith("record_agent")]
    # Outside serve() (health not bound) a completed turn records nothing.
    bot._health_active = False
    chat.response = ChatResponse(content="ok", session_id="s-1")
    await bot.run_turn(_job("hello"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert not [c for c in reporter.calls if c[0].startswith("record_agent")]


@pytest.mark.anyio
async def test_turn_exception_is_recorded_by_type_and_reraised(tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    reporter = RecordingReporter()
    monkeypatch.setattr(matrix_bot, "health_reporter", reporter)
    bot, _chat, _manager = _bot(tmp_path)
    bot._health_active = True

    async def boom() -> Any:
        raise RuntimeError("secret-looking detail")

    with pytest.raises(RuntimeError):
        await bot._record_turn_health(boom())
    assert reporter.calls == [("record_agent_error", ("agent turn failed: RuntimeError",), {})]

    async def cancelled() -> Any:
        raise asyncio.CancelledError

    reporter.calls.clear()
    with pytest.raises(asyncio.CancelledError):
        await bot._record_turn_health(cancelled())
    assert reporter.calls == []


# --- #1820: MatrixTransport signals -------------------------------------------


@pytest.mark.anyio
async def test_transport_signals_track_sync_retries_and_outbox_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from unittest.mock import patch

    from telegram_bot.core.matrix.transport import MatrixTransport
    from test_matrix_transport import FakeRunner, config, fake_aiohttp

    transport = MatrixTransport(config(tmp_path), FakeRunner("complete"))
    try:
        idle = transport.health_signals(now=1000.0)
        assert idle["sync_age_s"] is None and idle["outbox_pending"] == 0 and idle["outbox_head_age_s"] is None

        aiohttp = fake_aiohttp()

        async def failing() -> None:
            raise aiohttp.ClientError("https://homeserver.invalid/_matrix?access_token=x")

        with patch.dict(sys.modules, {"aiohttp": aiohttp}):
            task = asyncio.create_task(transport.retry(failing, leg="receive"))
            async with asyncio.timeout(5):
                while transport.leg_failures["receive"] < 1:
                    await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        signals = transport.health_signals(now=1000.0)
        assert signals["receive_failures"] == 1 and signals["receive_error"] == "ClientError"  # type name, never the URL
        assert transport.store.get_meta("health")["state"] == "network-retry"

        room = transport.c["rooms"][0]
        transport.enqueue_notice(room, "first", key="k1")
        transport.enqueue_notice(room, "second", key="k2")
        first = transport.health_signals(now=2000.0)
        assert first["outbox_pending"] == 2 and first["outbox_head_age_s"] == 0.0 and first["send_failures"] == 0
        transport.leg_failures["send"] += 2
        transport.leg_error["send"] = "group-key-share-incomplete"
        later = transport.health_signals(now=2150.0)
        assert later["outbox_head_age_s"] == 150.0 and later["send_failures"] == 2
        assert later["send_error"] == "group-key-share-incomplete"
        # Delivering the head starts a fresh observation (earlier failures cleared).
        transport.store.delivered(transport.store.outbox()[0]["event_id"])
        moved = transport.health_signals(now=2160.0)
        assert moved["outbox_pending"] == 1 and moved["outbox_head_age_s"] == 0.0
        assert moved["send_failures"] == 0 and moved["send_error"] == ""
        # A muted room's pending replies are not "stuck".
        transport.blocked.add(room)
        assert transport.health_signals(now=2500.0)["outbox_pending"] == 0
    finally:
        transport.store.close()


# --- #1820 review round: #1965 rejection streak, /skills, turn timeout -------


def test_delivery_rejection_streak_degrades_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reporter = RecordingReporter()
    monkeypatch.setattr(matrix_bot, "health_reporter", reporter)
    bot = _verdict_bot(tmp_path, fresh_signals())
    bot._transport.delivery_rejections_streak = 2  # below the threshold: still green
    bot._record_transport_health()
    assert [c[0] for c in _telegram_calls(reporter)] == ["record_telegram_ok"]
    reporter.calls.clear()
    bot._transport.delivery_rejections_streak = 5
    bot._record_transport_health()
    assert _telegram_calls(reporter) == [
        ("record_telegram_error", ("outbox-rejections",), {"consecutive_failures": 5})
    ]


@pytest.mark.anyio
async def test_skills_turn_records_agent_health(tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from telegram_bot.core.project_chat_types import ChatResponse
    from test_matrix_bot import FakeSink, _job

    reporter = RecordingReporter()
    monkeypatch.setattr(matrix_bot, "health_reporter", reporter)
    bot, chat, _manager = _bot(tmp_path)
    bot._health_active = True
    await bot.run_turn(_job("/skills"), sink=FakeSink(), session_id=None, room_kind="direct")
    chat.response = ChatResponse(content="x", success=False, error="provider exploded")
    await bot.run_turn(_job("/skills"), sink=FakeSink(), session_id=None, room_kind="direct")
    assert [c for c in reporter.calls if c[0].startswith("record_agent")] == [
        ("record_agent_ok", (), {}),
        ("record_agent_error", ("agent turn failed: provider exploded",), {}),
    ]


@pytest.mark.anyio
async def test_turn_timeout_cancellation_records_agent_error_but_stop_does_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reporter = RecordingReporter()
    monkeypatch.setattr(matrix_bot, "health_reporter", reporter)
    bot, _chat, _manager = _bot(tmp_path)
    bot._health_active = True
    bot._transport = FakeTransport({}, None)

    async def cancelled() -> Any:
        raise asyncio.CancelledError

    bot._transport.turn_timed_out = False  # /stop or shutdown
    with pytest.raises(asyncio.CancelledError):
        await bot._record_turn_health(cancelled())
    assert reporter.calls == []
    bot._transport.turn_timed_out = True  # transport turn_timeout expired
    with pytest.raises(asyncio.CancelledError):
        await bot._record_turn_health(cancelled())
    assert reporter.calls == [("record_agent_error", ("turn-timeout",), {})]


@pytest.mark.anyio
async def test_transport_flags_turn_timeout_before_cancelling_the_runner(tmp_path: Path) -> None:
    from telegram_bot.core.matrix.transport import TurnResult
    from test_matrix_transport import request, running

    seen: list[bool] = []

    async with running(tmp_path, turn_timeout=0.1) as h:
        f = h.f

        class HangingRunner:
            async def run(self, job: Any, *, sink: Any, session_id: Any, room_kind: str) -> TurnResult:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    seen.append(f.turn_timed_out)
                    raise
                return TurnResult("never", None)

            async def cancel(self, job: Any) -> bool:
                return True

        f.runner = HangingRunner()
        await f.input(request(f))
        h.work()
        await h.until(lambda: (f.store.get_meta("last_turn") or {}).get("outcome") == "timeout")
        assert seen == [True]
        # The next turn starts with the flag cleared.
        f.runner = h.runner
        h.drain()  # a scope claims nothing until its timeout notice is out
        await f.input(request(f, "$next"))
        await h.until(lambda: (f.store.get_meta("last_turn") or {}).get("event_id") == "$next")
        assert f.turn_timed_out is False
