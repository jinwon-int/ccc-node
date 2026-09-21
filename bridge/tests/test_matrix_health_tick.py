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
    holder = await _attach(bot)

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
