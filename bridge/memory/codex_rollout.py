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

from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
from typing import Literal, Mapping

from .distill_types import TranscriptBounds, TranscriptMessage

_MAX_HEADER_BYTES = 64 * 1024
_MAX_SCAN_BYTES = 16 * 1024 * 1024
_DEDUPE_NEIGHBOURHOOD = 8
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


def _scan_window(limits: TranscriptBounds) -> int:
    return min(
        _MAX_SCAN_BYTES,
        max(
            1024 * 1024,
            limits.max_bytes * 16,
            limits.max_message_bytes * limits.max_items,
        ),
    )


def _read_identified_tail(
    path: Path, session_id: str, scan_bytes: int
) -> tuple[bytes, bool]:
    """Confirm the file really is this session, then return a bounded tail.

    The filename embeds the thread id, but a filename is not identity: the
    ``session_meta`` header is checked so a renamed or copied rollout cannot be
    attributed to another session. The flag says whether the tail started
    after the beginning of the file, i.e. whether a wider read could see more.
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
        offset = max(0, metadata.st_size - scan_bytes)
        body = os.pread(descriptor, metadata.st_size - offset, offset)
        if offset:
            # A tail read almost certainly starts mid-record; drop the partial.
            newline = body.find(b"\n")
            body = b"" if newline < 0 else body[newline + 1 :]
        return body, offset > 0
    finally:
        os.close(descriptor)


_Role = Literal["user", "assistant"]
_Source = Literal["legacy", "item"]
# ``event_msg`` legacy speech events (Codex CLI before ~0.149).
_LEGACY_SPEECH: Mapping[str, _Role] = {
    "user_message": "user",
    "agent_message": "assistant",
}
# ``event_msg`` / ``item_completed`` turn items. Codex CLI 0.149+/0.160 records
# speech only here (core ``TurnItem``: ``UserMessage``/``AgentMessage`` with a
# ``content`` part list); the camelCase spelling is the app-server v2 form and
# is accepted so either serialization maps the same way.
_ITEM_SPEECH: Mapping[str, _Role] = {
    "UserMessage": "user",
    "AgentMessage": "assistant",
    "userMessage": "user",
    "agentMessage": "assistant",
}
# ``UserInput::Text`` serializes as ``text``; ``AgentMessageContent::Text`` is
# an internally tagged variant (``Text``). The response-item spellings are
# accepted too so a future rename does not empty the snapshot again (#2316).
_TEXT_PART_TYPES = frozenset({"text", "Text", "input_text", "output_text"})
_MAX_CONTENT_PARTS = 64


def _content_text(content: object) -> str | None:
    """Join the text parts of a turn item's ``content``; images etc. are skipped."""

    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, (list, tuple)):
        return None
    parts: list[str] = []
    for block in content[:_MAX_CONTENT_PARTS]:
        if not isinstance(block, Mapping) or block.get("type") not in _TEXT_PART_TYPES:
            continue
        text = block.get("text")
        if isinstance(text, str) and (text := text.strip()):
            parts.append(text)
    return "\n".join(parts) or None


def _message_from(
    payload: Mapping[str, object],
) -> tuple[_Role, str, _Source] | None:
    """Map one rollout record to (role, text, source), or None if not speech.

    Only ``event_msg`` payloads carry what the user and agent actually said:
    the legacy ``user_message``/``agent_message`` events, and since Codex CLI
    0.149 the ``item_completed`` ``UserMessage``/``AgentMessage`` turn items
    (0.160.1 rollouts contain no ``user_message`` event at all, #2316).
    ``response_item`` messages are deliberately ignored: their ``user`` role
    also holds injected context blocks (recommended-plugins, permission
    preambles) that were never user speech, and their ``assistant`` text
    duplicates the agent message. Tool calls, tool output and reasoning are
    excluded here exactly as ``_snapshot_item`` excludes them on the RPC path.
    """

    kind = payload.get("type")
    if isinstance(kind, str) and kind in _LEGACY_SPEECH:
        text = payload.get("message")
        if not isinstance(text, str) or not (text := text.strip()):
            return None
        return _LEGACY_SPEECH[kind], text, "legacy"
    if kind != "item_completed":
        return None
    item = payload.get("item")
    if not isinstance(item, Mapping):
        return None
    item_type = item.get("type")
    if not isinstance(item_type, str) or item_type not in _ITEM_SPEECH:
        return None
    role = _ITEM_SPEECH[item_type]
    raw_text = item.get("text") if role == "assistant" else None
    if isinstance(raw_text, str) and raw_text.strip():
        return role, raw_text.strip(), "item"
    text = _content_text(item.get("content"))
    if text is None:
        return None
    return role, text, "item"


