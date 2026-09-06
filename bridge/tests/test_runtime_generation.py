"""Running health binds source/interpreter evidence to the existing PID/state."""
import json
import os
import subprocess

import pytest

from telegram_bot import runtime_generation as generation
from telegram_bot.utils import health


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    source = tmp_path / "source" / "bridge"
    source.mkdir(parents=True)
    (source / "runtime_generation.py").write_text("# initial source\n")
    (source / ".env").write_text("DO_NOT_REPORT_PRIVATE_CONTENT")
    prefix = tmp_path / "venv"
    prefix.mkdir()
    (prefix / ".req_hash").write_text("a" * 64 + "\n")
    monkeypatch.setattr(generation, "__file__", str(source / "runtime_generation.py"))
    monkeypatch.setattr(generation.sys, "prefix", str(prefix))
    monkeypatch.setattr(generation.sys, "executable", str(prefix / "bin/python"))
    return source, prefix


def test_generation_uses_actual_process_paths_and_never_environment(runtime, monkeypatch):
    source, prefix = runtime
    for key in ("VIRTUAL_ENV", "VENV_DIR", "PREPARED_RUNTIME", "CCC_NODE_ROOT", "PYTHONPATH"):
        monkeypatch.setenv(key, "/unrelated/PRIVATE")
    result = generation.capture_runtime_generation()
    assert result["source_dir"] == str(source)
    assert result["python_prefix"] == str(prefix)
    assert result["python_executable"] == str(prefix / "bin/python")
    assert result["source_seal"] == generation.source_seal(source)
    assert result["dependency_fingerprint"] == "a" * 64
    assert result["source_git"] == {"head": None, "tracked_changes": None}
    assert result["collection_errors"] == []
    assert "PRIVATE" not in json.dumps(result)
    assert "ready" not in result


def test_generation_reports_git_commit_and_dirty_source(runtime, monkeypatch):
    source, _ = runtime
    repo = source.parent
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git("init", "-q")
    git("add", "bridge/runtime_generation.py")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    head = git("rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", "/irrelevant/PRIVATE")
    monkeypatch.setenv("GIT_WORK_TREE", "/irrelevant/PRIVATE")
    clean = generation.capture_runtime_generation()
    assert clean["source_git"] == {"head": head, "tracked_changes": False}
    (source / "runtime_generation.py").write_text("# changed\n")
    dirty = generation.capture_runtime_generation()
    assert dirty["source_git"] == {"head": head, "tracked_changes": True}
    assert dirty["source_seal"] != clean["source_seal"]


@pytest.mark.parametrize("value", [None, "PRIVATE", "a" * 1000, "\xff", "b" * 63])
def test_absent_or_malformed_bootstrap_marker_is_unknown_without_content(runtime, value):
    _, prefix = runtime
    marker = prefix / ".req_hash"
    if value is None:
        marker.rename(prefix / "retained-hash")
    else:
        marker.write_bytes(value.encode("latin1"))
    result = generation.capture_runtime_generation()
    assert result["dependency_fingerprint"] is None
    assert result["collection_errors"] == ["dependency_fingerprint_unavailable"]
    assert "PRIVATE" not in json.dumps(result)


def test_symlink_inputs_are_unknown_and_not_followed(runtime):
    source, prefix = runtime
    (source / "linked.py").symlink_to(source / ".env")
    marker = prefix / ".req_hash"
    marker.rename(prefix / "retained-hash")
    marker.symlink_to(source / ".env")
    result = generation.capture_runtime_generation()
    assert result["source_seal"] is None
    assert result["dependency_fingerprint"] is None
    assert set(result["collection_errors"]) == {"source_seal_unavailable", "dependency_fingerprint_unavailable"}
    assert "DO_NOT_REPORT" not in json.dumps(result)


def test_actual_health_keeps_startup_generation_across_status_changes(runtime, tmp_path, monkeypatch):
    source, prefix = runtime
    reporter = health.RuntimeHealthReporter(tmp_path / "project")
    reporter.initialize_process()
    initial = json.loads(reporter.health_file.read_text())
    assert initial["service"]["state"] == "starting"
    assert initial["runtime_generation"]["dependency_fingerprint"] == "a" * 64
    assert initial["process"]["pid"] > 0
    (source / "runtime_generation.py").write_text("# different generation\n")
    (prefix / ".req_hash").write_text("b" * 64)
    monkeypatch.setattr(health, "capture_runtime_generation", lambda: pytest.fail("startup identity recaptured"))
    reporter.initialize_process()
    reporter.record_telegram_ok()
    reporter.record_agent_ok()
    available = json.loads(reporter.health_file.read_text())
    assert available["service"]["state"] == "available"
    assert available["runtime_generation"] == initial["runtime_generation"]
    assert available["process"]["pid"] == initial["process"]["pid"]
    reporter.mark_unavailable("fixture stopped")
    stopped = json.loads(reporter.health_file.read_text())
    assert stopped["service"]["state"] == "unavailable"
    assert stopped["runtime_generation"] == initial["runtime_generation"]


def test_incomplete_identity_does_not_prevent_health_reporting(runtime, tmp_path):
    source, prefix = runtime
    (source / "linked.py").symlink_to(source / ".env")
    (prefix / ".req_hash").write_text("invalid")
    reporter = health.RuntimeHealthReporter(tmp_path / "project")
    reporter.initialize_process()
    reporter.record_telegram_ok()
    reporter.record_agent_ok()
    state = json.loads(reporter.health_file.read_text())
    assert state["service"]["state"] == "available"
    assert len(state["runtime_generation"]["collection_errors"]) == 2


def test_non_utf8_git_status_cannot_abort_health_startup(runtime, tmp_path):
    source, _ = runtime
    repo = source.parent
    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    git("init", "-q")
    git("config", "core.quotePath", "false")
    filename = os.fsencode(source) + b"/odd-\xff.py"
    with open(filename, "wb") as stream:
        stream.write(b"# initial\n")
    git("add", "bridge")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    with open(filename, "ab") as stream:
        stream.write(b"# modified\n")
    reporter = health.RuntimeHealthReporter(tmp_path / "project")
    reporter.initialize_process()
    reporter.record_telegram_ok()
    reporter.record_agent_ok()
    state = json.loads(reporter.health_file.read_text())
    assert state["service"]["state"] == "available"
    assert state["runtime_generation"]["source_git"] == {"head": None, "tracked_changes": None}
    assert state["runtime_generation"]["source_seal"] is None
    assert state["runtime_generation"]["dependency_fingerprint"] == "a" * 64
