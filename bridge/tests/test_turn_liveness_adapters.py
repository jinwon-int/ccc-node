"""Per-adapter turn-liveness wiring for the stall probe (#1741).

Piri, Grok, Danso, and Claude each register a ``TurnLivenessSource`` so the
provider-agnostic stall probe can ask "has this turn's underlying process
produced any new output/RPC activity in the last N minutes?" — and, once a
turn is stale, whether the engine is confirmed dead. Every adapter must
degrade fail-closed: an untracked session id is "no signal", and an engine
that cannot be classified honestly is "unknown" (log-only), never a
synthetic alive verdict.

All fixtures are synthetic (scripted transports, fake processes, a shared
fake clock). No live provider or CLI is contacted.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_agent_sdk import SystemMessage

from telegram_bot.core.agent_runtime import SessionRequest
from telegram_bot.core.claude_runtime import (
    ClaudeRuntime,
    ClaudeSession,
    ClaudeTurnLiveness,
    _LivenessSdkClient,
)
from telegram_bot.core.danso_runtime import (
    DansoRuntime,
    DansoTurnLiveness,
    _DansoLivenessSession,
)
from telegram_bot.core.grok_journal import GrokBinding, GrokJournal
from telegram_bot.core.grok_protocol import HOST_VERSION
from telegram_bot.core.grok_runtime import GrokRuntime, GrokTurnLiveness
from telegram_bot.core.piri_rpc import PiriRpcProcessClient
from telegram_bot.core.piri_runtime import PiriRuntime, PiriTurnLiveness
from telegram_bot.core.turn_stall import (
    StallProbeMonitor,
    registered_turn_liveness_sources,
    unregister_turn_liveness,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeClock:
    """Scripted monotonic clock shared by the adapter under test and the monitor."""

    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    def __init__(self) -> None:
        self.notifications: list[tuple[int, str]] = []
        self.recoveries = 0

    async def notify(self, chat_id: int, text: str) -> bool:
        self.notifications.append((chat_id, text))
        return True

    async def recover(self) -> None:
        self.recoveries += 1


def _monitor(
    clock: FakeClock,
    recorder: Recorder,
    bindings: Sequence[tuple[str, Any]],
    thread_id: str,
    *,
    stall_seconds: float = 600.0,
) -> StallProbeMonitor:
    return StallProbeMonitor(
        turns_provider=lambda: [(7, 70, thread_id, 0.0)],
        liveness_probe=lambda: "dead",  # Codex arm; must stay unused here
        recover=recorder.recover,
        notifier=recorder.notify,
        sessions_roots=[],
        stall_seconds=stall_seconds,
        clock=clock,
        wall_clock=FakeClock(0.0),
        turn_liveness_sources=list(bindings),
    )


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def _registered_names() -> list[str]:
    return [name for name, _ in registered_turn_liveness_sources()]


# -- Piri -------------------------------------------------------------------------


class FakePiriClient:
    """Scripted PiriClient with an optional #1741 liveness surface."""

    def __init__(self, session_id: str, *, last_activity: float | None = None,
                 verdict: str | None = None) -> None:
        self.state: Mapping[str, Any] = {"sessionId": session_id, "model": None}
        self.last_activity = last_activity
        self.verdict = verdict

    async def start(self) -> None:
        return None

    async def prompt(self, message: str) -> None:
        return None

    async def abort(self) -> None:
        return None

    async def get_state(self) -> Mapping[str, Any]:
        return self.state

    async def set_append_system_prompt(self, text: str | None = None) -> None:
        return None

    async def get_available_models(self) -> Sequence[Mapping[str, Any]]:
        return ()

    async def next_event(self) -> Mapping[str, Any]:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def close(self) -> None:
        return None

    def last_activity_monotonic(self) -> float | None:
        return self.last_activity

    def engine_verdict(self) -> str:
        assert self.verdict is not None
        return self.verdict


