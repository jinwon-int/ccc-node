"""Crush runtime turn-liveness reporting for the stall probe (#1741).

The crush adapter's stall signal is the turn's last inbound SSE event (or
its prompt send), tracked on ``_ActiveTurn``; the engine verdict comes from
the spawned ``crush server`` process handle on the real transport. Fakes
without that surface must degrade to "unknown" — the fail-closed no-op —
never to a synthetic alive verdict.

All fixtures are synthetic: a scripted transport, a scripted process
handle, and a shared fake clock. No live provider is contacted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from telegram_bot.core.agent_runtime import (
    CompletionEvent,
    SessionRequest,
)
from telegram_bot.core.crush_runtime import (
    CrushEvent,
    CrushRuntime,
    CrushServerClient,
    CrushSession,
    CrushTurnLiveness,
)
from telegram_bot.core.turn_stall import (
    StallProbeMonitor,
    registered_turn_liveness_sources,
    unregister_turn_liveness,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeClock:
    """Scripted monotonic clock shared by the adapter and the monitor."""

    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeTransportBase:
    """Minimal CrushClient double: scripted events, recorded prompts."""

    def __init__(self) -> None:
        self.events: asyncio.Queue[CrushEvent] = asyncio.Queue()
        self.prompts: list[str] = []
        self.started = asyncio.Event()

    async def start(self) -> None:
        self.started.set()

    async def workspace_ensure(self, *, cwd: str, model: str | None) -> str:
        return "ws-1"

    async def session_create(self, workspace_id: str) -> str:
        return "sess-crush-1"

    async def session_exists(self, workspace_id: str, session_id: str) -> bool:
        return session_id == "sess-crush-1"

    async def prompt_send(
        self, workspace_id: str, session_id: str, text: str, *, run_id: str
    ) -> None:
        self.prompts.append(text)

    async def turn_cancel(self, workspace_id: str, session_id: str) -> None:
        return None

    async def permission_reply(
        self, workspace_id: str, request: Mapping[str, Any], decision: str
    ) -> None:
        return None

    async def list_models(self, workspace_id: str) -> Sequence[Mapping[str, Any]]:
        return []

    async def session_list(
        self, workspace_id: str, *, limit: int
    ) -> Sequence[Mapping[str, Any]]:
        return []

    async def session_messages(
        self, workspace_id: str, session_id: str
    ) -> Sequence[Mapping[str, Any]]:
        return []

    async def session_usage(
        self, workspace_id: str, session_id: str
    ) -> Mapping[str, Any]:
        return {}

    async def next_event(self) -> CrushEvent:
        return await self.events.get()

    async def close(self) -> None:
        return None


class FakeCrushTransport(_FakeTransportBase):
    """Transport double that can answer the stall probe's engine question."""

    def __init__(self, verdict: str) -> None:
        super().__init__()
        self.verdict = verdict

    def engine_verdict(self) -> str:
        return self.verdict


class MuteCrushTransport(_FakeTransportBase):
    """Transport double without any liveness surface (the fail-closed shape)."""


class _MuteCrushRuntime(CrushRuntime):
    """CrushRuntime bound to a transport double without an engine verdict."""

    def __init__(self, transport: MuteCrushTransport) -> None:
        super().__init__(client_factory=lambda: transport)  # type: ignore[arg-type,return-value]


def _message_event(session_id: str, parts: list[Mapping[str, Any]]) -> CrushEvent:
    return CrushEvent("message", "", "ws-1", session_id, {"role": "assistant", "parts": parts})


def _finish_part() -> Mapping[str, Any]:
    return {"type": "finish", "data": {"reason": "end_turn"}}


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


# -- CrushTurnLiveness activity reporting ---------------------------------------


@pytest.mark.anyio
async def test_turn_start_and_sse_events_stamp_last_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe's last-activity signal follows prompt send and SSE events."""
    clock = FakeClock()
    monkeypatch.setattr(
        "telegram_bot.core.crush_runtime.time", SimpleNamespace(monotonic=clock.monotonic)
    )
    transport = FakeCrushTransport("alive")
    runtime = CrushRuntime(client_factory=lambda: transport)  # type: ignore[arg-type,return-value]
    try:
        session = await runtime.start_or_resume(
            SessionRequest(working_directory=".", session_id="sess-crush-1")
        )
        assert isinstance(session, CrushSession)
        liveness = CrushTurnLiveness(runtime)

        # Before any turn the session has no activity sample: no signal.
        assert liveness.last_activity("sess-crush-1") is None

        stream = session.send_turn("hello")
        consumed: list[Any] = []

        async def consume() -> None:
            async for event in stream:
                consumed.append(event)

        task = asyncio.create_task(consume())
        await transport.started.wait()
        await _wait_until(lambda: bool(transport.prompts))
        # The prompt send stamped activity at the send itself.
        stamp_at_send = clock.now
        await _wait_until(
            lambda: liveness.last_activity("sess-crush-1") == stamp_at_send
        )

        clock.advance(5)
        await transport.events.put(_message_event("sess-crush-1", []))
        stamp_after_event = clock.now
        await _wait_until(
            lambda: liveness.last_activity("sess-crush-1") == stamp_after_event
        )

        await transport.events.put(_message_event("sess-crush-1", [_finish_part()]))
        await _wait_until(
            lambda: consumed
            and isinstance(consumed[-1], CompletionEvent)
        )
        await task
        # A finished turn is no longer a stall-probe subject: no signal.
        assert liveness.last_activity("sess-crush-1") is None
    finally:
        await runtime.close()


