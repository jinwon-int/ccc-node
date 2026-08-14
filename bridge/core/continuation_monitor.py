"""Bridge-side yield-and-continue loop (#1113).

An agent registers the next work bundle (``continuation_cli``) and ends its
turn; this monitor notices the idle conversation and starts the bundle as a
fresh autonomous turn. The marathon continues as a baton pass instead of one
hours-long turn: reports land between bundles, user messages interleave, and
/stop works at bundle boundaries.

Loop-guards (never brakes on progress):

* **Double-start race** — a continuation starts only when the conversation
  has no fresh active-turn route; an unreadable route file is treated as
  "active" (fail-closed). The process-wide session registry remains the hard
  guard: a losing race raises inside ``process_message`` and the tick skips.
* **Daily tripwire** — ``CCC_CONTINUATION_DAILY_CAP`` (default 20) starts per
  day moves the record to ``cap-hold`` and notifies; /continue re-arms it.
* **Consecutive failures** — three in a row stop the chain and notify;
  registering a new bundle re-arms the counter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict

from telegram_bot.core.continuation import (
    MAX_CONSECUTIVE_FAILURES,
    ContinuationQueue,
    conversation_key_of,
)
from telegram_bot.core.external_wait import ACTIVE_ROUTE_TTL_SECONDS

logger = logging.getLogger(__name__)

DEFAULT_TICK_SECONDS = 5.0
DEFAULT_DAILY_CAP = 20

RunnerCallback = Callable[[Dict[str, Any], str], Awaitable[bool]]
NotifierCallback = Callable[[int, str], Awaitable[bool]]


def continuation_prompt_text(record: Dict[str, Any]) -> str:
    """Bridge-owned continuation turn: external_event origin, never user-authored."""
    cid = str(record.get("continuation_id") or "")
    prompt = str(record.get("prompt") or "").strip()
    return f"[external_event: continuation_queue id={cid}]\n\n{prompt}"


def cap_hold_notification_text(record: Dict[str, Any], cap: int) -> str:
    return (
        f"Auto-continue daily tripwire reached ({cap} turns today). "
        "The next bundle is queued, not lost — send /continue to keep going, "
        "or /stop to cancel it."
    )


def failure_stop_notification_text(failures: int) -> str:
    return (
        f"Auto-continue stopped after {failures} consecutive failed turns. "
        "The queued bundle is parked, not dropped — send /continue to retry "
        "it, or /stop to drop it."
    )


def _conversation_has_active_turn(
    active_turns_path: Path, user_id: int, chat_id: int, now: float
) -> bool:
    """True when in doubt: a continuation never starts next to a live turn."""
    try:
        raw = Path(active_turns_path).read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except FileNotFoundError:
        return False  # no route file: no turn ever published — idle
    except Exception:
        return True  # unreadable: fail closed
    if not isinstance(data, dict):
        return True
    entry = data.get(conversation_key_of(user_id, chat_id))
    if not isinstance(entry, dict):
        return False
    try:
        heartbeat = float(entry.get("heartbeat_epoch") or 0)
    except (TypeError, ValueError):
        return True
    return now - heartbeat <= ACTIVE_ROUTE_TTL_SECONDS


class ContinuationMonitor:
    def __init__(
        self,
        queue: ContinuationQueue,
        *,
        runner: RunnerCallback,
        notifier: NotifierCallback,
        active_turns_path: Path,
        clock: Callable[[], float] = time.time,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        daily_cap: int = DEFAULT_DAILY_CAP,
    ) -> None:
        self._queue = queue
        self._runner = runner
        self._notifier = notifier
        self._active_turns_path = Path(active_turns_path)
        self._clock = clock
        self._tick_seconds = float(tick_seconds)
        self._daily_cap = int(daily_cap)

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("Continuation monitor tick failed (continuing)")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        for record in self._queue.pending():
            await self._maybe_start(record)

    async def _maybe_start(self, record: Dict[str, Any]) -> None:
        cid = str(record.get("continuation_id") or "")
        user_id = int(record.get("user_id") or 0)
        chat_id = int(record.get("chat_id") or 0)
        now = self._clock()

        if _conversation_has_active_turn(self._active_turns_path, user_id, chat_id, now):
            return

        counter = self._queue.counter_for(user_id, chat_id)
        failures = int(counter.get("consecutive_failures") or 0)
        if failures >= MAX_CONSECUTIVE_FAILURES:
            if self._queue.mark_cap_hold(cid, reason="consecutive-failure-limit"):
                await self._notify(chat_id, failure_stop_notification_text(failures))
            return

        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        count_today = (
            int(counter.get("count") or 0) if counter.get("day") == today else 0
        )
        if count_today >= self._daily_cap:
            if self._queue.mark_cap_hold(cid):
                await self._notify(
                    chat_id, cap_hold_notification_text(record, self._daily_cap)
                )
            return

        if not self._queue.mark_running(cid):
            return  # replaced/cancelled between the read and the transition
        started = await self._run(record)
        if started:
            self._queue.mark_done(cid)
        else:
            failures = self._queue.mark_failed(cid, "turn_failed")
            if failures >= MAX_CONSECUTIVE_FAILURES:
                logger.warning(
                    "Continuation chain hit %s consecutive failures: %s",
                    failures,
                    cid,
                )

    async def _run(self, record: Dict[str, Any]) -> bool:
        try:
            return bool(await self._runner(record, continuation_prompt_text(record)))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Continuation runner raised: %s", record.get("continuation_id")
            )
            return False

    async def _notify(self, chat_id: int, text: str) -> None:
        try:
            delivered = await self._notifier(chat_id, text)
        except Exception:
            delivered = False
        if not delivered:
            logger.warning("Continuation notification failed: chat=%s", chat_id)


__all__ = [
    "DEFAULT_DAILY_CAP",
    "DEFAULT_TICK_SECONDS",
    "ContinuationMonitor",
    "continuation_prompt_text",
    "cap_hold_notification_text",
    "failure_stop_notification_text",
]
