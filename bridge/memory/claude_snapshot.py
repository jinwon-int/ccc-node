"""Secure bounded snapshots for Claude JSONL conversations."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Literal, Mapping

from .distill_types import CodexTranscriptSnapshot, TranscriptBounds, TranscriptMessage

_MAX_SCAN_BYTES = 8 * 1024 * 1024


def _validate_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("Claude transcript directory is unsafe")


def _bounded_text(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore").rstrip(), True


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


def _timestamp(value: object, fallback: datetime) -> tuple[str, datetime]:
    parsed: datetime | None = None
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    if parsed is None:
        parsed = fallback
    elif parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z"), parsed


def _read_private_tail(
    path: Path, bounds: TranscriptBounds
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError("Claude transcript file is unsafe")
        scan_bytes = min(
            _MAX_SCAN_BYTES,
            max(
                1024 * 1024,
                bounds.max_bytes * 16,
                bounds.max_message_bytes * bounds.max_items,
            ),
        )
        offset = max(0, metadata.st_size - scan_bytes)
        payload = os.pread(descriptor, metadata.st_size - offset, offset)
        if offset:
            newline = payload.find(b"\n")
            payload = b"" if newline < 0 else payload[newline + 1 :]
        return payload, metadata
    finally:
        os.close(descriptor)


def _message_from_line(
    raw_line: bytes,
    *,
    fallback: datetime,
    captured: datetime,
    max_age_seconds: int,
    max_message_bytes: int,
) -> tuple[TranscriptMessage | None, str | None, bool]:
    """Parse one untrusted JSONL row without exposing its body on failure."""

    try:
        entry: Any = json.loads(raw_line)
    except (UnicodeError, ValueError):
        return None, None, True
    if not isinstance(entry, Mapping):
        return None, None, False
    raw_role = entry.get("type")
    message = entry.get("message")
    if raw_role not in {"user", "assistant"} or not isinstance(message, Mapping):
        return None, None, False
    if message.get("role") != raw_role:
        return None, None, False
    text = _text_content(message.get("content"))
    if not text:
        return None, None, False
    timestamp, parsed_time = _timestamp(entry.get("timestamp"), fallback)
    if (captured - parsed_time).total_seconds() > max_age_seconds:
        return None, None, True
    text, shortened = _bounded_text(text, max_message_bytes)
    role: Literal["user", "assistant"] = raw_role
    raw_id = entry.get("uuid")
    item_id = raw_id if isinstance(raw_id, str) and raw_id else None
    return TranscriptMessage(role, text, timestamp), item_id, shortened


def read_claude_snapshot(
    transcripts_dir: Path,
    session_id: str,
    *,
    bounds: TranscriptBounds,
    now: datetime | None = None,
) -> CodexTranscriptSnapshot:
    """Return the bounded newest Claude user/assistant messages."""

    if not session_id:
        raise ValueError("Claude session id must not be empty")
    captured = now or datetime.now(timezone.utc)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    captured = captured.astimezone(timezone.utc)
    thread_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    directory = Path(transcripts_dir)
    _validate_directory(directory)
    path = directory / f"{session_id}.jsonl"
    try:
        path.lstat()
    except FileNotFoundError:
        return CodexTranscriptSnapshot(
            thread_hash, None, (), 0, False, captured.isoformat().replace("+00:00", "Z")
        )
    payload, metadata = _read_private_tail(path, bounds)
    fallback = datetime.fromtimestamp(metadata.st_mtime, tz=timezone.utc)
    newest: list[TranscriptMessage] = []
    byte_count = 0
    user_turns = 0
    items_seen = 0
    last_turn_id: str | None = None
    truncated = metadata.st_size > len(payload)
    for raw_line in reversed(payload.splitlines()):
        if items_seen >= bounds.max_items:
            truncated = True
            break
        items_seen += 1
        message, item_id, item_truncated = _message_from_line(
            raw_line,
            fallback=fallback,
            captured=captured,
            max_age_seconds=bounds.max_age_seconds,
            max_message_bytes=bounds.max_message_bytes,
        )
        truncated = truncated or item_truncated
        if message is None:
            continue
        if message.role == "user":
            user_turns += 1
            if user_turns > bounds.max_turns:
                truncated = True
                break
        remaining = bounds.max_bytes - byte_count
        if remaining <= 0:
            truncated = True
            break
        text, shortened = _bounded_text(message.text, remaining)
        truncated = truncated or shortened
        if not text:
            break
        newest.append(TranscriptMessage(message.role, text, message.timestamp))
        byte_count += len(text.encode("utf-8"))
        if last_turn_id is None and item_id is not None:
            last_turn_id = item_id
        if len(newest) >= bounds.max_messages:
            truncated = True
            break
    return CodexTranscriptSnapshot(
        thread_hash=thread_hash,
        last_turn_id=last_turn_id,
        messages=tuple(reversed(newest)),
        byte_count=byte_count,
        truncated=truncated,
        captured_at=captured.isoformat().replace("+00:00", "Z"),
    )


__all__ = ["read_claude_snapshot"]
