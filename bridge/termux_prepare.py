#!/usr/bin/env python3
"""Prepare and verify a NEW Termux runtime; never switch or restart a service."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time

if __package__:
    from .dependency_bootstrap import android_build_api
    from .runtime_readiness import PROBES
else:
    from dependency_bootstrap import android_build_api
    from runtime_readiness import PROBES

NATIVE = ("cryptography", "jiter", "pydantic-core", "rpds-py", "pyromark")
BUILD_TOOLS = ("setuptools", "packaging", "cffi", "pycparser")
MATURIN_VERSION = "1.14.1"
MIN_FREE_BYTES = 2 * 1024**3
# Cargo jobs do not constrain LLD's internal relocation-scanning threads.
# Keep the Android workaround job-local; never change the system compiler.
SERIAL_LINK_ARG = "-Wl,--threads=1"


class PreparationError(Exception):
    """Categorical, body-free failure suitable for receipts."""


class Cancelled(PreparationError):
    """Catchable CLI termination; SIGKILL remains outside any cleanup contract."""


def cancel(signum, frame):
    raise Cancelled("cancelled")


def private_write(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)


def fresh_workspace(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    # No resolve(): following a symlink would defeat the rejection below.
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or (info.st_mode & stat.S_IWOTH and not info.st_mode & stat.S_ISVTX):
            raise PreparationError("unsafe_workspace_parent")
    info = path.parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PreparationError("workspace_parent_must_be_owner_private")
    path.mkdir(mode=0o700)  # Atomic claim; existing successes/failures are never reused.
    path.chmod(0o700)
    return path


def lock_subset(path: Path, names: tuple[str, ...]) -> str:
    """Copy exact pins/hashes from canonical pip-compile locks, fail closed."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024**2:
        raise PreparationError("invalid_lock_file")
    logical = " ".join(line.strip() for line in path.read_text().splitlines()
                       if line.strip() and not line.lstrip().startswith("#"))
    # Backslash-newline continuations are now spaces. Match complete records,
    # rejecting directives, URLs, unpinned requirements and unhashed entries.
    logical = logical.replace("\\", " ")
    record = re.compile(r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([^\s]+)"
                        r"((?:\s+--hash=sha256:[0-9a-f]{64})+)(?=\s|$)")
    found = {}
    position = 0
    while position < len(logical):
        match = record.match(logical, position)
        if match is None:
            raise PreparationError("unsupported_lock_syntax")
        name = re.sub(r"[-_.]+", "-", match[1]).lower()
        if name in found:
            raise PreparationError("duplicate_lock_requirement")
        found[name] = match[0]
        position = match.end()
        while position < len(logical) and logical[position].isspace():
            position += 1
    if not set(names) <= found.keys():
        raise PreparationError("missing_locked_build_requirement")
    return "\n".join(found[name] for name in names) + "\n"


def build_environment(work: Path, api: int, jobs: int) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PIP_", "PYTHON", "CARGO_", "RUST", "CCC_DEPS_"))
           and key not in ("ANDROID_API_LEVEL", "VIRTUAL_ENV")}
    env.update(ANDROID_API_LEVEL=str(api), CARGO_BUILD_JOBS=str(jobs),
               RUSTFLAGS=f"-C link-arg={SERIAL_LINK_ARG}", LDFLAGS=SERIAL_LINK_ARG,
               CARGO_TARGET_DIR=str(work / "cargo-target"),
               PIP_CACHE_DIR=str(work / "pip-cache"), PIP_CONFIG_FILE=os.devnull,
               PIP_NO_INPUT="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
               PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(work / "tmp"),
               PATH=os.pathsep.join((str(work / "builder/bin"),
                                     str(Path(sys.base_prefix) / "bin"), "/system/bin")))
    return env


