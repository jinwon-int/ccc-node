"""Operator ack for dropped external-wait promises + cross-process lock (#2730).

A dropped promise (wake delivered, ``resumed is False``) resurfaced at every
SessionStart even after the promised step was done by hand, because nothing
could close it: ``cancel`` only touches monitoring waits, ``mark_wake`` is the
bridge's own journal call, and editing ``waits.json`` beside a running bridge
lost updates (the registry lock was a per-object ``threading.Lock``).
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import stat
import sys
from pathlib import Path

import pytest

from telegram_bot.core import external_wait, external_wait_cli
from telegram_bot.core.external_wait import (
    ACK_ACKED,
    ACK_ALREADY,
    ACK_MONITORING,
    ACK_NO_WAKE,
    ACK_NOT_FOUND,
    ACK_WAKE_PENDING,
    SOURCE_DETACHED_JOB,
    SOURCE_LOCAL_TASK,
    TERMINAL_LOST,
    TERMINAL_SUCCESS,
    ExternalWaitRegistry,
    default_registry_path,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CCC_EXTERNAL_WAIT_HOME", str(tmp_path))
    return tmp_path


def _registry(home: Path) -> ExternalWaitRegistry:
    return ExternalWaitRegistry(default_registry_path(home))


def _detached(registry: ExternalWaitRegistry, unit: str, summary: str = "read the log and report") -> str:
    return registry.register(
        repo="",
        pr_number=None,
        head_sha="",
        user_id=7,
        chat_id=70,
        session_id="sess-1",
        summary=summary,
        timeout_seconds=3600,
        poll_interval_seconds=30,
        source=SOURCE_DETACHED_JOB,
        unit=unit,
        log_path=f"/tmp/{unit}.log",
    )


def _dropped(registry: ExternalWaitRegistry, unit: str = "job-a") -> str:
    """The measured #2730 shape: terminal, wake done, resumed=false."""
    wait_id = _detached(registry, unit)
    assert registry.finish(wait_id, TERMINAL_LOST)
    registry.mark_wake(wait_id, delivered=True, resumed=False, skip_reason="non_terminal_rollup")
    return wait_id


# --- registry ---------------------------------------------------------------


def test_ack_closes_a_dropped_promise_and_keeps_how_it_ended(home: Path) -> None:
    registry = _registry(home)
    wait_id = _dropped(registry)
    assert [r["wait_id"] for r in registry.dropped_promises()] == [wait_id]

    assert registry.acknowledge(wait_id, note="  checked the\nlog by hand ") == ACK_ACKED

    rec = registry.get(wait_id)
    assert registry.dropped_promises() == []
    assert rec["state"] == TERMINAL_LOST and rec["terminal_status"] == TERMINAL_LOST
    wake = rec["wake"]
    assert wake["state"] == "done" and wake["resumed"] is True
    assert wake["skip_reason"] == "non_terminal_rollup"
    ack = wake["manual_ack"]
    assert ack["prior_resumed"] is False
    assert ack["note"] == "checked the log by hand"
    assert ack["at"]


def test_ack_is_idempotent(home: Path) -> None:
    registry = _registry(home)
    wait_id = _dropped(registry)
    assert registry.acknowledge(wait_id) == ACK_ACKED
    first = registry.get(wait_id)["wake"]["manual_ack"]

    assert registry.acknowledge(wait_id, note="again") == ACK_ALREADY
    assert registry.get(wait_id)["wake"]["manual_ack"] == first


def test_ack_refuses_what_it_must_not_touch(home: Path) -> None:
    registry = _registry(home)
    assert registry.acknowledge("nope") == ACK_NOT_FOUND

    monitoring = _detached(registry, "job-mon")
    assert registry.acknowledge(monitoring) == ACK_MONITORING
    assert registry.get(monitoring)["state"] == "monitoring"

    pending = _detached(registry, "job-pending")
    registry.finish(pending, TERMINAL_SUCCESS)
    assert registry.acknowledge(pending) == ACK_WAKE_PENDING
    assert registry.get(pending)["wake"] == {"state": "pending", "attempts": 0}

    task = registry.register(
        repo="",
        pr_number=None,
        head_sha="",
        user_id=7,
        chat_id=70,
        session_id="sess-1",
        summary="label",
        timeout_seconds=600,
        poll_interval_seconds=30,
        source=SOURCE_LOCAL_TASK,
        task_id="bgtask1",
    )
    # A record that predates the wake journal (wake=None) owed nothing.
    data = json.loads(default_registry_path(home).read_text(encoding="utf-8"))
    data[task]["state"] = TERMINAL_LOST
    data[task]["wake"] = None
    default_registry_path(home).write_text(json.dumps(data), encoding="utf-8")
    assert registry.acknowledge(task) == ACK_NO_WAKE


