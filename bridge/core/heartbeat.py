"""Heartbeat helpers for long-running Telegram bridge tasks."""

from __future__ import annotations

from typing import Any, Optional


def _truncate(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    return value[: max(0, limit - 1)] + "…"


def format_duration(seconds: float) -> str:
    """Return a compact human duration such as ``12s`` or ``3m 05s``."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def tool_label(name: Optional[str], tool_input: Any = None) -> Optional[str]:
    """Return a short, non-secret-ish label for the current SDK tool call."""
    if not name:
        return None
    if not isinstance(tool_input, dict):
        return str(name)

    summary = None
    limit = 60
    if name == "Bash":
        summary = tool_input.get("command")
        limit = 60
    elif name in {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit"}:
        summary = tool_input.get("file_path") or tool_input.get("path")
        limit = 40
    elif name == "Glob":
        summary = tool_input.get("pattern")
        limit = 50
    elif name == "Grep":
        summary = tool_input.get("pattern")
        limit = 50
    elif name == "WebFetch":
        summary = tool_input.get("url")
        limit = 60
    elif name == "WebSearch":
        summary = tool_input.get("query")
        limit = 60
    elif name == "Task":
        summary = tool_input.get("description") or tool_input.get("subagent_type")
        limit = 60

    if summary:
        return f"{name}: {_truncate(str(summary), limit)}"
    return str(name)


def compose_heartbeat_text(
    *,
    elapsed_seconds: float,
    current_tool: Optional[str] = None,
    forecast_seconds: Optional[float] = None,
) -> str:
    """Compose the Telegram heartbeat status line."""
    parts = [f"⏳ Working — {format_duration(elapsed_seconds)}"]
    if current_tool:
        parts.append(current_tool)
    text = " | ".join(parts)
    if forecast_seconds is not None and forecast_seconds > 0:
        text += f" · ETA ~{format_duration(forecast_seconds)}"
    return text


def should_update_heartbeat(
    *,
    now: float,
    started_at: float,
    last_update_at: float,
    threshold_seconds: float,
    update_interval_seconds: float,
) -> bool:
    """Return True when a heartbeat should be sent or edited now."""
    if now - started_at < max(0.0, threshold_seconds):
        return False
    if last_update_at <= 0:
        return True
    return now - last_update_at >= max(0.0, update_interval_seconds)


def has_recent_visible_progress(
    *,
    now: float,
    last_visible_progress_at: float,
    window_seconds: float,
) -> bool:
    """Return True when streaming already showed user-visible progress recently."""
    return last_visible_progress_at > 0 and now - last_visible_progress_at < max(0.0, window_seconds)
