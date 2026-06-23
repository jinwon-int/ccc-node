"""Conversation JSONL path helpers."""

from pathlib import Path
from typing import Optional


def resolve_conversation_file(conversations_dir: Path, session_id: str) -> Optional[Path]:
    """Resolve a session JSONL file path only if it stays under conversations_dir."""
    root = conversations_dir.resolve()
    filepath = (root / f"{session_id}.jsonl").resolve()
    try:
        filepath.relative_to(root)
    except ValueError:
        return None
    return filepath
