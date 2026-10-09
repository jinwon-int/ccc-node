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
import tarfile
import time

if __package__:
    from .dependency_bootstrap import android_build_api
    from .runtime_readiness import PROBES
    from .prepared_runtime import source_seal
else:
    from dependency_bootstrap import android_build_api
    from runtime_readiness import PROBES
    from prepared_runtime import source_seal

NATIVE = ("cryptography", "jiter", "pydantic-core", "rpds-py", "pyromark")
BUILD_TOOLS = ("setuptools", "packaging", "cffi", "pycparser")
MATURIN_VERSION = "1.14.1"
MIN_FREE_BYTES = 2 * 1024**3
# Same-node wheel reuse (#2175): the prior receipt must match on every input
# that determines the native build output. Toolchain hashes pin the node's
# compiler/backend binaries, so another node's (different) wheels never match.
REUSE_MATCH_FIELDS = ("android_api", "linker_threads", "lock_sha256", "toolchain")
MAX_RECEIPT_BYTES = 1024**2
# Optional frontend extras (#2175 B). They stay outside the hash lock by design
# (see each requirements file), so they are installed *constrained to the core
# runtime's own freeze*: an extra may add packages but never move a core pin.
EXTRAS = {
    "matrix": {
        "requirements": "requirements-matrix.txt",
        "probe": ("import aiohttp, nio, nio.crypto, olm; "
                  "from nio import AsyncClient, AsyncClientConfig; "
                  "assert nio.crypto.ENCRYPTION_ENABLED, 'matrix-nio built without e2e'"),
        "termux_olm": True,
    },
}
# python-olm's sdist builds a bundled C++ libolm 3.2.16 that current Termux
# toolchains cannot compile (CMake >= 4 refuses its cmake_minimum_required, and
# clang 21 rejects include/olm/list.hh). Termux ships a patched libolm 3.2.16
# package, so the binding is built against that instead: hash-pinned sdist,
# exact-hash build script, three fixed edits (system include/lib, no bundled
# build). No crypto source is changed.
# Fetched directly: `pip download` of an sdist prepares its metadata, which runs
# the very olm_build.py (bundled CMake build) this patch exists to avoid.
OLM_SDIST_URL = ("https://files.pythonhosted.org/packages/b8/eb/23ca73cbdc8c7466a774e515dfd917d9"
                 "fbe747c1257059246fdc63093f04/python-olm-3.2.16.tar.gz")
OLM_SDIST_SHA256 = "a1c47fce2505b7a16841e17694cbed4ed484519646ede96ee9e89545a49643c9"
OLM_FETCH = ("import hashlib,sys,urllib.request; url,dest,want=sys.argv[1:4]; "
             "data=urllib.request.urlopen(url, timeout=120).read(); "
             "assert hashlib.sha256(data).hexdigest() == want, 'sdist hash mismatch'; "
             "open(dest,'xb').write(data)")
OLM_SDIST_DIR = "python-olm-3.2.16"
OLM_BUILD_SCRIPT_SHA256 = "49803289396d08f25cb6b2bc31394340ca437ba85e8ad3fd1dc71aa8e8c7aedc"
OLM_BUNDLED_BUILD = """# Try to build with cmake first, fall back to GNU make
try:
    subprocess.run(
        ["cmake", ".", "-Bbuild", "-DBUILD_SHARED_LIBS=NO"],
        cwd="libolm", check=True,
    )
    subprocess.run(
        ["cmake", "--build", "build"],
        cwd="libolm", check=True,
    )
except FileNotFoundError:
    try:
        # try "gmake" first because some systems have a non-GNU make
        # installed as "make"
        subprocess.run(["gmake", "static"], cwd="libolm", check=True)
    except FileNotFoundError:
        # some systems have GNU make installed without the leading "g"
        # so give that a try (though this may fail if it isn't GNU make)
        subprocess.run(["make", "static"], cwd="libolm", check=True)
"""
MAX_EXTRA_REQUIREMENTS_BYTES = 64 * 1024
MAX_REUSE_CACHE_BYTES = 256 * 1024**2
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


