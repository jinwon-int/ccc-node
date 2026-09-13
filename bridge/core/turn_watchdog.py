"""Turn-age watchdog — notify-only dashboard (#1111).

Pure visibility: nothing here ever interrupts, pauses, or reroutes a turn
(hard caps are explicitly out of scope by work order). When a turn's age
crosses ``CCC_TURN_AGE_NOTIFY_MIN`` the owner gets an evidence-limited age
notice, re-notified at most every ``CCC_TURN_AGE_RENOTIFY_MIN``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Dict, List, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TICK_SECONDS = 60.0
DEFAULT_RENOTIFY_SECONDS = 30 * 60.0
DEFAULT_NOTIFY_MINUTES = 30

TurnsProvider = Callable[[], List[Tuple[int, int, float]]]
NotifierCallback = Callable[[int, str], Awaitable[bool]]


def turn_age_text(minutes: int) -> str:
    return (
        f"⏱ Turn has been active for {minutes} min. "
        "This notice alone does not establish progress. Use /stop to stop it."
    )


class TurnAgeWatchdog:
    """Notify-once-then-cooldown age tracker; never touches the turn itself."""

    def __init__(
        self,
        *,
        turns_provider: TurnsProvider,
        notifier: NotifierCallback,
        clock: Callable[[], float] = time.monotonic,
        threshold_seconds: float = DEFAULT_NOTIFY_MINUTES * 60.0,
        renotify_seconds: float = DEFAULT_RENOTIFY_SECONDS,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        self._turns_provider = turns_provider
        self._notifier = notifier
        self._clock = clock
        self._threshold = float(threshold_seconds)
        self._renotify = float(renotify_seconds)
        self._tick_seconds = float(tick_seconds)
        self._last_notified: Dict[Tuple[int, int, float], float] = {}

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("Turn-age watchdog tick failed (continuing)")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        if self._threshold <= 0:
            return
        now = self._clock()
        active_turns = set()
        for user_id, chat_id, started_at in self._turns_provider():
            key = (int(user_id), int(chat_id))
            identity = (key[0], key[1], float(started_at))
            active_turns.add(identity)
            age = now - identity[2]
            if age < self._threshold:
                continue
            last = self._last_notified.get(identity)
            if last is not None and now - last < self._renotify:
                continue
            delivered = await self._notify(key, turn_age_text(int(age // 60)))
            if delivered:
                self._last_notified[identity] = now
        # Forget ended or replaced turns so a new turn never inherits an old
        # conversation's cooldown, even when no empty tick observed the gap.
        for identity in list(self._last_notified):
            if identity not in active_turns:
                self._last_notified.pop(identity, None)

    async def _notify(self, key: Tuple[int, int], text: str) -> bool:
        try:
            return bool(await self._notifier(key[1], text))
        except Exception:
            logger.warning("Turn-age watchdog notifier raised: chat=%s", key[1])
            return False


__all__ = [
    "DEFAULT_NOTIFY_MINUTES",
    "DEFAULT_RENOTIFY_SECONDS",
    "DEFAULT_TICK_SECONDS",
    "TurnAgeWatchdog",
    "turn_age_text",
]