class BarePiriClient(FakePiriClient):
    """A PiriClient double stripped of any #1741 liveness surface (legacy)."""

    def __init__(self, session_id: str) -> None:
        super().__init__(session_id)
        del self.last_activity
        del self.verdict


# Remove the inherited liveness methods entirely: getattr then reports a
# client without the surface, which the adapter must treat as "no signal".
BarePiriClient.last_activity_monotonic = None  # type: ignore[assignment]
BarePiriClient.engine_verdict = None  # type: ignore[assignment]


def _piri_runtime(client: FakePiriClient) -> PiriRuntime:
    return PiriRuntime(
        client_factory=lambda config: client,  # type: ignore[arg-type,return-value]
        process_environment={"PATH": "/usr/bin"},
    )


@pytest.mark.anyio
async def test_piri_liveness_wiring_reports_tracked_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    client = FakePiriClient(
        "piri-sess-1", last_activity=clock.now, verdict="dead"
    )
    runtime = _piri_runtime(client)
    try:
        session = await runtime.start_or_resume(
            SessionRequest(working_directory=".", session_id="piri-sess-1")
        )
        assert session.session_id == "piri-sess-1"
        liveness = PiriTurnLiveness(runtime)

        assert liveness.last_activity("piri-sess-1") == clock.now
        assert liveness.last_activity("piri-unknown") is None  # no signal
        assert liveness.engine_verdict("piri-sess-1") == "dead"
        assert liveness.engine_verdict("piri-unknown") == "unknown"
    finally:
        unregister_turn_liveness("piri")
        await runtime.close()


@pytest.mark.anyio
async def test_piri_client_without_liveness_surface_fails_closed() -> None:
    runtime = _piri_runtime(BarePiriClient("piri-sess-1"))
    try:
        await runtime.start_or_resume(
            SessionRequest(working_directory=".", session_id="piri-sess-1")
        )
        liveness = PiriTurnLiveness(runtime)
        assert liveness.last_activity("piri-sess-1") is None
        assert liveness.engine_verdict("piri-sess-1") == "unknown"
    finally:
        unregister_turn_liveness("piri")
        await runtime.close()


