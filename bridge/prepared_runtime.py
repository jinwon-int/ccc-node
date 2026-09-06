#!/usr/bin/env python3
"""Validate a prepared runtime without installing packages or starting a bot."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
import uuid

if __package__:
    from .dependency_bootstrap import DependencyPaths, InstallMode, dependency_fingerprint
    from .runtime_readiness import PROBES, git_identity
else:
    # -I omits the script directory; load only this explicitly selected source.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dependency_bootstrap import DependencyPaths, InstallMode, dependency_fingerprint
    from runtime_readiness import PROBES, git_identity

SKIP_DIRS = {"venv", ".venv", "__pycache__", ".git", ".pytest_cache", "tests"}
CODE_SUFFIXES = {".py", ".sh", ".toml", ".txt", ".json"}


def bounded_read(path: Path, limit: int = 4 * 1024**2) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("non_regular_input")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("oversize_input")
    return data


def source_seal(source: Path) -> dict:
    """Seal runtime Python/shell/config inputs, excluding secrets and outputs."""
    digest = hashlib.sha256()
    count = size = 0
    def fail_walk(error):
        raise error
    for current, dirs, files in os.walk(source, followlinks=False, onerror=fail_walk):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and not d.endswith(".egg-info"))
        if any((Path(current) / d).is_symlink() for d in dirs):
            raise ValueError("source_directory_symlink")
        for name in sorted(files):
            path = Path(current) / name
            if name.startswith(".") or (path.suffix not in CODE_SUFFIXES and name != "crash-policy.env"):
                continue
            data = bounded_read(path)
            count += 1
            size += len(data)
            if count > 5000 or size > 64 * 1024**2:
                raise ValueError("source_tree_too_large")
            digest.update(path.relative_to(source).as_posix().encode())
            digest.update(b"\0" + hashlib.sha256(data).digest())
    if not count:
        raise ValueError("empty_source_tree")
    return {"sha256": digest.hexdigest(), "files": count, "bytes": size}


def safe_parent(path: Path) -> None:
    for item in path.parents:
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("directory_symlink_or_non_directory")
        if info.st_mode & stat.S_IWOTH and not info.st_mode & stat.S_ISVTX:
            raise ValueError("writable_ancestor")
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise ValueError("unsafe_immediate_parent")


def private_directory(path: Path) -> None:
    safe_parent(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("directory_symlink_or_non_directory")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("directory_not_owner_private")


def prepared_identity(source: Path, work: Path) -> dict:
    private_directory(work)
    runtime_info = (work / "runtime").lstat()
    if not stat.S_ISDIR(runtime_info.st_mode) or runtime_info.st_uid != os.getuid():
        raise ValueError("runtime_not_owned_directory")
    receipt_path = work / "receipt.json"
    info = receipt_path.lstat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("receipt_not_owner_private")
    receipt = json.loads(bounded_read(receipt_path, 1024**2))
    if (receipt.get("schema") != "ccc.termux-preparation.v1"
            or receipt.get("status") != "ready"
            or receipt.get("scenarios", {}).get("fresh_install") != "pass"
            or receipt.get("work_dir") != str(work)):
        raise ValueError("preparation_not_ready_or_relocated")
    seal = source_seal(source)
    if receipt.get("source_seal") != seal:
        raise ValueError("source_changed_or_unsealed")
    if Path(sys.prefix).resolve() != work / "runtime":
        raise ValueError("wrong_runtime_interpreter")
    # The editable first-party package must point at the selected launcher.
    import importlib.util
    package = importlib.util.find_spec("telegram_bot")
    if package is None or package.origin is None or Path(package.origin).resolve().parent != source:
        raise ValueError("editable_source_mismatch")
    paths = DependencyPaths.from_roots(source, work / "runtime", work / "absent.env")
    fingerprint = dependency_fingerprint(paths, InstallMode.LOCKED)
    if bounded_read(paths.hash_cache, 256).decode().strip() != fingerprint:
        raise ValueError("dependency_fingerprint_mismatch")
    return {"source_dir": str(source), "source_seal": seal,
            "dependency_fingerprint": fingerprint, "runtime_dir": str(work / "runtime")}


def probe(name: str, args: tuple[str, ...], deadline: float) -> dict:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"id": name, "status": "not_run"}
    started = time.monotonic()
    child = None
    result = {"id": name, "status": "error"}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
    try:
        child = subprocess.Popen([sys.executable, "-I", "-B", *args],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        code = child.wait(timeout=max(0.001, deadline - time.monotonic()))
        result.update(status="pass" if code == 0 else "fail", exit_code=code)
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
    finally:
        if child is not None and result["status"] != "pass":
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    return result


def record_validation(directory: Path, report: dict, launcher_pid: int) -> None:
    # Each launch appends its own receipt; no shared read/modify/write or
    # overwrite of a previous source/environment reference occurs here.
    safe_parent(directory)
    if not directory.exists():
        directory.mkdir(mode=0o700)
    private_directory(directory)
    path = directory / f"{uuid.uuid4().hex}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(dict(report, phase="validated_before_launch", launcher_pid=launcher_pid), stream)
        stream.write("\n")


def cancel(signum, frame):
    raise KeyboardInterrupt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-dir", type=Path, required=True)
    parser.add_argument("--prepared-dir", type=Path, required=True)
    parser.add_argument("--record-dir", type=Path)
    parser.add_argument("--launcher-pid", type=int, default=0)
    args = parser.parse_args(argv)
    report = {"schema": "ccc.prepared-runtime.v1", "status": "error",
              "checked_at": datetime.now(timezone.utc).isoformat()}
    deadline = time.monotonic() + 60
    previous = signal.signal(signal.SIGTERM, cancel)
    try:
        source = args.bridge_dir.resolve()
        # Preserve visible symlinks for private_directory's rejection.
        work = Path(os.path.abspath(args.prepared_dir))
        report.update(prepared_identity(source, work))
        report["source_git"] = git_identity(source, min(4, deadline - time.monotonic()))
        report["checks"] = [probe(name, cmd, deadline) for name, cmd in PROBES]
        if all(p["status"] == "pass" for p in report["checks"]):
            # Detect source edits during probes, before stop/exec proceeds.
            if source_seal(source) != report["source_seal"]:
                raise ValueError("source_changed_during_validation")
            report["status"] = "ready"
            if args.record_dir:
                record_validation(args.record_dir, report, args.launcher_pid)
        else:
            report["status"] = "unready"
    except KeyboardInterrupt:
        report.update(status="error", reason="cancelled")
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        report.update(status="error", reason="prepared_validation_failed")
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
