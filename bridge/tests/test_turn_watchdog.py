"""Turn-age watchdog: notify-only, once + cooldown (#1111)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core.turn_watchdog import (
    DEFAULT_NOTIFY_MINUTES,
    TurnAgeWatchdog,
    turn_age_text,
)


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
    def __init__(self, outcomes: list[bool] | None = None) -> None:
        self.notifications: list[tuple[int, str]] = []
        self.outcomes = list(outcomes or ())

    async def notify(self, chat_id: int, text: str) -> bool:
        self.notifications.append((chat_id, text))
        return self.outcomes.pop(0) if self.outcomes else True


def test_turn_age_text_is_informational_and_names_stop() -> None:
    text = turn_age_text(47)
    assert "47" in text and "/stop" in text
    assert "does not establish progress" in text
    assert "still working" not in text
    assert "No action needed" not in text


def test_watchdog_default_threshold_is_thirty_minutes() -> None:
    watchdog = TurnAgeWatchdog(
        turns_provider=lambda: [],
        notifier=Recorder().notify,
    )

    assert DEFAULT_NOTIFY_MINUTES == 30
    assert watchdog._threshold == DEFAULT_NOTIFY_MINUTES * 60.0


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
async def test_delivery_failure_does_not_start_the_cooldown() -> None:
    clock = Clock()
    recorder = Recorder([False, True])
    turns = [(7, 70, clock.now - 31 * 60)]
    watchdog = TurnAgeWatchdog(
        turns_provider=lambda: turns,
        notifier=recorder.notify,
        clock=clock,
        threshold_seconds=30 * 60,
        renotify_seconds=30 * 60,
    )

    await watchdog._tick()
    await watchdog._tick()

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
    assert watchdog._last_notified == {}

    state["turns"] = [(7, 70, clock.now - 31 * 60)]  # a new long turn starts
    await watchdog._tick()
    assert len(recorder.notifications) == 2


@pytest.mark.anyio
async def test_replacement_turn_does_not_inherit_cooldown_without_empty_tick() -> None:
    clock = Clock(start=1_000.0)
    recorder = Recorder()
    state = {"turns": [(7, 70, 940.0)]}
    watchdog = TurnAgeWatchdog(
        turns_provider=lambda: state["turns"],
        notifier=recorder.notify,
        clock=clock,
        threshold_seconds=60.0,
        renotify_seconds=600.0,
    )

    await watchdog._tick()  # old turn crosses the threshold at t=1000
    state["turns"] = [(7, 70, 1_001.0)]  # replaced before the next tick
    clock.advance(62.0)
    await watchdog._tick()  # replacement is 61s old at t=1062

    assert len(recorder.notifications) == 2
    assert set(watchdog._last_notified) == {(7, 70, 1_001.0)}


@pytest.mark.anyio
async def test_conversation_cooldowns_are_independent() -> None:
    clock = Clock()
    recorder = Recorder()
    turns = [
        (7, 70, clock.now - 31 * 60),
        (8, 80, clock.now - 31 * 60),
    ]
    watchdog = TurnAgeWatchdog(
        turns_provider=lambda: turns,
        notifier=recorder.notify,
        clock=clock,
        threshold_seconds=30 * 60,
        renotify_seconds=30 * 60,
    )

    await watchdog._tick()
    await watchdog._tick()

    assert [chat_id for chat_id, _text in recorder.notifications] == [70, 80]


# ---------------------------------------------------------------------------
# Lifecycle wiring: default, explicit off, missing registry fails safe (#1111)
# ---------------------------------------------------------------------------


def test_lifecycle_watchdog_is_none_when_the_threshold_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from telegram_bot.core import bot_lifecycle

    monkeypatch.setenv("CCC_TURN_AGE_NOTIFY_MIN", "0")
    lifecycle = bot_lifecycle.BotLifecycleMixin()
    lifecycle._config = SimpleNamespace(  # type: ignore[assignment]
        bot_data_dir=tmp_path, project_root=str(tmp_path)
    )
    assert lifecycle._build_turn_age_watchdog() is None


def test_lifecycle_watchdog_defaults_to_thirty_minutes_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from telegram_bot.core import agent_session_registry, bot_lifecycle

    monkeypatch.delenv("CCC_TURN_AGE_NOTIFY_MIN", raising=False)
    monkeypatch.delenv("CCC_TURN_AGE_RENOTIFY_MIN", raising=False)
    lifecycle = bot_lifecycle.BotLifecycleMixin()
    lifecycle._config = SimpleNamespace(  # type: ignore[assignment]
        bot_data_dir=tmp_path, project_root=str(tmp_path)
    )
    lifecycle._project_chat = SimpleNamespace(  # type: ignore[assignment]
        _agent_session_registry=agent_session_registry.AgentSessionRegistry()
    )

    watchdog = lifecycle._build_turn_age_watchdog()

    assert watchdog is not None
    assert watchdog._threshold == DEFAULT_NOTIFY_MINUTES * 60.0
    assert watchdog._renotify == 30 * 60.0


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

    monkeypatch.setenv("CCC_TURN_AGE_NOTIFY_MIN", "45")
    lifecycle = bot_lifecycle.BotLifecycleMixin()
    lifecycle._config = SimpleNamespace(  # type: ignore[assignment]
        bot_data_dir=tmp_path, project_root=str(tmp_path)
    )
    lifecycle._project_chat = SimpleNamespace(  # type: ignore[assignment]
        _agent_session_registry=agent_session_registry.AgentSessionRegistry()
    )
    watchdog = lifecycle._build_turn_age_watchdog()
    assert watchdog is not None
    assert watchdog._threshold == 45 * 60.0


@pytest.mark.anyio
async def test_registry_watchdog_is_provider_neutral_and_never_sends_turn_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from telegram_bot.core import agent_session_registry, bot_lifecycle

    clock = Clock()
    registry = agent_session_registry.AgentSessionRegistry()
    for index, provider in enumerate(("piri", "codex", "other"), start=1):
        registry.register_active(
            (index, 100 + index),
            SimpleNamespace(provider=provider, prompt="synthetic prompt"),
            started_at=clock.now - 31 * 60,
        )

    monkeypatch.setenv("CCC_TURN_AGE_NOTIFY_MIN", "30")
    lifecycle = bot_lifecycle.BotLifecycleMixin()
    lifecycle._config = SimpleNamespace(  # type: ignore[assignment]
        bot_data_dir=tmp_path, project_root=str(tmp_path)
    )
    lifecycle._project_chat = SimpleNamespace(  # type: ignore[assignment]
        _agent_session_registry=registry
    )
    watchdog = lifecycle._build_turn_age_watchdog()
    assert watchdog is not None
    watchdog._clock = clock
    recorder = Recorder()
    watchdog._notifier = recorder.notify

    await watchdog._tick()

    assert [chat_id for chat_id, _text in recorder.notifications] == [101, 102, 103]
    assert all(
        "synthetic prompt" not in text for _chat_id, text in recorder.notifications
    )
    assert all(
        "does not establish progress" in text
        for _chat_id, text in recorder.notifications
    )


def test_registry_exposes_active_turn_ages() -> None:
    from telegram_bot.core.agent_session_registry import AgentSessionRegistry

    registry = AgentSessionRegistry()
    key = (7, 70)
    registry.register_active(key, object(), started_at=123.0)
    registry.register_active((9, 90), object(), started_at=456.0)

    ages = dict(registry.active_turn_ages())
    assert ages == {key: 123.0, (9, 90): 456.0}