async def _recovery_scenario(
    monkeypatch: pytest.MonkeyPatch, transport: _FakeTransportBase, verdict: str
) -> tuple[int, int]:
    """Drive a stalled crush turn into a monitor tick; return (recoveries, probes)."""
    clock = FakeClock()
    monkeypatch.setattr(
        "telegram_bot.core.crush_runtime.time", SimpleNamespace(monotonic=clock.monotonic)
    )
    if isinstance(transport, FakeCrushTransport):
        transport.verdict = verdict
    runtime = (
        CrushRuntime(client_factory=lambda: transport)  # type: ignore[arg-type,return-value]
        if not isinstance(transport, MuteCrushTransport)
        else _MuteCrushRuntime(transport)
    )
    recoveries = 0
    notifications: list[tuple[int, str]] = []

    async def recover() -> None:
        nonlocal recoveries
        recoveries += 1

    async def notify(chat_id: int, text: str) -> bool:
        notifications.append((chat_id, text))
        return True

    try:
        session = await runtime.start_or_resume(
            SessionRequest(working_directory=".", session_id="sess-crush-1")
        )
        stream = session.send_turn("hello")

        async def consume() -> None:
            async for _ in stream:
                pass

        task = asyncio.create_task(consume())
        await _wait_until(lambda: bool(transport.prompts))

        monitor = StallProbeMonitor(
            turns_provider=lambda: [(7, 70, "sess-crush-1", 0.0)],
            liveness_probe=lambda: "dead",  # the Codex arm; must stay unused
            recover=recover,
            notifier=notify,
            sessions_roots=[],
            stall_seconds=600.0,
            clock=clock,
            wall_clock=FakeClock(0.0),
            turn_liveness_sources=[("crush", CrushTurnLiveness(runtime))],
        )
        # The turn went quiet the moment its prompt was sent; jump past the
        # stall threshold and let the probe consult the crush source.
        clock.advance(700)
        await monitor._tick()
        await transport.events.put(_message_event("sess-crush-1", [_finish_part()]))
        await task
    finally:
        await runtime.close()
    return recoveries, len(notifications)


@pytest.mark.anyio
async def test_stale_turn_with_dead_engine_triggers_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recoveries, notifications = await _recovery_scenario(
        monkeypatch, FakeCrushTransport("dead"), "dead"
    )
    assert (recoveries, notifications) == (1, 1)


@pytest.mark.anyio
async def test_stale_turn_with_live_engine_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recoveries, notifications = await _recovery_scenario(
        monkeypatch, FakeCrushTransport("alive"), "alive"
    )
    assert (recoveries, notifications) == (0, 0)


@pytest.mark.anyio
async def test_transport_without_liveness_surface_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No engine verdict available -> log-only no-op, never a recovery."""
    recoveries, notifications = await _recovery_scenario(
        monkeypatch, MuteCrushTransport(), "dead"
    )
    assert (recoveries, notifications) == (0, 0)


# -- CrushServerClient process verdict -------------------------------------------


def test_crush_server_client_engine_verdict_states(tmp_path: Path) -> None:
    client = CrushServerClient(
        process_environment={"PATH": "/usr/bin"},
        readiness_timeout_seconds=0.1,
    )
    # Never started: the transport cannot classify honestly.
    assert client.engine_verdict() == "unknown"

    class FakeProc:
        def __init__(self, code: int | None) -> None:
            self._code = code

        def poll(self) -> int | None:
            return self._code

    client._proc = FakeProc(None)  # type: ignore[assignment]
    assert client.engine_verdict() == "alive"
    client._proc = FakeProc(1)  # type: ignore[assignment]
    assert client.engine_verdict() == "dead"

    client._closed = True
    assert client.engine_verdict() == "unknown"  # deliberate close is not death


# -- registry lifecycle ------------------------------------------------------------


@pytest.mark.anyio
async def test_crush_runtime_registers_and_close_unregisters() -> None:
    runtime = CrushRuntime(client_factory=lambda: FakeCrushTransport("alive"))  # type: ignore[arg-type,return-value]
    try:
        names = [name for name, _ in registered_turn_liveness_sources()]
        assert "crush" in names
    finally:
        await runtime.close()
    names = [name for name, _ in registered_turn_liveness_sources()]
    assert "crush" not in names


@pytest.mark.anyio
async def test_registry_registration_reaches_a_default_bindings_monitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lifecycle-style monitor (no explicit sources) sees the crush source."""
    clock = FakeClock()
    monkeypatch.setattr(
        "telegram_bot.core.crush_runtime.time", SimpleNamespace(monotonic=clock.monotonic)
    )
    transport = FakeCrushTransport("dead")
    runtime = CrushRuntime(client_factory=lambda: transport)  # type: ignore[arg-type,return-value]
    recoveries = 0

    async def recover() -> None:
        nonlocal recoveries
        recoveries += 1

    try:
        session = await runtime.start_or_resume(
            SessionRequest(working_directory=".", session_id="sess-crush-1")
        )
        stream = session.send_turn("hello")

        async def consume() -> None:
            async for _ in stream:
                pass

        task = asyncio.create_task(consume())
        await _wait_until(lambda: bool(transport.prompts))

        monitor = StallProbeMonitor(
            turns_provider=lambda: [(7, 70, "sess-crush-1", 0.0)],
            liveness_probe=lambda: "dead",
            recover=recover,
            notifier=lambda chat_id, text: _async_true(),
            sessions_roots=[],
            stall_seconds=600.0,
            clock=clock,
            wall_clock=FakeClock(0.0),
            # turn_liveness_sources unset: the registry path the builder uses
        )
        clock.advance(700)
        await monitor._tick()
        assert recoveries == 1

        await transport.events.put(_message_event("sess-crush-1", [_finish_part()]))
        await task
    finally:
        unregister_turn_liveness("crush")
        await runtime.close()


async def _async_true() -> bool:
    return True
