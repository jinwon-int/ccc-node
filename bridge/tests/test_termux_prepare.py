"""Preparation stays private/bounded and retains pip's original hash gates."""
import json
import os
from pathlib import Path
import sys
import time

import pytest

from telegram_bot import termux_prepare as prep


@pytest.fixture(autouse=True)
def _base_interpreter(monkeypatch, request):
    # The suite may run inside a venv; only the guard test exercises the check.
    if "base_interpreter_guard" not in request.node.name:
        monkeypatch.setattr(prep, "require_base_interpreter", lambda: None)


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
    for key in ("LDFLAGS", "CARGO_ENCODED_RUSTFLAGS", "PIP_NO_DEPS", "PIP_NO_VERIFY", "PIP_INDEX_URL", "PYTHONPATH", "RUSTFLAGS", "CARGO_TARGET_DIR", "CCC_DEPS_SMOKE_STRICT"):
        monkeypatch.setenv(key, "PRIVATE")
    env = prep.build_environment(tmp_path, 24, 1)
    assert "PRIVATE" not in json.dumps(env)
    assert env["ANDROID_API_LEVEL"] == "24"
    assert env["CARGO_BUILD_JOBS"] == "1"
    assert env["RUSTFLAGS"] == "-C link-arg=-Wl,--threads=1"
    assert env["LDFLAGS"] == "-Wl,--threads=1"
    assert "CARGO_ENCODED_RUSTFLAGS" not in env
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
m.require_base_interpreter = lambda: None
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


