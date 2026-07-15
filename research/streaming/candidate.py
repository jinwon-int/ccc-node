"""Editable candidate policy for the streaming-boundary autoresearch pilot.

The evaluator imports only :func:`segment`. Keep this file self-contained so an
agent can experiment here without touching production bridge code.
"""

from __future__ import annotations

from typing import Any


def _emit(bubbles: list[dict[str, Any]], text: str | None, event_index: int) -> None:
    if not text:
        return
    cleaned = text.strip()
    if not cleaned:
        return
    if bubbles and bubbles[-1]["text"] == cleaned:
        return
    bubbles.append({"text": cleaned, "released_at_event": event_index})


def segment(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn normalized provider events into user-visible message bubbles.

    A completed assistant message stays pending until look-ahead proves that
    more work follows. This mirrors the production bridge's conservative rule:
    do not emit a terminal answer early, but release genuine progress before a
    tool call or the next assistant message.
    """

    bubbles: list[dict[str, Any]] = []
    current_parts: list[str] = []
    pending: str | None = None

    for event_index, event in enumerate(events):
        event_type = event.get("type")

        if event_type == "text_delta":
            if pending is not None:
                _emit(bubbles, pending, event_index)
                pending = None
            text = event.get("text")
            if isinstance(text, str):
                current_parts.append(text)
            continue

        if event_type == "message_completed":
            completed = "".join(current_parts).strip()
            current_parts.clear()
            if pending is not None:
                _emit(bubbles, pending, event_index)
            pending = completed or None
            continue

        if event_type == "tool_started":
            if pending is not None:
                _emit(bubbles, pending, event_index)
                pending = None
            continue

        if event_type in {"result", "turn_completed"}:
            completed = "".join(current_parts).strip()
            current_parts.clear()
            terminal = pending or completed
            result_text = event.get("text")
            if terminal is None and isinstance(result_text, str):
                terminal = result_text
            _emit(bubbles, terminal, event_index)
            pending = None

    if pending is not None or current_parts:
        _emit(bubbles, pending or "".join(current_parts), len(events))

    return bubbles
