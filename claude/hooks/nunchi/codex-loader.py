#!/usr/bin/env python3
"""Optionally prepend safe local nunchi context to canonical hook JSON on stdin.

Nunchi is the primary working memory during the gate-3 transition (#824), so
the bounded nunchi block is placed BEFORE the canonical context: when the
materializer's whole-snapshot byte cap truncates the merged output, the
canonical tail is sacrificed first instead of silently dropping nunchi.

The Codex materializer runs ``load-memory.sh`` itself with the full configured
deadline, then invokes this managed helper only with the time left over. Every
nunchi failure returns a non-zero status so the parent can retain the canonical
snapshot unchanged. No memory body is written to stderr or diagnostics.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Mapping


DEFAULT_NUNCHI_BYTES = 3072
MAX_NUNCHI_BYTES = 8192
MAX_MANAGED_FILE_BYTES = 1024 * 1024
DEFAULT_SNAPSHOT_MAX_AGE_SECONDS = 15 * 60


def _bounded_int(
    raw: str | None, default: int, *, minimum: int, maximum: int
) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _bounded_float(
    raw: str | None, default: float, *, minimum: float, maximum: float
) -> float:
    try:
        value = float(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return min(max(value, minimum), maximum)


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() not in ("", "0", "false", "off", "no")


def _owner_safe_regular(metadata: os.stat_result, *, local_only: bool) -> bool:
    owners = {os.geteuid()} if local_only else {0, os.geteuid()}
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and metadata.st_uid in owners
        and not stat.S_IMODE(metadata.st_mode) & 0o022
    )


def _safe_read(
    path: Path, *, max_bytes: int, local_only: bool
) -> tuple[bytes, os.stat_result] | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        if (
            not _owner_safe_regular(before, local_only=local_only)
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            return None
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                return None
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            return None
        return b"".join(chunks), after
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _read_stdin_bounded() -> bytes | None:
    raw = sys.stdin.buffer.read(MAX_MANAGED_FILE_BYTES + 1)
    if not raw or len(raw) > MAX_MANAGED_FILE_BYTES:
        return None
    return raw


def _validate_managed_script(path: Path) -> Path | None:
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if (
        not _owner_safe_regular(metadata, local_only=False)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_MANAGED_FILE_BYTES
    ):
        return None
    return path


def _state_dir(environ: Mapping[str, str], claude_dir: Path) -> Path:
    return Path(environ.get("CCC_STATE_DIR") or claude_dir / "state").expanduser().absolute()


def _private_legacy_route(
    environ: Mapping[str, str], *, home: Path, claude_dir: Path
) -> tuple[Path, Path] | None:
    if not _truthy(environ.get("CCC_MEMORY_AUDIENCE_SCOPED")):
        return None
    scope = environ.get("CCC_MEMORY_SCOPE") or ""
    state = Path(environ.get("CCC_MEMORY_LEGACY_STATE_DIR") or "").expanduser()
    nunchi = Path(environ.get("CCC_MEMORY_LEGACY_NUNCHI_HOME") or "").expanduser()
    if not (
        environ.get("CCC_MEMORY_AUDIENCE") == "private"
        and re.fullmatch(r"private-[0-9a-f]{32}", scope)
        and _truthy(environ.get("CCC_MEMORY_LEGACY_PRIVATE_READS"))
        and state.is_absolute()
        and state == claude_dir / "state"
        and nunchi.is_absolute()
        and nunchi == home / ".nunchi"
    ):
        return None
    return state, nunchi


def _mode_is_on(environ: Mapping[str, str], claude_dir: Path, home: Path) -> bool:
    state_dir = _state_dir(environ, claude_dir)
    if _truthy(environ.get("CCC_MEMORY_AUDIENCE_SCOPED")):
        legacy = _private_legacy_route(environ, home=home, claude_dir=claude_dir)
        if legacy is None:
            return False
        state_dir = legacy[0]
    mode = _safe_read(
        state_dir / "nunchi.mode",
        max_bytes=16,
        local_only=True,
    )
    if mode is None:
        return False
    try:
        return mode[0].decode("ascii").strip() == "on"
    except UnicodeDecodeError:
        return False


def _strict_base_document(raw: bytes) -> tuple[dict[str, object], str] | None:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    hook_output = document.get("hookSpecificOutput")
    if not isinstance(hook_output, dict):
        return None
    context = hook_output.get("additionalContext")
    if not isinstance(context, str) or not context.strip():
        return None
    return document, context


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=0.2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass


def _start_process(
    command: list[str],
    *,
    environ: Mapping[str, str],
    stdin_data: bytes | None = None,
    capture: bool,
) -> subprocess.Popen[bytes] | None:
    stdin_file = None
    try:
        if stdin_data is not None:
            stdin_file = tempfile.TemporaryFile()
            stdin_file.write(stdin_data)
            stdin_file.seek(0)
        return subprocess.Popen(
            command,
            env=dict(environ),
            stdin=stdin_file if stdin_file is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            bufsize=0,
        )
    except OSError:
        return None
    finally:
        if stdin_file is not None:
            stdin_file.close()


def _capture_bounded(
    process: subprocess.Popen[bytes], *, timeout: float
) -> tuple[int, bytes] | None:
    if process.stdout is None:
        _terminate(process)
        return None
    deadline = time.monotonic() + timeout
    descriptor = process.stdout.fileno()
    os.set_blocking(descriptor, False)
    selector = selectors.DefaultSelector()
    selector.register(descriptor, selectors.EVENT_READ)
    output = bytearray()
    eof = False
    try:
        while not eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate(process)
                return None
            events = selector.select(remaining)
            if not events:
                _terminate(process)
                return None
            for _key, _mask in events:
                try:
                    chunk = os.read(descriptor, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    eof = True
                    break
                output.extend(chunk)
                if len(output) > MAX_MANAGED_FILE_BYTES:
                    _terminate(process)
                    return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate(process)
            return None
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _terminate(process)
            return None
        return returncode, bytes(output)
    finally:
        selector.close()
        process.stdout.close()


def _run_bounded(
    command: list[str],
    *,
    environ: Mapping[str, str],
    timeout: float,
    stdin_data: bytes | None = None,
    capture: bool,
) -> tuple[int, bytes] | None:
    if timeout <= 0:
        return None
    process = _start_process(
        command,
        environ=environ,
        stdin_data=stdin_data,
        capture=capture,
    )
    if process is None:
        return None
    if capture:
        return _capture_bounded(process, timeout=timeout)
    try:
        return process.wait(timeout=timeout), b""
    except subprocess.TimeoutExpired:
        _terminate(process)
        return None


def _regenerate_snapshot(
    script: Path, environ: Mapping[str, str], *, deadline: float
) -> bool:
    configured = _bounded_float(
        environ.get("CCC_CODEX_NUNCHI_REGEN_TIMEOUT_SEC"),
        2.0,
        minimum=0.1,
        maximum=3.0,
    )
    remaining = deadline - time.monotonic() - 0.05
    if remaining <= 0:
        return False
    result = _run_bounded(
        [sys.executable, str(script), "snapshot", "--limit", "25"],
        environ=environ,
        timeout=min(configured, remaining),
        capture=False,
    )
    return result is not None and result[0] == 0


def _truncate_utf8(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _scan_snapshot(
    raw: bytes, *, hook_dir: Path, environ: Mapping[str, str], deadline: float
) -> str | None:
    scanner = _validate_managed_script(hook_dir.parent / "scan-injection.sh")
    remaining = deadline - time.monotonic() - 0.05
    if scanner is None or remaining <= 0:
        return None
    result = _run_bounded(
        ["bash", str(scanner), "nunchi-snapshot"],
        environ=environ,
        timeout=remaining,
        stdin_data=raw,
        capture=True,
    )
    if result is None or result[0] != 0:
        return None
    try:
        return result[1].decode("utf-8").strip()
    except UnicodeDecodeError:
        return None


def _nunchi_context(
    environ: Mapping[str, str],
    *,
    home: Path,
    claude_dir: Path,
    hook_dir: Path,
    deadline: float,
) -> str | None:
    if not _mode_is_on(environ, claude_dir, home):
        return None
    legacy = _private_legacy_route(environ, home=home, claude_dir=claude_dir)
    if legacy is not None:
        nunchi_home = legacy[1]
        snapshot_path = nunchi_home / "snapshot.md"
        nunchi_environ = dict(environ)
        nunchi_environ.update(
            {
                "NUNCHI_HOME": str(nunchi_home),
                "NUNCHI_DB": str(nunchi_home / "facts.db"),
                "NUNCHI_SNAPSHOT": str(snapshot_path),
            }
        )
    else:
        nunchi_home = Path(
            environ.get("NUNCHI_HOME") or home / ".nunchi"
        ).expanduser().absolute()
        snapshot_path = Path(
            environ.get("NUNCHI_SNAPSHOT") or nunchi_home / "snapshot.md"
        ).expanduser().absolute()
        nunchi_environ = environ
    snapshot = _safe_read(
        snapshot_path,
        max_bytes=MAX_MANAGED_FILE_BYTES,
        local_only=True,
    )
    if snapshot is None:
        return None

    max_age = _bounded_int(
        environ.get("CCC_CODEX_NUNCHI_SNAPSHOT_MAX_AGE_SEC"),
        DEFAULT_SNAPSHOT_MAX_AGE_SECONDS,
        minimum=0,
        maximum=24 * 60 * 60,
    )
    if time.time() - snapshot[1].st_mtime > max_age:
        script = _validate_managed_script(hook_dir / "nunchi.py")
        if script is None or not _regenerate_snapshot(
            script, nunchi_environ, deadline=deadline
        ):
            return None
        snapshot = _safe_read(
            snapshot_path,
            max_bytes=MAX_MANAGED_FILE_BYTES,
            local_only=True,
        )
        if snapshot is None or time.time() - snapshot[1].st_mtime > max_age:
            return None

    try:
        snapshot[0].decode("utf-8")
    except UnicodeDecodeError:
        return None
    text = _scan_snapshot(
        snapshot[0], hook_dir=hook_dir, environ=nunchi_environ, deadline=deadline
    )
    if not text:
        return None
    limit = _bounded_int(
        environ.get("CCC_CODEX_NUNCHI_MAX_BYTES"),
        DEFAULT_NUNCHI_BYTES,
        minimum=128,
        maximum=MAX_NUNCHI_BYTES,
    )
    return _truncate_utf8(text, limit).rstrip()


def main() -> int:
    raw = _read_stdin_bounded()
    if raw is None:
        return 1
    parsed = _strict_base_document(raw)
    if parsed is None:
        return 1
    document, base_context = parsed

    environ = os.environ
    remaining = _bounded_float(
        environ.get("CCC_CODEX_NUNCHI_REMAINING_SEC"),
        3.0,
        minimum=0.05,
        maximum=14.0,
    )
    deadline = time.monotonic() + remaining
    home = Path(environ.get("HOME") or str(Path.home())).expanduser().absolute()
    claude_dir = Path(environ.get("CCC_CLAUDE_DIR") or home / ".claude").expanduser().absolute()
    hook_dir = Path(__file__).absolute().parent
    addition = _nunchi_context(
        environ,
        home=home,
        claude_dir=claude_dir,
        hook_dir=hook_dir,
        deadline=deadline,
    )
    if addition:
        hook_output = document["hookSpecificOutput"]
        assert isinstance(hook_output, dict)
        # Nunchi-primary ordering (#824): the bounded nunchi block leads so a
        # later whole-snapshot truncation cuts the canonical tail, not nunchi.
        hook_output["additionalContext"] = f"{addition}\n\n{base_context}"
    sys.stdout.write(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
