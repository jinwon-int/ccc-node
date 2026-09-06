"""Prepared restart requires a fresh observed candidate generation."""
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
import os
import subprocess
import sys
import time

import pytest

from telegram_bot import prepared_serving as serving
from telegram_bot.utils.health_render import render_status_lines


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


@pytest.fixture
def candidate(tmp_path):
    now = time.time()
    source = str(tmp_path / "source")
    runtime = str(tmp_path / "job/runtime")
    seal = {"sha256": "a" * 64, "files": 12, "bytes": 1024}
    expected = dict(schema="ccc.prepared-runtime.v1", status="ready", source_dir=source,
                    runtime_dir=runtime, source_seal=seal, dependency_fingerprint="b" * 64,
                    source_git={"head": "c" * 40, "tracked_changes": False})
    health = dict(schema_version=1, updated_at=iso(now - 1),
                  process={"pid": os.getpid(), "started_at": iso(now - 3)},
                  service={"state": "available"}, telegram={"state": "healthy"},
                  agent={"state": "healthy", "provider": "piri"},
                  runtime_generation=dict(schema="ccc.runtime-generation.v1", observed_at=iso(now - 2),
                                          source_dir=source, source_seal=deepcopy(seal),
                                          source_git=deepcopy(expected["source_git"]),
                                          python_prefix=runtime, python_executable=runtime + "/bin/python",
                                          dependency_fingerprint="b" * 64, collection_errors=[]))
    return expected, health, now


def test_matching_generation_is_observed_available(candidate):
    expected, health, now = candidate
    result = serving.verify(expected, health, os.getpid(), now - 10, 150, now)
    assert result["status"] == "available"
    assert result["source_seal"] == expected["source_seal"]


@pytest.mark.parametrize("field,value", [
    ("source_dir", "/another/source"), ("source_seal", {"sha256": "d" * 64}),
    ("python_prefix", "/old/venv"), ("python_executable", "/usr/bin/python"),
    ("python_executable", "relative/python"), ("dependency_fingerprint", "e" * 64),
    ("source_git", {"head": "f" * 40}), ("source_git", None),
    ("collection_errors", ["source_seal_unavailable"]), ("collection_errors", None),
    ("schema", "other"),
])
def test_generic_available_does_not_prove_generation(candidate, tmp_path, field, value):
    expected, health, now = candidate
    health["runtime_generation"][field] = value
    path = tmp_path / "health.json"
    path.write_text(json.dumps(health))
    assert "Bot status: available" in render_status_lines(
        path, str(os.getpid()), 150, "piri", now=datetime.fromtimestamp(now, timezone.utc)
    )[0]
    with pytest.raises(ValueError):
        serving.verify(expected, health, os.getpid(), now - 10, 150, now)


@pytest.mark.parametrize("where,field,value", [
    ("process", "pid", 1), ("process", "pid", True), ("process", "pid", "42"),
    ("service", "state", "starting"), ("telegram", "state", "degraded"),
    ("agent", "state", "degraded"), ("process", "started_at", "bad"),
    ("process", "started_at", "2026-01-01T00:00:00"),
])
def test_bad_state_pid_or_times_fail(candidate, where, field, value):
    expected, health, now = candidate
    health[where][field] = value
    with pytest.raises(ValueError):
        serving.verify(expected, health, os.getpid(), now - 10, 150, now)


@pytest.mark.parametrize("field,offset", [("updated_at", -200), ("updated_at", 5),
                                         ("started_at", -20), ("observed_at", -4),
                                         ("observed_at", 1)])
def test_old_future_or_inconsistent_snapshot_fails(candidate, field, offset):
    expected, health, now = candidate
    target = health if field == "updated_at" else health["process" if field == "started_at" else "runtime_generation"]
    target[field] = iso(now + offset)
    with pytest.raises(ValueError):
        serving.verify(expected, health, os.getpid(), now - 10, 150, now)


def test_archive_without_git_uses_sealed_source(candidate):
    expected, health, now = candidate
    expected["source_git"] = health["runtime_generation"]["source_git"] = {"head": None}
    assert serving.verify(expected, health, os.getpid(), now - 10, 150, now)["status"] == "available"


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), 3601])
def test_invalid_max_age_fails(candidate, value):
    expected, health, now = candidate
    with pytest.raises(ValueError):
        serving.verify(expected, health, os.getpid(), now - 10, value, now)


def test_regular_health_read_and_unsafe_inputs(tmp_path):
    path = tmp_path / "health.json"
    path.write_text('{"schema_version": 1}')
    assert serving.read_health(path) == {"schema_version": 1}
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        serving.read_health(link)
    os.link(path, tmp_path / "hardlink")
    with pytest.raises(ValueError):
        serving.read_health(path)
    (tmp_path / "hardlink").unlink()
    path.chmod(0o666)
    with pytest.raises(ValueError):
        serving.read_health(path)
    path.chmod(0o600)
    path.write_bytes(b" " * (serving.LIMIT + 1))
    with pytest.raises(ValueError):
        serving.read_health(path)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError):
        serving.read_health(fifo)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        serving.read_health(alias / "health.json")


def test_cli_success_and_body_free_failure(candidate, tmp_path, monkeypatch, capsys):
    expected, health, now = candidate
    path = tmp_path / "health.json"
    path.write_text(json.dumps(health))
    args = ["--health-file", str(path), "--pid", str(os.getpid()), "--not-before", str(now - 10)]
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(expected).encode())))
    assert serving.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "available"
    for malformed in (b"sensitive test content", b"[]", b"null", b" " * (serving.LIMIT + 1)):
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(malformed)))
        assert serving.main(args) == 1
        report = capsys.readouterr().out
        assert "sensitive test content" not in report
        assert json.loads(report)["reason"] == "serving_generation_unverified"


@pytest.mark.skipif(not hasattr(os, "WNOWAIT"), reason="requires zombie observation")
def test_exited_unreaped_process_is_not_serving():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        os.kill(child.pid, 0)
        assert serving.process_alive(child.pid) is False
    finally:
        child.wait(timeout=5)


def test_dead_process_rejected(candidate, monkeypatch):
    expected, health, now = candidate
    monkeypatch.setattr(serving, "process_alive", lambda _: False)
    with pytest.raises(ValueError, match="not_alive"):
        serving.verify(expected, health, os.getpid(), now - 10, 150, now)


@pytest.mark.parametrize("state,alive", [("R", True), ("Ss+", True), ("T", True),
                                        ("Z", False), ("Z+", False), ("X", False),
                                        ("", False), ("?", False)])
def test_ps_fallback_requires_live_state(monkeypatch, state, alive):
    monkeypatch.setattr(serving.os, "kill", lambda *_: None)
    monkeypatch.setattr(serving.Path, "exists", lambda _: False)
    monkeypatch.setattr(serving.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, stdout=state))
    assert serving.process_alive(12345) is alive


def test_ps_timeout_is_unready(monkeypatch):
    monkeypatch.setattr(serving.os, "kill", lambda *_: None)
    monkeypatch.setattr(serving.Path, "exists", lambda _: False)
    def timeout(*a, **kw):
        assert kw["timeout"] == 1
        raise subprocess.TimeoutExpired("ps", 1)
    monkeypatch.setattr(serving.subprocess, "run", timeout)
    assert serving.process_alive(12345) is False
