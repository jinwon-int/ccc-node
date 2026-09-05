"""Conversation history helpers for ProjectChatHandler."""

import json
import logging
import os
import re
from pathlib import Path
from typing import IO, Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Compiled once at import (#1479): ``_clean_response`` runs on every delivered
# response, so a per-call ``re.compile`` was pure event-loop overhead.
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Backwards block size for the tail-first last-assistant read (#1479). One
# block covers a typical trailing assistant record; a longer record simply
# spans more blocks, and the scan stops at the first hit either way.
_TAIL_BLOCK_BYTES = 64 * 1024


def _last_assistant_text_from_line(line: bytes) -> Optional[str]:
    """Last non-empty text block of one assistant JSONL record, else None.

    Mirrors the per-record rule of the forward scan: ``type`` and
    ``message.role`` must both be ``assistant``, only list-shaped content is
    considered, and the LAST non-empty text block wins. Malformed lines and
    records without text yield None so the caller keeps walking backwards.
    """
    if not line.strip():
        return None
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(d, dict) or d.get("type") != "assistant":
        return None
    msg = d.get("message", {})
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return None
    content = msg.get("content", "")
    if not isinstance(content, list):
        return None
    last_text = None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            text = text.strip() if isinstance(text, str) else ""
            if text:
                last_text = text
    return last_text


def read_last_assistant_text(
    handle: IO[bytes], *, block_bytes: int = _TAIL_BLOCK_BYTES
) -> Optional[str]:
    """Tail-first read of the last assistant text in a transcript (#1479).

    Walks the file backwards in ``block_bytes`` chunks and returns as soon as
    one complete line parses as an assistant record with non-empty text, so a
    multi-megabyte transcript costs a few blocks instead of a full scan.
    ``handle`` must be a seekable binary file. A partial line at the front of a
    chunk is carried into the previous chunk until it is complete.
    """
    size = handle.seek(0, os.SEEK_END)
    position = size
    carry = b""
    while position > 0:
        read_from = max(0, position - block_bytes)
        handle.seek(read_from)
        chunk = handle.read(position - read_from)
        position = read_from
        buffer = chunk + carry
        lines = buffer.split(b"\n")
        # lines[0] may be the tail of a line that starts in an earlier chunk;
        # it is only complete once we reach the start of the file.
        carry = lines[0]
        for line in reversed(lines[1:]):
            text = _last_assistant_text_from_line(line)
            if text is not None:
                return text
    return _last_assistant_text_from_line(carry)


def _first_text_block(content: Any) -> str:
    """First non-empty ``text`` block from a content list, or a stripped string.

    The shared content-extraction used by the recent-messages and revert-history
    accessors. ``get_session_last_assistant_message`` (last block wins) and
    ``_extract_first_user_message`` (first block, may be empty, ``<``-filtered)
    keep their own extraction on top of the shared parse loop below.
    """
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    return text
        return ""
    if isinstance(content, str):
        return content.strip()
    return ""


def iter_transcript_messages(
    filepath: Path, *, types: Tuple[str, ...] = ("user", "assistant")
) -> Iterator[Tuple[int, str, Any, str]]:
    """Single source of the transcript JSONL parse loop (#456).

    Yields ``(line_index, role, content, timestamp)`` for every JSONL line whose
    ``type`` is in *types* and whose ``message.role`` matches that type. Malformed
    JSON lines are skipped; a missing or unreadable file yields nothing.
    ``line_index`` is the 0-based position in the file (used by the revert view).
    """
    try:
        handle = open(filepath, "r", encoding="utf-8")
    except OSError:
        return
    with handle as f:
        for idx, line in enumerate(f):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_type = d.get("type")
            if msg_type not in types:
                continue
            msg = d.get("message", {})
            role = msg.get("role")
            if role != msg_type:
                continue
            yield idx, role, msg.get("content", ""), d.get("timestamp", "")


class ProjectChatHistoryMixin:
    conversations_dir: Path

    def list_sessions(self, limit: int = 10) -> List[Tuple[str, str, float]]:
        """List recent conversations: [(session_id, first_user_msg, mtime)]"""
        conv_dir = self.conversations_dir
        if not conv_dir.exists():
            return []
        files = sorted(
            conv_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True
        )
        results = []
        for f in files[: limit * 2]:
            session_id = f.stem
            mtime = f.stat().st_mtime
            first_msg = self._extract_first_user_message(f)
            if first_msg:
                results.append((session_id, first_msg, mtime))
            if len(results) >= limit:
                break
        return results

    def get_session_last_assistant_message(
        self, session_id: str, max_chars: int = 300
    ) -> Optional[str]:
        """Extract the last assistant text message from a session JSONL file.

        Reads the transcript tail-first and stops at the first assistant
        record with non-empty text (#1479), which is exactly the LAST
        non-empty text block a forward scan would have kept. Only list-shaped
        content is considered.
        """
        filepath = self.conversations_dir / f"{session_id}.jsonl"
        try:
            handle = open(filepath, "rb")
        except OSError:
            return None
        with handle:
            last_text = read_last_assistant_text(handle)
        if not last_text:
            return None
        if len(last_text) > max_chars:
            last_text = last_text[:max_chars] + "..."
        return last_text

    def get_recent_messages(
        self, session_id: str, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Get the last N messages from a session in chronological order."""
        filepath = self.conversations_dir / f"{session_id}.jsonl"
        all_messages = []
        for _idx, role, content, timestamp in iter_transcript_messages(filepath):
            text = _first_text_block(content)
            if not text:
                continue
            all_messages.append(
                {"role": role, "content": text, "timestamp": timestamp}
            )
        return all_messages[-limit:] if all_messages else []

    def get_conversation_history(
        self, session_id: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get conversation history with message index for revert operations.

        Returns list of USER messages only with index, timestamp, role, and content preview.
        Messages are returned in reverse chronological order (newest first).
        """
        filepath = self.conversations_dir / f"{session_id}.jsonl"
        all_messages = []
        for idx, role, content, timestamp in iter_transcript_messages(
            filepath, types=("user",)
        ):
            text = _first_text_block(content)
            if not text:
                continue
            all_messages.append(
                {
                    "index": idx,
                    "role": role,
                    "content": text,
                    "timestamp": timestamp,
                }
            )
        # Return newest first (reverse order)
        recent_messages = all_messages[-limit:] if all_messages else []
        return list(reversed(recent_messages))

    @staticmethod
    def _extract_first_user_message(filepath: Path) -> Optional[str]:
        for _idx, _role, content, _ts in iter_transcript_messages(
            filepath, types=("user",)
        ):
            # First text block (may be empty), then require non-empty and a
            # non-tag ('<') opening; this differs from _first_text_block, so it
            # is kept explicit.
            text = ""
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        text = c.get("text", "")
                        break
            elif isinstance(content, str):
                text = content
            text = text.strip()
            if text and not text.startswith("<"):
                return text[:80]
        return None

    def _clean_response(self, response: str) -> str:
        cleaned = _ANSI_ESCAPE_RE.sub("", response)
        cleaned = "".join(
            char for char in cleaned if ord(char) >= 32 or char in "\n\r\t"
        )
        return cleaned.strip()
