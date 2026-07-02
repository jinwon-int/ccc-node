"""Conversation history helpers for ProjectChatHandler."""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _conversations_dir() -> Path:
    """Read the active project_chat module compatibility constant at call time."""
    import sys

    project_chat = sys.modules["telegram_bot.core.project_chat"]
    return project_chat.CONVERSATIONS_DIR


class ProjectChatHistoryMixin:
    def list_sessions(self, limit: int = 10) -> List[Tuple[str, str, float]]:
        """List recent conversations: [(session_id, first_user_msg, mtime)]"""
        conv_dir = _conversations_dir()
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
        filepath = _conversations_dir() / f"{session_id}.jsonl"
        if not filepath.exists():
            return None
        try:
            last_text = None
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") != "assistant":
                        continue
                    msg = d.get("message", {})
                    if msg.get("role") != "assistant":
                        continue
                    content = msg.get("content", [])
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
        except Exception:
            return None

    def get_recent_messages(
        self, session_id: str, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Get the last N messages from a session in chronological order."""
        filepath = _conversations_dir() / f"{session_id}.jsonl"
        if not filepath.exists():
            return []

        try:
            all_messages = []
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg_type = d.get("type")
                    if msg_type not in ("user", "assistant"):
                        continue

                    msg = d.get("message", {})
                    role = msg.get("role")
                    if role not in ("user", "assistant"):
                        continue

                    content = msg.get("content", "")
                    text = ""
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "").strip()
                                if text:
                                    break
                    elif isinstance(content, str):
                        text = content.strip()

                    if not text:
                        continue

                    timestamp = d.get("timestamp", "")
                    all_messages.append(
                        {"role": role, "content": text, "timestamp": timestamp}
                    )

            return all_messages[-limit:] if all_messages else []
        except Exception as e:
            logger.error(f"Error reading session messages: {e}")
            return []

    def get_conversation_history(
        self, session_id: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get conversation history with message index for revert operations.

        Returns list of USER messages only with index, timestamp, role, and content preview.
        Messages are returned in reverse chronological order (newest first).
        """
        filepath = _conversations_dir() / f"{session_id}.jsonl"
        if not filepath.exists():
            return []

        try:
            all_messages = []
            with open(filepath, "r", encoding="utf-8") as f:
                for idx, line in enumerate(f):
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg_type = d.get("type")
                    if msg_type != "user":
                        continue

                    msg = d.get("message", {})
                    role = msg.get("role")
                    if role != "user":
                        continue

                    content = msg.get("content", "")
                    text = ""
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "").strip()
                                if text:
                                    break
                    elif isinstance(content, str):
                        text = content.strip()

                    if not text:
                        continue

                    timestamp = d.get("timestamp", "")
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
        except Exception as e:
            logger.error(f"Error reading conversation history: {e}")
            return []

    @staticmethod
    def _extract_first_user_message(filepath: Path) -> Optional[str]:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    d = json.loads(line)
                    if d.get("type") != "user":
                        continue
                    msg = d.get("message", {})
                    if msg.get("role") != "user":
                        continue
                    content = msg.get("content", "")
                    text = ""
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                text = c["text"]
                                break
                    elif isinstance(content, str):
                        text = content
                    text = text.strip()
                    if text and not text.startswith("<"):
                        return text[:80]
        except Exception:
            pass
        return None

    def _clean_response(self, response: str) -> str:
        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        cleaned = ansi_escape.sub("", response)
        cleaned = "".join(
            char for char in cleaned if ord(char) >= 32 or char in "\n\r\t"
        )
        return cleaned.strip()