def test_piri_rpc_process_client_liveness_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real transport stamps RPC frames and classifies its process."""
    clock = FakeClock()
    monkeypatch.setattr(
        "telegram_bot.core.piri_rpc.time", SimpleNamespace(monotonic=clock.monotonic)
    )
    client = PiriRpcProcessClient(["piri", "--mode", "rpc"], working_directory=".")
    assert client.last_activity_monotonic() is None  # no frame yet: no signal
    assert client.engine_verdict() == "unknown"  # never started

    client._process = SimpleNamespace(returncode=None)
    assert client.engine_verdict() == "alive"

    client._process = SimpleNamespace(returncode=1)
    assert client.engine_verdict() == "dead"  # exited while still open

    client._closed = True
    assert client.engine_verdict() == "unknown"  # deliberate close is not death

    client._closed = False
    client._process = SimpleNamespace(returncode=None)
    clock.advance(5)
    client._note_activity()
    assert client.last_activity_monotonic() == clock.now


@pytest.mark.anyio
async def test_piri_stale_dead_engine_end_to_end_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    client = FakePiriClient(
        "piri-sess-1", last_activity=clock.now, verdict="dead"
    )
    runtime = _piri_runtime(client)
    recorder = Recorder()
    try:
        await runtime.start_or_resume(
            SessionRequest(working_directory=".", session_id="piri-sess-1")
        )
        monitor = _monitor(
            clock, recorder, [("piri", PiriTurnLiveness(runtime))], "piri-sess-1"
        )
        clock.advance(700)
        await monitor._tick()
        assert recorder.recoveries == 1
        assert recorder.notifications and "died silently" in recorder.notifications[0][1]
    finally:
        unregister_turn_liveness("piri")
        await runtime.close()


@pytest.mark.anyio
async def test_piri_registration_lifecycle() -> None:
    runtime = _piri_runtime(FakePiriClient("piri-sess-1"))
    try:
        assert "piri" in _registered_names()
    finally:
        await runtime.close()
    assert "piri" not in _registered_names()


# -- Grok -------------------------------------------------------------------------


class FakeGrokTransport:
    """Scripted GrokTransport answering only the status probe."""

    destination = "box@fixture.invalid"
    agent_id = "00000000-0000-4000-8000-000000000001"

    def __init__(self, verdict_getter=None) -> None:
        self.calls: list[str] = []
        self.verdict_getter = verdict_getter

    async def call(self, operation: str, arguments: Any = None) -> Any:
        self.calls.append(operation)
        if operation == "status":
            return {
                "hostVersion": HOST_VERSION,
                "capabilities": ["orderedReplicasV1", "sendAcceptanceV1"],
                "isBusy": False,
            }
        raise AssertionError(f"unqualified RPC {operation}")

    def engine_verdict(self) -> str:
        assert self.verdict_getter is not None
        return self.verdict_getter()


def _grok_runtime(tmp_path: Path, transport: FakeGrokTransport) -> GrokRuntime:
    binding = GrokBinding(
        transport.destination, transport.agent_id, "owner-dm", str(tmp_path)
    )
    journal = GrokJournal(tmp_path / "journal", binding)
    journal.create()
    return GrokRuntime(journal, transport)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_grok_liveness_wiring_tracks_host_rpcs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Completed host RPCs stamp activity; the verdict stays fail-closed."""
    clock = FakeClock()
    monkeypatch.setattr(
        "telegram_bot.core.grok_runtime.time", SimpleNamespace(monotonic=clock.monotonic)
    )
    transport = FakeGrokTransport()
    runtime = _grok_runtime(tmp_path, transport)
    try:
        session = await runtime.start_or_resume(
            SessionRequest(str(tmp_path), session_id=runtime.journal.binding.session_id)
        )
        session_id = session.session_id
        liveness = GrokTurnLiveness(runtime)

        # start_or_resume already status-probes the host, so activity exists;
        # the point is that each completed RPC re-stamps it.
        initial = liveness.last_activity(session_id)
        assert initial is not None
        clock.advance(3)
        await runtime._call("status")
        assert liveness.last_activity(session_id) == clock.now > initial
        assert liveness.last_activity("grok-unknown") is None
        # No transport surface: a remote Bot engine cannot be confirmed dead.
        assert liveness.engine_verdict(session_id) == "unknown"
    finally:
        unregister_turn_liveness("grok")


@pytest.mark.anyio
async def test_grok_transport_surface_can_confirm_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(
        "telegram_bot.core.grok_runtime.time", SimpleNamespace(monotonic=clock.monotonic)
    )
    transport = FakeGrokTransport(verdict_getter=lambda: "dead")
    runtime = _grok_runtime(tmp_path, transport)
    recorder = Recorder()
    try:
        session = await runtime.start_or_resume(
            SessionRequest(str(tmp_path), session_id=runtime.journal.binding.session_id)
        )
        session_id = session.session_id
        await runtime._call("status")  # activity stamp
        monitor = _monitor(
            clock, recorder, [("grok", GrokTurnLiveness(runtime))], session_id
        )
        clock.advance(700)
        await monitor._tick()
        assert recorder.recoveries == 1  # the transport itself confirmed death
    finally:
        unregister_turn_liveness("grok")


@pytest.mark.anyio
async def test_grok_registration_is_process_wide(tmp_path: Path) -> None:
    transport = FakeGrokTransport()
    _grok_runtime(tmp_path, transport)  # construction itself registers
    try:
        assert "grok" in _registered_names()
    finally:
        unregister_turn_liveness("grok")


# -- Danso ------------------------------------------------------------------------


