"""Readiness receipts never claim installation/rollback or expose probe output."""
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from telegram_bot import runtime_readiness as readiness


@pytest.fixture
def source(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    for name in ("requirements.lock.txt", "requirements.txt", "pyproject.toml"):
        (tmp_path / name).write_text("synthetic source\n")
    return tmp_path


def test_real_probe_suppresses_all_child_output(capsys):
    result = readiness.probe("fixture", ("-c", "import sys; print('PRIVATE-STDOUT'); sys.stderr.write('PRIVATE-STDERR'); raise SystemExit(17)"), 3)
    assert result["status"] == "fail" and result["exit_code"] == 17
    assert capsys.readouterr() == ("", "")
    assert "PRIVATE" not in json.dumps(result)


def test_probe_ignores_ambient_pythonpath_and_does_not_write_bytecode(tmp_path, monkeypatch):
    (tmp_path / "injected.py").write_text("raise RuntimeError('loaded')")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    result = readiness.probe("fixture", ("-c", "import sys, importlib.util; assert sys.dont_write_bytecode; assert importlib.util.find_spec('injected') is None"), 3)
    assert result["status"] == "pass"
    assert not (tmp_path / "__pycache__").exists()


def test_timeout_kills_term_resistant_descendant(tmp_path):
    marker = tmp_path / "child.pid"
    code = ("import subprocess,time,signal; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "p=subprocess.Popen(['sleep','30']); "
            f"Path({str(marker)!r}).write_text(str(p.pid)); time.sleep(30)")
    started = time.monotonic()
    result = readiness.probe("fixture", ("-c", code), 0.3)
    assert result["status"] == "timeout"
    assert time.monotonic() - started < 3
    pid = int(marker.read_text())
    # Reparented children can remain zombies until init reaps them; zombies
    # cannot run later side effects and count as terminated here.
    stat = Path(f"/proc/{pid}/stat")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            state = stat.read_text().split()[2]
        except (FileNotFoundError, ProcessLookupError):
            return  # Reaped before or during the procfs read.
        if state == "Z":
            return
        time.sleep(0.02)
    pytest.fail("descendant remained runnable after the probe timeout")


def test_exhausted_budget_does_not_spawn(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("spawned after deadline")
    monkeypatch.setattr(subprocess, "Popen", unexpected)
    assert readiness.probe("fixture", ("-c", "pass"), 0)["status"] == "not_run"


def test_collect_uses_one_budget_and_preserves_failed_checks(source, monkeypatch):
    monkeypatch.setattr(readiness, "git_identity", lambda path, timeout: {"head": None, "tracked_changes": None})
    monkeypatch.setattr(readiness, "runtime_identity", lambda: {"python": "fixture"})
    monkeypatch.setattr(readiness, "PROBES", (("slow", ("-c", "import time; time.sleep(20)")),
                                           ("next", ("-c", "raise SystemExit(0)"))))
    report = readiness.collect(source, 0.2)
    assert report["status"] == "unready"
    assert [check["status"] for check in report["checks"]] == ["timeout", "not_run"]
    assert report["lifecycle_scenarios"] == {name: "not_run" for name in
                                           ("fresh_install", "reinstall", "rollback", "service_restart")}


def test_identity_failure_does_not_run_probes_or_echo_exception(source, monkeypatch):
    (source / "requirements.lock.txt").unlink()
    monkeypatch.setattr(readiness, "probe", lambda *args: pytest.fail("probe called"))
    report = readiness.collect(source, 1)
    assert report["status"] == "error"
    assert report["reason"] == "identity_unavailable"


def test_source_symlink_is_rejected(source):
    path = source / "requirements.lock.txt"
    path.unlink()
    path.symlink_to(source / "requirements.txt")
    assert readiness.collect(source, 1)["status"] == "error"


def test_metadata_git_failure_is_explicit_unknown(source, monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/synthetic-invalid")
    assert readiness.git_identity(source) == {"head": None, "tracked_changes": None}


def test_git_identity_ignores_ambient_repo(source, monkeypatch):
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
    monkeypatch.setenv("GIT_DIR", "/synthetic-invalid")
    value = readiness.git_identity(source)
    assert value["head"] and value["tracked_changes"] is False
    index = source / ".git/index"
    before_index = index.read_bytes()
    tracked = source / "requirements.txt"
    stamp = tracked.stat().st_mtime_ns + 2_000_000_000
    os.utime(tracked, ns=(stamp, stamp))
    assert readiness.git_identity(source)["tracked_changes"] is False
    assert index.read_bytes() == before_index
    tracked.write_text("changed")
    assert readiness.git_identity(source)["tracked_changes"] is True
    assert index.read_bytes() == before_index
    # Also include staged content while preserving the newly staged index.
    clean_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run(["git", "-C", str(source), "add", "requirements.txt"], env=clean_env, check=True)
    staged_index = index.read_bytes()
    assert readiness.git_identity(source)["tracked_changes"] is True
    assert index.read_bytes() == staged_index


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1", "301"])
def test_invalid_budget_is_rejected(value):
    with pytest.raises(SystemExit) as exc:
        readiness.main(["--timeout-seconds", value])
    assert exc.value.code == 2


def test_cli_receipt_exit_and_no_install_claim(source, monkeypatch, capsys):
    monkeypatch.setattr(readiness, "git_identity", lambda path, timeout: {"head": "f" * 40, "tracked_changes": False})
    monkeypatch.setattr(readiness, "runtime_identity", lambda: {"python": "fixture"})
    monkeypatch.setattr(readiness, "PROBES", (("fixture", ("-c", "pass")),))
    assert readiness.main(["--bridge-dir", str(source)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "ccc.runtime-readiness.v1"
    assert report["status"] == "ready"
    assert len(report["source"]["inputs_sha256"]["requirements.lock.txt"]) == 64
    assert report["lifecycle_scenarios"]["service_restart"] == "not_run"


def test_expired_git_budget_does_not_spawn(source, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("spawn after deadline"))
    assert readiness.git_identity(source, 0) == {"head": None, "tracked_changes": None}


def test_git_wait_is_capped_by_remaining_shared_budget(source, monkeypatch):
    observed = []
    def run(*args, **kwargs):
        observed.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(subprocess, "run", run)
    assert readiness.git_identity(source, 0.1)["head"] is None
    assert 0 < observed[0] <= 0.1


def test_android_api_is_labeled_as_python_compatibility(monkeypatch):
    monkeypatch.setattr(readiness.sys, "getandroidapilevel", lambda: 24, raising=False)
    result = readiness.runtime_identity()
    assert result["python_android_api_level"] == 24
    assert "android_api_level" not in result
