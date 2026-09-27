"""Live answer preview for the Matrix frontend (#1796, first stage; off by default).

Telegram grows its answer in one edited draft (``core/streaming.py``). Matrix
already has an edit-in-place surface: the turn's progress bubble
(``_RoomSink.status`` — created once, refreshed with ``m.replace`` while it is
the newest event, reposted at the bottom when buried, redacted on ``None``).
This sink streams the answer-in-progress into that bubble.

It is a *preview*, never the delivery:

* ``update_if_needed`` / ``add_tool_call`` refresh the bubble (throttled) and
  report nothing delivered;
* ``finalize_segment`` delivers a completed intermediate message through the
  frontend's normal interim path (durable outbox) and clears the bubble —
  exactly what the interim callback did before a sink was injected, since the
  handler routes interim segments to the sink once one is present;
* ``finalize_all`` / ``cancel`` clear the bubble and report ``False``, so the
  handler keeps ``streamed=False`` and the final answer still goes through the
  durable outbox — a crash or a failed edit can never lose it.

The bubble is cosmetic (a direct send outside the outbox, like the status
bubble it reuses); the handler's heartbeat cleanup only runs when a heartbeat
was posted, so the preview clears its own bubble. While the preview shows,
:class:`MatrixBot` suppresses heartbeat texts that would overwrite it.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 2.0
MIN_INTERVAL_S = 1.0
MAX_INTERVAL_S = 60.0
# The bubble is one event (``trim_to_event(edit=True)`` in the sink); show the
# tail of a long answer so the newest text stays visible.
PREVIEW_MAX_CHARS = 3000
TOOL_LINES = 3
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def clean(text: str) -> str:
    """The handler's ``_clean_response``: drop ANSI escapes and control characters, strip."""
    text = _ANSI.sub("", text)
    return "".join(ch for ch in text if ord(ch) >= 32 or ch in "\n\r\t").strip()


def interval_from(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S
    if value != value:  # NaN
        return DEFAULT_INTERVAL_S
    return min(max(value, MIN_INTERVAL_S), MAX_INTERVAL_S)


class MatrixAnswerPreview:
    """``StreamingSinkPort`` that previews the answer in the turn's progress bubble."""

    def __init__(
        self,
        sink: Any,
        deliver_interim: Callable[[str], Awaitable[None]],
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sink = sink
        self._deliver_interim = deliver_interim
        self._interval = interval_from(interval_s)
        self._clock = clock
        self._segment = ""
        self._tools: list[str] = []
        self._last_push = float("-inf")
        self._pushed_text: str | None = None
        self.showing = False

    # -- rendering -----------------------------------------------------------

    def _preview_text(self) -> str:
        body = clean(self._segment)
        if len(body) > PREVIEW_MAX_CHARS:
            body = "…" + body[-PREVIEW_MAX_CHARS:]
        lines = [f"🔧 {name}" for name in self._tools[-TOOL_LINES:]]
        if body:
            lines.append(body + " ▍")
        return "\n".join(lines)

    async def _push(self, *, force: bool = False) -> None:
        now = self._clock()
        if not force and now - self._last_push < self._interval:
            return
        text = self._preview_text()
        if not text.strip() or text == self._pushed_text:
            return
        self._last_push = now
        try:
            await self._sink.status(text)
        except Exception:  # noqa: BLE001 - a cosmetic preview never fails the turn
            logger.debug("Matrix answer preview update failed", exc_info=True)
            return
        self._pushed_text = text
        self.showing = True

    async def _clear(self) -> None:
        if not self.showing:
            return
        self.showing = False
        self._pushed_text = None
        try:
            await self._sink.status(None)
        except Exception:  # noqa: BLE001
            logger.debug("Matrix answer preview clear failed", exc_info=True)

    # -- StreamingSinkPort ---------------------------------------------------

    async def update_if_needed(self, new_text_chunk: str) -> bool:
        if new_text_chunk:
            self._segment += new_text_chunk
            await self._push()
        return False

    async def add_tool_call(self, name: str, input: dict[str, Any]) -> bool:
        del input  # arguments may be sensitive; the preview shows names only
        label = str(name or "tool").strip()[:64] or "tool"
        self._tools.append(label)
        await self._push()
        return False

    async def finalize_segment(self) -> bool:
        """Deliver the completed intermediate message durably; ``True`` when it went out."""
        text = clean(self._segment)
        self._segment = ""
        self._tools = []
        await self._clear()
        if not text:
            return False
        try:
            await self._deliver_interim(text)
        except Exception:  # noqa: BLE001 - kept for the final reply instead
            logger.warning("Matrix interim delivery failed; retaining it for the final reply", exc_info=True)
            return False
        return True

    async def finalize_all(self) -> bool:
        await self._clear()
        return False

    async def cancel(self) -> bool:
        await self._clear()
        return False


__all__ = ["DEFAULT_INTERVAL_S", "MatrixAnswerPreview", "clean", "interval_from"]