def require_base_interpreter() -> None:
    """Refuse a venv interpreter: the backend and toolchain live in the base prefix.

    An inherited PATH whose first ``python3`` is an old runtime venv made the
    backend-provenance probe fail with a bare ``No module named 'maturin'``.
    """
    if sys.prefix != sys.base_prefix:
        raise PreparationError("must_run_with_base_interpreter")


def _check_ancestors(path: Path, reason: str) -> None:
    # No resolve(): following a symlink would defeat the rejection below.
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or (info.st_mode & stat.S_IWOTH and not info.st_mode & stat.S_ISVTX):
            raise PreparationError(reason)


def fresh_workspace(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    _check_ancestors(path, "unsafe_workspace_parent")
    info = path.parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PreparationError("workspace_parent_must_be_owner_private")
    path.mkdir(mode=0o700)  # Atomic claim; existing successes/failures are never reused.
    path.chmod(0o700)
    return path


def _owner_private(info: os.stat_result) -> bool:
    return info.st_uid == os.getuid() and not info.st_mode & 0o077


def _sha256(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


def _dist_name(wheel: str) -> str:
    return re.sub(r"[-_.]+", "-", wheel.split("-", 1)[0]).lower()


def load_reuse_source(path: Path) -> dict:
    """Validate a prior job before claiming a workspace; return its receipt.

    Only an owner-private, non-symlinked job on this filesystem whose receipt
    says ``ready`` with a passing fresh install is eligible. Build-input
    equality is checked later, once this run has measured its own toolchain.
    """
    path = Path(os.path.abspath(path))
    _check_ancestors(path, "unsafe_reuse_source")
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or not _owner_private(info):
        raise PreparationError("reuse_source_must_be_owner_private_dir")
    receipt_path = path / "receipt.json"
    info = receipt_path.lstat()
    if not stat.S_ISREG(info.st_mode) or not _owner_private(info) or info.st_size > MAX_RECEIPT_BYTES:
        raise PreparationError("reuse_receipt_invalid")
    raw = receipt_path.read_bytes()
    try:
        receipt = json.loads(raw)
    except ValueError:
        raise PreparationError("reuse_receipt_invalid") from None
    if (not isinstance(receipt, dict) or receipt.get("schema") != "ccc.termux-preparation.v1"
            or receipt.get("status") != "ready"
            or (receipt.get("scenarios") or {}).get("fresh_install") != "pass"):
        raise PreparationError("reuse_receipt_not_ready")
    if receipt.get("work_dir") != str(path):
        raise PreparationError("reuse_receipt_work_dir_mismatch")
    wheels = receipt.get("wheels")
    if (not isinstance(wheels, dict)
            or not all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in wheels.values())
            or not all(isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", k) for k in wheels)
            or sorted(_dist_name(name) for name in wheels) != sorted(NATIVE)):
        raise PreparationError("reuse_receipt_wheels_invalid")
    receipt["_path"] = str(path)
    receipt["_sha256"] = hashlib.sha256(raw).hexdigest()
    return receipt


def _private_copy(source: Path, target: Path) -> int:
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise PreparationError("reuse_cache_entry_invalid")
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        data = stream.read()
    out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(out, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
    return len(data)


def native_cache_wheels(cache: Path) -> dict[str, list[str]]:
    """Map each native wheel filename in pip's wheel cache to its digests."""
    found: dict[str, list[str]] = {}
    for path in sorted(cache.rglob("*.whl")):
        if _dist_name(path.name) in NATIVE:
            found.setdefault(path.name, []).append(_sha256(path))
    return found


def reuse_native_wheels(work: Path, receipt: dict, report: dict) -> None:
    """Seed this job's private caches from a matching prior job (#2175).

    pip's wheel cache (``pip-cache/wheels``) is what ordinary hash-locked
    bootstrap consumes: each entry's ``origin.json`` keeps the original sdist
    hash, so ``--require-hashes`` still checks the lock. The wheelhouse copy is
    evidence only. Every native wheel must match the prior receipt's digest.
    """
    for field in REUSE_MATCH_FIELDS:
        if report.get(field) != receipt.get(field):
            raise PreparationError(f"reuse_{field}_mismatch")
    prior = Path(receipt["_path"])
    source_cache, target_cache = prior / "pip-cache" / "wheels", work / "pip-cache" / "wheels"
    info = source_cache.lstat()
    if not stat.S_ISDIR(info.st_mode) or not _owner_private(info):
        raise PreparationError("reuse_cache_missing")
    total = 0
    for root, dirs, files in os.walk(source_cache):
        relative = Path(root).relative_to(source_cache)
        (target_cache / relative).mkdir(mode=0o700, exist_ok=relative == Path("."))
        for name in dirs:
            if Path(root, name).is_symlink():  # os.walk lists, but never follows, these.
                raise PreparationError("reuse_cache_entry_invalid")
        for name in files:
            total += _private_copy(Path(root, name), target_cache / relative / name)
            if total > MAX_REUSE_CACHE_BYTES:
                raise PreparationError("reuse_cache_too_large")
    expected = receipt["wheels"]
    if native_cache_wheels(target_cache) != {name: [digest] for name, digest in expected.items()}:
        raise PreparationError("reuse_wheel_hash_mismatch")
    for name, digest in expected.items():
        _private_copy(prior / "wheelhouse" / name, work / "wheelhouse" / name)
        if _sha256(work / "wheelhouse" / name) != digest:
            raise PreparationError("reuse_wheel_hash_mismatch")
    report["native_wheels"] = {"mode": "reused", "from": receipt["_path"],
                               "receipt_sha256": receipt["_sha256"]}


def verify_reused_wheels_used(work: Path, report: dict) -> None:
    """Fail if bootstrap rebuilt a native package instead of using the cache."""
    expected = {name: [digest] for name, digest in report["wheels"].items()}
    if native_cache_wheels(work / "pip-cache" / "wheels") != expected:
        raise PreparationError("reused_wheels_not_used")


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


def prepare(runner: Runner, source: Path, report: dict, reinstall: bool,
            reuse: dict | None = None, extras: list[str] | None = None) -> None:
    work = runner.work
    report["source_seal"] = source_seal(source)
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
    if reuse is None:
        runner.run("builder-venv", [*base, "--system-site-packages", str(work / "builder")])
        builder = str(work / "builder/bin/python")
        pip = [builder, "-I", "-B", "-m", "pip"]
        runner.run("build-tools", [*pip, "install", "--require-hashes", "-r", str(work / "build-tools.lock.txt")])
        # pip validates pyproject build requirements against the prepared builder;
        # a future minimum maturin bump fails rather than silently ignoring it.
        runner.run("native-wheels", [*pip, "wheel", "--no-build-isolation", "--check-build-dependencies",
                                   "--no-deps", "--require-hashes", "-r", str(work / "native.lock.txt"),
                                   "--wheel-dir", str(work / "wheelhouse")])
        report["native_wheels"] = {"mode": "built"}
    else:
        started = time.monotonic()
        stage = {"id": "native-wheels-reuse", "status": "error"}
        runner.stages.append(stage)
        print("termux-prepare: native-wheels-reuse", file=sys.stderr, flush=True)
        try:
            reuse_native_wheels(work, reuse, report)
            stage["status"] = "pass"
        finally:
            stage["duration_ms"] = round((time.monotonic() - started) * 1000)
    report["wheels"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted((work / "wheelhouse").glob("*.whl"))}
    runner.run("runtime-venv", [*base, str(work / "runtime")])
    runtime = str(work / "runtime/bin/python")
    bootstrap = [runtime, "-B", str(source / "dependency_bootstrap.py"),
                 "--bridge-dir", str(source), "--venv-dir", str(work / "runtime"),
                 "--project-env", str(work / "absent.env"), "--process-unlocked", "0"]
    report["scenarios"]["fresh_install"] = "in_progress"
    runner.run("fresh-install", bootstrap)
    if reuse is not None:
        verify_reused_wheels_used(work, report)
    verify_runtime(runner, runtime, "fresh-readiness")
    report["scenarios"]["fresh_install"] = "pass"
    if reinstall:
        report["scenarios"]["reinstall"] = "in_progress"
        runner.run("force-reinstall", [runtime, "-I", "-B", "-m", "pip", "install",
                                      "--force-reinstall", "--require-hashes", "-r",
                                      str(source / "requirements.lock.txt")])
        runner.run("reinstall-reconcile", bootstrap)
        if reuse is not None:
            verify_reused_wheels_used(work, report)
        verify_runtime(runner, runtime, "reinstall-readiness")
        report["scenarios"]["reinstall"] = "pass"
    if extras:
        install_extras(runner, source, runtime, extras, report)
    if source_seal(source) != report["source_seal"]:
        raise PreparationError("source_changed_during_preparation")


def _freeze_lines(text: str) -> set[str]:
    return {line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")}


def _freeze_names(lines: set[str]) -> dict[str, str]:
    found = {}
    for line in lines:
        name = re.split(r"\s*(?:==|@)\s*", line, maxsplit=1)[0]
        found[re.sub(r"[-_.]+", "-", name).lower()] = line
    return found


def patch_olm_build(text: str, prefix: Path) -> str:
    """Point python-olm's cffi build at the system libolm; fail closed on drift."""
    edits = (
        ('compile_args = ["-Ilibolm/include"]', f"compile_args = [{'-I' + str(prefix / 'include')!r}]"),
        (OLM_BUNDLED_BUILD, "# termux_prepare: link the installed system libolm; no bundled C++ build.\n"),
        ('library_dirs=[os.path.join("libolm", "build")]', f"library_dirs=[{str(prefix / 'lib')!r}]"),
    )
    for old, new in edits:
        if text.count(old) != 1:
            raise PreparationError("extra_matrix_olm_build_script_unexpected")
        text = text.replace(old, new)
    return text


def build_termux_olm(runner: Runner, pip: list[str]) -> dict:
    """Build and install python-olm against the system libolm (Termux)."""
    work = runner.work
    prefix = Path(sys.base_prefix)
    library, header = prefix / "lib/libolm.so", prefix / "include/olm/olm.h"
    if not library.is_file() or not header.is_file():
        raise PreparationError("extra_matrix_system_libolm_missing")
    root = work / "extra-matrix-olm"
    root.mkdir(mode=0o700)
    archive_path = root / "python-olm-3.2.16.tar.gz"
    runner.run("extra-matrix-olm-download", [pip[0], "-I", "-B", "-c", OLM_FETCH,
                                             OLM_SDIST_URL, str(archive_path), OLM_SDIST_SHA256])
    if not archive_path.is_file() or _sha256(archive_path) != OLM_SDIST_SHA256:
        raise PreparationError("extra_matrix_olm_sdist_hash_mismatch")
    with tarfile.open(archive_path) as archive:
        archive.extractall(root / "src", filter="data")
    script = root / "src" / OLM_SDIST_DIR / "olm_build.py"
    original = script.read_bytes()
    if hashlib.sha256(original).hexdigest() != OLM_BUILD_SCRIPT_SHA256:
        raise PreparationError("extra_matrix_olm_build_script_unexpected")
    script.write_text(patch_olm_build(original.decode(), prefix))
    runner.run("extra-matrix-olm-wheel", [*pip, "wheel", "--no-deps", str(script.parent),
                                          "-w", str(root / "wheel")])
    wheels = sorted((root / "wheel").glob("python_olm-*.whl"))
    if len(wheels) != 1:
        raise PreparationError("extra_matrix_olm_wheel_missing")
    runner.run("extra-matrix-olm-install", [*pip, "install", "--no-deps", str(wheels[0])])
    return {"sdist_sha256": _sha256(archive_path),
            "build_script_sha256": OLM_BUILD_SCRIPT_SHA256,
            "patched_build_script_sha256": _sha256(script),
            "system_libolm_sha256": _sha256(library),
            "wheel": wheels[0].name, "wheel_sha256": _sha256(wheels[0])}


def install_extras(runner: Runner, source: Path, runtime: str, extras: list[str], report: dict) -> None:
    """Install optional frontend extras into the prepared runtime (#2175 B).

    The core runtime is frozen first and that freeze is passed as pip
    constraints, so an extra can only add distributions. Afterwards every
    core line must still be present unchanged, the extra's own probe must
    import, ``pip check`` must pass and the core readiness probes run again.
    """
    work = runner.work
    pip = [runtime, "-I", "-B", "-m", "pip"]
    results = report.setdefault("extras", {})
    for name in extras:
        spec = EXTRAS[name]
        results[name] = {"status": "in_progress"}
        requirements = source / spec["requirements"]
        info = requirements.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_EXTRA_REQUIREMENTS_BYTES:
            raise PreparationError(f"extra_{name}_requirements_invalid")
        before = _freeze_lines(runner.run(f"extra-{name}-freeze", [*pip, "freeze", "--exclude-editable"]).read_text())
        constraints = work / f"extra-{name}-constraints.txt"
        private_write(constraints, "".join(f"{line}\n" for line in sorted(before)))
        olm = build_termux_olm(runner, pip) if spec.get("termux_olm") else None
        runner.run(f"extra-{name}-install", [*pip, "install", "--require-virtualenv",
                                             "-r", str(requirements), "-c", str(constraints)])
        after = _freeze_lines(runner.run(f"extra-{name}-freeze-after",
                                         [*pip, "freeze", "--exclude-editable"]).read_text())
        if not before <= after:
            raise PreparationError(f"extra_{name}_changed_core")
        runner.run(f"extra-{name}-imports", [runtime, "-I", "-B", "-c", spec["probe"]])
        runner.run(f"extra-{name}-pip-check", [*pip, "check"])
        added = _freeze_names(after - before)
        results[name] = {"status": "pass",
                         "requirements_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
                         "added": dict(sorted(added.items()))}
        if olm is not None:
            results[name]["olm"] = olm
    verify_runtime(runner, runtime, "extras-readiness")


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
    parser.add_argument("--extra", action="append", choices=sorted(EXTRAS), default=[],
                        help="also install an optional frontend extra, constrained to the core "
                             "runtime freeze (repeatable)")
    parser.add_argument("--reuse-wheels-from", type=Path, metavar="PRIOR_JOB",
                        help="reuse native wheels from a ready job on this node whose locks, "
                             "toolchain, API and linker profile match exactly (else refuse)")
    args = parser.parse_args(argv)
    report = {"schema": "ccc.termux-preparation.v1", "status": "error", "stages": [],
              "scenarios": {name: "not_run" for name in
                            ("fresh_install", "reinstall", "rollback", "service_restart", "promotion")}}
    runner = None
    previous_handler = signal.signal(signal.SIGTERM, cancel)
    try:
        require_base_interpreter()
        reuse = load_reuse_source(args.reuse_wheels_from) if args.reuse_wheels_from else None
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
                      cache_scope=("new_private_pip_and_cargo_target; shared_default_cargo_registry"
                                   if reuse is None else
                                   "reused_prior_job_wheel_cache; new_private_pip_http_cache; "
                                   "shared_default_cargo_registry"),
                      stages=runner.stages)
        options: dict = {}
        if reuse is not None:
            options["reuse"] = reuse
        if args.extra:
            options["extras"] = list(dict.fromkeys(args.extra))
        prepare(runner, Path(__file__).resolve().parent, report, args.verify_reinstall, **options)
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
    for extra in (report.get("extras") or {}).values():
        if extra.get("status") == "in_progress":
            extra["status"] = "fail"
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
