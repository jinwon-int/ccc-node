"""Silent-death stall probe + orphan tool-call loop detection (#1112).

Both arms report or recover — neither limits work:

1. **Stall probe** — an active turn whose rollout file has not moved for
   ``CCC_TURN_STALL_PROBE_MIN`` minutes triggers an app-server liveness
   check. Recovery runs ONLY on a confirmed-dead engine (spawned process
   exited). A quiet but alive turn is never touched, and an ambiguous
   liveness verdict only gets logged — never recovered on a guess
   (fail-closed, per the work-continuity guard).
2. **Orphan tool-call loop** — the app-server stderr drain counts
   ``Custom tool call output is missing`` occurrences (2026-08-14: 15s
   interval repeats for 13 minutes with no surfacing). The count is exported
   as a health signal; the alerts probe owns the one-per-cooldown alert.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable, Deque, List, Optional, Tuple

logger = logging.getLogger(__name__)

ORPHAN_LOOP_PATTERN = "Custom tool call output is missing"
ORPHAN_LOOP_WINDOW_SECONDS = 300.0
ORPHAN_LOOP_THRESHOLD = 10

DEFAULT_TICK_SECONDS = 60.0
DEFAULT_REPROBE_SECONDS = 10 * 60.0


class OrphanLoopTracker:
    """Sliding-window counter for the orphan tool-call stderr pattern."""

    def __init__(
        self,
        *,
        window_seconds: float = ORPHAN_LOOP_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = float(window_seconds)
        self._clock = clock
        self._events: Deque[float] = deque()

    def _trim(self, now: float) -> None:
        while self._events and now - self._events[0] > self._window:
            self._events.popleft()

    def record_line(self, text: str, *, now: Optional[float] = None) -> int:
        """Count one stderr line; returns the in-window pattern count."""
        now = self._clock() if now is None else float(now)
        if ORPHAN_LOOP_PATTERN in text:
            self._events.append(now)
        self._trim(now)
        return len(self._events)

    def recent_count(self, *, now: Optional[float] = None) -> int:
        now = self._clock() if now is None else float(now)
        self._trim(now)
        return len(self._events)

    def reset(self) -> None:
        self._events.clear()


#: Process-wide sink the app-server stderr drain feeds; the health probe reads.
orphan_tool_loop_tracker = OrphanLoopTracker()


# --- stall probe -------------------------------------------------------------

TurnsProvider = Callable[[], List[Tuple[int, int, Optional[str], float]]]
LivenessProbe = Callable[[], str]  # "alive" | "dead" | "unknown"
RecoverCallback = Callable[[], Awaitable[None]]
NotifierCallback = Callable[[int, str], Awaitable[bool]]


def find_rollout(sessions_roots: List[Path], thread_id: str) -> Optional[Path]:
    """Newest rollout file for a codex thread across the given CODEX_HOMEs.

    Rollout filenames end with the thread/session id
    (``rollout-<timestamp>-<thread_id>.jsonl``). Any stat/glob error reads as
    "not found" — the probe then simply does nothing this tick.
    """
    if not thread_id:
        return None
    best: Optional[Path] = None
    best_mtime = -1.0
    for root in sessions_roots:
        sessions_dir = Path(root) / "sessions"
        try:
            candidates = sessions_dir.glob(f"**/rollout-*-{thread_id}.jsonl")
            for candidate in candidates:
                try:
                    mtime = candidate.stat().st_mtime
                except OSError:
                    continue
                if mtime > best_mtime:
                    best = candidate
                    best_mtime = mtime
        except OSError:
            continue
    return best


def engine_dead_notification_text(minutes: int) -> str:
    return (
        f"⚠️ The agent engine died silently — no turn output for {minutes} min. "
        "Dead-session recovery ran; queued work resumes through the normal "
        "path. Re-issue the last step if the turn does not restart."
    )


class StallProbeMonitor:
    """Probe stalled turns; recover only on a confirmed-dead engine."""

    def __init__(
        self,
        *,
        turns_provider: TurnsProvider,
        liveness_probe: LivenessProbe,
        recover: RecoverCallback,
        notifier: NotifierCallback,
        sessions_roots: List[Path],
        stall_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        reprobe_seconds: float = DEFAULT_REPROBE_SECONDS,
    ) -> None:
        self._turns_provider = turns_provider
        self._liveness_probe = liveness_probe
        self._recover = recover
        self._notifier = notifier
        self._sessions_roots = list(sessions_roots)
        self._stall_seconds = float(stall_seconds)
        self._clock = clock
        self._wall_clock = wall_clock
        self._tick_seconds = float(tick_seconds)
        self._reprobe_seconds = float(reprobe_seconds)
        self._probed: dict[Tuple[int, int], float] = {}

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("Turn-stall probe tick failed (continuing)")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        if self._stall_seconds <= 0:
            return
        now = self._clock()
        wall_now = self._wall_clock()
        for user_id, chat_id, thread_id, _started_at in self._turns_provider():
            key = (int(user_id), int(chat_id))
            last = self._probed.get(key)
            if last is not None and now - last < self._reprobe_seconds:
                continue
            rollout = find_rollout(self._sessions_roots, thread_id or "")
            if rollout is None:
                continue
            silent_for = wall_now - rollout.stat().st_mtime
            if silent_for < self._stall_seconds:
                continue
            self._probed[key] = now
            verdict = self._liveness_probe()
            if verdict == "alive":
                # A genuinely long turn: by the continuity guard, nothing
                # happens — not even a notification (that is PR-4's job).
                logger.info(
                    "Turn stall probe: engine alive, turn just quiet (user=%s chat=%s)",
                    user_id,
                    chat_id,
                )
                continue
            if verdict != "dead":
                # Ambiguous liveness: never recover on a guess (fail-closed).
                logger.warning(
                    "Turn stall probe: liveness unknown, no recovery (user=%s chat=%s)",
                    user_id,
                    chat_id,
                )
                continue
            logger.warning(
                "Turn stall probe: engine confirmed dead after %ds silence "
                "(user=%s chat=%s) — recovering",
                int(silent_for),
                user_id,
                chat_id,
            )
            await self._notify(chat_id, engine_dead_notification_text(int(silent_for // 60)))
            try:
                await self._recover()
            except Exception:
                logger.exception("Turn-stall recovery failed (user=%s)", user_id)

    async def _notify(self, chat_id: int, text: str) -> None:
        try:
            delivered = await self._notifier(chat_id, text)
        except Exception:
            delivered = False
        if not delivered:
            logger.warning("Turn-stall notification failed: chat=%s", chat_id)


__all__ = [
    "DEFAULT_REPROBE_SECONDS",
    "DEFAULT_TICK_SECONDS",
    "ORPHAN_LOOP_PATTERN",
    "ORPHAN_LOOP_THRESHOLD",
    "ORPHAN_LOOP_WINDOW_SECONDS",
    "OrphanLoopTracker",
    "StallProbeMonitor",
    "engine_dead_notification_text",
    "find_rollout",
    "orphan_tool_loop_tracker",
]
