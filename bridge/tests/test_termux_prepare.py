"""Preparation stays private/bounded and retains pip's original hash gates."""
import json
import os
from pathlib import Path
import sys
import time

import pytest

from telegram_bot import termux_prepare as prep


def pin(name, version="1"):
    return f"{name}=={version} \\\n    --hash=sha256:{'a' * 64}\n"


def test_repository_lock_subsets_include_every_native_backend():
    source = Path(prep.__file__).resolve().parent
    native = prep.lock_subset(source / "requirements.lock.txt", prep.NATIVE)
    for name in prep.NATIVE:
        assert f"{name}==" in native
    assert "pyromark==" in native  # Missing this triggered a second maturin build.
    assert "maturin==" not in native
    tools = prep.lock_subset(source.parent / ".github/requirements/bridge-ci.txt", prep.BUILD_TOOLS)
    assert all(f"{name}==" in tools for name in prep.BUILD_TOOLS)


@pytest.mark.parametrize("content", [
    "thing>=1", "thing==1", "-r other.txt", "--index-url https://invalid.example",
    pin("thing") + pin("thing"), pin("thing") + "--extra-index-url https://invalid.example",
    pin("thing").replace("sha256:", "sha1:"), pin("other"),
    pin("thing").replace("a" * 64, "a" * 63),
])
def test_malformed_or_unhashed_locks_fail_closed(tmp_path, content):
    path = tmp_path / "lock"
    path.write_text(content)
    with pytest.raises(prep.PreparationError):
        prep.lock_subset(path, ("thing",))


def test_lock_symlink_and_oversize_rejected(tmp_path):
    target = tmp_path / "target"
    target.write_text(pin("thing"))
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(prep.PreparationError):
        prep.lock_subset(link, ("thing",))
    target.write_bytes(b"x" * (4 * 1024**2 + 1))
    with pytest.raises(prep.PreparationError):
        prep.lock_subset(target, ("thing",))


def test_private_atomic_workspace_and_receipt_under_permissive_umask(tmp_path):
    tmp_path.chmod(0o700)
    old = os.umask(0o022)
    try:
        work = prep.fresh_workspace(tmp_path / "new")
        prep.private_write(work / "receipt", "preserved")
    finally:
        os.umask(old)
    assert work.stat().st_mode & 0o777 == 0o700
    assert (work / "receipt").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        prep.fresh_workspace(work)
    with pytest.raises(FileExistsError):
        prep.private_write(work / "receipt", "overwrite")
    assert (work / "receipt").read_text() == "preserved"