# --- CLI ----------------------------------------------------------------------


def test_cli_ack_round_trip_without_an_active_route(home: Path, capsys: pytest.CaptureFixture) -> None:
    wait_id = _dropped(_registry(home))

    rc = external_wait_cli.main(["ack", wait_id, "--note", "cleaned up by hand"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {
        "ok": True,
        "wait_id": wait_id,
        "outcome": ACK_ACKED,
        "message": payload["message"],
    }
    external_wait_cli.main(["list"])
    listed = json.loads(capsys.readouterr().out.strip())["waits"]
    assert listed[0]["resumed"] is True and listed[0]["manual_ack"] is True

    assert external_wait_cli.main(["ack", wait_id]) == 0
    assert json.loads(capsys.readouterr().out.strip())["outcome"] == ACK_ALREADY


def test_cli_ack_failures_are_machine_visible(home: Path, capsys: pytest.CaptureFixture) -> None:
    assert external_wait_cli.main(["ack"]) == external_wait_cli.RC_USAGE
    assert json.loads(capsys.readouterr().out.strip())["code"] == "validation"

    assert external_wait_cli.main(["ack", "unknown"]) == external_wait_cli.RC_USAGE
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False and payload["code"] == "not-ackable"
    assert payload["outcome"] == ACK_NOT_FOUND

    monitoring = _detached(_registry(home), "job-mon")
    assert external_wait_cli.main(["ack", monitoring]) == external_wait_cli.RC_USAGE
    assert json.loads(capsys.readouterr().out.strip())["outcome"] == ACK_MONITORING


# --- cross-process lock -------------------------------------------------------


def _register_many(path: str, worker: int, count: int) -> None:
    registry = ExternalWaitRegistry(Path(path))
    for i in range(count):
        registry.register(
            repo="",
            pr_number=None,
            head_sha="",
            user_id=7,
            chat_id=70,
            session_id=None,
            summary="",
            timeout_seconds=600,
            poll_interval_seconds=30,
            source=SOURCE_LOCAL_TASK,
            task_id=f"w{worker}-t{i}",
        )


@pytest.mark.skipif(
    external_wait.fcntl is None or sys.platform == "win32"
    or "fork" not in multiprocessing.get_all_start_methods(),
    reason="needs POSIX flock and fork",
)
def test_concurrent_writers_in_separate_processes_lose_nothing(tmp_path: Path) -> None:
    path = default_registry_path(tmp_path)
    ctx = multiprocessing.get_context("fork")
    workers, count = 4, 15
    procs = [ctx.Process(target=_register_many, args=(str(path), w, count)) for w in range(workers)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0

    records = ExternalWaitRegistry(path).records()
    assert len(records) == workers * count
    lock = path.with_name(path.name + ".lock")
    assert stat.S_IMODE(os.stat(lock).st_mode) == 0o600


@pytest.mark.skipif(external_wait.fcntl is None, reason="needs POSIX flock")
def test_a_held_lock_times_out_fail_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(external_wait, "_FILE_LOCK_TIMEOUT_SECONDS", 0.1)
    path = default_registry_path(tmp_path)
    registry = ExternalWaitRegistry(path)
    wait_id = _dropped(registry)
    lock = path.with_name(path.name + ".lock")
    fd = os.open(lock, os.O_RDWR)
    try:
        external_wait.fcntl.flock(fd, external_wait.fcntl.LOCK_EX)
        with caplog.at_level(logging.WARNING, logger=external_wait.logger.name):
            assert registry.acknowledge(wait_id) == ACK_ACKED
    finally:
        os.close(fd)
    assert "lock timed out" in caplog.text
    assert registry.get(wait_id)["wake"]["resumed"] is True
