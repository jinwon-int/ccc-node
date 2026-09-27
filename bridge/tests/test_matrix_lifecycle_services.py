"""Matrix-native background services (#1825, the part #1998 left open).

Covers the rapid-crash budget, task-ledger reconciliation, the turn-stall
probe and webhook-nudge builders, and that ``serve()`` actually launches the
health-alert probe, the session resource guard, the skill-candidate
collector, the stall probe, the nudge listener and the orphan reaper.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
import types
from typing import Any

import anyio
import pytest

import telegram_bot.core.matrix.bot as bot_module
from telegram_bot.core.matrix import lifecycle as ml
from telegram_bot.core.matrix.bot import MatrixBot
from telegram_bot.core.task_ledger import INTERRUPTED, TaskLedger, default_task_ledger_path
from test_matrix_bot import (
    DM_ROOM,
    OWNER,
    FakeProjectChat,
    FakeSessionManager,
    FakeTransport,
    _settings,
)


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


def _bot(tmp_path: Path, **overrides: Any) -> tuple[MatrixBot, FakeProjectChat]:
    settings = _settings(tmp_path, **overrides)
    chat = FakeProjectChat()
    manager = FakeSessionManager(provider=settings.agent_provider)
    bot = MatrixBot(settings, project_chat=chat, session_manager=manager)
    return bot, chat


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# --- rapid-crash budget -----------------------------------------------------------


def _budget(tmp_path: Path, clock: Clock, **kwargs: Any) -> ml.CrashBudget:
    params = dict(window_seconds=60, max_rapid=3, base_delay_seconds=3, max_delay_seconds=10)
    params.update(kwargs)
    return ml.CrashBudget(tmp_path / ml.CRASH_BUDGET_FILENAME, clock=clock, **params)


def test_first_start_and_clean_restarts_are_never_delayed(tmp_path: Path) -> None:
    clock = Clock()
    budget = _budget(tmp_path, clock)
    first = budget.begin()
    assert (first.streak, first.delay_seconds, first.alert) == (0, 0.0, False)
    budget.mark_clean()
    clock.now += 5
    again = budget.begin()
    assert (again.streak, again.delay_seconds, again.alert) == (0, 0.0, False)


def test_rapid_unclean_exits_back_off_exponentially_and_alert_once(tmp_path: Path) -> None:
    clock = Clock()
    budget = _budget(tmp_path, clock)
    budget.begin()
    decisions = []
    for _ in range(5):
        budget.record_error(RuntimeError("secret body must not persist"))
        clock.now += 6  # systemd RestartSec=5 plus start-up
        decisions.append(budget.begin())
    assert [d.streak for d in decisions] == [1, 2, 3, 4, 5]
    assert [d.delay_seconds for d in decisions] == [3.0, 6.0, 10.0, 10.0, 10.0]
    assert [d.alert for d in decisions] == [False, False, True, False, False]
    assert decisions[-1].last_error == "RuntimeError"
    raw = (tmp_path / ml.CRASH_BUDGET_FILENAME).read_text(encoding="utf-8")
    assert "secret body" not in raw, "only the exception class name is persisted"
    mode = stat.S_IMODE(os.stat(tmp_path / ml.CRASH_BUDGET_FILENAME).st_mode)
    assert mode == 0o600


def test_a_crash_after_a_long_healthy_run_is_not_rapid(tmp_path: Path) -> None:
    clock = Clock()
    budget = _budget(tmp_path, clock)
    budget.begin()
    clock.now += 6
    assert budget.begin().streak == 1
    clock.now += 3600  # the run that followed lived for an hour, then died
    decision = budget.begin()
    assert (decision.streak, decision.delay_seconds) == (0, 0.0)


def test_an_orderly_stop_ends_the_streak_and_rearms_the_alert(tmp_path: Path) -> None:
    clock = Clock()
    budget = _budget(tmp_path, clock, max_rapid=2)
    budget.begin()
    for _ in range(2):
        clock.now += 6
        last = budget.begin()
    assert last.alert is True
    budget.mark_clean()
    clock.now += 6
    budget.begin()
    for _ in range(2):
        clock.now += 6
        last = budget.begin()
    assert last.alert is True, "a new streak alerts again"


@pytest.mark.parametrize("uptime", [0.5, 10.0, 28.0, 35.0, 50.0])
def test_a_steady_crash_loop_keeps_its_streak_through_the_backoff(tmp_path: Path, uptime: float) -> None:
    """The applied delay must not count toward the rapid window (review of #1825).

    Stamping the pre-sleep time let a loop at a 24-30 s delay read as
    non-rapid: the streak reset itself and the alert never fired, or fired
    once per mini-cycle.
    """

    clock = Clock()
    budget = _budget(tmp_path, clock, max_rapid=5, base_delay_seconds=3, max_delay_seconds=30)
    decisions = []
    for _ in range(20):
        decision = budget.begin()
        decisions.append(decision)
        budget.record_error(RuntimeError())
        clock.now += decision.delay_seconds + uptime + 5  # back-off, run, RestartSec
    streaks = [d.streak for d in decisions]
    assert streaks == list(range(20)), streaks
    assert sum(d.alert for d in decisions) == 1
    assert max(d.delay_seconds for d in decisions) == 30.0


def test_a_corrupt_record_is_treated_as_a_first_start(tmp_path: Path) -> None:
    (tmp_path / ml.CRASH_BUDGET_FILENAME).write_text("{not json", encoding="utf-8")
    decision = _budget(tmp_path, Clock()).begin()
    assert (decision.streak, decision.delay_seconds, decision.alert) == (0, 0.0, False)


def test_crash_loop_alert_is_numbers_and_class_names_only() -> None:
    alert = ml.crash_loop_alert(ml.CrashDecision(streak=5, delay_seconds=30, alert=True, last_error="SafetyStop"))
    assert alert.code == ml.CRASH_LOOP_ALERT_CODE
    assert "5 times" in alert.message and "SafetyStop" in alert.message and "30s" in alert.message


@pytest.mark.anyio
async def test_crash_backoff_sleeps_and_spools_one_alert(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = tmp_path / "spool"
    bot, _chat = _bot(tmp_path, push_enabled=True, push_spool_dir=spool)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    decision = ml.CrashDecision(streak=5, delay_seconds=30.0, alert=True, last_error="RuntimeError")
    monkeypatch.setattr(bot_module.asyncio, "sleep", fake_sleep)
    await bot._crash_backoff(decision)
    assert slept == [30.0]
    records = [json.loads(p.read_text(encoding="utf-8")) for p in spool.glob("*.json")]
    assert len(records) == 1
    assert records[0]["dedup"] == f"health-alert:{ml.CRASH_LOOP_ALERT_CODE}"


@pytest.mark.anyio
async def test_crash_backoff_does_not_spool_without_the_push_opt_in(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    spool = tmp_path / "spool"
    bot, _chat = _bot(tmp_path, push_enabled=False, push_spool_dir=spool)
    decision = ml.CrashDecision(streak=5, delay_seconds=0.0, alert=True, last_error=None)
    await bot._crash_backoff(decision)
    assert not spool.exists() or not list(spool.glob("*.json"))


def test_run_marks_an_orderly_serve_clean_and_a_crash_unclean(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, _chat = _bot(tmp_path)
    bot._transport_factory = lambda config, runner: FakeTransport(config, runner)
    bot.run()
    record = json.loads((tmp_path / "data" / ml.CRASH_BUDGET_FILENAME).read_text(encoding="utf-8"))
    assert record["running"] is False and record["streak"] == 0

    crashed, _chat = _bot(tmp_path)
    crashed._transport_factory = lambda config, runner: FakeTransport(config, runner, fail_run=True)
    with pytest.raises(RuntimeError, match="sync loop died"):
        crashed.run()
    record = json.loads((tmp_path / "data" / ml.CRASH_BUDGET_FILENAME).read_text(encoding="utf-8"))
    assert record["running"] is True, "an unclean exit leaves the run marked running"
    assert record["last_error"] == "RuntimeError"


def test_run_counts_a_start_up_check_failure_as_unclean(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import telegram_bot.core.bot_shared as bot_shared

    def refuse(settings: Any) -> None:
        raise SystemExit("allowlist required")

    monkeypatch.setattr(bot_shared, "enforce_access_control", refuse)
    bot, _chat = _bot(tmp_path)
    with pytest.raises(SystemExit):
        bot.run()
    record = json.loads((tmp_path / "data" / ml.CRASH_BUDGET_FILENAME).read_text(encoding="utf-8"))
    assert record["running"] is True and record["last_error"] == "SystemExit"


# --- task ledger --------------------------------------------------------------------


def test_reconcile_task_ledger_closes_what_the_previous_process_left(tmp_path: Path) -> None:
    data = tmp_path / "data"
    ledger = TaskLedger(default_task_ledger_path(data))
    with_bubble = ledger.create(7, 7)
    ledger.set_status_message(with_bubble, 1)
    ledger.create(7, 7)  # never got a status bubble
    settings = SimpleNamespace(bot_data_dir=data)
    assert ml.reconcile_task_ledger(settings) == 2
    assert ledger.records() == [], "interrupted records leave no terminal op behind"
    assert ledger.pending_terminal_ops() == []
    assert ml.reconcile_task_ledger(settings) == 0


def test_reconcile_task_ledger_never_touches_the_telegram_ledger(tmp_path: Path) -> None:
    """A Matrix unit without BOT_DATA_DIR or with a shared ledger path must not erase Telegram's records."""

    telegram_dir = tmp_path / ".telegram_bot"
    ledger = TaskLedger(default_task_ledger_path(telegram_dir))
    ledger.create(7, 7)
    assert ml.reconcile_task_ledger(SimpleNamespace(bot_data_dir=telegram_dir)) == 0
    shared = SimpleNamespace(bot_data_dir=tmp_path / ".ccc-matrix", task_ledger_path=default_task_ledger_path(telegram_dir))
    assert ml.reconcile_task_ledger(shared) == 0
    assert len(ledger.records()) == 1


def test_reconcile_task_ledger_without_a_data_dir_is_a_no_op() -> None:
    assert ml.reconcile_task_ledger(SimpleNamespace(bot_data_dir=None)) == 0


def test_interrupted_is_a_terminal_state_for_the_ledger() -> None:
    assert INTERRUPTED == "interrupted"


# --- turn-stall probe builder -----------------------------------------------------------


async def _notify(chat_id: int, text: str) -> bool:
    return True


async def _recover() -> None:
    return None


def test_turn_stall_probe_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = SimpleNamespace(_agent_session_registry=SimpleNamespace(active_handles_snapshot=tuple))
    assert ml.build_turn_stall_probe(chat, notifier=_notify, recover=_recover) is None


def test_turn_stall_probe_builds_when_enabled_and_reads_registry_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCC_TURN_STALL_PROBE_MIN", "15")
    handle = SimpleNamespace(token=SimpleNamespace(key=(3, 4)), session=SimpleNamespace(id="thread-1"))
    short = SimpleNamespace(token=SimpleNamespace(key=(9,)), session=None)
    chat = SimpleNamespace(
        _agent_session_registry=SimpleNamespace(active_handles_snapshot=lambda: (handle, short))
    )
    probe = ml.build_turn_stall_probe(chat, notifier=_notify, recover=_recover)
    assert probe is not None
    assert probe._turns_provider() == [(3, 4, "thread-1", 0.0)]


def test_turn_stall_probe_degrades_without_a_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCC_TURN_STALL_PROBE_MIN", "15")
    assert ml.build_turn_stall_probe(SimpleNamespace(), notifier=_notify, recover=_recover) is None


# --- webhook nudge builder ---------------------------------------------------------------

_NUDGE_ON = {"CCC_WEBHOOK_NUDGE_ENABLED": "1", "CCC_WEBHOOK_NUDGE_SECRET": "synthetic"}


def test_nudge_is_off_unless_enabled(tmp_path: Path) -> None:
    assert ml.build_webhook_nudge_server(tmp_path, environ={ml.MATRIX_NUDGE_PORT_ENV: "8792"}) is None


def test_nudge_needs_a_matrix_port_of_its_own(tmp_path: Path) -> None:
    assert ml.build_webhook_nudge_server(tmp_path, environ=dict(_NUDGE_ON)) is None
    # Same as the Telegram listener's explicit port, or its default (8791).
    same = {**_NUDGE_ON, "CCC_WEBHOOK_NUDGE_PORT": "9000", ml.MATRIX_NUDGE_PORT_ENV: "9000"}
    assert ml.build_webhook_nudge_server(tmp_path, environ=same) is None
    default = {**_NUDGE_ON, ml.MATRIX_NUDGE_PORT_ENV: "8791"}
    assert ml.build_webhook_nudge_server(tmp_path, environ=default) is None
    for bad in ("not-a-port", "0", "70000"):
        assert ml.build_webhook_nudge_server(tmp_path, environ={**_NUDGE_ON, ml.MATRIX_NUDGE_PORT_ENV: bad}) is None


def test_nudge_binds_the_matrix_port_over_this_frontends_registry(tmp_path: Path) -> None:
    env = {**_NUDGE_ON, "CCC_WEBHOOK_NUDGE_PORT": "8791", ml.MATRIX_NUDGE_PORT_ENV: "8792"}
    server = ml.build_webhook_nudge_server(tmp_path, environ=env)
    assert server is not None
    assert server._port == 8792
    assert Path(server._registry._path).parent == tmp_path / "external-wait"


def test_nudge_still_refuses_to_start_without_a_secret(tmp_path: Path) -> None:
    env = {"CCC_WEBHOOK_NUDGE_ENABLED": "1", ml.MATRIX_NUDGE_PORT_ENV: "8792"}
    assert ml.build_webhook_nudge_server(tmp_path, environ=env) is None


# --- serve() launches the services -------------------------------------------------------


def _record_loop(calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]], name: str):
    async def loop(*args: Any, **kwargs: Any) -> None:
        calls.append((name, args, kwargs))

    return loop


