"""Detached systemd jobs as external waits (#2605 layer 3)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from telegram_bot.core import external_wait_cli
from telegram_bot.core.external_wait import (
    SOURCE_DETACHED_JOB,
    STATE_MONITORING,
    TERMINAL_FAILURE,
    TERMINAL_LOST,
    TERMINAL_SUCCESS,
    ExternalWaitRegistry,
    ExternalWaitValidationError,
    default_registry_path,
    publish_active_turn,
    render_waits,
    validate_log_path,
    validate_unit_name,
)
from telegram_bot.core.external_wait_monitor import (
    DetachedJobState,
    ExternalWaitMonitor,
    detached_job_terminal,
    inspect_detached_job,
    read_exit_marker,
    resume_prompt_text,
    wake_notification_text,
)
from telegram_bot.core.external_wait_status import render_wait_status


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


NOW = 1_700_000_000.0


class _Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _registry(tmp_path: Path, clock: _Clock) -> ExternalWaitRegistry:
    return ExternalWaitRegistry(default_registry_path(tmp_path / "external-wait"), clock=clock)


def _register(registry: ExternalWaitRegistry, log: Path, *, unit: str = "nightly-compare", now: float = NOW, **overrides) -> str:
    params = dict(
        repo="", pr_number=None, head_sha="", user_id=7, chat_id=70, session_id="sess-1",
        summary="read the compare log and report", timeout_seconds=7200, poll_interval_seconds=30,
        now=now, source=SOURCE_DETACHED_JOB, unit=unit, log_path=str(log),
    )
    params.update(overrides)
    return registry.register(**params)


class _NoTransport:
    async def fetch_pr_state(self, repo, pr_number):  # pragma: no cover - must never run
        raise AssertionError("detached-job waits must not reach a transport")

    fetch_actions_run = fetch_merge_queue_state = fetch_failed_queue_run = fetch_pr_state


class _Recorder:
    def __init__(self) -> None:
        self.notifications: list[tuple[int, str]] = []
        self.resumes: list[tuple[dict, str]] = []
        self.synced: list[tuple[int, int]] = []

    async def notify(self, chat_id, text):
        self.notifications.append((chat_id, text))
        return True

    async def resume(self, record, prompt):
        self.resumes.append((record, prompt))
        return True

    async def sync(self, user_id, chat_id):
        self.synced.append((user_id, chat_id))


async def _session_of(user_id, chat_id):
    return "sess-1"


def _monitor(registry, recorder, clock, *, unit_active: bool = False) -> ExternalWaitMonitor:
    monitor = ExternalWaitMonitor(
        registry, transport=_NoTransport(), notifier=recorder.notify, resumer=recorder.resume,
        session_lookup=_session_of, clock=clock, status_syncer=recorder.sync,
    )

    def probe(log_path: str, unit: str) -> DetachedJobState:
        real = inspect_detached_job(log_path, unit)
        return DetachedJobState(real.exit_code, real.log_exists, real.log_age_seconds, unit_active and real.exit_code is None)

    monitor._inspect_detached_job = staticmethod(probe)  # type: ignore[assignment]
    return monitor


# --- validation + registry -----------------------------------------------------------


def test_unit_and_log_validation() -> None:
    assert validate_unit_name("nightly-compare.service") == "nightly-compare"
    assert validate_unit_name("self-restart-self-update-2579-1725") == "self-restart-self-update-2579-1725"
    for bad in ("", "has space", "a/b", "x" * 250):
        with pytest.raises(ExternalWaitValidationError):
            validate_unit_name(bad)
    assert validate_log_path("/root/.claude/state/x.log") == "/root/.claude/state/x.log"
    assert validate_log_path("~/.claude/state/x.log") == os.path.join(os.path.expanduser("~"), ".claude/state/x.log")
    for bad in ("", "relative.log", "/root/../etc/passwd", "/x/" + "y" * 1030):
        with pytest.raises(ExternalWaitValidationError):
            validate_log_path(bad)


def test_registry_keys_detached_jobs_by_unit_and_log(tmp_path: Path) -> None:
    clock = _Clock()
    registry = _registry(tmp_path, clock)
    log = tmp_path / "job.log"
    wait_id = _register(registry, log)
    rec = registry.get(wait_id)
    assert rec["source"] == SOURCE_DETACHED_JOB and rec["unit"] == "nightly-compare"
    assert rec["log_path"] == str(log) and rec["repo"] == "" and rec["pr_number"] is None
    assert rec["next_poll_epoch"] == NOW  # polled from the start, unlike local tasks
    assert _register(registry, log) == wait_id
    assert _register(registry, tmp_path / "other.log") != wait_id
    assert "detached job nightly-compare" in render_waits(registry.records())


# --- marker + classification ---------------------------------------------------------


def test_exit_marker_reads_only_the_tail_and_tolerates_garbage(tmp_path: Path) -> None:
    log = tmp_path / "big.log"
    log.write_bytes(b"x" * 20_000 + b"\nEXIT=0\n")
    assert read_exit_marker(str(log)) == 0
    log.write_text("line\nEXIT=3\nmore\nEXIT=abc\n")
    assert read_exit_marker(str(log)) == -1  # malformed marker still proves a stop
    log.write_text("still running\n")
    assert read_exit_marker(str(log)) is None
    assert read_exit_marker(str(tmp_path / "missing.log")) is None


def test_detached_job_terminal_matrix() -> None:
    assert detached_job_terminal(DetachedJobState(0, True, 1.0, False)) == (TERMINAL_SUCCESS, {"exit_code": 0})
    assert detached_job_terminal(DetachedJobState(2, True, 1.0, False)) == (TERMINAL_FAILURE, {"exit_code": 2})
    assert detached_job_terminal(DetachedJobState(None, True, 5.0, False)) is None  # fresh log
    assert detached_job_terminal(DetachedJobState(None, True, 5000.0, True)) is None  # quiet but alive
    assert detached_job_terminal(DetachedJobState(None, True, 5000.0, False)) == (TERMINAL_LOST, {"reason": "no-exit-marker"})
    assert detached_job_terminal(DetachedJobState(None, False, None, False)) == (TERMINAL_LOST, {"reason": "no-log"})
    assert detached_job_terminal(DetachedJobState(None, False, None, True)) is None  # unit up, log not yet written


# --- monitor -------------------------------------------------------------------------


@pytest.mark.anyio
async def test_monitor_waits_for_the_marker_then_wakes_and_resumes(tmp_path: Path) -> None:
    clock = _Clock()
    registry = _registry(tmp_path, clock)
    log = tmp_path / "job.log"
    log.write_text("working...\n")
    wait_id = _register(registry, log)
    recorder = _Recorder()
    monitor = _monitor(registry, recorder, clock, unit_active=True)

    await monitor._tick()
    assert registry.get(wait_id)["state"] == STATE_MONITORING
    assert recorder.notifications == [] and recorder.resumes == []
    # Backoff is bounded by the detached-job ceiling.
    assert registry.get(wait_id)["poll_interval_seconds"] <= 60

    log.write_text("working...\ndone\nEXIT=0\n")
    clock.now += 120
    await monitor._tick()
    rec = registry.get(wait_id)
    assert rec["terminal_status"] == TERMINAL_SUCCESS and rec["terminal_detail"] == {"exit_code": 0}
    assert len(recorder.notifications) == 1 and len(recorder.resumes) == 1
    chat_id, text = recorder.notifications[0]
    assert chat_id == 70 and text.startswith("✅ Detached job finished — detached job nightly-compare")
    assert "Exit code: 0" in text and "Continuing automatically." in text
    _, prompt = recorder.resumes[0]
    assert prompt.startswith(f"[external_event: detached_job terminal=success unit=nightly-compare log={log} exit=0]")
    assert "read the compare log and report" in prompt


@pytest.mark.anyio
async def test_monitor_reports_failure_exit_codes(tmp_path: Path) -> None:
    clock = _Clock()
    registry = _registry(tmp_path, clock)
    log = tmp_path / "job.log"
    log.write_text("boom\nEXIT=7\n")
    wait_id = _register(registry, log)
    recorder = _Recorder()
    await _monitor(registry, recorder, clock)._tick()
    rec = registry.get(wait_id)
    assert rec["terminal_status"] == TERMINAL_FAILURE and rec["terminal_detail"] == {"exit_code": 7}
    assert recorder.notifications[0][1].startswith("❌ Detached job failed")
    assert "exit=7" in recorder.resumes[0][1]


@pytest.mark.anyio
async def test_monitor_marks_a_vanished_unit_without_marker_as_lost(tmp_path: Path) -> None:
    clock = _Clock()
    registry = _registry(tmp_path, clock)
    missing = tmp_path / "never-written.log"
    wait_id = _register(registry, missing)
    recorder = _Recorder()
    await _monitor(registry, recorder, clock, unit_active=False)._tick()
    rec = registry.get(wait_id)
    assert rec["terminal_status"] == TERMINAL_LOST and rec["terminal_detail"] == {"reason": "no-log"}
    assert recorder.notifications[0][1].startswith("ℹ️ Detached job lost (no EXIT marker)")
    assert "The log was never written" in recorder.notifications[0][1]

    stale = tmp_path / "stale.log"
    stale.write_text("partial\n")
    os.utime(stale, (time.time() - 3600, time.time() - 3600))
    registry2 = _registry(tmp_path / "two", clock)
    wait2 = _register(registry2, stale)
    recorder2 = _Recorder()
    await _monitor(registry2, recorder2, clock, unit_active=False)._tick()
    assert registry2.get(wait2)["terminal_detail"] == {"reason": "no-exit-marker"}
    assert "read the log before re-running" in recorder2.notifications[0][1]


# --- status line + wording -------------------------------------------------------------


def test_status_lines_for_detached_jobs(tmp_path: Path) -> None:
    clock = _Clock()
    registry = _registry(tmp_path, clock)
    wait_id = _register(registry, tmp_path / "job.log")
    text = render_wait_status(registry.records(), NOW + 10)
    assert text.startswith("⏳ Running detached · detached job nightly-compare — read the compare log and report (registered ")
    assert "up to 2h" in text
    registry.finish(wait_id, TERMINAL_FAILURE, now=NOW + 20, detail={"exit_code": 3})
    assert render_wait_status(registry.records(), NOW + 30).startswith("❌ detached job failed → investigating · detached job nightly-compare (")
    rec = registry.get(wait_id)
    assert "Exit code: 3" in wake_notification_text(rec, resumed=False)
    assert " exit=3]" in resume_prompt_text(rec)
    assert "(exit 3)" in render_waits(registry.records())


# --- CLI --------------------------------------------------------------------------------


def test_cli_registers_a_detached_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("CCC_EXTERNAL_WAIT_HOME", str(tmp_path))
    publish_active_turn(tmp_path, user_id=7, chat_id=70, session_id="sess-1")
    log = tmp_path / "state" / "job.log"
    rc = external_wait_cli.main([
        "register", "--source", "detached-job", "--unit", "nightly-compare.service",
        "--log", str(log), "--summary", "read the compare log", "--timeout-seconds", "7200",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] and payload["source"] == SOURCE_DETACHED_JOB and payload["unit"] == "nightly-compare"
    rec = ExternalWaitRegistry(default_registry_path(tmp_path)).get(payload["wait_id"])
    assert rec["log_path"] == str(log) and rec["user_id"] == 7 and rec["chat_id"] == 70

    rc = external_wait_cli.main(["register", "--source", "detached-job", "--unit", "x", "--log", "relative.log"])
    assert rc == 2
    assert json.loads(capsys.readouterr().out.strip())["code"] == "validation"
