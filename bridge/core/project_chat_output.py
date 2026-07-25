"""Typed output state for one provider-neutral project-chat turn (#346)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

TextCleaner = Callable[[str], str]


@dataclass(slots=True)
class TurnOutputBuffer:
    """Assemble semantic assistant messages without owning delivery I/O.

    A completed provider message remains pending until later turn activity
    proves that it was interim.  The caller owns Telegram/stream delivery and
    reports its outcome back through :meth:`resolve_pending_interim`; this
    object owns only the typed, independently testable output state.
    """

    _current_message_parts: list[str] = field(default_factory=list)
    _response_parts: list[str] = field(default_factory=list)
    _pending_completed_message: str | None = None
    _interim_delivered: bool = False
    _saw_text: bool = False

    @property
    def has_text(self) -> bool:
        return self._saw_text

    @property
    def interim_delivered(self) -> bool:
        return self._interim_delivered

    @property
    def pending_interim(self) -> str | None:
        """Peek at pending content; delivery cancellation leaves it intact."""

        return self._pending_completed_message

    def append_delta(self, text: str) -> None:
        if not text:
            return
        self._current_message_parts.append(text)
        self._saw_text = True

    def complete_message(self, cleaner: TextCleaner) -> None:
        completed = cleaner("".join(self._current_message_parts))
        self._current_message_parts.clear()
        if completed:
            self._pending_completed_message = completed

    def resolve_pending_interim(self, *, delivered: bool) -> None:
        content = self._pending_completed_message
        if content is None:
            return
        self._pending_completed_message = None
        if delivered:
            self._interim_delivered = True
        else:
            self._response_parts.append(content)

    def render(self, cleaner: TextCleaner) -> str:
        parts = list(self._response_parts)
        if self._pending_completed_message is not None:
            parts.append(self._pending_completed_message)
        current = cleaner("".join(self._current_message_parts))
        if current:
            parts.append(current)
        return "\n\n".join(part for part in parts if part)


__all__ = ["TextCleaner", "TurnOutputBuffer"]
