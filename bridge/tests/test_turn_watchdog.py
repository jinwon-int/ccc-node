"""Turn-age watchdog: notify-only, once + cooldown, off by default (#1111)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core.turn_watchdog import TurnAgeWatchdog, turn_age_text


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Clock:
    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    def __init__(self) -> None:
        self.notifications: list[tuple[int, str]] = []

    async def notify(self, chat_id: int, text: str) -> bool:
        self.notifications.append((chat_id, text))
        return True


def test_turn_age_text_is_informational_and_names_stop() -> None:
    text = turn_age_text(47)
    assert "47" in text and "/stop" in text
    assert "No action needed" in text  # visibility, not an instruction


@pytest.mark.anyio
async def test_off_threshold_never_notifies() -> None:
    clock = Clock()
    recorder = Recorder()
    watchdog = TurnAgeWatchdog(
        turns_provider=lambda: [(7, 70, 0.0)],  # ancient turn
        notifier=recorder.notify,
        clock=clock,
        threshold_seconds=0.0,  # off
    )

    await watchdog._tick()
    await watchdog._tick()

    assert recorder.notifications == []


@pytest.mark.anyio
async def test_crossing_the_threshold_notifies_once() -> None:
    clock = Clock()
    recorder = Recorder()
    turns = [(7, 70, clock.now - 31 * 60)]  # 31 minutes old
    watchdog = TurnAgeWatchdog(
        turns_provider=lambda: turns,
        notifier=recorder.notify,
        clock=clock,
        threshold_seconds=30 * 60,
        renotify_seconds=30 * 60,
    )

    await watchdog._tick()
    await watchdog._tick()  # cooldown: no repeat

    assert len(recorder.notifications) == 1
    chat_id, text = recorder.notifications[0]
    assert chat_id == 70
    assert "31" in text and "/stop" in text


@pytest.mark.anyio
async def test_renotify_after_the_cooldown() -> None:
    clock = Clock()
    recorder = Recorder()
    turns = [(7, 70, clock.now - 31 * 60)]
    watchdog = TurnAgeWatchdog(
        turns_provider=lambda: turns,
        notifier=recorder.notify,
        clock=clock,
        threshold_seconds=30 * 60,
        renotify_seconds=30 * 60,
    )

    await watchdog._tick()
    clock.advance(29 * 60)
    await watchdog._tick()  # still inside the cooldown
    assert len(recorder.notifications) == 1

    clock.advance(2 * 60)
    await watchdog._tick()  # cooldown elapsed
    assert len(recorder.notifications) == 2


@pytest.mark.anyio
async def test_under_threshold_turns_are_left_alone() -> None:
    clock = Clock()
    recorder = Recorder()
    turns = [(7, 70, clock.now - 5 * 60)]
    watchdog = TurnAgeWatchdog(
        turns_provider=lambda: turns,
        notifier=recorder.notify,
        clock=clock,
        threshold_seconds=30 * 60,
    )

    await watchdog._tick()

    assert recorder.notifications == []


@pytest.mark.anyio
async def test_an_ended_turn_is_forgotten_so_the_next_one_notifies_fresh() -> None:
    clock = Clock()
    recorder = Recorder()
    state = {"turns": [(7, 70, clock.now - 31 * 60)]}
    watchdog = TurnAgeWatchdog(
        turns_provider=lambda: state["turns"],
        notifier=recorder.notify,
        clock=clock,
        threshold_seconds=30 * 60,
        renotify_seconds=30 * 60,
    )

    await watchdog._tick()
    assert len(recorder.notifications) == 1

    state["turns"] = []  # turn finished (or /stop) before the cooldown ends
    await watchdog._tick()

    state["turns"] = [(7, 70, clock.now - 31 * 60)]  # a new long turn starts
    await watchdog._tick()
    assert len(recorder.notifications) == 2


# ---------------------------------------------------------------------------
# Lifecycle wiring: default off, missing registry fails safe (#1111)
# ---------------------------------------------------------------------------


def test_lifecycle_watchdog_is_none_when_the_threshold_is_off(tmp_path: Path) -> None:
    from telegram_bot.core import bot_lifecycle

    lifecycle = bot_lifecycle.BotLifecycleMixin()
    lifecycle._config = SimpleNamespace(  # type: ignore[assignment]
        bot_data_dir=tmp_path, project_root=str(tmp_path)
    )
    assert lifecycle._build_turn_age_watchdog() is None


def test_lifecycle_watchdog_is_none_without_a_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from telegram_bot.core import bot_lifecycle

    monkeypatch.setenv("CCC_TURN_AGE_NOTIFY_MIN", "30")
    lifecycle = bot_lifecycle.BotLifecycleMixin()
    lifecycle._config = SimpleNamespace(  # type: ignore[assignment]
        bot_data_dir=tmp_path, project_root=str(tmp_path)
    )
    lifecycle._project_chat = SimpleNamespace()  # type: ignore[assignment]
    assert lifecycle._build_turn_age_watchdog() is None


def test_lifecycle_watchdog_builds_with_registry_and_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from telegram_bot.core import agent_session_registry, bot_lifecycle

    monkeypatch.setenv("CCC_TURN_AGE_NOTIFY_MIN", "30")
    lifecycle = bot_lifecycle.BotLifecycleMixin()
    lifecycle._config = SimpleNamespace(  # type: ignore[assignment]
        bot_data_dir=tmp_path, project_root=str(tmp_path)
    )
    lifecycle._project_chat = SimpleNamespace(  # type: ignore[assignment]
        _agent_session_registry=agent_session_registry.AgentSessionRegistry()
    )
    assert lifecycle._build_turn_age_watchdog() is not None


def test_registry_exposes_active_turn_ages() -> None:
    from telegram_bot.core.agent_session_registry import AgentSessionRegistry

    registry = AgentSessionRegistry()
    key = (7, 70)
    registry.register_active(key, object(), started_at=123.0)
    registry.register_active((9, 90), object(), started_at=456.0)

    ages = dict(registry.active_turn_ages())
    assert ages == {key: 123.0, (9, 90): 456.0}
