"""Secure, bounded snapshots for audience-scoped Piri JSONL sessions."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Literal, Mapping

from .distill_types import (
    CodexTranscriptSnapshot,
    SnapshotUnavailableError,
    TranscriptBounds,
    TranscriptMessage,
)

_MAX_DIRECTORY_ENTRIES = 4096
_MAX_HEADER_BYTES = 64 * 1024
_MAX_SCAN_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _Candidate:
    role: Literal["user", "assistant"]
    text: str
    timestamp: str
    entry_id: str | None


def _timestamp(value: object, *, fallback: datetime) -> tuple[str, datetime]:
    parsed: datetime | None = None
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000.0 if abs(float(value)) >= 10**11 else float(value)
        try:
            parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            parsed = None
    if parsed is None:
        parsed = fallback
    elif parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z"), parsed


def _text_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return " ".join(parts).strip()


def _bounded_text(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore").rstrip(), True


def _validate_directory(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Piri session directory is unsafe")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise ValueError("Piri session directory owner is unsafe")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("Piri session directory permissions are unsafe")


def _open_session(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    metadata = os.fstat(descriptor)
    unsafe = (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
        or stat.S_IMODE(metadata.st_mode) & 0o077
    )
    if unsafe:
        os.close(descriptor)
        raise ValueError("Piri session file is unsafe")
    return descriptor, metadata


def _matches_session_name(name: str, session_id: str) -> bool:
    return name == f"{session_id}.jsonl" or name.endswith(f"_{session_id}.jsonl")


def _session_path(session_dir: Path, session_id: str) -> Path | None:
    if not session_dir.exists():
        return None
    _validate_directory(session_dir)
    candidates: list[Path] = []
    with os.scandir(session_dir) as entries:
        for index, entry in enumerate(entries, start=1):
            if index > _MAX_DIRECTORY_ENTRIES:
                raise ValueError("Piri session directory exceeds its safe bound")
            if not _matches_session_name(entry.name, session_id):
                continue
            if not entry.is_file(follow_symlinks=False):
                raise ValueError("Piri session entry is unsafe")
            candidates.append(session_dir / entry.name)
    if not candidates:
        return None
    if len(candidates) != 1:
        raise ValueError("Piri session id is ambiguous")
    return candidates[0]


def _read_payload(path: Path, session_id: str, limits: TranscriptBounds) -> tuple[bytes, os.stat_result]:
    descriptor, metadata = _open_session(path)
    try:
        header = os.pread(descriptor, _MAX_HEADER_BYTES + 1, 0)
        first_line = header.splitlines()[0] if header else b""
        if len(first_line) > _MAX_HEADER_BYTES:
            raise ValueError("Piri session header exceeds its safe bound")
        try:
            value = json.loads(first_line)
        except (UnicodeError, ValueError):
            raise ValueError("Piri session header is invalid") from None
        if (
            not isinstance(value, Mapping)
            or value.get("type") != "session"
            or value.get("id") != session_id
        ):
            raise ValueError("Piri session identity does not match")
        scan_bytes = min(
            _MAX_SCAN_BYTES,
            max(1024 * 1024, limits.max_bytes * 16, limits.max_message_bytes * limits.max_items),
        )
        offset = max(0, metadata.st_size - scan_bytes)
        payload = os.pread(descriptor, metadata.st_size - offset, offset)
        if offset:
            newline = payload.find(b"\n")
            payload = b"" if newline < 0 else payload[newline + 1 :]
        return payload, metadata
    finally:
        os.close(descriptor)


def _parse_candidate(
    raw_line: bytes,
    *,
    fallback: datetime,
    captured: datetime,
    limits: TranscriptBounds,
) -> tuple[_Candidate | None, bool]:
    try:
        entry: Any = json.loads(raw_line)
    except (UnicodeError, ValueError):
        return None, True
    if not isinstance(entry, Mapping) or entry.get("type") != "message":
        return None, False
    message = entry.get("message")
    if not isinstance(message, Mapping):
        return None, False
    raw_role = message.get("role")
    if raw_role not in {"user", "assistant"}:
        return None, False
    role: Literal["user", "assistant"] = raw_role
    text = _text_content(message.get("content"))
    if not text:
        return None, False
    timestamp, parsed_time = _timestamp(entry.get("timestamp"), fallback=fallback)
    if (captured - parsed_time).total_seconds() > limits.max_age_seconds:
        return None, True
    text, was_truncated = _bounded_text(text, limits.max_message_bytes)
    raw_id = entry.get("id")
    entry_id = raw_id if isinstance(raw_id, str) and raw_id else None
    return _Candidate(role, text, timestamp, entry_id), was_truncated


def _collect_messages(
    payload: bytes,
    *,
    metadata: os.stat_result,
    limits: TranscriptBounds,
    captured: datetime,
) -> tuple[tuple[TranscriptMessage, ...], int, str | None, bool]:
    fallback = datetime.fromtimestamp(metadata.st_mtime, tz=timezone.utc)
    newest: list[TranscriptMessage] = []
    byte_count = 0
    user_turns = 0
    items_seen = 0
    last_turn_id: str | None = None
    truncated = metadata.st_size > len(payload)
    for raw_line in reversed(payload.splitlines()):
        if items_seen >= limits.max_items:
            truncated = True
            break
        items_seen += 1
        candidate, candidate_truncated = _parse_candidate(
            raw_line,
            fallback=fallback,
            captured=captured,
            limits=limits,
        )
        truncated = truncated or candidate_truncated
        if candidate is None:
            continue
        if candidate.role == "user":
            user_turns += 1
            if user_turns > limits.max_turns:
                truncated = True
                break
        remaining = limits.max_bytes - byte_count
        if remaining <= 0:
            truncated = True
            break
        text, was_truncated = _bounded_text(candidate.text, remaining)
        truncated = truncated or was_truncated
        if not text:
            break
        newest.append(TranscriptMessage(candidate.role, text, candidate.timestamp))
        byte_count += len(text.encode("utf-8"))
        if last_turn_id is None:
            last_turn_id = candidate.entry_id
        if len(newest) >= limits.max_messages:
            truncated = True
            break
    return tuple(reversed(newest)), byte_count, last_turn_id, truncated


def read_piri_snapshot(
    session_dir: Path,
    session_id: str,
    *,
    bounds: TranscriptBounds,
    now: datetime | None = None,
) -> CodexTranscriptSnapshot:
    """Return newest user/assistant messages without following provider paths."""

    if not session_id:
        raise ValueError("Piri session id must not be empty")
    captured = now or datetime.now(timezone.utc)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    captured = captured.astimezone(timezone.utc)
    thread_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    path = _session_path(Path(session_dir), session_id)
    if path is None:
        # Same fail-open as the Codex reader had: an absent transcript must not
        # be reported as an empty one, or the job completes having stored
        # nothing. Not yet observed on a live Piri node (0/69 zero-byte
        # snapshots as of 2026-08-19), but the defect is identical in kind.
        raise SnapshotUnavailableError(
            f"Piri session transcript is not readable for snapshot: {thread_hash}"
        )

    payload, metadata = _read_payload(path, session_id, bounds)
    messages, byte_count, last_turn_id, truncated = _collect_messages(
        payload,
        metadata=metadata,
        limits=bounds,
        captured=captured,
    )

    return CodexTranscriptSnapshot(
        thread_hash=thread_hash,
        last_turn_id=last_turn_id,
        messages=messages,
        byte_count=byte_count,
        truncated=truncated,
        captured_at=captured.isoformat().replace("+00:00", "Z"),
    )


def _scan_session_matches(
    directory: Path,
    session_id: str,
    scanned: list[int],
    *,
    collect_subdirs: bool,
) -> tuple[list[Path], list[Path]]:
    """Return (matching dirs, child dirs) within the shared entry budget."""

    matches: list[Path] = []
    children: list[Path] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            scanned[0] += 1
            if scanned[0] > _MAX_DIRECTORY_ENTRIES:
                raise ValueError("Piri session directory exceeds its safe bound")
            if _matches_session_name(entry.name, session_id):
                if not entry.is_file(follow_symlinks=False):
                    raise ValueError("Piri session entry is unsafe")
                matches.append(directory)
            elif collect_subdirs and entry.is_dir(follow_symlinks=False):
                children.append(directory / entry.name)
    return matches, children


def find_piri_session_directory(root: Path, session_id: str) -> Path | None:
    """Locate the directory holding ``session_id`` under a sessions root.

    Unscoped Piri stores transcripts either directly under the sessions root
    or inside per-cwd slug subdirectories one level deep. The scan never
    follows symlinks, skips subdirectories that fail the same ownership and
    permission checks applied to session directories, and stays within the
    same entry budget as single-directory reads. The returned directory is
    meant for ``read_piri_snapshot``, which re-validates it before reading.
    """

    if not session_id:
        raise ValueError("Piri session id must not be empty")
    root = Path(root)
    if not root.exists():
        return None
    _validate_directory(root)
    scanned = [0]
    matches, directories = _scan_session_matches(root, session_id, scanned, collect_subdirs=True)
    for directory in directories:
        try:
            _validate_directory(directory)
        except (OSError, ValueError):
            continue
        found, _ = _scan_session_matches(directory, session_id, scanned, collect_subdirs=False)
        matches.extend(found)
    unique = set(matches)
    if not unique:
        return None
    if len(unique) != 1:
        raise ValueError("Piri session id is ambiguous")
    return unique.pop()


__all__ = ["find_piri_session_directory", "read_piri_snapshot"]
