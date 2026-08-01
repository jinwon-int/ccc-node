#!/usr/bin/env python3
"""Merge the local nunchi snapshot into the canonical Codex memory hook JSON.

This loader is deliberately local-only.  The canonical ``load-memory.sh``
remains authoritative; every nunchi error returns its unmodified JSON document.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
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


def _state_dir(environ: Mapping[str, str], home: Path, claude_dir: Path) -> Path:
    return Path(environ.get("CCC_STATE_DIR") or claude_dir / "state").expanduser().absolute()


def _mode_is_on(environ: Mapping[str, str], home: Path, claude_dir: Path) -> bool:
    mode = _safe_read(
        _state_dir(environ, home, claude_dir) / "nunchi.mode",
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


def _run_base(loader: Path, environ: Mapping[str, str]) -> bytes | None:
    try:
        completed = subprocess.run(
            ["bash", str(loader), "SessionStart"],
            env=dict(environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=11.5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or len(completed.stdout) > MAX_MANAGED_FILE_BYTES:
        return None
    return completed.stdout


def _regenerate_snapshot(script: Path, environ: Mapping[str, str]) -> bool:
    timeout = _bounded_float(
        environ.get("CCC_CODEX_NUNCHI_REGEN_TIMEOUT_SEC"),
        2.0,
        minimum=0.1,
        maximum=3.0,
    )
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "snapshot", "--limit", "25"],
            env=dict(environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _truncate_utf8(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _nunchi_context(
    environ: Mapping[str, str], *, home: Path, claude_dir: Path, hook_dir: Path
) -> str | None:
    if not _mode_is_on(environ, home, claude_dir):
        return None
    nunchi_home = Path(environ.get("NUNCHI_HOME") or home / ".nunchi").expanduser().absolute()
    snapshot_path = Path(
        environ.get("NUNCHI_SNAPSHOT") or nunchi_home / "snapshot.md"
    ).expanduser().absolute()
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
        if script is None or not _regenerate_snapshot(script, environ):
            return None
        snapshot = _safe_read(
            snapshot_path,
            max_bytes=MAX_MANAGED_FILE_BYTES,
            local_only=True,
        )
        if snapshot is None or time.time() - snapshot[1].st_mtime > max_age:
            return None

    try:
        text = snapshot[0].decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
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
    environ = os.environ
    home = Path(environ.get("HOME") or str(Path.home())).expanduser().absolute()
    claude_dir = Path(environ.get("CCC_CLAUDE_DIR") or home / ".claude").expanduser().absolute()
    hook_dir = Path(__file__).absolute().parent
    base_loader = _validate_managed_script(hook_dir.parent / "load-memory.sh")
    if base_loader is None:
        return 1
    base_raw = _run_base(base_loader, environ)
    if base_raw is None:
        return 1
    parsed = _strict_base_document(base_raw)
    if parsed is None:
        return 1
    document, base_context = parsed

    addition = _nunchi_context(
        environ, home=home, claude_dir=claude_dir, hook_dir=hook_dir
    )
    if addition:
        hook_output = document["hookSpecificOutput"]
        assert isinstance(hook_output, dict)
        hook_output["additionalContext"] = f"{base_context}\n\n{addition}"
    sys.stdout.write(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