def test_workspace_rejects_symlink_ancestors_and_public_parent(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(prep.PreparationError):
        prep.fresh_workspace(link / "run")
    actual.chmod(0o755)
    with pytest.raises(prep.PreparationError):
        prep.fresh_workspace(actual / "run")
    assert not (actual / "run").exists()


def test_child_environment_removes_install_and_build_overrides(tmp_path, monkeypatch):
    for key in ("PIP_NO_DEPS", "PIP_NO_VERIFY", "PIP_INDEX_URL", "PYTHONPATH", "RUSTFLAGS", "CARGO_TARGET_DIR", "CCC_DEPS_SMOKE_STRICT"):
        monkeypatch.setenv(key, "PRIVATE")
    env = prep.build_environment(tmp_path, 24, 1)
    assert "PRIVATE" not in json.dumps(env)
    assert env["ANDROID_API_LEVEL"] == "24"
    assert env["CARGO_BUILD_JOBS"] == "1"
    assert env["PIP_CONFIG_FILE"] == os.devnull
    assert env["PIP_CACHE_DIR"] == str(tmp_path / "pip-cache")


def test_runner_preserves_failure_without_echoing_child_body(tmp_path, capsys):
    runner = prep.Runner(tmp_path, dict(os.environ), 3)
    with pytest.raises(prep.PreparationError, match="fixture_fail"):
        runner.run("fixture", [sys.executable, "-c", "print('PRIVATE'); raise SystemExit(9)"])
    assert runner.stages[0]["exit_code"] == 9
    assert "PRIVATE" not in json.dumps(runner.stages)
    assert "PRIVATE" not in str(capsys.readouterr())
    assert "PRIVATE" in (tmp_path / "fixture.log").read_text()
    assert (tmp_path / "fixture.log").stat().st_mode & 0o777 == 0o600


def test_deadline_kills_descendant_and_prevents_next_spawn(tmp_path):
    runner = prep.Runner(tmp_path, dict(os.environ), 0.3)
    marker = tmp_path / "pid"
    code = ("import subprocess,time; from pathlib import Path; "
            "p=subprocess.Popen(['sleep','30']); "
            f"Path({str(marker)!r}).write_text(str(p.pid)); time.sleep(30)")
    with pytest.raises(prep.PreparationError, match="slow_timeout"):
        runner.run("slow", [sys.executable, "-c", code])
    with pytest.raises(prep.PreparationError, match="budget_exhausted"):
        runner.run("never", [sys.executable, "-c", "raise SystemExit(0)"])
    assert len(runner.stages) == 1
    proc = Path(f"/proc/{int(marker.read_text())}/stat")
    for _ in range(100):
        try:
            if proc.read_text().split()[2] == "Z":
                return
        except (FileNotFoundError, ProcessLookupError):
            return
        time.sleep(0.01)
    pytest.fail("child remained runnable")


@pytest.mark.parametrize("value", ["nan", "inf", "0", "7201", "-1"])
def test_invalid_deadlines(value):
    with pytest.raises(Exception):
        prep.timeout_value(value)


def test_no_workspace_on_api_mismatch(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(prep, "android_build_api", lambda: 24)
    monkeypatch.setenv("ANDROID_API_LEVEL", "33")
    target = tmp_path / "run"
    assert prep.main(["--work-dir", str(target)]) == 1
    assert not target.exists()
    assert json.loads(capsys.readouterr().out)["reason"] == "android_api_override_mismatch"


def test_no_workspace_on_low_disk(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(prep, "android_build_api", lambda: 24)
    monkeypatch.delenv("ANDROID_API_LEVEL", raising=False)
    monkeypatch.setattr(prep.shutil, "disk_usage", lambda p: type("Disk", (), {"free": 1})())
    assert prep.main(["--work-dir", str(tmp_path / "run")]) == 1
    assert not (tmp_path / "run").exists()
    assert json.loads(capsys.readouterr().out)["reason"] == "insufficient_free_disk"


def test_preparation_keeps_hash_and_backend_checks_and_real_reinstall(tmp_path, monkeypatch):
    source = Path(prep.__file__).resolve().parent
    monkeypatch.setattr(prep, "provenance", lambda runner: {"fixture": True})
    (tmp_path / "wheelhouse").mkdir()
    runner = prep.Runner(tmp_path, {}, 30)
    calls = []
    monkeypatch.setattr(runner, "run", lambda name, argv: calls.append((name, argv)))
    report = {"scenarios": {"fresh_install": "not_run", "reinstall": "not_run", "promotion": "not_run"}}
    prep.prepare(runner, source, report, True)
    commands = dict(calls)
    assert "--check-build-dependencies" in commands["native-wheels"]
    assert "--no-build-isolation" in commands["native-wheels"]
    assert "--require-hashes" in commands["native-wheels"]
    assert "--require-hashes" in commands["build-tools"]
    assert "--force-reinstall" in commands["force-reinstall"]
    assert "--require-hashes" in commands["force-reinstall"]
    assert commands["fresh-install"][-2:] == ["--process-unlocked", "0"]
    assert "--system-site-packages" not in commands["runtime-venv"]
    assert report["scenarios"] == {"fresh_install": "pass", "reinstall": "pass", "promotion": "not_run"}
    assert list(commands).index("native-wheels") < list(commands).index("fresh-install")


def test_failed_install_is_recorded_and_preserved(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(prep, "android_build_api", lambda: 24)
    monkeypatch.delenv("ANDROID_API_LEVEL", raising=False)
    def fail(runner, source, report, reinstall):
        report["scenarios"]["fresh_install"] = "in_progress"
        raise prep.PreparationError("fresh-install_fail")
    monkeypatch.setattr(prep, "prepare", fail)
    tmp_path.chmod(0o700)
    work = tmp_path / "failed"
    assert prep.main(["--work-dir", str(work)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["scenarios"]["fresh_install"] == "fail"
    assert report["scenarios"]["promotion"] == "not_run"
    assert json.loads((work / "receipt.json").read_text()) == report


@pytest.mark.parametrize("sig", [2, 15])
def test_cli_cancellation_kills_probe_and_keeps_receipt(tmp_path, sig):
    import signal
    import subprocess

    tmp_path.chmod(0o700)
    work = tmp_path / "job"
    marker = tmp_path / "probe.pid"
    source = Path(prep.__file__).resolve()
    # Run the real CLI lifecycle with a controlled readiness probe. This uses
    # verify_runtime's actual Runner path, including its process group owner.
    code = f'''
import importlib.util, sys
sys.path.insert(0, {str(source.parent)!r})
spec = importlib.util.spec_from_file_location("preparation_fixture", {str(source)!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.android_build_api = lambda: 24
m.PROBES = (("blocked", ("-c", "import os,time; from pathlib import Path; Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)")),)
def prepare(runner, source, report, reinstall):
    report["scenarios"]["fresh_install"] = "in_progress"
    original = runner.run
    def run(name, argv):
        if name.endswith("-identity"):
            return
        return original(name, argv)
    runner.run = run
    m.verify_runtime(runner, sys.executable, "fresh-readiness")
m.prepare = prepare
raise SystemExit(m.main(["--work-dir", {str(work)!r}]))
'''
    env = dict(os.environ)
    env.pop("ANDROID_API_LEVEL", None)
    with subprocess.Popen([sys.executable, "-c", code], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as driver:
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                if driver.poll() is not None:
                    pytest.fail(driver.communicate()[1])
                time.sleep(0.02)
            assert marker.exists()
            driver.send_signal(sig)
            stdout, _ = driver.communicate(timeout=5)
        finally:
            if driver.poll() is None:
                os.killpg(driver.pid, signal.SIGKILL)
                driver.wait()
    report = json.loads(stdout)
    assert report["status"] == "error"
    assert report["stages"][-1]["status"] == "cancelled"
    assert report["scenarios"]["fresh_install"] == "fail"
    assert json.loads((work / "receipt.json").read_text()) == report
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)  # The direct probe was reaped.
