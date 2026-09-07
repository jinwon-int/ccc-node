"""Read a locked, exact native journal; never search another audience's files."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from .distill_types import CodexTranscriptSnapshot, SnapshotUnavailableError, TranscriptBounds
from .piri_snapshot import _collect_messages

MAX_JOURNAL_BYTES = 16 * 1024 * 1024


def _read_locked(directory: Path, session_id: str):
    if str(uuid.UUID(session_id)) != session_id:
        raise ValueError("invalid Danso session id")
    if not directory.is_absolute() or ".." in directory.parts:
        raise ValueError("invalid Danso journal directory")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in directory.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        parent = os.fstat(descriptor)
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError("unsafe Danso journal directory")
        file = os.open(
            session_id + ".jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
        )
        try:
            metadata = os.fstat(file)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or not 0 < metadata.st_size <= MAX_JOURNAL_BYTES
            ):
                raise ValueError("unsafe Danso journal")
            # Native Session::open holds the same flock through the whole run.
            fcntl.flock(file, fcntl.LOCK_SH | fcntl.LOCK_NB)
            payload = os.pread(file, metadata.st_size + 1, 0)
            if (
                len(payload) != metadata.st_size
                or os.fstat(file).st_mtime_ns != metadata.st_mtime_ns
            ):
                raise SnapshotUnavailableError("Danso journal changed")
        finally:
            os.close(file)
    except FileNotFoundError:
        raise SnapshotUnavailableError("Danso journal unavailable") from None
    finally:
        os.close(descriptor)
    return payload, metadata


def _validate_payload(payload, session_id):
    if not payload.endswith(b"\n"):
        raise SnapshotUnavailableError("Danso journal is incomplete")
    try:
        rows = [json.loads(line) for line in payload.splitlines()]
        if (
            rows[0].get("type") != "session"
            or rows[0].get("version") != 3
            or rows[0].get("id") != session_id
        ):
            raise ValueError("Danso journal identity mismatch")
        calls, results, started, settled = set(), set(), set(), set()
        for row in rows[1:]:
            _recovery_row(row, calls, results, started, settled)
        if started or calls != results:
            raise SnapshotUnavailableError("Danso journal has unresolved operations")
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError, UnicodeError):
        raise ValueError("invalid Danso journal") from None


def _recovery_row(row, calls, results, started, settled):
    if row.get("type") == "message":
        message = row["message"]
        if message.get("role") == "assistant" and isinstance(message.get("content"), list):
            for block in message["content"]:
                if block.get("type") == "toolCall":
                    ident = block["id"]
                    if not isinstance(ident, str) or not ident or ident in calls:
                        raise ValueError("invalid tool call identity")
                    calls.add(ident)
        if message.get("role") == "toolResult":
            ident = message["toolCallId"]
            if ident not in calls or ident in results:
                raise ValueError("invalid tool result")
            results.add(ident)
    if row.get("type") == "custom" and row.get("customType") == "danso.operation.v1":
        _operation(row["data"], calls, results, started, settled)


def _operation(data, calls, results, started, settled):
    ident, state = data["toolCallId"], data["state"]
    if ident not in calls:
        raise ValueError("orphan operation")
    if state == "started":
        if ident in results or ident in started or ident in settled:
            raise ValueError("invalid operation start")
        started.add(ident)
    elif state == "settled":
        if ident not in results or ident not in started or ident in settled:
            raise ValueError("invalid operation settlement")
        started.remove(ident)
        settled.add(ident)
    else:
        raise ValueError("invalid operation state")


def read_danso_snapshot(
    directory: Path, session_id: str, *, bounds: TranscriptBounds
) -> CodexTranscriptSnapshot:
    payload, metadata = _read_locked(directory, session_id)
    _validate_payload(payload, session_id)
    captured = datetime.now(timezone.utc)
    messages, count, last, truncated = _collect_messages(
        payload, metadata=metadata, limits=bounds, captured=captured
    )
    return CodexTranscriptSnapshot(
        hashlib.sha256(session_id.encode()).hexdigest(),
        last,
        messages,
        count,
        truncated,
        captured.isoformat(),
    )