async def _serve_briefly(bot: MatrixBot) -> FakeTransport:
    holder: dict[str, FakeTransport] = {}

    async def script(transport: FakeTransport) -> None:
        holder["transport"] = transport
        await anyio.sleep(0.05)  # let the sibling legs start

    bot._transport_factory = lambda config, runner: FakeTransport(config, runner, script=script)
    with anyio.fail_after(5):
        await bot.serve()
    return holder["transport"]


@pytest.mark.anyio
async def test_serve_launches_health_alerts_and_skips_opt_in_legs_by_default(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(bot_module, "run_health_alerts_probe", _record_loop(calls, "health"))
    monkeypatch.setattr(bot_module, "run_session_resource_guard", _record_loop(calls, "guard"))
    monkeypatch.setattr(bot_module, "run_skill_candidate_collector", _record_loop(calls, "collector"))
    spool = tmp_path / "spool"
    bot, chat = _bot(tmp_path, push_spool_dir=spool)
    await _serve_briefly(bot)
    names = [name for name, _args, _kwargs in calls]
    assert names == ["health"], "guard needs session_guard_enabled; collector needs a worker + journal"
    _name, args, kwargs = calls[0]
    assert args[0] is bot._settings and args[1] is chat
    assert kwargs == {"spool_dir": spool, "write_spool_dir": spool}


@pytest.mark.anyio
async def test_serve_launches_the_guard_and_collector_when_configured(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(bot_module, "run_health_alerts_probe", _record_loop(calls, "health"))
    monkeypatch.setattr(bot_module, "run_session_resource_guard", _record_loop(calls, "guard"))
    monkeypatch.setattr(bot_module, "run_skill_candidate_collector", _record_loop(calls, "collector"))
    journal = SimpleNamespace(validate_path=lambda: None, initialize=lambda: None)
    worker = SimpleNamespace(provider="codex")
    bot, chat = _bot(tmp_path, session_guard_enabled=True)
    bot._distill_journal = journal
    bot._skill_candidate_collector_worker = worker
    monkeypatch.setattr(bot, "_enqueue_shutdown_distills", _record_loop([], "shutdown"))
    await _serve_briefly(bot)
    by_name = {name: args for name, args, _kwargs in calls}
    assert set(by_name) == {"health", "guard", "collector"}
    assert by_name["guard"][:2] == (bot._settings, chat)
    assert by_name["collector"][0] is worker
    assert by_name["collector"][2] is bot._settings


@pytest.mark.anyio
async def test_serve_runs_the_stall_probe_when_enabled(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[bool] = []

    class Probe:
        async def run(self, stop: asyncio.Event) -> None:
            ran.append(True)
            await stop.wait()

    bot, _chat = _bot(tmp_path)
    monkeypatch.setattr(bot, "_build_turn_stall_probe", lambda: Probe())
    await _serve_briefly(bot)
    assert ran == [True]


@pytest.mark.anyio
async def test_serve_starts_and_closes_the_nudge_listener(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class Server:
        async def start(self) -> bool:
            events.append("start")
            return True

        async def close(self) -> None:
            events.append("close")

    bot, _chat = _bot(tmp_path)
    monkeypatch.setattr(bot, "_build_webhook_nudge_server", lambda: Server())
    await _serve_briefly(bot)
    assert events == ["start", "close"]


@pytest.mark.anyio
async def test_serve_closes_the_nudge_listener_when_the_transport_dies(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class Server:
        async def start(self) -> bool:
            events.append("start")
            return True

        async def close(self) -> None:
            events.append("close")

    bot, _chat = _bot(tmp_path)
    monkeypatch.setattr(bot, "_build_webhook_nudge_server", lambda: Server())
    bot._transport_factory = lambda config, runner: FakeTransport(config, runner, fail_run=True)
    with pytest.raises(RuntimeError, match="sync loop died"):
        await bot.serve()
    assert events == ["start", "close"]


@pytest.mark.anyio
async def test_a_failing_leg_builder_never_leaves_the_nudge_listener_open(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class Server:
        async def start(self) -> bool:
            events.append("start")
            return True

        async def close(self) -> None:
            events.append("close")

    def broken_builder() -> Any:
        raise RuntimeError("builder failed")

    bot, _chat = _bot(tmp_path)
    monkeypatch.setattr(bot, "_build_webhook_nudge_server", lambda: Server())
    monkeypatch.setattr(bot, "_build_turn_stall_probe", broken_builder)
    bot._transport_factory = lambda config, runner: FakeTransport(config, runner)
    with pytest.raises(RuntimeError, match="builder failed"):
        await bot.serve()
    assert events in ([], ["start", "close"])


@pytest.mark.anyio
async def test_orphan_reaper_is_off_under_tests_and_runs_when_enabled(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    sweeps: list[str] = []

    def sweep() -> list[int]:
        sweeps.append("startup")
        return [4242]

    async def periodic() -> None:
        sweeps.append("periodic")
        await asyncio.Event().wait()  # like the real reaper: only cancellation stops it

    monkeypatch.setattr(MatrixBot, "_orphan_sweep", staticmethod(sweep))
    monkeypatch.setattr(MatrixBot, "_periodic_reaper", staticmethod(periodic))

    bot, _chat = _bot(tmp_path)
    await _serve_briefly(bot)
    assert sweeps == [], "conftest keeps the host-signalling reaper off"

    monkeypatch.setenv("CCC_MATRIX_ORPHAN_REAPER", "1")
    bot, _chat = _bot(tmp_path)
    await _serve_briefly(bot)  # must still finish: the periodic leg is cancelled on stop
    assert sweeps == ["startup", "periodic"]


@pytest.mark.anyio
async def test_serve_reconciles_the_ledger_before_serving(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    bot, _chat = _bot(tmp_path)
    ledger = TaskLedger(default_task_ledger_path(tmp_path / "data"))
    stale = ledger.create(7, 7)
    ledger.set_status_message(stale, 1)
    seen: list[list[dict[str, Any]]] = []

    async def script(transport: FakeTransport) -> None:
        seen.append(ledger.records())

    bot._transport_factory = lambda config, runner: FakeTransport(config, runner, script=script)
    await bot.serve()
    assert seen == [[]]


@pytest.mark.anyio
async def test_a_failing_startup_sweep_never_blocks_serving(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(settings: Any) -> int:
        raise OSError("disk")

    monkeypatch.setattr(ml, "reconcile_task_ledger", boom)
    monkeypatch.setenv("CCC_MATRIX_ORPHAN_REAPER", "1")

    def sweep() -> list[int]:
        raise PermissionError("proc")

    async def periodic() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(MatrixBot, "_orphan_sweep", staticmethod(sweep))
    monkeypatch.setattr(MatrixBot, "_periodic_reaper", staticmethod(periodic))
    bot, _chat = _bot(tmp_path)
    transport = await _serve_briefly(bot)
    assert transport.events == ["open", "run", "close"]


class _PolicyTransport(FakeTransport):
    """FakeTransport with the room policy the spool notifier reads."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.policy = SimpleNamespace(rooms={DM_ROOM: "direct"})


@pytest.mark.anyio
async def test_serve_returns_when_the_transport_ends_with_the_spool_notifier_on(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    """The notifier polls forever without watching ``stop``; it must be cancelled, not awaited."""

    bot, _chat = _bot(tmp_path, push_enabled=True, push_spool_dir=tmp_path / "spool", push_poll_interval=0.01)

    async def script(transport: FakeTransport) -> None:
        await anyio.sleep(0.05)  # the notifier is polling by now

    bot._transport_factory = lambda config, runner: _PolicyTransport(config, runner, script=script)
    with anyio.fail_after(5):
        await bot.serve()
