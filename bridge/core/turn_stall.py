"""Silent-death stall probe + orphan tool-call loop detection (#1112).

Both arms report or recover — neither limits work:

1. **Stall probe** — an active turn whose rollout file has not moved for
   ``CCC_TURN_STALL_PROBE_MIN`` minutes triggers an app-server liveness
   check. Recovery runs ONLY on a confirmed-dead engine (spawned process
   exited). A quiet but alive turn is never touched, and an ambiguous
   liveness verdict only gets logged — never recovered on a guess
   (fail-closed, per the work-continuity guard).

   Since #1741 the stall signal is provider-agnostic: a runtime adapter can
   register a :class:`TurnLivenessSource` — "has this turn's underlying
   process produced any new output/RPC activity in the last N minutes",
   carried as a monotonic last-activity timestamp — and the probe consults
   it before the Codex rollout-file fallback. A turn no source recognizes
   still resolves through the Codex rollout contract exactly as before, so
   a runtime without a registered liveness signal stays a fail-closed
   no-op, never a synthetic alive verdict.
2. **Orphan tool-call loop** — the app-server stderr drain counts
   ``Custom tool call output is missing`` occurrences (2026-08-14: 15s
   interval repeats for 13 minutes with no surfacing). The count is exported
   as a health signal; the alerts probe owns the one-per-cooldown alert.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable, Deque, List, Optional, Protocol, Sequence, Tuple

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

_LIVENESS_VERDICTS = frozenset({"alive", "dead", "unknown"})


class TurnLivenessSource(Protocol):
    """Provider-agnostic last-activity liveness for one runtime (#1741).

    Answers, for one active turn, "has this turn's underlying process
    produced any new output/RPC activity in the last N minutes?" via a
    monotonic last-activity timestamp, plus an engine verdict consulted
    only once the turn is already stale.

    Implementations must fail closed: an untracked session id reports
    ``None`` from :meth:`last_activity` (the probe then falls back to the
    Codex rollout contract for that turn), anything that cannot honestly
    classify the engine reports ``"unknown"`` from :meth:`engine_verdict`,
    and only a confirmed-dead engine may report ``"dead"`` — never a
    synthetic ``"alive"`` and never a recovery on a guess.
    """

    def last_activity(self, session_id: str) -> Optional[float]:
        """Monotonic timestamp of the session's last output/RPC activity.

        ``None`` when this source does not track ``session_id`` — no
        registered signal for that turn, so the probe must fail closed.
        """
        ...

    def engine_verdict(self, session_id: str) -> str:
        """``"alive" | "dead" | "unknown"`` for the turn's engine process."""
        ...


TurnLivenessBinding = Tuple[str, TurnLivenessSource]

# One slot per provider name: a later registration replaces the earlier one,
# so a source can only ever answer for the runtime that registered it last.
# Sources answer strictly from their own session records, and an untracked
# session id reads as "no signal" — a stale registration degrades to the
# fail-closed no-op, never to a false verdict.
_TURN_LIVENESS_SOURCES: dict[str, TurnLivenessBinding] = {}
_TURN_LIVENESS_LOCK = threading.Lock()


def register_turn_liveness(provider: str, source: TurnLivenessSource) -> None:
    """Register a runtime adapter's liveness source under its provider name."""
    with _TURN_LIVENESS_LOCK:
        _TURN_LIVENESS_SOURCES[provider] = (provider, source)


def unregister_turn_liveness(
    provider: str, source: TurnLivenessSource | None = None
) -> None:
    """Drop a provider's liveness registration (runtime closed).

    With ``source`` given, only a registration whose source is that exact
    object is removed, so closing an older runtime can never revoke a
    newer runtime's registration; the slot then simply stays with the
    newest owner.
    """
    with _TURN_LIVENESS_LOCK:
        binding = _TURN_LIVENESS_SOURCES.get(provider)
        if source is None or (binding is not None and binding[1] is source):
            _TURN_LIVENESS_SOURCES.pop(provider, None)


def registered_turn_liveness_sources() -> Tuple[TurnLivenessBinding, ...]:
    """Snapshot of the currently registered per-provider liveness sources."""
    with _TURN_LIVENESS_LOCK:
        return tuple(_TURN_LIVENESS_SOURCES.values())


def _normalize_verdict(verdict: object) -> str:
    """Honor only the three known verdicts; anything else reads "unknown"."""
    return verdict if verdict in _LIVENESS_VERDICTS else "unknown"


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
        turn_liveness_sources: Optional[Sequence[TurnLivenessBinding]] = None,
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
        # Explicit bindings win (tests, focused deployments); the default
        # consults the process-wide registry so adapter-registered sources
        # are picked up without touching the lifecycle builder.
        self._turn_liveness_sources: Optional[Tuple[TurnLivenessBinding, ...]] = (
            tuple(turn_liveness_sources) if turn_liveness_sources is not None else None
        )
        self._probed: dict[Tuple[int, int], float] = {}

    def _liveness_bindings(self) -> Tuple[TurnLivenessBinding, ...]:
        if self._turn_liveness_sources is not None:
            return self._turn_liveness_sources
        return registered_turn_liveness_sources()

    def _generic_activity(
        self, session_id: str
    ) -> Optional[Tuple[str, TurnLivenessSource, float]]:
        """First registered source that tracks ``session_id``, with timestamp.

        ``None`` when no source recognizes the turn — the caller then falls
        back to the Codex rollout contract. A source that raises fails
        closed: it is skipped rather than allowed to kill the whole tick.
        """
        for provider, source in self._liveness_bindings():
            try:
                last = source.last_activity(session_id)
            except Exception:
                logger.exception(
                    "Turn liveness source %s failed on last_activity; skipping",
                    provider,
                )
                continue
            if last is not None:
                return provider, source, float(last)
        return None

    def _generic_verdict(self, provider: str, source: TurnLivenessSource,
                         session_id: str) -> str:
        try:
            return _normalize_verdict(source.engine_verdict(session_id))
        except Exception:
            logger.exception(
                "Turn liveness source %s failed on engine_verdict; failing closed",
                provider,
            )
            return "unknown"

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
            session_id = thread_id or ""
            generic = self._generic_activity(session_id)
            if generic is not None:
                # Provider-agnostic arm (#1741): the adapter tracks this
                # turn's output/RPC activity on the monitor's clock.
                provider, source, last_activity = generic
                silent_for = now - last_activity
                if silent_for < self._stall_seconds:
                    continue
                verdict = self._generic_verdict(provider, source, session_id)
            else:
                # Codex arm: the rollout-file contract, unchanged.
                rollout = find_rollout(self._sessions_roots, session_id)
                if rollout is None:
                    continue
                silent_for = wall_now - rollout.stat().st_mtime
                if silent_for < self._stall_seconds:
                    continue
                verdict = self._liveness_probe()
            self._probed[key] = now
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
    "TurnLivenessBinding",
    "TurnLivenessSource",
    "engine_dead_notification_text",
    "find_rollout",
    "orphan_tool_loop_tracker",
    "register_turn_liveness",
    "registered_turn_liveness_sources",
    "unregister_turn_liveness",
]
