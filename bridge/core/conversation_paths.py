"""Conversation JSONL path helpers."""

from pathlib import Path
from typing import Optional


def claude_project_dir_name(project_root: Path) -> str:
    """Return Claude Code's ``~/.claude/projects`` directory slug.

    Claude Code replaces path separators, underscores, and dots with dashes.
    The dot rule matters on Android/Termux, whose app-private home includes
    ``com.termux``.
    """
    return str(project_root).replace("/", "-").replace("_", "-").replace(".", "-")


def resolve_conversation_file(conversations_dir: Path, session_id: str) -> Optional[Path]:
    """Resolve a session JSONL file path only if it stays under conversations_dir."""
    root = conversations_dir.resolve()
    filepath = (root / f"{session_id}.jsonl").resolve()
    try:
        filepath.relative_to(root)
    except ValueError:
        return None
    return filepath
