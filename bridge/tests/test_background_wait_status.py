"""Background work on the per-conversation status line (#2605, the #2081 §3 follow-up).

Layer 1: the bridge's own body-free background-task count reaches the status
message. Layer 2: ``--source local-task`` labels one task; never polled, closed
from the task's terminal frame without a wake, ``lost`` after a restart.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core import external_wait_cli
from telegram_bot.core.agent_session_registry import AgentSessionRegistry
from telegram_bot.core.bot_wait_status import BotWaitStatusMixin
from telegram_bot.core.claude_session_task_tracking import ClaudeSessionTaskTrackingMixin
from telegram_bot.core.external_wait import (
    SOURCE_LOCAL_TASK,
    STATE_MONITORING,
    TERMINAL_EXPIRED,
    TERMINAL_LOST,
    TERMINAL_SUCCESS,
    ExternalWaitRegistry,
    ExternalWaitValidationError,
    default_registry_path,
    publish_active_turn,
    render_waits,
)
from telegram_bot.core.external_wait_monitor import ExternalWaitMonitor
from telegram_bot.core.external_wait_status import (
    SOURCE_LOCAL_BACKGROUND,
    ExternalWaitStatusStore,
    background_records,
    default_status_store_path,
    merge_background_records,
    plan_status_update,
    render_wait_status,
    route_background_snapshot,
)
from telegram_bot.core.project_chat_state import ProjectChatStateMixin
from telegram_bot.core.project_chat_types import AgentSessionEntry


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


NOW = 1_700_000_000.0


def _registry(tmp_path: Path, clock=None) -> ExternalWaitRegistry:
    # A fixed clock near NOW: the registry prunes terminal records older than
    # its retention window against *its* clock, so wall time would hide
    # outcomes finished at NOW+120 from the renderer.
    return ExternalWaitRegistry(
        default_registry_path(tmp_path / "external-wait"), clock=clock or (lambda: NOW + 600)
    )


def _register_local(registry: ExternalWaitRegistry, *, task_id: str = "b6gw3sysl", now: float = NOW, timeout: float = 1800.0, **overrides) -> str:
    params = dict(
        repo="",
        pr_number=None,
        head_sha="",
        user_id=7,
        chat_id=70,
        session_id="sess-1",
        summary="nightly skill sweep",
        timeout_seconds=timeout,
        poll_interval_seconds=30,
        now=now,
        source=SOURCE_LOCAL_TASK,
        task_id=task_id,
    )
    params.update(overrides)
    return registry.register(**params)


# --- registry ---------------------------------------------------------------------


def test_local_task_registers_without_repo_and_is_never_due_before_its_deadline(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    wait_id = _register_local(registry)
    rec = registry.get(wait_id)
    assert rec["source"] == SOURCE_LOCAL_TASK
    assert rec["task_id"] == "b6gw3sysl" and rec["repo"] == "" and rec["pr_number"] is None
    assert rec["next_poll_epoch"] == rec["expires_epoch"] == NOW + 1800
    assert registry.due(now=NOW + 1700) == []
    assert [r["wait_id"] for r in registry.due(now=NOW + 1800)] == [wait_id]
    # Idempotent per (task, route); a different task id is a new wait.
    assert _register_local(registry) == wait_id
    assert _register_local(registry, task_id="other") != wait_id


def test_local_task_rejects_bad_task_ids(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    for bad in ("", "has space", "a" * 300, "../x", None):
        with pytest.raises(ExternalWaitValidationError):
            _register_local(registry, task_id=bad)  # type: ignore[arg-type]


def test_finish_without_wake_never_journals_a_pending_wake(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    wait_id = _register_local(registry)
    assert registry.finish(wait_id, TERMINAL_SUCCESS, now=NOW + 60, wake=False) is True
    rec = registry.get(wait_id)
    assert rec["state"] == TERMINAL_SUCCESS and rec["wake"]["state"] == "skipped"
    assert registry.pending_wakes() == []
    # Second finish is a no-op (first write wins).
    assert registry.finish(wait_id, TERMINAL_EXPIRED, now=NOW + 61, wake=False) is False


def test_finish_local_tasks_closes_only_the_matching_route_and_task(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    mine = _register_local(registry, task_id="t1")
    other_task = _register_local(registry, task_id="t2")
    other_route = _register_local(registry, task_id="t1", chat_id=71)
    closed = registry.finish_local_tasks(user_id=7, chat_id=70, task_id="t1", terminal_status=TERMINAL_SUCCESS, now=NOW + 5)
    assert closed == [mine]
    assert registry.get(other_task)["state"] == STATE_MONITORING
    assert registry.get(other_route)["state"] == STATE_MONITORING
    assert registry.pending_wakes() == []
    # task_id=None closes everything left on the route (session gone).
    assert registry.finish_local_tasks(user_id=7, chat_id=70, task_id=None, terminal_status=TERMINAL_LOST, now=NOW + 6) == [other_task]
    assert registry.get(other_task)["terminal_status"] == TERMINAL_LOST


def test_restart_reconcile_marks_local_tasks_lost_and_repolls_github_waits(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    local = _register_local(registry)
    github = registry.register(
        repo="jinwon-int/ccc-node", pr_number=1, head_sha="abc1234", user_id=7, chat_id=70,
        session_id="s", summary=None, timeout_seconds=3600, poll_interval_seconds=30, now=NOW,
    )
    assert registry.reconcile_on_start(now=NOW + 10) == 1
    assert registry.get(local)["state"] == TERMINAL_LOST
    assert registry.get(local)["wake"]["state"] == "skipped"
    assert registry.get(github)["state"] == STATE_MONITORING
    assert registry.pending_wakes() == []


def test_waits_listing_names_the_background_task(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _register_local(registry)
    text = render_waits(registry.records())
    assert "background task b6gw3sysl" in text and "nightly skill sweep" in text


# --- renderer -------------------------------------------------------------------------


def test_render_local_task_lines_say_background_not_ci(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    wait_id = _register_local(registry)
    text = render_wait_status(registry.records(), NOW + 60)
    assert text is not None
    assert text.startswith("⏳ Running in background · background task — nightly skill sweep (registered ")
    assert "up to 30m; resumes automatically when done)" in text
    assert "CI" not in text
    registry.finish(wait_id, TERMINAL_SUCCESS, now=NOW + 120, wake=False)
    done = render_wait_status(registry.records(), NOW + 130)
    assert done is not None and done.startswith("✅ background task finished → continuing · background task (")
    registry2 = _registry(tmp_path / "two")
    expired = _register_local(registry2)
    registry2.finish(expired, TERMINAL_EXPIRED, now=NOW + 1, wake=False)
    assert render_wait_status(registry2.records(), NOW + 2).startswith("⏰ background task watch expired")


def test_background_records_count_running_and_recent_finishes_only() -> None:
    recs = background_records(7, 70, running=2, oldest_age_seconds=600, finished_epochs=[NOW - 60, NOW - 7200], now_epoch=NOW)
    kinds = [(r["state"], r.get("task_count")) for r in recs]
    assert kinds == [(STATE_MONITORING, 2), (TERMINAL_SUCCESS, 1)]
    assert all(r["source"] == SOURCE_LOCAL_BACKGROUND for r in recs)
    assert recs[0]["created_epoch"] == NOW - 600
    text = render_wait_status(recs, NOW)
    assert "⏳ Running in background · 2 background tasks (since " in text
    assert "✅ background task finished → continuing · background task (" in text
    # Nothing running, nothing recent: nothing to show.
    assert background_records(7, 70, 0, 0.0, [NOW - 7200], NOW) == []
    # Labelled tasks are subtracted so a local-task line never doubles the count.
    assert background_records(7, 70, 1, 10.0, [], NOW, labelled=1) == []


def test_merge_background_records_subtracts_labelled_tasks_and_passes_through_without_snapshot(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _register_local(registry)
    base = registry.records()
    assert merge_background_records(base, None, 7, 70, NOW) == base
    merged = merge_background_records(base, (1, 30.0, []), 7, 70, NOW)
    assert [r["source"] for r in merged] == [SOURCE_LOCAL_TASK]
    merged = merge_background_records(base, (3, 30.0, []), 7, 70, NOW)
    extra = [r for r in merged if r["source"] == SOURCE_LOCAL_BACKGROUND]
    assert len(extra) == 1 and extra[0]["task_count"] == 2
    # A background-only route sends a fresh status message.
    action, text = plan_status_update(background_records(7, 70, 1, 5.0, [], NOW), None, NOW)
    assert action == "send" and "Running in background" in text


def test_route_background_snapshot_is_fail_open() -> None:
    assert route_background_snapshot(None, 7, 70) is None
    assert route_background_snapshot(SimpleNamespace(), 7, 70) is None

    def boom(user_id, chat_id):
        raise RuntimeError("probe failed")

    assert route_background_snapshot(SimpleNamespace(background_workload_for_route=boom), 7, 70) is None
    ok = SimpleNamespace(background_workload_for_route=lambda u, c: (2, 12.5, (NOW,)))
    assert route_background_snapshot(ok, 7, 70) == (2, 12.5, [NOW])
    none = SimpleNamespace(background_workload_for_route=lambda u, c: None)
    assert route_background_snapshot(none, 7, 70) is None


# --- monitor ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _NoTransport:
    async def fetch_pr_state(self, repo, pr_number):  # pragma: no cover - must never run
        raise AssertionError("local-task waits must not reach a transport")

    fetch_actions_run = fetch_merge_queue_state = fetch_failed_queue_run = fetch_pr_state


@pytest.mark.anyio
async def test_monitor_never_polls_local_tasks_and_expires_them_without_a_wake(tmp_path: Path) -> None:
    clock = _Clock(NOW)
    registry = _registry(tmp_path, clock)
    wait_id = _register_local(registry, now=NOW - 1000, timeout=900)  # deadline already passed
    notified: list = []
    resumed: list = []
    synced: list = []

    async def notify(chat_id, text):
        notified.append((chat_id, text))
        return True

    async def resume(record, prompt):
        resumed.append(record)
        return True

    async def sync(user_id, chat_id):
        synced.append((user_id, chat_id))

    async def session_of(user_id, chat_id):
        return "sess-1"

    monitor = ExternalWaitMonitor(
        registry, transport=_NoTransport(), notifier=notify, resumer=resume,
        session_lookup=session_of, clock=clock, status_syncer=sync,
    )
    await monitor._tick()
    rec = registry.get(wait_id)
    assert rec["state"] == TERMINAL_EXPIRED and rec["wake"]["state"] == "skipped"
    assert notified == [] and resumed == []
    assert synced == [(7, 70)]
    # A second tick finds nothing to do.
    await monitor._tick()
    assert synced == [(7, 70)]


# --- CLI ------------------------------------------------------------------------------


def test_cli_registers_a_local_task_label_on_the_active_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("CCC_EXTERNAL_WAIT_HOME", str(tmp_path))
    publish_active_turn(tmp_path, user_id=7, chat_id=70, session_id="sess-1")
    rc = external_wait_cli.main([
        "register", "--source", "local-task", "--task-id", "b6gw3sysl",
        "--summary", "nightly skill sweep", "--timeout-seconds", "1800",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True and payload["source"] == SOURCE_LOCAL_TASK and payload["task_id"] == "b6gw3sysl"
    rec = ExternalWaitRegistry(default_registry_path(tmp_path)).get(payload["wait_id"])
    assert rec["user_id"] == 7 and rec["chat_id"] == 70 and rec["summary"] == "nightly skill sweep"
    assert rec["expires_epoch"] - rec["created_epoch"] == 1800


def test_cli_local_task_requires_a_task_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("CCC_EXTERNAL_WAIT_HOME", str(tmp_path))
    publish_active_turn(tmp_path, user_id=7, chat_id=70, session_id="sess-1")
    rc = external_wait_cli.main(["register", "--source", "local-task", "--summary", "x"])
    assert rc == 2
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False and payload["code"] == "validation"


# --- session tracker seam --------------------------------------------------------------


class _Tracker(ClaudeSessionTaskTrackingMixin):
    def __init__(self) -> None:
        self._background_tasks = {}
        self._background_task_terminal_ids = set()
        self._background_task_listener = None
        self.seen: list[tuple[str, str]] = []
        self._background_task_listener = lambda phase, task_id: self.seen.append((phase, task_id))

    @staticmethod
    def _content_texts(value):
        return [value] if isinstance(value, str) else []


@pytest.mark.anyio
async def test_tracker_reports_each_transition_once_and_survives_a_raising_listener() -> None:
    tracker = _Tracker()
    tracker._track_background_task_start("bg-1")
    tracker._track_background_task_start("bg-1")  # duplicate start edge
    tracker._finish_background_task("bg-1")
    tracker._finish_background_task("bg-1")  # duplicate terminal frame
    tracker._finish_background_task("never-started")  # terminal without a start: no event
    tracker._track_background_task_start("bg-1")  # replay after terminal: ignored
    assert tracker.seen == [("start", "bg-1"), ("finish", "bg-1")]
    assert tracker.background_workload_snapshot(asyncio.get_running_loop().time()) == (0, 0.0)

    def boom(phase, task_id):
        raise RuntimeError("listener failed")

    tracker._background_task_listener = boom
    tracker._track_background_task_start("bg-2")  # must not raise
    assert tracker.background_workload_snapshot(asyncio.get_running_loop().time())[0] == 1


# --- chat handler probe ----------------------------------------------------------------


class _Session:
    def __init__(self, count: int, age: float) -> None:
        self.count, self.age = count, age

    def background_workload_snapshot(self, now: float):
        return self.count, self.age


class _Probe(ProjectChatStateMixin):
    def __init__(self) -> None:
        self._agent_session_registry = AgentSessionRegistry()
        self._background_finished = {}

    def _stream_key(self, user_id: int, chat_id: int):
        return (int(user_id), int(chat_id))


@pytest.mark.anyio
async def test_handler_probe_reads_the_route_session_and_recent_finishes() -> None:
    probe = _Probe()
    assert probe.background_workload_for_route(7, 70) is None
    probe._agent_session_registry.put_cached((7, 70), AgentSessionEntry(session=_Session(2, 45.0), last_used_at=0.0))
    assert probe.background_workload_for_route(7, 70) == (2, 45.0, [])
    probe._remember_background_finish((7, 70), time.time() - 5)
    probe._remember_background_finish((7, 70), time.time() - 7200)  # too old, dropped
    running, age, finished = probe.background_workload_for_route(7, 70)
    assert (running, age) == (2, 45.0) and len(finished) == 1
    # Another route sees nothing.
    assert probe.background_workload_for_route(8, 80) is None
    # A raising session probe counts as nothing running.
    probe._agent_session_registry.put_cached((9, 90), AgentSessionEntry(session=SimpleNamespace(background_workload_snapshot=lambda now: 1 / 0), last_used_at=0.0))
    assert probe.background_workload_for_route(9, 90) is None


# --- Telegram projection end to end ---------------------------------------------------


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.deleted: list[dict] = []
        self._next_id = 100

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)
        return SimpleNamespace(message_id=kwargs.get("message_id"))

    async def delete_message(self, **kwargs):
        self.deleted.append(kwargs)
        return True


class _StatusBot(BotWaitStatusMixin):
    pass


@pytest.mark.anyio
async def test_telegram_status_shows_background_work_then_its_finish(tmp_path: Path) -> None:
    fake = _FakeBot()
    bot = _StatusBot()
    bot._config = SimpleNamespace(bot_data_dir=tmp_path, project_root=str(tmp_path))  # type: ignore[assignment]
    bot.application = SimpleNamespace(bot=fake)
    snapshot = {"value": (1, 90.0, [])}
    bot._project_chat = SimpleNamespace(background_workload_for_route=lambda u, c: snapshot["value"])

    await bot._sync_external_wait_status(7, 70)
    assert len(fake.sent) == 1 and "⏳ Running in background · background task (since " in fake.sent[0]["text"]
    assert fake.sent[0]["disable_notification"] is True

    snapshot["value"] = (0, 0.0, [time.time()])
    await bot._sync_external_wait_status(7, 70)
    assert len(fake.edited) == 1 and fake.edited[0]["text"].startswith("✅ background task finished → continuing")

    snapshot["value"] = None
    await bot._sync_external_wait_status(7, 70)
    assert len(fake.deleted) == 1
    assert ExternalWaitStatusStore(default_status_store_path(tmp_path / "external-wait")).get(7, 70) is None