class _StubDansoInner:
    """Duck-typed stand-in for the worker session the wrapper delegates to."""

    def __init__(self, session_id: str, process: Any = None) -> None:
        self.session_id = session_id
        self._process = process
        self.turns: list[str] = []
        self.patched: Any = None

    async def interrupt(self) -> None:
        return None

    async def send_turn(self, message: str, *, approval_handler: Any = None):
        self.turns.append(message)
        for item in ("a", "b"):
            yield item


@pytest.mark.anyio
async def test_danso_wrapper_stamps_turn_output_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(
        "telegram_bot.core.danso_runtime.time", SimpleNamespace(monotonic=clock.monotonic)
    )
    inner = _StubDansoInner("danso-sess-1")
    wrapped = _DansoLivenessSession(inner)
    start_stamp = clock.now

    events = [event async for event in wrapped.send_turn("hello")]

    assert events == ["a", "b"]
    assert wrapped.last_activity_monotonic == start_stamp
    clock.advance(4)
    inner_turn = [event async for event in inner.send_turn("second")]  # direct inner
    assert inner_turn == ["a", "b"]


@pytest.mark.anyio
async def test_danso_wrapper_writes_attributes_through_and_delegates() -> None:
    inner = _StubDansoInner("danso-sess-1")
    wrapped = _DansoLivenessSession(inner)

    wrapped.authorize_task_resume = "patched"  # type: ignore[assignment]
    assert inner.authorize_task_resume == "patched"  # landed on the inner session
    wrapped.interrupt  # attribute delegation reaches the inner session


@pytest.mark.anyio
async def test_danso_engine_verdict_follows_the_turn_subprocess() -> None:
    inner = _StubDansoInner("danso-sess-1", process=None)
    wrapped = _DansoLivenessSession(inner)
    assert wrapped.engine_verdict() == "unknown"  # between turns: no engine

    inner._process = SimpleNamespace(returncode=None)
    assert wrapped.engine_verdict() == "alive"
    inner._process = SimpleNamespace(returncode=3)
    assert wrapped.engine_verdict() == "dead"


def _danso_runtime(tmp_path: Path) -> DansoRuntime:
    binary = tmp_path / "danso-fixture"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    return DansoRuntime(
        binary=str(binary),
        state_directory=tmp_path / "state" / "journals",
        provider="openai",
        model="gpt-6-astra",
        environment={"HOME": str(tmp_path / "home"), "OPENAI_API_KEY": "fixture-key"},
        default_effort="medium",
    )


@pytest.mark.anyio
async def test_danso_start_or_resume_wraps_sessions_for_liveness(
    tmp_path: Path,
) -> None:
    runtime = _danso_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        # A brand-new session (no resume id): the worker allocates the UUID.
        session = await runtime.start_or_resume(SessionRequest(str(workspace)))
        assert isinstance(session, _DansoLivenessSession)
        session_id = session.session_id
        liveness = DansoTurnLiveness(runtime)
        assert liveness.last_activity(session_id) is not None
        assert liveness.engine_verdict(session_id) == "unknown"
        assert liveness.last_activity("danso-unknown") is None

        # A resumed session id replaces its wrapper instead of growing the map.
        journal = runtime.root / (session_id + ".jsonl")
        journal.write_text('{"type":"session"}\n')
        journal.chmod(0o600)
        await runtime.start_or_resume(
            SessionRequest(str(workspace), session_id=session_id)
        )
        assert len(runtime._liveness_sessions) == 1
    finally:
        unregister_turn_liveness("danso")


@pytest.mark.anyio
async def test_danso_stale_dead_engine_end_to_end_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(
        "telegram_bot.core.danso_runtime.time", SimpleNamespace(monotonic=clock.monotonic)
    )
    runtime = _danso_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    recorder = Recorder()
    try:
        session = await runtime.start_or_resume(SessionRequest(str(workspace)))
        # Simulate a turn whose subprocess died and whose output went silent.
        session._inner._process = SimpleNamespace(returncode=1)
        monitor = _monitor(
            clock,
            recorder,
            [("danso", DansoTurnLiveness(runtime))],
            session.session_id,
        )
        clock.advance(700)
        await monitor._tick()
        assert recorder.recoveries == 1
    finally:
        unregister_turn_liveness("danso")


