"""Turn-stall probe + orphan tool-call loop detection (#1112)."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core.turn_stall import (
    ORPHAN_LOOP_PATTERN,
    OrphanLoopTracker,
    StallProbeMonitor,
    engine_dead_notification_text,
    find_rollout,
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


# --- orphan loop tracker ------------------------------------------------------


def test_tracker_counts_only_the_pattern_inside_the_window() -> None:
    clock = Clock()
    tracker = OrphanLoopTracker(clock=clock)
    assert tracker.record_line(f"ERROR {ORPHAN_LOOP_PATTERN} for call xyz") == 1
    assert tracker.record_line("ordinary stderr noise") == 1
    for _ in range(8):
        tracker.record_line(f"ERROR {ORPHAN_LOOP_PATTERN}")
    assert tracker.recent_count() == 9

    clock.advance(301)
    assert tracker.recent_count() == 0  # window slid past


# --- rollout locator ----------------------------------------------------------


def _make_rollout(root: Path, thread_id: str, when: str = "2026-08-14T09-03-00") -> Path:
    path = root / "sessions" / "2026" / "08" / "14" / f"rollout-{when}-{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    return path


def test_find_rollout_matches_the_thread_id_suffix(tmp_path: Path) -> None:
    thread_id = "019fcf78-e982-7521-8a5c-d2ba6221ef60"
    mine = _make_rollout(tmp_path, thread_id)
    _make_rollout(tmp_path, "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")

    assert find_rollout([tmp_path], thread_id) == mine
    assert find_rollout([tmp_path], "00000000-0000-4000-8000-000000000000") is None
    assert find_rollout([tmp_path], "") is None
    assert find_rollout([tmp_path / "missing"], thread_id) is None


# --- stall probe monitor --------------------------------------------------------


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
    tmp_path: Path,
    recorder: Recorder,
    clock: Clock,
    *,
    liveness: str,
    stall_seconds: float = 600.0,
    wall_clock=None,
) -> StallProbeMonitor:
    rollout = tmp_path / "sessions" / "2026" / "08" / "14" / "rollout-2026-08-14T09-03-00-thread-1.jsonl"
    wall = wall_clock or Clock(rollout.stat().st_mtime if rollout.exists() else 0.0)
    return StallProbeMonitor(
        turns_provider=lambda: [(7, 70, "thread-1", 0.0)],
        liveness_probe=lambda: liveness,
        recover=recorder.recover,
        notifier=recorder.notify,
        sessions_roots=[tmp_path],
        stall_seconds=stall_seconds,
        clock=clock,
        wall_clock=wall,
    )


@pytest.mark.anyio
async def test_confirmed_death_recovers_and_notifies(tmp_path: Path) -> None:
    rollout = _make_rollout(tmp_path, "thread-1")
    clock = Clock()
    wall = Clock(rollout.stat().st_mtime)
    recorder = Recorder()
    monitor = _monitor(tmp_path, recorder, clock, liveness="dead", wall_clock=wall)
    clock.advance(700)  # rollout 700s stale, past the 600s probe
    wall.advance(700)

    await monitor._tick()

    assert recorder.recoveries == 1
    assert len(recorder.notifications) == 1
    chat_id, text = recorder.notifications[0]
    assert chat_id == 70
    assert "died silently" in text and "recovery" in text


@pytest.mark.anyio
async def test_alive_engine_means_a_quiet_turn_is_left_untouched(
    tmp_path: Path,
) -> None:
    rollout = _make_rollout(tmp_path, "thread-1")
    clock = Clock()
    wall = Clock(rollout.stat().st_mtime)
    recorder = Recorder()
    monitor = _monitor(tmp_path, recorder, clock, liveness="alive", wall_clock=wall)
    clock.advance(700)
    wall.advance(700)

    await monitor._tick()

    assert recorder.recoveries == 0
    assert recorder.notifications == []  # PR-4 owns the long-turn notification


@pytest.mark.anyio
async def test_unknown_liveness_never_recovers(tmp_path: Path) -> None:
    rollout = _make_rollout(tmp_path, "thread-1")
    clock = Clock()
    wall = Clock(rollout.stat().st_mtime)
    recorder = Recorder()
    monitor = _monitor(tmp_path, recorder, clock, liveness="unknown", wall_clock=wall)
    clock.advance(700)
    wall.advance(700)

    await monitor._tick()

    assert recorder.recoveries == 0
    assert recorder.notifications == []  # fail-closed: log only, no alarm


@pytest.mark.anyio
async def test_fresh_rollout_or_missing_rollout_skips_the_probe(
    tmp_path: Path,
) -> None:
    rollout = _make_rollout(tmp_path, "thread-1")
    clock = Clock()
    wall = Clock(rollout.stat().st_mtime)
    recorder = Recorder()
    monitor = _monitor(tmp_path, recorder, clock, liveness="dead", wall_clock=wall)
    clock.advance(100)  # still fresh vs the 600s stall
    wall.advance(100)

    await monitor._tick()
    assert recorder.recoveries == 0

    monitor2 = StallProbeMonitor(  # no rollout for the thread at all
        turns_provider=lambda: [(7, 70, "no-such-thread", 0.0)],
        liveness_probe=lambda: "dead",
        recover=recorder.recover,
        notifier=recorder.notify,
        sessions_roots=[tmp_path],
        stall_seconds=600.0,
        clock=clock,
        wall_clock=wall,
    )
    clock.advance(700)
    wall.advance(700)
    await monitor2._tick()
    assert recorder.recoveries == 0


@pytest.mark.anyio
async def test_reprobe_cooldown_prevents_repeat_recovery(tmp_path: Path) -> None:
    rollout = _make_rollout(tmp_path, "thread-1")
    clock = Clock()
    wall = Clock(rollout.stat().st_mtime)
    recorder = Recorder()
    monitor = _monitor(tmp_path, recorder, clock, liveness="dead", wall_clock=wall)
    clock.advance(700)
    wall.advance(700)

    await monitor._tick()
    await monitor._tick()  # inside the reprobe cooldown
    assert recorder.recoveries == 1
    assert len(recorder.notifications) == 1


def test_engine_dead_text_names_recovery_and_next_step() -> None:
    text = engine_dead_notification_text(11)
    assert "11" in text and "recovery" in text and "Re-issue" in text


# --- lifecycle builder --------------------------------------------------------


def test_lifecycle_stall_probe_is_none_when_off(tmp_path: Path) -> None:
    from telegram_bot.core import bot_lifecycle

    lifecycle = bot_lifecycle.BotLifecycleMixin()
    lifecycle._config = SimpleNamespace(  # type: ignore[assignment]
        bot_data_dir=tmp_path, project_root=str(tmp_path)
    )
    assert lifecycle._build_turn_stall_probe() is None


def test_liveness_verdict_requires_confirmed_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The builder's liveness: only all-exited counts as dead (fail-closed)."""
    from telegram_bot.core import agent_session_registry, bot_lifecycle

    monkeypatch.setenv("CCC_TURN_STALL_PROBE_MIN", "10")
    lifecycle = bot_lifecycle.BotLifecycleMixin()
    lifecycle._config = SimpleNamespace(  # type: ignore[assignment]
        bot_data_dir=tmp_path, project_root=str(tmp_path)
    )
    lifecycle._project_chat = SimpleNamespace(  # type: ignore[assignment]
        _agent_session_registry=agent_session_registry.AgentSessionRegistry()
    )
    lifecycle.application = SimpleNamespace(bot=object())
    lifecycle._session_manager = SimpleNamespace()  # type: ignore[assignment]

    monitor = lifecycle._build_turn_stall_probe()
    assert monitor is not None

    class _Client:
        def __init__(self, exited):
            self._exited = exited

        def process_exited(self):
            return self._exited

    monkeypatch.setattr(
        bot_lifecycle, "live_app_server_clients", lambda: [_Client(True)]
    )
    # Rebuild so the provider picks up the patched client list.
    monitor = lifecycle._build_turn_stall_probe()
    assert monitor._liveness_probe() == "dead"

    monkeypatch.setattr(
        bot_lifecycle,
        "live_app_server_clients",
        lambda: [_Client(True), _Client(False)],
    )
    monitor = lifecycle._build_turn_stall_probe()
    assert monitor._liveness_probe() == "alive"

    monkeypatch.setattr(
        bot_lifecycle, "live_app_server_clients", lambda: [_Client(None)]
    )
    monitor = lifecycle._build_turn_stall_probe()
    assert monitor._liveness_probe() == "unknown"