def test_cancellation_kills_workers_after_group_leader_exits(tmp_path):
    import signal
    import subprocess

    marker = tmp_path / "pids"
    source = Path(prep.__file__).resolve().parent
    probe = ("import os,subprocess,sys,time; from pathlib import Path; "
             "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
             f"Path({str(marker)!r}).write_text(str(os.getpid())+' '+str(p.pid)); time.sleep(0.7)")
    code = (f"import sys,signal; sys.path.insert(0,{str(source)!r}); import termux_prepare as m; "
            f"r=m.Runner(m.Path({str(tmp_path)!r}),dict(m.os.environ),10); "
            f"r.run('fixture',[sys.executable,'-c',{probe!r}])")
    with subprocess.Popen([sys.executable, "-c", code], start_new_session=True,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as driver:
        leader = worker = None
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists()
            leader, worker = map(int, marker.read_text().split())
            os.kill(driver.pid, signal.SIGSTOP)
            time.sleep(0.9)  # The leader exits while the driver cannot reap it.
            os.kill(driver.pid, signal.SIGINT)
            os.kill(driver.pid, signal.SIGCONT)
            assert driver.wait(timeout=5) != 0
            proc = Path(f"/proc/{worker}/stat")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    if proc.read_text().split()[2] == "Z":
                        return
                except (FileNotFoundError, ProcessLookupError):
                    return
                time.sleep(0.01)
            pytest.fail("worker survived cancellation after leader exit")
        finally:
            for group in (driver.pid, leader):
                if group is not None:
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            driver.wait()


# --- #2175: same-node native wheel reuse and base-interpreter guard ---------

WHEELS = {
    "cryptography-50.0.1-cp314-abi3-android_24_arm64_v8a.whl": b"crypto",
    "jiter-0.16.0-cp314-cp314-android_24_arm64_v8a.whl": b"jiter",
    "pydantic_core-2.46.5-cp314-cp314-android_24_arm64_v8a.whl": b"pydantic",
    "pyromark-0.9.13-cp314-cp314-android_24_arm64_v8a.whl": b"pyromark",
    "rpds_py-2026.6.3-cp314-cp314-android_24_arm64_v8a.whl": b"rpds",
}
INPUTS = {"android_api": 24, "linker_threads": 1, "lock_sha256": {"native": "n", "build-tools": "b"},
          "toolchain": {"rustc": {"binary_sha256": "r"}, "python": "3.14.6"}}


def sha(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def prior_job(root, **overrides):
    root.chmod(0o700)
    job = root / "prior"
    job.mkdir(mode=0o700)
    for name in ("wheelhouse", "pip-cache"):
        (job / name).mkdir(mode=0o700)
    (job / "pip-cache/wheels").mkdir(mode=0o700)
    for index, (name, data) in enumerate(WHEELS.items()):
        entry = job / "pip-cache/wheels" / f"{index:02x}" / "key"
        entry.mkdir(parents=True, mode=0o700)
        (entry / name).write_bytes(data)
        (entry / "origin.json").write_text('{"archive_info": {}}')
        (job / "wheelhouse" / name).write_bytes(data)
    extra = job / "pip-cache/wheels/ff/key"
    extra.mkdir(parents=True, mode=0o700)
    (extra / "cffi-2.1.1-cp314-cp314-android_24_arm64_v8a.whl").write_bytes(b"cffi")
    receipt = {"schema": "ccc.termux-preparation.v1", "status": "ready", "work_dir": str(job),
               "scenarios": {"fresh_install": "pass"}, **INPUTS,
               "wheels": {name: sha(data) for name, data in WHEELS.items()}}
    receipt.update(overrides)
    prep.private_write(job / "receipt.json", json.dumps(receipt))
    return job


def new_work(root):
    work = root / "new"
    work.mkdir(mode=0o700)
    for name in ("wheelhouse", "pip-cache", "tmp", "cargo-target"):
        (work / name).mkdir(mode=0o700)
    return work


def test_base_interpreter_guard_refuses_venv_before_workspace(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(prep.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(prep.sys, "base_prefix", str(tmp_path / "base"))
    assert prep.main(["--work-dir", str(tmp_path / "run")]) == 1
    assert not (tmp_path / "run").exists()
    assert json.loads(capsys.readouterr().out)["reason"] == "must_run_with_base_interpreter"


def test_base_interpreter_guard_accepts_base_prefix(monkeypatch):
    monkeypatch.setattr(prep.sys, "prefix", prep.sys.base_prefix)
    prep.require_base_interpreter()


def test_reuse_copies_private_cache_and_records_source(tmp_path):
    job = prior_job(tmp_path)
    receipt = prep.load_reuse_source(job)
    work = new_work(tmp_path)
    report = dict(INPUTS)
    prep.reuse_native_wheels(work, receipt, report)
    assert report["native_wheels"]["mode"] == "reused"
    assert report["native_wheels"]["from"] == str(job)
    assert report["native_wheels"]["receipt_sha256"] == sha((job / "receipt.json").read_bytes())
    cache = prep.native_cache_wheels(work / "pip-cache/wheels")
    assert cache == {name: [sha(data)] for name, data in WHEELS.items()}
    assert (work / "pip-cache/wheels/ff/key/cffi-2.1.1-cp314-cp314-android_24_arm64_v8a.whl").exists()
    assert (work / "pip-cache/wheels/00/key/origin.json").exists()
    for path in (work / "pip-cache/wheels").rglob("*"):
        assert path.stat().st_mode & 0o077 == 0
    assert sorted(p.name for p in (work / "wheelhouse").iterdir()) == sorted(WHEELS)


@pytest.mark.parametrize("field,value", [
    ("android_api", 33), ("linker_threads", 2),
    ("lock_sha256", {"native": "changed", "build-tools": "b"}),
    ("toolchain", {"rustc": {"binary_sha256": "other-node"}, "python": "3.14.6"}),
])
def test_reuse_refuses_any_build_input_mismatch(tmp_path, field, value):
    receipt = prep.load_reuse_source(prior_job(tmp_path))
    work = new_work(tmp_path)
    report = {**INPUTS, field: value}
    with pytest.raises(prep.PreparationError, match=f"reuse_{field}_mismatch"):
        prep.reuse_native_wheels(work, receipt, report)
    assert not (work / "pip-cache/wheels").exists()


@pytest.mark.parametrize("overrides,reason", [
    ({"status": "error"}, "reuse_receipt_not_ready"),
    ({"scenarios": {"fresh_install": "fail"}}, "reuse_receipt_not_ready"),
    ({"schema": "other"}, "reuse_receipt_not_ready"),
    ({"work_dir": "/elsewhere/job"}, "reuse_receipt_work_dir_mismatch"),
    ({"wheels": {"cryptography-1-x.whl": "a" * 64}}, "reuse_receipt_wheels_invalid"),
    ({"wheels": {**{n: sha(d) for n, d in WHEELS.items()}, "extra-1-x.whl": "a" * 64}},
     "reuse_receipt_wheels_invalid"),
    ({"wheels": {n: "short" for n in WHEELS}}, "reuse_receipt_wheels_invalid"),
])
def test_reuse_source_receipt_must_be_ready_and_complete(tmp_path, overrides, reason):
    job = prior_job(tmp_path, **overrides)
    with pytest.raises(prep.PreparationError, match=reason):
        prep.load_reuse_source(job)


def test_reuse_source_rejects_symlink_and_shared_permissions(tmp_path):
    job = prior_job(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(job, target_is_directory=True)
    with pytest.raises(prep.PreparationError, match="reuse_source_must_be_owner_private_dir"):
        prep.load_reuse_source(link)
    (job / "receipt.json").chmod(0o644)
    with pytest.raises(prep.PreparationError, match="reuse_receipt_invalid"):
        prep.load_reuse_source(job)
    (job / "receipt.json").chmod(0o600)
    job.chmod(0o750)
    with pytest.raises(prep.PreparationError, match="reuse_source_must_be_owner_private_dir"):
        prep.load_reuse_source(job)


def test_reuse_refuses_tampered_or_symlinked_cache(tmp_path):
    job = prior_job(tmp_path)
    receipt = prep.load_reuse_source(job)
    victim = next((job / "pip-cache/wheels").rglob("jiter-*.whl"))
    victim.write_bytes(b"tampered")
    with pytest.raises(prep.PreparationError, match="reuse_wheel_hash_mismatch"):
        prep.reuse_native_wheels(new_work(tmp_path), receipt, dict(INPUTS))

    other = tmp_path / "other"
    other.mkdir()
    job2 = prior_job(other)
    receipt2 = prep.load_reuse_source(job2)
    (job2 / "pip-cache/wheels/evil").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(prep.PreparationError, match="reuse_cache_entry_invalid"):
        prep.reuse_native_wheels(new_work(other), receipt2, dict(INPUTS))


def test_prepare_with_reuse_skips_build_and_detects_rebuild(tmp_path, monkeypatch):
    source = Path(prep.__file__).resolve().parent
    receipt = prep.load_reuse_source(prior_job(tmp_path))
    work = new_work(tmp_path)
    monkeypatch.setattr(prep, "provenance", lambda runner: INPUTS["toolchain"])
    real_lock = prep.hashlib.sha256((source / "requirements.lock.txt").read_bytes()).hexdigest()
    real_tools = prep.hashlib.sha256((source.parent / ".github/requirements/bridge-ci.txt").read_bytes()).hexdigest()
    receipt["lock_sha256"] = {"build-tools": real_tools, "native": real_lock}
    runner = prep.Runner(work, {}, 30)
    calls = []
    monkeypatch.setattr(runner, "run", lambda name, argv: calls.append(name))
    report = {"android_api": 24, "linker_threads": 1,
              "scenarios": {"fresh_install": "not_run", "reinstall": "not_run"}}
    prep.prepare(runner, source, report, True, reuse=receipt)
    assert not {"builder-venv", "build-tools", "native-wheels"} & set(calls)
    assert "fresh-install" in calls and "force-reinstall" in calls
    assert runner.stages[-1]["id"] == "native-wheels-reuse" and runner.stages[-1]["status"] == "pass"
    assert report["wheels"] == receipt["wheels"]
    assert report["scenarios"] == {"fresh_install": "pass", "reinstall": "pass"}

    # A bootstrap that ignored the seeded cache would add a freshly built wheel.
    work2 = tmp_path / "new2"
    work2.mkdir(mode=0o700)
    for name in ("wheelhouse", "pip-cache"):
        (work2 / name).mkdir(mode=0o700)
    runner2 = prep.Runner(work2, {}, 30)

    def run(name, argv):
        if name == "fresh-install":
            rebuilt = work2 / "pip-cache/wheels/zz/key"
            rebuilt.mkdir(parents=True)
            (rebuilt / next(iter(WHEELS))).write_bytes(b"rebuilt")
    monkeypatch.setattr(runner2, "run", run)
    report2 = {"android_api": 24, "linker_threads": 1, "scenarios": {"fresh_install": "not_run"}}
    with pytest.raises(prep.PreparationError, match="reused_wheels_not_used"):
        prep.prepare(runner2, source, report2, False, reuse=receipt)


def test_invalid_reuse_source_claims_no_workspace(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(prep, "android_build_api", lambda: 24)
    monkeypatch.delenv("ANDROID_API_LEVEL", raising=False)
    tmp_path.chmod(0o700)
    target = tmp_path / "run"
    assert prep.main(["--work-dir", str(target), "--reuse-wheels-from", str(tmp_path / "missing")]) == 1
    assert not target.exists()
    assert json.loads(capsys.readouterr().out)["reason"] == "preflight_or_io_error"


# --- #2175 B: optional frontend extras constrained to the core freeze -------

class FakeRunner:
    """Records stages; freeze output is scripted per call, other stages pass."""

    def __init__(self, work, freezes):
        self.work, self.freezes, self.calls, self.stages = work, list(freezes), [], []
        self.env, self.envs = {"BASE": "1"}, {}

    def run(self, name, argv):
        self.calls.append((name, argv))
        self.envs[name] = dict(self.env)
        log = self.work / f"{name}.log"
        log.write_text(self.freezes.pop(0) if name.startswith("extra-") and "-freeze" in name else "")
        return log


CORE = "aiohappyeyeballs==2.6.1\ncryptography==50.0.1\nh11==0.16.0\n"
WITH_MATRIX = CORE + "matrix-nio==0.25.2\npython-olm==3.2.16\naiohttp==3.14.4\n"


@pytest.fixture
def fake_olm(monkeypatch):
    def build(runner, pip):
        runner.olm_calls = getattr(runner, "olm_calls", 0) + 1
        return {"fixture": True}
    monkeypatch.setattr(prep, "build_termux_olm", build)


def test_extra_matrix_is_constrained_to_core_freeze_and_rechecked(tmp_path, fake_olm):
    source = Path(prep.__file__).resolve().parent
    runner = FakeRunner(tmp_path, [CORE, WITH_MATRIX])
    report = {}
    prep.install_extras(runner, source, "/runtime/python", ["matrix"], report)
    names = [name for name, _ in runner.calls]
    assert names[:5] == ["extra-matrix-freeze", "extra-matrix-install", "extra-matrix-freeze-after",
                         "extra-matrix-imports", "extra-matrix-pip-check"]
    assert "extras-readiness-all-native" in names  # core readiness repeated afterwards
    install = dict(runner.calls)["extra-matrix-install"]
    constraints = tmp_path / "extra-matrix-constraints.txt"
    assert install[install.index("-c") + 1] == str(constraints)
    assert install[install.index("-r") + 1] == str(source / "requirements-matrix.txt")
    assert "--require-virtualenv" in install
    assert constraints.read_text().splitlines() == sorted(CORE.splitlines())
    assert constraints.stat().st_mode & 0o777 == 0o600
    assert "nio.crypto.ENCRYPTION_ENABLED" in dict(runner.calls)["extra-matrix-imports"][-1]
    result = report["extras"]["matrix"]
    assert result["status"] == "pass"
    assert result["added"] == {"aiohttp": "aiohttp==3.14.4", "matrix-nio": "matrix-nio==0.25.2",
                               "python-olm": "python-olm==3.2.16"}
    assert len(result["requirements_sha256"]) == 64
    assert result["olm"] == {"fixture": True}
    assert runner.olm_calls == 1


def test_extra_that_moves_a_core_pin_is_refused(tmp_path, fake_olm):
    source = Path(prep.__file__).resolve().parent
    moved = CORE.replace("h11==0.16.0", "h11==0.14.0") + "matrix-nio==0.25.2\n"
    runner = FakeRunner(tmp_path, [CORE, moved])
    report = {}
    with pytest.raises(prep.PreparationError, match="extra_matrix_changed_core"):
        prep.install_extras(runner, source, "/runtime/python", ["matrix"], report)
    assert report["extras"]["matrix"]["status"] == "in_progress"  # main() turns this into "fail"
    assert "extra-matrix-imports" not in [name for name, _ in runner.calls]


def test_extra_requirements_symlink_refused(tmp_path):
    source = tmp_path / "bridge"
    source.mkdir()
    (tmp_path / "real.txt").write_text("matrix-nio[e2e]==0.25.2\n")
    (source / "requirements-matrix.txt").symlink_to(tmp_path / "real.txt")
    with pytest.raises(prep.PreparationError, match="extra_matrix_requirements_invalid"):
        prep.install_extras(FakeRunner(tmp_path, []), source, "/runtime/python", ["matrix"], {})


def test_prepare_runs_extras_after_reinstall_and_main_marks_failure(tmp_path, monkeypatch, capsys):
    source = Path(prep.__file__).resolve().parent
    monkeypatch.setattr(prep, "provenance", lambda runner: {"fixture": True})
    (tmp_path / "wheelhouse").mkdir()
    order = []
    monkeypatch.setattr(prep, "install_extras",
                        lambda runner, src, runtime, extras, report: order.append(("extras", extras)))
    runner = prep.Runner(tmp_path, {}, 30)
    monkeypatch.setattr(runner, "run", lambda name, argv: order.append(name))
    report = {"scenarios": {"fresh_install": "not_run", "reinstall": "not_run"}}
    prep.prepare(runner, source, report, True, extras=["matrix"])
    assert order[-1] == ("extras", ["matrix"])
    assert order.index("reinstall-readiness-all-native") < len(order) - 1

    monkeypatch.setattr(prep, "android_build_api", lambda: 24)
    monkeypatch.delenv("ANDROID_API_LEVEL", raising=False)
    seen = {}

    def failing(runner, src, report, reinstall, **options):
        seen.update(options)
        report.setdefault("extras", {})["matrix"] = {"status": "in_progress"}
        raise prep.PreparationError("extra-matrix-install_fail")
    monkeypatch.setattr(prep, "prepare", failing)
    tmp_path.chmod(0o700)
    assert prep.main(["--work-dir", str(tmp_path / "job"), "--extra", "matrix", "--extra", "matrix"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert seen == {"extras": ["matrix"]}
    assert out["extras"]["matrix"]["status"] == "fail"
    assert out["reason"] == "extra-matrix-install_fail"


def test_unknown_extra_rejected_by_cli():
    with pytest.raises(SystemExit):
        prep.main(["--work-dir", "/nonexistent/x", "--extra", "voice"])


OLM_SCRIPT = """import os
import subprocess

from cffi import FFI

compile_args = ["-Ilibolm/include"]

""" + prep.OLM_BUNDLED_BUILD + """
ffibuilder.set_source(
    "_libolm",
    libraries=["olm"],
    library_dirs=[os.path.join("libolm", "build")],
    extra_compile_args=compile_args,
)
"""


def test_olm_build_script_points_at_system_libolm(tmp_path):
    patched = prep.patch_olm_build(OLM_SCRIPT, Path("/prefix"))
    assert 'compile_args = [\'-I/prefix/include\']' in patched
    assert "library_dirs=['/prefix/lib']" in patched
    assert "cmake" not in patched and '"make", "static"' not in patched
    assert 'libraries=["olm"]' in patched  # still links libolm, now the system one


@pytest.mark.parametrize("drop", ['compile_args = ["-Ilibolm/include"]', "cmake", 'os.path.join("libolm", "build")'])
def test_olm_build_script_drift_fails_closed(drop):
    text = OLM_SCRIPT.replace(drop, "changed", 1)
    with pytest.raises(prep.PreparationError, match="extra_matrix_olm_build_script_unexpected"):
        prep.patch_olm_build(text, Path("/prefix"))


def test_build_termux_olm_requires_system_libolm(tmp_path, monkeypatch):
    monkeypatch.setattr(prep.sys, "base_prefix", str(tmp_path / "noprefix"))
    with pytest.raises(prep.PreparationError, match="extra_matrix_system_libolm_missing"):
        prep.build_termux_olm(FakeRunner(tmp_path, []), ["pip"])


def test_build_termux_olm_pins_sdist_and_checks_build_script(tmp_path, monkeypatch):
    import io
    import tarfile
    prefix = tmp_path / "prefix"
    (prefix / "lib").mkdir(parents=True)
    (prefix / "include/olm").mkdir(parents=True)
    (prefix / "lib/libolm.so").write_bytes(b"lib")
    (prefix / "include/olm/olm.h").write_text("")
    monkeypatch.setattr(prep.sys, "base_prefix", str(prefix))
    work = tmp_path / "work"
    work.mkdir()

    class OlmRunner(FakeRunner):
        def run(self, name, argv):
            log = super().run(name, argv)
            if name == "extra-matrix-olm-download":
                data = OLM_SCRIPT.encode()
                with tarfile.open(argv[-2], "w:gz") as archive:
                    info = tarfile.TarInfo(prep.OLM_SDIST_DIR + "/olm_build.py")
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                monkeypatch.setattr(prep, "OLM_SDIST_SHA256", prep._sha256(Path(argv[-2])))
            if name == "extra-matrix-olm-wheel":
                out = Path(argv[argv.index("-w") + 1])
                out.mkdir()
                (out / "python_olm-3.2.16-cp314-cp314-android_24_arm64_v8a.whl").write_bytes(b"whl")
            return log

    # The fixture script is not upstream's, so its hash must be refused ...
    runner = OlmRunner(work, [])
    with pytest.raises(prep.PreparationError, match="extra_matrix_olm_build_script_unexpected"):
        prep.build_termux_olm(runner, ["pip"])
    download = dict(runner.calls)["extra-matrix-olm-download"]
    assert download[:4] == ["pip", "-I", "-B", "-c"]
    assert download[-3] == prep.OLM_SDIST_URL and download[-3].endswith("/python-olm-3.2.16.tar.gz")
    assert "extra-matrix-olm-wheel" not in dict(runner.calls)

    # ... and accepted once it is the pinned script.
    import shutil
    shutil.rmtree(work / "extra-matrix-olm")
    monkeypatch.setattr(prep, "OLM_BUILD_SCRIPT_SHA256", prep.hashlib.sha256(OLM_SCRIPT.encode()).hexdigest())
    runner = OlmRunner(work, [])
    result = prep.build_termux_olm(runner, ["pip"])
    names = [name for name, _ in runner.calls]
    assert names == ["extra-matrix-olm-download", "extra-matrix-olm-wheel", "extra-matrix-olm-install"]
    assert "library_dirs=['" + str(prefix / "lib") + "']" in (work / "extra-matrix-olm/src" / prep.OLM_SDIST_DIR / "olm_build.py").read_text()
    assert dict(runner.calls)["extra-matrix-olm-install"][-1].endswith(".whl")
    assert result["wheel"].startswith("python_olm-3.2.16") and len(result["system_libolm_sha256"]) == 64


def test_olm_sdist_pin_and_fetch_hash_check(tmp_path):
    assert prep.OLM_SDIST_SHA256 == "a1c47fce2505b7a16841e17694cbed4ed484519646ede96ee9e89545a49643c9"
    src = tmp_path / "sdist"
    src.write_bytes(b"payload")
    import subprocess
    good = subprocess.run([sys.executable, "-I", "-c", prep.OLM_FETCH, src.as_uri(), str(tmp_path / "ok"),
                           prep.hashlib.sha256(b"payload").hexdigest()])
    assert good.returncode == 0 and (tmp_path / "ok").read_bytes() == b"payload"
    bad = subprocess.run([sys.executable, "-I", "-c", prep.OLM_FETCH, src.as_uri(), str(tmp_path / "bad"), "0" * 64],
                         capture_output=True)
    assert bad.returncode != 0 and not (tmp_path / "bad").exists()