class Runner:
    def __init__(self, work: Path, env: dict[str, str], seconds: float):
        self.work, self.env = work, env
        self.started = time.monotonic()
        self.deadline = self.started + seconds
        self.stages: list[dict] = []

    def run(self, name: str, argv: list[str]) -> Path:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise PreparationError("budget_exhausted")
        started = time.monotonic()
        stage = {"id": name, "status": "error"}
        self.stages.append(stage)
        print(f"termux-prepare: {name}", file=sys.stderr, flush=True)
        log = self.work / f"{name}.log"
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            # Defer catchable cancellation across spawn so a signal cannot
            # land after fork but before we have the child handle to reap.
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
            child = None
            try:
                child = subprocess.Popen(argv, env=self.env, cwd=self.work,
                                         stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                                         start_new_session=True)
                try:
                    signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                    code = child.wait(timeout=max(0.001, self.deadline - time.monotonic()))
                    stage.update(status="pass" if code == 0 else "fail", exit_code=code)
                except (subprocess.TimeoutExpired, KeyboardInterrupt, Cancelled) as exc:
                    stage["status"] = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "cancelled"
            finally:
                if child is not None and stage["status"] != "pass":
                    # An exited leader can still have live same-group workers.
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        stage["duration_ms"] = round((time.monotonic() - started) * 1000)
        if stage["status"] != "pass":
            raise PreparationError(f"{name}_{stage['status']}")
        return log


