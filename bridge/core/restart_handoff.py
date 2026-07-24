"""Durable, systemd-backed bridge self-restart handoff.

The bridge process must never restart its own unit directly: systemd kills the
whole target cgroup, including the command that is trying to complete the
restart.  This module first asks systemd to create a delayed transient unit in
a separate cgroup.  That worker then restarts the allowlisted bridge unit and
records a body-free result for the new bridge process to deliver.

The worker entry point deliberately uses only the Python standard library so
``python restart_handoff.py worker ...`` works without package import state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

RECEIPT_NAME = "restart-handoff.json"
ARCHIVE_NAME = "restart-handoff.last.json"
SCHEMA_VERSION = 1
MAX_RECEIPT_BYTES = 8192
ACTIVE_TTL_SECONDS = 300
TERMINAL_STATES = {"completed", "failed"}
_UNIT_RE = re.compile(r"ccc-telegram-bridge(?:-[A-Za-z0-9_.@:-]+)?\.service\Z")
_RUNNER = Callable[..., subprocess.CompletedProcess[str]]


class RestartHandoffError(RuntimeError):
    """A safe, body-free restart scheduling failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ScheduledRestart:
    request_id: str
    transient_unit: str


def validate_unit(unit: str) -> str:
    value = str(unit).strip()
    if not _UNIT_RE.fullmatch(value):
        raise RestartHandoffError("invalid_unit")
    return value


def _open_private_directory(path: Path) -> int:
    path = Path(os.path.abspath(os.fspath(path)))
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(metadata.st_mode):
            raise RestartHandoffError("unsafe_data_dir")
        if not stat.S_ISDIR(metadata.st_mode):
            raise RestartHandoffError("unsafe_data_dir")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RestartHandoffError("unsafe_data_dir")
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise RestartHandoffError("unsafe_data_dir")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags)


def _write_at(data_dir: Path, name: str, record: dict[str, Any]) -> None:
    payload = (
        json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_RECEIPT_BYTES:
        raise RestartHandoffError("receipt_too_large")
    directory_fd = _open_private_directory(data_dir)
    temp_name = f".{name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temp_name, flags, 0o600, dir_fd=directory_fd)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RestartHandoffError("receipt_write_failed")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temp_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def read_receipt(data_dir: Path) -> dict[str, Any] | None:
    directory_fd = _open_private_directory(data_dir)
    descriptor = -1
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(RECEIPT_NAME, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > MAX_RECEIPT_BYTES
        ):
            raise RestartHandoffError("unsafe_receipt")
        payload = os.read(descriptor, MAX_RECEIPT_BYTES + 1)
        if len(payload) > MAX_RECEIPT_BYTES:
            raise RestartHandoffError("receipt_too_large")
        try:
            record = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RestartHandoffError("invalid_receipt") from exc
        if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
            raise RestartHandoffError("invalid_receipt")
        return record
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)


def _remove_receipt(data_dir: Path) -> None:
    directory_fd = _open_private_directory(data_dir)
    try:
        try:
            os.unlink(RECEIPT_NAME, dir_fd=directory_fd)
        except FileNotFoundError:
            return
    finally:
        os.close(directory_fd)


