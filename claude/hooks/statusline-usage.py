#!/usr/bin/env python3
"""Persist a tiny allowlisted Claude status-line usage snapshot."""

from __future__ import annotations

import json
import math
import os
import stat
import sys
import time
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
from typing import Any, Mapping

MAX_INPUT_BYTES = 256 * 1024
MAX_SNAPSHOTS = 64
PRUNE_AFTER_SECONDS = 24 * 60 * 60


def number(value: object, maximum: float) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0 or parsed > maximum:
        return None
    return int(value) if isinstance(value, int) else parsed


def mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def sanitized_snapshot(data: object, now: float) -> tuple[str, dict[str, object]] | None:
    root = mapping(data)
    session_id = root.get("session_id")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
        return None
    context = mapping(root.get("context_window"))
    cost = mapping(root.get("cost"))
    limits = mapping(root.get("rate_limits"))
    result: dict[str, object] = {"observedAt": now}

    used = number(context.get("total_input_tokens"), 10**9)
    output = number(context.get("total_output_tokens"), 10**9)
    size = number(context.get("context_window_size"), 10**9)
    if used is not None or output is not None or size is not None:
        result["context"] = {
            key: value
            for key, value in (
                ("usedTokens", used),
                ("outputTokens", output),
                ("contextWindow", size),
            )
            if value is not None
        }
    total_cost = number(cost.get("total_cost_usd"), 10**9)
    if total_cost is not None:
        result["totalCostUsd"] = total_cost

    rate_limits: dict[str, object] = {}
    for key in ("five_hour", "seven_day"):
        window = mapping(limits.get(key))
        used_percent = number(window.get("used_percentage"), 100)
        resets_at = number(window.get("resets_at"), 10**11)
        if used_percent is None:
            continue
        rate_limits[key] = {
            field: value
            for field, value in (
                ("used_percentage", used_percent),
                ("resets_at", resets_at),
            )
            if value is not None
        }
    if rate_limits:
        result["rateLimits"] = rate_limits
    return session_id, result


def open_directory_path(path: Path) -> int:
    """Create/open a directory by dirfd-walking without following symlinks."""

    absolute = Path(os.path.abspath(os.path.expanduser(str(path))))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    current_fd = os.open(absolute.anchor or os.sep, flags)
    try:
        for component in absolute.parts[1:]:
            try:
                os.mkdir(component, 0o700, dir_fd=current_fd)
            except FileExistsError:
                pass
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def open_snapshot_directory() -> int:
    state = Path(os.environ.get("CCC_STATE_DIR", Path.home() / ".claude" / "state"))
    state_fd = open_directory_path(state)
    try:
        info = os.fstat(state_fd)
        if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode):
            raise OSError("unsafe state directory")
        try:
            os.mkdir("usage", 0o700, dir_fd=state_fd)
        except FileExistsError:
            pass
        usage_fd = os.open(
            "usage",
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=state_fd,
        )
    finally:
        os.close(state_fd)
    info = os.fstat(usage_fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(usage_fd)
        raise OSError("unsafe usage directory")
    return usage_fd


def write_snapshot(directory_fd: int, session_id: str, snapshot: Mapping[str, object]) -> None:
    name = f"{sha256(session_id.encode('utf-8')).hexdigest()}.json"
    try:
        existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid()
    ):
        return
    payload = json.dumps(snapshot, separators=(",", ":"), sort_keys=True).encode("utf-8")
    temporary = f".{name}.{os.getpid()}.{token_hex(6)}.tmp"
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("short snapshot write")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def prune(directory_fd: int, now: float) -> None:
    entries: list[tuple[float, str]] = []
    for name in os.listdir(directory_fd):
        if len(name) != 69 or not name.endswith(".json"):
            continue
        try:
            int(name[:-5], 16)
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except (FileNotFoundError, ValueError):
            continue
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
            entries.append((info.st_mtime, name))
    entries.sort(reverse=True)
    for index, (modified, name) in enumerate(entries):
        if index < MAX_SNAPSHOTS and now - modified <= PRUNE_AFTER_SECONDS:
            continue
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        return 0
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 0
    now = time.time()
    sanitized = sanitized_snapshot(data, now)
    if sanitized is None:
        return 0
    try:
        directory_fd = open_snapshot_directory()
        try:
            write_snapshot(directory_fd, *sanitized)
            prune(directory_fd, now)
        finally:
            os.close(directory_fd)
    except OSError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
