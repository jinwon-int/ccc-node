"""Regression coverage for the Termux pre-stop dependency boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from telegram_bot import restart_preflight as gate
from telegram_bot.dependency_bootstrap import DependencyPaths, InstallMode, dependency_fingerprint


@pytest.fixture
def ready(tmp_path, monkeypatch):
    source = tmp_path / "source"
    runtime = tmp_path / "runtime"
    source.mkdir()
    runtime.mkdir()
    for name in ("requirements.txt", "requirements.lock.txt", "pyproject.toml"):
        (source / name).write_text("fixture\n")
    paths = DependencyPaths.from_roots(source, runtime, source / ".env")
    paths.hash_cache.write_text(dependency_fingerprint(paths, InstallMode.LOCKED))
    monkeypatch.setattr(sys, "prefix", str(runtime))
    monkeypatch.setattr(gate.deps, "android_build_api", lambda: 24)
    monkeypatch.delenv("ANDROID_API_LEVEL", raising=False)
    monkeypatch.setattr(gate, "probe", lambda name, args, budget: {"id": name, "status": "pass"})
    return source, runtime


@pytest.mark.parametrize("failure,reason", [
    ("api", "android_api_override_mismatch"),
    ("missing", "runtime_inputs_unavailable"),
    ("changed", "dependencies_changed"),
    ("link", "runtime_inputs_unavailable"),
    ("oversize", "runtime_inputs_unavailable"),
])
def test_refusal_before_probes(ready, monkeypatch, failure, reason):
    source, runtime = ready
    if failure == "api":
        monkeypatch.setenv("ANDROID_API_LEVEL", "33")
    elif failure == "missing":
        (runtime / ".req_hash").unlink()
    elif failure == "changed":
        (source / "requirements.lock.txt").write_text("new generation")
    elif failure == "link":
        (runtime / ".req_hash").unlink()
        (runtime / ".req_hash").symlink_to(source / "requirements.txt")
    else:
        (runtime / ".req_hash").write_bytes(b"x" * 129)
    monkeypatch.setattr(gate, "probe", lambda *args: pytest.fail("probe after refusal"))
    report = gate.check(source, runtime, "0", 30)
    assert report["status"] == "preparation_required" and report["reason"] == reason


def test_wrong_interpreter_refused(ready, monkeypatch):
    source, runtime = ready
    monkeypatch.setattr(sys, "prefix", sys.base_prefix)
    assert gate.check(source, runtime, "0", 30)["reason"] == "selected_venv_mismatch"


@pytest.mark.parametrize("failed", ["android_native_import", "sdk_import", "aes_gcm", "pip_check"])
def test_failed_probe_cannot_pass(ready, monkeypatch, failed):
    monkeypatch.setattr(gate, "probe", lambda name, args, budget:
                        {"id": name, "status": "fail" if name == failed else "pass"})
    assert gate.check(*ready, "0", 30)["reason"] == "runtime_checks_failed"


def test_ready_gate_preserves_inputs_and_uses_single_budget(ready, monkeypatch):
    source, runtime = ready
    before = {p: p.read_bytes() for d in ready for p in d.iterdir()}
    budgets = []
    def probe(name, args, budget):
        budgets.append(budget)
        return {"id": name, "status": "pass"}
    monkeypatch.setattr(gate, "probe", probe)
    result = gate.check(source, runtime, "0", 30)
    assert result["status"] == "ready" and len(budgets) == 4
    assert 0 < budgets[-1] <= budgets[0] <= 30
    assert before == {p: p.read_bytes() for d in ready for p in d.iterdir()}


def test_install_mode_matches_bootstrap(ready):
    source, runtime = ready
    (source / ".env").write_text("CCC_DEPS_UNLOCKED=1\n")
    assert gate.check(source, runtime, "0", 30)["status"] == "ready"
    assert gate.check(source, runtime, "", 30)["reason"] == "dependencies_changed"


def test_cli_refusal_is_categorical_json(ready, capsys, monkeypatch):
    monkeypatch.setenv("ANDROID_API_LEVEL", "PRIVATE-WRONG-API")
    assert gate.main(["--bridge-dir", str(ready[0]), "--venv-dir", str(ready[1])]) == 6
    out = capsys.readouterr().out
    assert "PRIVATE" not in out
    assert json.loads(out)["reason"] == "android_api_override_mismatch"


@pytest.mark.parametrize("platform, prepared, verdict, expected, stopped", [
    ("termux", False, 6, 6, False),
    ("termux", False, 0, 1, True),
    ("linux", False, 6, 1, True),
    ("termux", True, 6, 1, True),
])
def test_shell_gate_preserves_live_pid_and_health(tmp_path, platform, prepared, verdict, expected, stopped):
    # Only the owned sleeper can be stopped. Manager and spawn paths are
    # fixtures; the real do_restart decides whether it is safe to call stop.
    start = Path(__file__).resolve().parents[1] / "start.sh"
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    python = runtime / "bin/python"
    python.write_text(f"#!{subprocess.check_output(['which', 'sh'], text=True).strip()}\nexit {verdict}\n")
    python.chmod(0o700)
    health = tmp_path / "health.json"
    health.write_text('{"service":{"state":"available"},"telegram":{"state":"healthy"}}')
    original = health.read_bytes()
    with subprocess.Popen(["sleep", "60"]) as old:
        script = r'''
CCC_START_SH_LIB_ONLY=1 . "$1" --path "$2" >/dev/null
VENV_DIR="$2/runtime"
PREPARED_RUNTIME="$PREPARED"
restart_caller_bridge_ancestor() { :; }
bash() { case "$1" in */service-systemd.sh) return 1;; *) exit 99;; esac; }
merge_env_files() { :; }
check_env() { :; }
validate_prepared_runtime() { echo '{}'; }
read_pid() { echo "$OLD_PID"; }
read_supervisor_pid() { :; }
do_stop() { kill -TERM "$OLD_PID"; echo stopped > "$PROJECT_ROOT/stopped"; return 1; }
do_restart
'''
        env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"], "OLD_PID": str(old.pid),
               "PREPARED": "fixture" if prepared else ""}
        if platform == "termux":
            env["PREFIX"] = "/data/data/com.termux/files/usr"
        try:
            result = subprocess.run(["bash", "-c", script, "fixture", str(start), str(tmp_path)],
                                    env=env, text=True, capture_output=True, timeout=10)
            assert result.returncode == expected, result.stdout + result.stderr
            assert (tmp_path / "stopped").exists() == stopped
            if not stopped:
                assert old.poll() is None
                assert health.read_bytes() == original
                assert "before stop" in result.stdout
        finally:
            if old.poll() is None:
                old.terminate()
            old.wait(timeout=5)
