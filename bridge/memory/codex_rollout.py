"""Read a Codex rollout transcript from disk for distill snapshots.

``thread/read`` is authoritative while a thread is live, but a distill job is
snapshotted by a worker that runs after the interactive path enqueued it (the
trigger contract in #475 deliberately never awaits a provider call). By then the
app-server no longer knows the thread, so the read returned nothing and the job
completed having stored zero facts. Codex writes the same conversation to
``<CODEX_HOME>/sessions/**/rollout-<timestamp>-<thread-id>.jsonl``, which
outlives the process, so that file is the fallback source.

This module only parses. Locating the file stays with the caller so the search
is confined to the CODEX_HOME of the audience-bound runtime that asked for it;
a global search would read across audience partitions.

Safety differs from ``piri_snapshot`` on one point: Codex writes rollouts 0644
under 0755 directories, so an owner-only (``0o077``) test would reject every
real file. The check here rejects group/other *write* (``0o022``), matching the
existing ``validate_codex_loader`` contract for files this repo does not own the
mode of.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
from typing import Literal, Mapping

from .distill_types import TranscriptBounds, TranscriptMessage

_MAX_HEADER_BYTES = 64 * 1024
_MAX_SCAN_BYTES = 16 * 1024 * 1024
_MAX_LINE_BYTES = 4 * 1024 * 1024


def _unsafe_mode(mode: int) -> bool:
    return bool(stat.S_IMODE(mode) & 0o022)


def validate_rollout_root(path: Path) -> None:
    """Reject a sessions root that another user could write through."""

    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Codex sessions root is unsafe")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise ValueError("Codex sessions root owner is unsafe")
    if _unsafe_mode(metadata.st_mode):
        raise ValueError("Codex sessions root permissions are unsafe")


def _open_rollout(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    metadata = os.fstat(descriptor)
    unsafe = (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
        or _unsafe_mode(metadata.st_mode)
    )
    if unsafe:
        os.close(descriptor)
        raise ValueError("Codex rollout file is unsafe")
    return descriptor, metadata


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_identified_tail(
    path: Path, session_id: str, limits: TranscriptBounds
) -> bytes:
    """Confirm the file really is this session, then return a bounded tail.

    The filename embeds the thread id, but a filename is not identity: the
    ``session_meta`` header is checked so a renamed or copied rollout cannot be
    attributed to another session.
    """

    descriptor, metadata = _open_rollout(path)
    try:
        header = os.pread(descriptor, _MAX_HEADER_BYTES + 1, 0)
        first_line = header.splitlines()[0] if header else b""
        if len(first_line) > _MAX_HEADER_BYTES:
            raise ValueError("Codex rollout header exceeds its safe bound")
        try:
            value = json.loads(first_line)
        except (UnicodeError, ValueError):
            raise ValueError("Codex rollout header is invalid") from None
        payload = value.get("payload") if isinstance(value, Mapping) else None
        if (
            not isinstance(value, Mapping)
            or value.get("type") != "session_meta"
            or not isinstance(payload, Mapping)
            or session_id not in {payload.get("id"), payload.get("session_id")}
        ):
            raise ValueError("Codex rollout identity does not match")
        scan_bytes = min(
            _MAX_SCAN_BYTES,
            max(
                1024 * 1024,
                limits.max_bytes * 16,
                limits.max_message_bytes * limits.max_items,
            ),
        )
        offset = max(0, metadata.st_size - scan_bytes)
        body = os.pread(descriptor, metadata.st_size - offset, offset)
        if offset:
            # A tail read almost certainly starts mid-record; drop the partial.
            newline = body.find(b"\n")
            body = b"" if newline < 0 else body[newline + 1 :]
        return body
    finally:
        os.close(descriptor)


def _message_from(
    payload: Mapping[str, object],
) -> tuple[Literal["user", "assistant"], str] | None:
    """Map one rollout record to (role, text), or None if it is not a message.

    ``event_msg`` carries what the user and agent actually said.
    ``response_item`` messages are deliberately ignored: their ``user`` role
    also holds injected context blocks (recommended-plugins, permission
    preambles) that were never user speech, and their ``assistant`` text
    duplicates ``agent_message``. Tool calls, tool output and reasoning are
    excluded here exactly as ``_snapshot_item`` excludes them on the RPC path.
    """

    kind = payload.get("type")
    if kind == "user_message":
        role: Literal["user", "assistant"] = "user"
    elif kind == "agent_message":
        role = "assistant"
    else:
        return None
    text = payload.get("message")
    if not isinstance(text, str) or not (text := text.strip()):
        return None
    return role, text


def read_codex_rollout_candidates(
    path: Path,
    session_id: str,
    *,
    limits: TranscriptBounds,
    captured: datetime,
) -> tuple[list[TranscriptMessage], str | None, bool]:
    """Return newest-first messages, the newest turn id, and a truncation flag.

    The shape matches ``CodexRuntime._snapshot_candidates`` so the caller can
    hand the result to the same byte-bounding pass and both sources produce
    identically bounded snapshots.
    """

    body = _read_identified_tail(path, session_id, limits)
    records: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    for raw_line in body.splitlines():
        if not raw_line.strip():
            continue
        if len(raw_line) > _MAX_LINE_BYTES:
            raise ValueError("Codex rollout line exceeds its safe bound")
        try:
            value = json.loads(raw_line)
        except (UnicodeError, ValueError):
            # A rollout is appended to live; a trailing partial record is
            # expected rather than exceptional.
            continue
        if not isinstance(value, Mapping):
            continue
        payload = value.get("payload")
        records.append((value, payload if isinstance(payload, Mapping) else {}))

    last_turn_id: str | None = None
    for _value, payload in reversed(records):
        candidate = payload.get("turn_id")
        if isinstance(candidate, str) and candidate:
            last_turn_id = candidate
            break

    horizon = captured - timedelta(seconds=limits.max_age_seconds)
    newest: list[TranscriptMessage] = []
    truncated = False
    items_seen = 0
    for value, payload in reversed(records):
        if items_seen >= limits.max_items:
            truncated = True
            break
        items_seen += 1
        message = _message_from(payload)
        if message is None:
            continue
        role, text = message
        timestamp = value.get("timestamp")
        parsed = _parse_time(timestamp)
        if parsed is None or parsed < horizon:
            # Same contract as the RPC path: an unparseable or too-old message
            # is dropped and the snapshot is marked incomplete rather than
            # silently presented as the whole conversation.
            truncated = True
            continue
        newest.append(TranscriptMessage(role, text, str(timestamp)))
        if len(newest) >= limits.max_messages:
            truncated = True
            break
    return newest, last_turn_id, truncated