def _speech_candidates(
    body: bytes,
) -> tuple[list[tuple[Mapping[str, object], _Role, str]], str | None]:
    """Parse a tail into file-ordered speech candidates and the newest turn id.

    A rollout can hold both spellings of one utterance (an older CLI that
    emitted the legacy event beside the turn item, or a 0.160 file that still
    has a few ``agent_message`` events). Turn items are authoritative; a
    legacy event is dropped when an as-yet-unmatched turn item of the same
    role carries the same text, and kept otherwise so legacy-only speech is
    never lost.
    """

    last_turn_id: str | None = None
    parsed: list[tuple[Mapping[str, object], _Role, str, _Source]] = []
    positions: list[int] = []  # raw record index of each parsed utterance
    for position, raw_line in enumerate(body.splitlines()):
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
        if not isinstance(payload, Mapping):
            continue
        turn_id = payload.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            last_turn_id = turn_id
        # ``response_item`` also has an ``agent_message`` variant (inter-agent
        # traffic with a ``content`` list), so the record type gates speech.
        message = _message_from(payload) if value.get("type") == "event_msg" else None
        if message is not None:
            parsed.append((value, *message))
            positions.append(position)

    # A legacy event and an item carry the same utterance only when they are
    # written next to each other in the file (measured in raw records, not in
    # utterances); matching across the whole file dropped a repeated short
    # message ("ok") from an earlier turn (#2316 review).
    keys = [_dedupe_key(role, text) for _value, role, text, _source in parsed]
    consumed: set[int] = set()
    dropped: set[int] = set()
    for index, (_value, _role, _text, source) in enumerate(parsed):
        if source != "legacy":
            continue
        low = bisect_left(positions, positions[index] - _DEDUPE_NEIGHBOURHOOD)
        high = bisect_right(positions, positions[index] + _DEDUPE_NEIGHBOURHOOD)
        for other in range(low, high):
            if (
                other not in consumed
                and parsed[other][3] == "item"
                and keys[other] == keys[index]
            ):
                consumed.add(other)
                dropped.add(index)
                break
    candidates = [
        (value, role, text)
        for index, (value, role, text, _source) in enumerate(parsed)
        if index not in dropped
    ]
    return candidates, last_turn_id


def _dedupe_key(role: _Role, text: str) -> tuple[_Role, str]:
    # Whitespace-insensitive: a multi-part item joins its parts with newlines.
    return role, "".join(text.split())


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

    window = _scan_window(limits)
    body, partial = _read_identified_tail(path, session_id, window)
    candidates, last_turn_id = _speech_candidates(body)
    if (
        partial
        and window < _MAX_SCAN_BYTES
        and not any(role == "user" for _value, role, _text in candidates)
    ):
        # An agentic tail can be megabytes of tool output and reasoning with
        # no user turn in it — only agent progress messages — so the turn
        # that started the work sits before the window. Widen once to the
        # hard scan bound, so the work stays bounded by bytes rather than by
        # record count (#2316 review: a tail with agent speech only dropped
        # every user turn and still reported the snapshot complete).
        body, partial = _read_identified_tail(path, session_id, _MAX_SCAN_BYTES)
        candidates, last_turn_id = _speech_candidates(body)

    horizon = captured - timedelta(seconds=limits.max_age_seconds)
    newest: list[TranscriptMessage] = []
    # A tail read that did not reach the start of the file may have left
    # older speech unread: never present it as the whole conversation.
    truncated = partial
    items_seen = 0
    for value, role, text in reversed(candidates):
        # Only speech counts against max_items (#2316): tool, reasoning and
        # token_count records used to exhaust the budget before any message.
        if items_seen >= limits.max_items:
            truncated = True
            break
        items_seen += 1
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