# --- registry cosmetic: benign double-deactivate vs the #860 race ---------------


def test_double_deactivate_after_close_is_debug_not_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """/stop deactivates first; the turn's finally must not log a scare (#1112)."""
    from telegram_bot.core.agent_session_registry import AgentSessionRegistry

    registry = AgentSessionRegistry()
    token = registry.register_active((7, 70), object(), started_at=1.0)
    assert registry.deactivate_if_same(token, touch_at=2.0)

    with caplog.at_level(logging.DEBUG):
        assert registry.deactivate_if_same(token, touch_at=3.0) is False
    warnings = [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert warnings == []


def test_conflicting_live_token_still_warns(caplog: pytest.LogCaptureFixture) -> None:
    """The #860 race keeps its WARNING diagnostics."""
    from telegram_bot.core.agent_session_registry import (
        ActiveToken,
        AgentSessionRegistry,
    )

    registry = AgentSessionRegistry()
    token = registry.register_active((7, 70), object(), started_at=1.0)
    wrong = ActiveToken((7, 70), token.generation + 1)

    with caplog.at_level(logging.WARNING):
        assert registry.deactivate_if_same(wrong) is False
    assert any("Deactivate failed: token mismatch" in rec.message for rec in caplog.records)


# --- health signal -----------------------------------------------------------------


def test_health_probe_exports_orphan_loop_count_and_alert() -> None:
    from telegram_bot.core import turn_stall
    from telegram_bot.utils.health_alerts import (
        AlertThresholds,
        HealthProbe,
        evaluate_alerts,
    )

    turn_stall.orphan_tool_loop_tracker.reset()
    for _ in range(10):
        turn_stall.orphan_tool_loop_tracker.record_line(
            f"ERROR {turn_stall.ORPHAN_LOOP_PATTERN}"
        )
    try:
        probe = HealthProbe(project_chat=None, spool_dir=Path("/nonexistent"))
        signals = probe.collect(now=turn_stall.time.monotonic())
        assert signals.orphan_tool_loop_recent >= 10
        alerts = evaluate_alerts(signals, AlertThresholds())
        assert any(alert.code == "orphan_tool_loop" for alert in alerts)
    finally:
        turn_stall.orphan_tool_loop_tracker.reset()