def provenance(runner: Runner) -> dict:
    code = ("import hashlib,importlib.metadata as m,json,pathlib,maturin,sys; "
            "p=pathlib.Path(maturin.__file__); "
            f"assert m.version('maturin') == {MATURIN_VERSION!r}; "
            "assert p.is_relative_to(sys.base_prefix); "
            "print(json.dumps({'maturin_version':m.version('maturin'),"
            "'module_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),"
            "'python':sys.version.split()[0]}))")
    log = runner.run("backend-provenance", [sys.executable, "-I", "-B", "-c", code])
    result = json.loads(log.read_text())
    for name in ("maturin", "rustc", "clang", "ld.lld", "patchelf"):
        binary = Path(sys.base_prefix) / "bin" / name
        output = runner.run(f"tool-{name}", [str(binary), "--version"])
        version = output.read_text().splitlines()[0]
        if name == "maturin" and version != f"maturin {MATURIN_VERSION}":
            raise PreparationError("maturin_cli_version_mismatch")
        result[name] = {"version": version, "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
    return result


def prepare(runner: Runner, source: Path, report: dict, reinstall: bool) -> None:
    work = runner.work
    report["toolchain"] = provenance(runner)
    report["lock_sha256"] = {}
    for name, path, subset in (
        ("build-tools", source.parent / ".github/requirements/bridge-ci.txt", BUILD_TOOLS),
        ("native", source / "requirements.lock.txt", NATIVE),
    ):
        private_write(work / f"{name}.lock.txt", lock_subset(path, subset))
        report["lock_sha256"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    report["source_sha256"] = {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                               for name in ("termux_prepare.py", "dependency_bootstrap.py",
                                            "runtime_readiness.py", "requirements.txt", "pyproject.toml")}
    base = [sys.executable, "-I", "-B", "-m", "venv"]
    runner.run("builder-venv", [*base, "--system-site-packages", str(work / "builder")])
    builder = str(work / "builder/bin/python")
    pip = [builder, "-I", "-B", "-m", "pip"]
    runner.run("build-tools", [*pip, "install", "--require-hashes", "-r", str(work / "build-tools.lock.txt")])
    # pip validates pyproject build requirements against the prepared builder;
    # a future minimum maturin bump fails rather than silently ignoring it.
    runner.run("native-wheels", [*pip, "wheel", "--no-build-isolation", "--check-build-dependencies",
                               "--no-deps", "--require-hashes", "-r", str(work / "native.lock.txt"),
                               "--wheel-dir", str(work / "wheelhouse")])
    report["wheels"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted((work / "wheelhouse").glob("*.whl"))}
    runner.run("runtime-venv", [*base, str(work / "runtime")])
    runtime = str(work / "runtime/bin/python")
    bootstrap = [runtime, "-B", str(source / "dependency_bootstrap.py"),
                 "--bridge-dir", str(source), "--venv-dir", str(work / "runtime"),
                 "--project-env", str(work / "absent.env"), "--process-unlocked", "0"]
    report["scenarios"]["fresh_install"] = "in_progress"
    runner.run("fresh-install", bootstrap)
    verify_runtime(runner, runtime, "fresh-readiness")
    report["scenarios"]["fresh_install"] = "pass"
    if reinstall:
        report["scenarios"]["reinstall"] = "in_progress"
        runner.run("force-reinstall", [runtime, "-I", "-B", "-m", "pip", "install",
                                      "--force-reinstall", "--require-hashes", "-r",
                                      str(source / "requirements.lock.txt")])
        runner.run("reinstall-reconcile", bootstrap)
        verify_runtime(runner, runtime, "reinstall-readiness")
        report["scenarios"]["reinstall"] = "pass"


def verify_runtime(runner: Runner, runtime: str, name: str) -> None:
    # Reuse canonical probes directly: nesting the observer CLI would create
    # grandchild sessions outside this driver's cancellation/process group.
    runner.run(f"{name}-identity", [runtime, "-I", "-B", "-c",
               "import json; from telegram_bot.runtime_readiness import runtime_identity; "
               "print(json.dumps(runtime_identity(), sort_keys=True))"])
    for probe_name, args in PROBES:
        runner.run(f"{name}-{probe_name}", [runtime, "-I", "-B", *args])
    runner.run(f"{name}-all-native", [runtime, "-I", "-B", "-c",
               "import cryptography.hazmat.bindings._rust,jiter,pydantic_core,rpds,pyromark,claude_agent_sdk"])


def timeout_value(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not 1 <= number <= 7200:
        raise argparse.ArgumentTypeError("timeout must be finite, between 1 and 7200 seconds")
    return number


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True, help="new directory under an owner-private parent")
    parser.add_argument("--timeout-seconds", type=timeout_value, default=3600)
    parser.add_argument("--jobs", type=int, choices=(1, 2), default=1)
    parser.add_argument("--verify-reinstall", action="store_true")
    args = parser.parse_args(argv)
    report = {"schema": "ccc.termux-preparation.v1", "status": "error", "stages": [],
              "scenarios": {name: "not_run" for name in
                            ("fresh_install", "reinstall", "rollback", "service_restart", "promotion")}}
    runner = None
    previous_handler = signal.signal(signal.SIGTERM, cancel)
    try:
        api = android_build_api()
        if os.environ.get("ANDROID_API_LEVEL", str(api)) != str(api):
            raise PreparationError("android_api_override_mismatch")
        if shutil.disk_usage(args.work_dir.parent).free < MIN_FREE_BYTES:
            raise PreparationError("insufficient_free_disk")
        work = fresh_workspace(args.work_dir)
        for name in ("tmp", "pip-cache", "cargo-target", "wheelhouse"):
            (work / name).mkdir(mode=0o700)
        runner = Runner(work, build_environment(work, api, args.jobs), args.timeout_seconds)
        report.update(android_api=api, jobs=args.jobs, linker_threads=1, work_dir=str(work),
                      cache_scope="new_private_pip_and_cargo_target; shared_default_cargo_registry",
                      stages=runner.stages)
        prepare(runner, Path(__file__).resolve().parent, report, args.verify_reinstall)
        report["status"] = "ready"
    except PreparationError as exc:
        report["reason"] = str(exc)
    except KeyboardInterrupt:
        report["reason"] = "cancelled"
    except (OSError, ValueError, subprocess.SubprocessError):
        report["reason"] = "preflight_or_io_error"
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    for scenario, status in report["scenarios"].items():
        if status == "in_progress":
            report["scenarios"][scenario] = "fail"
    if runner:
        report["duration_ms"] = round((time.monotonic() - runner.started) * 1000)
        try:
            private_write(runner.work / "receipt.json", json.dumps(report, indent=2) + "\n")
        except OSError:
            report.update(status="error", reason="receipt_write_failed")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