def archive_receipt(data_dir: Path, request_id: str) -> bool:
    """Archive the still-current terminal receipt after successful delivery."""
    record = read_receipt(data_dir)
    if (
        record is None
        or record.get("request_id") != request_id
        or record.get("state") not in TERMINAL_STATES
    ):
        return False
    directory_fd = _open_private_directory(data_dir)
    try:
        os.replace(
            RECEIPT_NAME,
            ARCHIVE_NAME,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        return True
    finally:
        os.close(directory_fd)


def schedule_restart(
    *,
    data_dir: Path,
    chat_id: int,
    unit: str,
    delay_seconds: int = 5,
    runner: _RUNNER = subprocess.run,
    systemd_run_path: str = "/usr/bin/systemd-run",
    python_executable: str = sys.executable,
    now: Callable[[], float] = time.time,
    origin_pid: int | None = None,
    user_scope: bool | None = None,
) -> ScheduledRestart:
    """Persist a request and ask systemd to start the external worker."""
    unit = validate_unit(unit)
    current = read_receipt(data_dir)
    timestamp = now()
    if current:
        if current.get("state") in TERMINAL_STATES:
            # Never overwrite a result before the bridge has delivered and
            # archived it. This also closes the read-then-rename archive race.
            raise RestartHandoffError("restart_result_pending")
        created_at = current.get("created_at")
        if isinstance(created_at, (int, float)) and timestamp - created_at < ACTIVE_TTL_SECONDS:
            raise RestartHandoffError("restart_already_pending")

    request_id = secrets.token_hex(8)
    transient_unit = f"ccc-bridge-restart-{request_id}"
    origin_pid = os.getpid() if origin_pid is None else int(origin_pid)
    scope_is_user = os.geteuid() != 0 if user_scope is None else user_scope
    record = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "state": "prepared",
        "unit": unit,
        "chat_id": int(chat_id),
        "origin_pid": origin_pid,
        "created_at": timestamp,
        "updated_at": timestamp,
        "user_scope": scope_is_user,
    }
    _write_at(data_dir, RECEIPT_NAME, record)

    argv = [systemd_run_path]
    if scope_is_user:
        argv.append("--user")
    argv.extend(
        [
            "--quiet",
            "--collect",
            f"--unit={transient_unit}",
            f"--on-active={max(5, min(int(delay_seconds), 30))}s",
            python_executable,
            str(Path(__file__).resolve()),
            "worker",
            "--data-dir",
            str(Path(data_dir).resolve()),
            "--request-id",
            request_id,
        ]
    )
    if scope_is_user:
        argv.append("--user-scope")
    try:
        result = runner(argv, capture_output=True, text=True, timeout=8, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        _remove_receipt(data_dir)
        raise RestartHandoffError("systemd_run_unavailable") from exc
    if result.returncode != 0:
        _remove_receipt(data_dir)
        raise RestartHandoffError("systemd_run_rejected")
    return ScheduledRestart(request_id=request_id, transient_unit=transient_unit)


def _systemctl_argv(systemctl_path: str, user_scope: bool, *args: str) -> list[str]:
    argv = [systemctl_path]
    if user_scope:
        argv.append("--user")
    argv.extend(args)
    return argv


def _health_matches(data_dir: Path, *, main_pid: int, origin_pid: int, created_at: float) -> bool:
    health_path = data_dir / "health.json"
    try:
        if health_path.stat().st_mtime + 1 < created_at:
            return False
        health = json.loads(health_path.read_text(encoding="utf-8"))
        return (
            health.get("service", {}).get("state") == "available"
            and int(health.get("process", {}).get("pid", 0)) == main_pid
            and main_pid > 0
            and main_pid != origin_pid
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def run_worker(
    *,
    data_dir: Path,
    request_id: str,
    user_scope: bool,
    runner: _RUNNER = subprocess.run,
    systemctl_path: str = "/usr/bin/systemctl",
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    timeout_seconds: int = 60,
) -> int:
    """Restart the exact receipt unit, verify replacement health, and persist result."""
    try:
        record = read_receipt(data_dir)
        if (
            record is None
            or record.get("request_id") != request_id
            or record.get("state") != "prepared"
            or bool(record.get("user_scope")) != user_scope
        ):
            return 2
        unit = validate_unit(record.get("unit", ""))
        record["state"] = "armed"
        record["updated_at"] = now()
        _write_at(data_dir, RECEIPT_NAME, record)

        restart = runner(
            _systemctl_argv(systemctl_path, user_scope, "restart", "--", unit),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if restart.returncode != 0:
            raise RestartHandoffError("restart_failed")

        deadline = now() + timeout_seconds
        while now() < deadline:
            active = runner(
                _systemctl_argv(systemctl_path, user_scope, "is-active", "--quiet", "--", unit),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            show = runner(
                _systemctl_argv(
                    systemctl_path, user_scope, "show", "--property=MainPID", "--value", "--", unit
                ),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            try:
                main_pid = int(show.stdout.strip()) if show.returncode == 0 else 0
            except ValueError:
                main_pid = 0
            if active.returncode == 0 and _health_matches(
                data_dir,
                main_pid=main_pid,
                origin_pid=int(record["origin_pid"]),
                created_at=float(record["created_at"]),
            ):
                record["state"] = "completed"
                record["new_pid"] = main_pid
                record["updated_at"] = now()
                _write_at(data_dir, RECEIPT_NAME, record)
                return 0
            sleep(1)
        raise RestartHandoffError("health_timeout")
    except (OSError, subprocess.SubprocessError, RestartHandoffError) as exc:
        try:
            record = read_receipt(data_dir)
            if record and record.get("request_id") == request_id:
                record["state"] = "failed"
                record["reason_code"] = (
                    exc.code if isinstance(exc, RestartHandoffError) else "worker_error"
                )
                record["updated_at"] = now()
                _write_at(data_dir, RECEIPT_NAME, record)
        except Exception:
            pass
        return 1


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    worker = subparsers.add_parser("worker")
    worker.add_argument("--data-dir", type=Path, required=True)
    worker.add_argument("--request-id", required=True)
    worker.add_argument("--user-scope", action="store_true")
    args = parser.parse_args(argv)
    return run_worker(
        data_dir=args.data_dir,
        request_id=args.request_id,
        user_scope=args.user_scope,
    )


if __name__ == "__main__":
    raise SystemExit(_main())
