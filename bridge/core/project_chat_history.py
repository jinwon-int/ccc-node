"""Conversation history helpers for ProjectChatHandler."""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)


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
        """Extract the last assistant text message from a session JSONL file."""
        filepath = self.conversations_dir / f"{session_id}.jsonl"
        last_text = None
        for _idx, _role, content, _ts in iter_transcript_messages(
            filepath, types=("assistant",)
        ):
            # This accessor keeps the LAST non-empty text block across the file,
            # and only considers list-shaped content.
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text:
                        last_text = text
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
        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        cleaned = ansi_escape.sub("", response)
        cleaned = "".join(
            char for char in cleaned if ord(char) >= 32 or char in "\n\r\t"
        )
        return cleaned.strip()
