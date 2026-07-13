"""Transport-only polling reconnect keeps in-flight agent turns alive (#411).

A transient Telegram ``NetworkError``/``TimedOut`` previously tore down the
whole bridge lifecycle, cancelling in-progress AI turns with it. These tests
pin the decoupled contract: the updater reconnects with bounded backoff while
the Application object — and every in-flight agent task — survives untouched.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import telegram.error

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from telegram_bot.core.bot_shared import _PollingRestart


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def telegram_bot_class():
    chat_logger = sys.modules.get("telegram_bot.utils.chat_logger")
    if chat_logger is not None and not callable(getattr(chat_logger, "log_debug", None)):
        sys.modules.pop("telegram_bot.utils.chat_logger", None)
        sys.modules.pop("telegram_bot.core.bot", None)
    from telegram_bot.core.bot import TelegramBot

    return TelegramBot


class FakeHealth:
    """Counting stand-in for the module-global health reporter."""

    def __init__(self) -> None:
        self.reconnects = 0
        self.cancelled_by_transport = 0
        self.telegram_ok = 0
        self.telegram_errors: list[str] = []
        self.starting: list[str] = []

    def mark_starting(self, reason: str) -> None:
        self.starting.append(reason)

    def record_transport_reconnect(self) -> None:
        self.reconnects += 1

    def record_cancelled_by_transport(self, count: int = 1) -> None:
        self.cancelled_by_transport += count

    def record_telegram_ok(self) -> None:
        self.telegram_ok += 1

    def record_telegram_error(self, error: str, consecutive_failures=None) -> None:
        self.telegram_errors.append(error)


class FakeUpdater:
    def __init__(self, start_failures: int = 0, dies_instantly: bool = False) -> None:
        self.running = False
        self.stop_calls = 0
        self.start_calls: list[dict] = []
        self._start_failures = start_failures
        self._dies_instantly = dies_instantly

    async def stop(self) -> None:
        self.stop_calls += 1
        self.running = False

    async def start_polling(self, **kwargs) -> None:
        self.start_calls.append(kwargs)
        if self._start_failures:
            self._start_failures -= 1
            raise telegram.error.NetworkError("still down")
        # A dying transport accepts the start call but never stays running.
        self.running = not self._dies_instantly


def bare_lifecycle_bot(updater: FakeUpdater | None):
    import time

    bot = telegram_bot_class().__new__(telegram_bot_class())
    bot.application = (
        SimpleNamespace(updater=updater, bot=object(), running=True)
        if updater is not None
        else None
    )
    bot._clock = SimpleNamespace(time=time.monotonic)
    # Keep the exponential backoff sub-millisecond in tests.
    bot._RECONNECT_BASE_DELAY = 0.001
    bot._RECONNECT_MAX_DELAY = 0.001
    return bot


@pytest.fixture
def fake_health(monkeypatch: pytest.MonkeyPatch) -> FakeHealth:
    """Patch the exact globals the lifecycle methods read.

    Sibling test modules (test_watchdog) rebuild ``telegram_bot.core.*``
    entries in ``sys.modules`` at import time, so patching by dotted name can
    hit a different module object than the one whose functions execute here.
    Patching ``__globals__`` of the method itself is immune to that.
    """

    fake = FakeHealth()
    lifecycle_globals = telegram_bot_class()._reconnect_polling.__globals__
    monkeypatch.setitem(lifecycle_globals, "health_reporter", fake)
    return fake


@pytest.mark.anyio
async def test_reconnect_preserves_application_and_inflight_agent_task(
    fake_health: FakeHealth,
) -> None:
    updater = FakeUpdater()
    bot = bare_lifecycle_bot(updater)
    application_before = bot.application

    release = asyncio.Event()

    async def agent_turn() -> str:
        await release.wait()
        return "turn result"

    inflight = asyncio.create_task(agent_turn())
    await asyncio.sleep(0)

    ok = await bot._reconnect_polling(asyncio.Event())

    assert ok is True
    assert updater.running is True
    # The Application (bot pools, FIFO, handler state) survives untouched …
    assert bot.application is application_before
    # … and so does the in-flight agent turn.
    assert not inflight.cancelled() and not inflight.done()
    release.set()
    assert await inflight == "turn result"
    # Reconnects never drop the update backlog: messages sent during the
    # outage must not be lost.
    assert updater.start_calls[-1]["drop_pending_updates"] is False
    assert fake_health.reconnects == 1
    assert fake_health.telegram_ok == 1
    assert fake_health.cancelled_by_transport == 0


@pytest.mark.anyio
async def test_reconnect_is_bounded_then_reports_failure(fake_health: FakeHealth) -> None:
    updater = FakeUpdater(start_failures=999)
    bot = bare_lifecycle_bot(updater)

    ok = await bot._reconnect_polling(asyncio.Event())

    assert ok is False
    assert len(updater.start_calls) == bot._RECONNECT_ATTEMPTS
    assert len(fake_health.telegram_errors) == bot._RECONNECT_ATTEMPTS
    assert fake_health.reconnects == 0


@pytest.mark.anyio
async def test_reconnect_stops_early_when_shutdown_requested(
    fake_health: FakeHealth,
) -> None:
    updater = FakeUpdater(start_failures=999)
    bot = bare_lifecycle_bot(updater)
    stop_event = asyncio.Event()

    async def request_stop() -> None:
        await asyncio.sleep(0)
        stop_event.set()

    stopper = asyncio.create_task(request_stop())
    ok = await bot._reconnect_polling(stop_event)
    await stopper

    assert ok is False
    assert len(updater.start_calls) < bot._RECONNECT_ATTEMPTS


@pytest.mark.anyio
async def test_reconnect_without_application_fails_fast(fake_health: FakeHealth) -> None:
    bot = bare_lifecycle_bot(None)

    assert await bot._reconnect_polling(asyncio.Event()) is False
    assert fake_health.reconnects == 0


@pytest.mark.anyio
async def test_supervise_polling_recovers_transient_exit_without_escalating(
    fake_health: FakeHealth,
) -> None:
    updater = FakeUpdater()
    updater.running = False  # simulates the watchdog having stopped polling
    bot = bare_lifecycle_bot(updater)
    stop_event = asyncio.Event()

    async def stop_soon() -> None:
        # Give the supervised loop time to reconnect and re-enter waiting.
        for _ in range(200):
            if updater.running:
                break
            await asyncio.sleep(0.01)
        stop_event.set()

    stopper = asyncio.create_task(stop_soon())
    await asyncio.wait_for(bot._supervise_polling(stop_event), timeout=10)
    await stopper

    assert updater.running is True
    assert fake_health.reconnects == 1


@pytest.mark.anyio
async def test_supervise_polling_escalates_when_reconnect_fails(
    fake_health: FakeHealth,
) -> None:
    updater = FakeUpdater(start_failures=999)
    updater.running = False
    bot = bare_lifecycle_bot(updater)

    with pytest.raises(_PollingRestart):
        await bot._supervise_polling(asyncio.Event())

    assert len(updater.start_calls) == bot._RECONNECT_ATTEMPTS


@pytest.mark.anyio
async def test_supervise_polling_escalates_on_rapid_reconnect_death_loop(
    fake_health: FakeHealth,
) -> None:
    """A reconnect that keeps dying instantly must not hot-loop forever.

    Escalating after ``_MAX_RAPID_RECONNECT_CYCLES`` hands control back to the
    full rebuild path, whose rapid-crash accounting can reach SystemExit.
    """

    updater = FakeUpdater(dies_instantly=True)
    updater.running = False
    bot = bare_lifecycle_bot(updater)

    with pytest.raises(_PollingRestart):
        await asyncio.wait_for(bot._supervise_polling(asyncio.Event()), timeout=5)

    # Cycles 1..N-1 reconnect "successfully"; cycle N escalates instead.
    assert len(updater.start_calls) == bot._MAX_RAPID_RECONNECT_CYCLES - 1


def test_polling_error_callback_flags_only_permanent_errors(
    fake_health: FakeHealth,
) -> None:
    """PTB retries getUpdates forever with updater.running True; permanent
    errors surface only through this callback (#418 review)."""

    bot = bare_lifecycle_bot(FakeUpdater())

    bot._on_polling_error(telegram.error.NetworkError("blip"))
    assert getattr(bot, "_fatal_polling_error", None) is None

    conflict = telegram.error.Conflict("another instance is polling")
    bot._on_polling_error(conflict)
    assert bot._fatal_polling_error is conflict
    assert any("permanent polling failure" in e for e in fake_health.telegram_errors)


@pytest.mark.anyio
async def test_wait_for_polling_exit_surfaces_fatal_error_while_running(
    fake_health: FakeHealth,
) -> None:
    """The core #418 regression: a post-start Conflict leaves updater.running
    True, so the wait loop must check the fatal flag explicitly."""

    updater = FakeUpdater()
    updater.running = True
    bot = bare_lifecycle_bot(updater)
    bot._on_polling_error(telegram.error.Conflict("duplicate poller"))

    with pytest.raises(_PollingRestart):
        await asyncio.wait_for(bot._wait_for_polling_exit(asyncio.Event()), timeout=5)


@pytest.mark.anyio
async def test_supervise_polling_fails_closed_on_permanent_polling_error(
    fake_health: FakeHealth,
) -> None:
    updater = FakeUpdater()
    updater.running = True
    bot = bare_lifecycle_bot(updater)
    conflict = telegram.error.Conflict("duplicate poller")
    bot._on_polling_error(conflict)

    with pytest.raises(telegram.error.Conflict):
        await asyncio.wait_for(bot._supervise_polling(asyncio.Event()), timeout=5)

    # The permanent error is never "reconnected around".
    assert updater.start_calls == []


@pytest.mark.anyio
async def test_reconnect_registers_polling_error_callback(
    fake_health: FakeHealth,
) -> None:
    updater = FakeUpdater()
    bot = bare_lifecycle_bot(updater)

    assert await bot._reconnect_polling(asyncio.Event()) is True
    assert updater.start_calls[-1]["error_callback"] == bot._on_polling_error


def test_only_first_polling_start_drops_backlog() -> None:
    bot = bare_lifecycle_bot(FakeUpdater())

    assert bot._consume_initial_polling_start() is True
    assert bot._consume_initial_polling_start() is False
    assert bot._consume_initial_polling_start() is False


@pytest.mark.anyio
async def test_permanent_failure_attributes_cancelled_requests(
    fake_health: FakeHealth,
) -> None:
    bot = bare_lifecycle_bot(FakeUpdater())
    bot._project_chat = SimpleNamespace(workload_snapshot=lambda now: (2, 42.0))

    bot._record_transport_teardown("telegram token revoked or bot blocked")

    assert fake_health.cancelled_by_transport == 2


@pytest.mark.anyio
async def test_teardown_attribution_is_silent_when_idle(fake_health: FakeHealth) -> None:
    bot = bare_lifecycle_bot(FakeUpdater())
    bot._project_chat = SimpleNamespace(workload_snapshot=lambda now: (0, 0.0))

    bot._record_transport_teardown("telegram getUpdates conflict")

    assert fake_health.cancelled_by_transport == 0


def test_health_reporter_exposes_transport_counters(tmp_path: Path) -> None:
    import importlib
    import json

    # Sibling test modules inject a fake telegram_bot.utils.health; reload the
    # real one (the conftest autouse fixture restores the previous entry after).
    sys.modules.pop("telegram_bot.utils.health", None)
    health_module = importlib.import_module("telegram_bot.utils.health")

    reporter = health_module.RuntimeHealthReporter(tmp_path / ".telegram_bot")
    reporter.record_transport_reconnect()
    reporter.record_transport_reconnect()
    reporter.record_cancelled_by_transport(3)

    snapshot = reporter.snapshot()
    assert snapshot["transport"] == {"reconnects": 2, "cancelled_by_transport": 3}
    on_disk = json.loads(reporter.health_file.read_text(encoding="utf-8"))
    assert on_disk["transport"] == {"reconnects": 2, "cancelled_by_transport": 3}