# -- Claude -----------------------------------------------------------------------


class FakeClaudeSdkClient:
    """Scripted SdkClient: frames flow from a queue until the sentinel."""

    def __init__(self) -> None:
        self.frames: asyncio.Queue[Any] = asyncio.Queue()
        self.queries: list[str] = []
        self.disconnects = 0

    async def connect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_messages(self) -> AsyncIterator[Any]:
        while True:
            message = await self.frames.get()
            if message is None:
                return
            yield message

    async def interrupt(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnects += 1


def _claude_runtime(tmp_path: Path, client: FakeClaudeSdkClient) -> ClaudeRuntime:
    return ClaudeRuntime(
        sdk_client_factory=lambda options: client,  # type: ignore[arg-type,return-value]
        transcripts_dir=str(tmp_path / "transcripts"),
    )


@pytest.mark.anyio
async def test_claude_liveness_wiring_stamps_frames_and_reader_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(
        "telegram_bot.core.claude_runtime.time", SimpleNamespace(monotonic=clock.monotonic)
    )
    client = FakeClaudeSdkClient()
    runtime = _claude_runtime(tmp_path, client)
    try:
        session = await runtime.start_or_resume(
            SessionRequest(working_directory=str(tmp_path))
        )
        assert isinstance(session, ClaudeSession)
        assert isinstance(session._client, _LivenessSdkClient)
        session_id = session.session_id
        liveness = ClaudeTurnLiveness(runtime)

        assert liveness.last_activity(session_id) is None  # no frames yet
        assert liveness.engine_verdict(session_id) == "unknown"  # reader running

        await client.frames.put(SystemMessage(subtype="x", data={"session_id": session_id}))
        stamp = clock.now
        await _wait_until(lambda: liveness.last_activity(session_id) == stamp)

        clock.advance(5)
        await client.frames.put(SystemMessage(subtype="x", data={"session_id": session_id}))
        await _wait_until(lambda: liveness.last_activity(session_id) == clock.now)

        # The stream ending while the session is open is the confirmable death.
        await client.frames.put(None)
        await _wait_until(lambda: liveness.engine_verdict(session_id) == "dead")
        assert liveness.last_activity("claude-unknown") is None
        assert liveness.engine_verdict("claude-unknown") == "unknown"

        await session.close()
        assert liveness.engine_verdict(session_id) == "unknown"  # closed: not death
    finally:
        unregister_turn_liveness("claude")


@pytest.mark.anyio
async def test_claude_stale_dead_engine_end_to_end_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(
        "telegram_bot.core.claude_runtime.time", SimpleNamespace(monotonic=clock.monotonic)
    )
    client = FakeClaudeSdkClient()
    runtime = _claude_runtime(tmp_path, client)
    recorder = Recorder()
    try:
        session = await runtime.start_or_resume(
            SessionRequest(working_directory=str(tmp_path))
        )
        session_id = session.session_id
        await client.frames.put(SystemMessage(subtype="x", data={"session_id": session_id}))
        await _wait_until(lambda: session._last_activity is not None)

        monitor = _monitor(
            clock, recorder, [("claude", ClaudeTurnLiveness(runtime))], session_id
        )
        await client.frames.put(None)  # the CLI transport stream terminates
        await _wait_until(lambda: session._reader_task is not None and session._reader_task.done())
        clock.advance(700)
        await monitor._tick()
        assert recorder.recoveries == 1
    finally:
        unregister_turn_liveness("claude")
        await runtime.close()


@pytest.mark.anyio
async def test_claude_registration_lifecycle(tmp_path: Path) -> None:
    runtime = _claude_runtime(tmp_path, FakeClaudeSdkClient())
    try:
        assert "claude" in _registered_names()
    finally:
        await runtime.close()
    assert "claude" not in _registered_names()
