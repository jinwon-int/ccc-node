"""Private lifecycle archives for the working-state checkpoint contract.

Claude owns this boundary through ``checkpoint.sh`` and ``notify.sh``. Piri
and Codex do not run those hooks, so their runtime adapters call this module at
the provider lifecycle seams they can prove: Piri ``compaction_start`` and a
conversation-local session close for both providers.

The archive never logs or returns the file body. Default paths remain inside
the process-owned state tree; shared audiences never fall back to the node's
private legacy working state.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import hashlib
import os
from pathlib import Path
import stat
from typing import Literal

from telegram_bot.utils.secure_fs import (
    atomic_write_bytes_at,
    ensure_private_directory,
    owner_only_regular_violation,
)


ArchiveEvent = Literal["pre_compact", "session_end"]
_DEFAULT_MAX_BYTES = 65_536
_HARD_MAX_BYTES = 1_048_576
_CHECKPOINT_KEEP = 30
_OFF = frozenset({"0", "false", "off", "no"})
_ENVIRONMENT_KEYS = frozenset(
    {
        "HOME",
        "CCC_STATE_DIR",
        "CCC_WORKING_STATE",
        "CCC_CHECKPOINT_DIR",
        "CCC_SESSION_ARCHIVE",
        "CCC_WORKING_STATE_ARCHIVE",
        "CCC_WORKING_STATE_ARCHIVE_MAX_BYTES",
        "CCC_MEMORY_AUDIENCE_SCOPED",
        "CCC_MEMORY_AUDIENCE",
        "CCC_MEMORY_LEGACY_STATE_DIR",
    }
)


def select_working_state_environment(
    environment: Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Copy only archive-contract keys, never unrelated process secrets."""

    if environment is None:
        return None
    selected = {
        name: value
        for name, value in environment.items()
        if name in _ENVIRONMENT_KEYS and isinstance(value, str)
    }
    return selected or None


def _enabled(environment: Mapping[str, str]) -> bool:
    return environment.get("CCC_WORKING_STATE_ARCHIVE", "1").strip().lower() not in _OFF


def _state_dir(environment: Mapping[str, str]) -> Path | None:
    configured = environment.get("CCC_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    home = environment.get("HOME", "").strip()
    if not home:
        return None
    return Path(home).expanduser() / ".claude" / "state"


def _max_bytes(environment: Mapping[str, str]) -> int:
    raw = environment.get("CCC_WORKING_STATE_ARCHIVE_MAX_BYTES", "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_MAX_BYTES
    except ValueError:
        value = _DEFAULT_MAX_BYTES
    return min(_HARD_MAX_BYTES, max(1, value))


def _read_private_regular(path: Path, *, max_bytes: int) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if owner_only_regular_violation(before, owner_id=os.getuid()) is not None:
            return None
        if before.st_size <= 0 or before.st_size > max_bytes:
            return None
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(payload) != before.st_size or len(payload) > max_bytes:
            return None
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            return None
        return payload
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _primary_source(environment: Mapping[str, str], state_dir: Path) -> Path:
    configured = environment.get("CCC_WORKING_STATE", "").strip()
    return Path(configured).expanduser() if configured else state_dir / "working-state.md"


def _read_working_state(
    environment: Mapping[str, str], state_dir: Path
) -> bytes | None:
    primary = _primary_source(environment, state_dir)
    payload = _read_private_regular(primary, max_bytes=_max_bytes(environment))
    if payload is not None:
        return payload

    # An unsafe/non-empty primary is a refusal, not a reason to read another
    # audience. Only the same missing/empty case as checkpoint.sh #1155 may
    # use private legacy input.
    try:
        primary_metadata = primary.lstat()
    except FileNotFoundError:
        primary_metadata = None
    except OSError:
        return None
    if primary_metadata is not None and primary_metadata.st_size > 0:
        return None
    if environment.get("CCC_WORKING_STATE", "").strip():
        return None
    if environment.get("CCC_MEMORY_AUDIENCE_SCOPED", "0").strip().lower() in _OFF:
        return None
    if environment.get("CCC_MEMORY_AUDIENCE", "legacy").strip() != "private":
        return None
    legacy = environment.get("CCC_MEMORY_LEGACY_STATE_DIR", "").strip()
    if not legacy:
        return None
    legacy_path = Path(legacy).expanduser() / "working-state.md"
    if legacy_path == primary:
        return None
    return _read_private_regular(legacy_path, max_bytes=_max_bytes(environment))


def _destination_directory(
    event: ArchiveEvent, environment: Mapping[str, str], state_dir: Path
) -> Path:
    if event == "pre_compact":
        configured = environment.get("CCC_CHECKPOINT_DIR", "").strip()
        return Path(configured).expanduser() if configured else state_dir / "checkpoints"
    configured = environment.get("CCC_SESSION_ARCHIVE", "").strip()
    return Path(configured).expanduser() if configured else state_dir / "session-archive"


def _archive_name(event: ArchiveEvent, session_id: str, payload: bytes) -> str:
    if event == "pre_compact":
        # Match checkpoint.sh's portable local-time filename contract.
        return f"working-state-{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    digest = hashlib.sha256()
    digest.update(event.encode("ascii"))
    digest.update(b"\0")
    digest.update(session_id.encode("utf-8", errors="replace"))
    digest.update(b"\0")
    digest.update(payload)
    return f"working-state-{digest.hexdigest()[:24]}.md"


def _existing_archive_is_safe(directory: Path, name: str, payload: bytes) -> bool:
    existing = _read_private_regular(directory / name, max_bytes=len(payload))
    return existing == payload


def _open_private_directory(directory: Path) -> int:
    ensure_private_directory(directory)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    descriptor = os.open(directory, flags)
    try:
        metadata = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        raise
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise PermissionError("working-state archive directory is not owner-private")
    return descriptor


def _prune_checkpoints(directory: Path) -> None:
    candidates: list[tuple[int, str, Path]] = []
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        return
    for path in entries:
        if not path.name.startswith("working-state-") or not path.name.endswith(".md"):
            continue
        try:
            metadata = path.lstat()
        except OSError:
            continue
        if owner_only_regular_violation(metadata, owner_id=os.getuid()) is not None:
            continue
        candidates.append((metadata.st_mtime_ns, path.name, path))
    candidates.sort(reverse=True)
    for _mtime, _name, path in candidates[_CHECKPOINT_KEEP:]:
        try:
            path.unlink()
        except OSError:
            continue


def archive_working_state(
    event: ArchiveEvent,
    *,
    environment: Mapping[str, str],
    session_id: str,
) -> Path | None:
    """Archive the current contract file and return only its destination path.

    Missing, empty, oversized, or unsafe state returns ``None``; preservation
    is best-effort and never weakens a provider turn or close.
    """

    if event not in {"pre_compact", "session_end"}:
        raise ValueError("working-state archive event is invalid")
    if not _enabled(environment):
        return None
    state_dir = _state_dir(environment)
    if state_dir is None:
        return None
    payload = _read_working_state(environment, state_dir)
    if payload is None:
        return None
    directory = _destination_directory(event, environment, state_dir)
    name = _archive_name(event, session_id, payload)
    try:
        dir_fd = _open_private_directory(directory)
        try:
            if not _existing_archive_is_safe(directory, name, payload):
                atomic_write_bytes_at(dir_fd, name, payload)
        finally:
            os.close(dir_fd)
        destination = directory / name
        if event == "pre_compact":
            _prune_checkpoints(directory)
        return destination
    except OSError:
        return None
