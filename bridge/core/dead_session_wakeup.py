"""Opt-in dead-session wakeup for orphaned Claude task notifications (#364 P2).

When a harness background task completes AFTER its owning session died
(bridge restart, ``/new``, model switch, reader crash), the Claude CLI still
durably enqueues a ``<task-notification>`` into the session transcript — but
nothing resumes the session, so the result reaches the user only when they
happen to send the next message. This module closes that gap behind an
explicit opt-in flag (``Config.dead_session_wakeup`` /
``CCC_DEAD_SESSION_WAKEUP``, default off = zero behavior change): it detects
conversations whose LAST persisted session has an *unconsumed* terminal task
notification and runs exactly one budget-gated autonomous turn so the CLI
dequeues and reports it through the normal delivery machinery.

Detection rule ("unconsumed", made precise):

* the notification is still in the transcript's replayed queue FIFO — an
  ``enqueue`` row without a matching later ``dequeue``/``remove`` (the CLI
  writes a ``dequeue`` row when it injects the notification into a turn), and
* it parses as a *terminal* task notification (same ``_task_notification``
  rule the recovery scanner uses), and
* its ``enqueue`` timestamp is strictly newer than the timestamp of the last
  ``assistant`` row in the transcript (or the transcript has no assistant
  row at all). A remaining FIFO entry that is *older* than later assistant
  output means the CLI ran a turn after the notification arrived and chose
  not to consume it (or the transcript forked) — waking the session for it
  would spend autonomous budget on an anomaly, so it is conservatively
  ignored. A pending entry without a parseable timestamp is likewise ignored.

Bounds and safety:

* the scan reuses the recovery loop's cadence (``wakeup_tick`` seam in
  ``run_periodic_dead_session_recovery``) — no second periodic loop;
* at most ``max_wakeups_per_scan`` turns per tick, a per-conversation
  cooldown, and a persisted per-session attempts cap: the attempt record is
  written to the SessionStore *before* the turn starts, so a wakeup that
  itself dies (process crash, SDK failure) cannot loop;
* the turn is metered as AUTONOMOUS in the usage meter and consults the #388
  budget gate first — an exhausted/enforced autonomous budget skips the
  wakeup with a log line and never blocks interactive traffic;
* quarantined transcripts (#424/#500) are never parsed or resumed here, and
  quarantine bookkeeping stays owned by the recovery scanner — this module is
  strictly read-only with respect to quarantine records.

Codex is out of scope by design and guarded via the same provider boundary as
the recovery scanner (``_uses_claude_transcript``): Codex background-task
semantics differ — the Codex app-server owns its own turn lifecycle and there
is no CLI transcript FIFO (``queue-operation`` rows) to replay, no JSONL
transcript to resume, and no equivalent "neutral continuation turn" contract.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from telegram_bot.core.dead_session_recovery import (
    DEFAULT_LOCK_TIMEOUT,
    DEFAULT_MAX_SESSIONS,
    DEFAULT_SEND_TIMEOUT,
    QUARANTINE_KEY,
    TranscriptQueueReplay,
    TranscriptRejected,
    _safe_transcript_path,
    _task_notification,
    _uses_claude_transcript,
    _valid_quarantine,
    _validated_session_id,
    parse_conversation_route,
    replay_transcript_queue,
)
from telegram_bot.core.session_resume import (
    persisted_transcript_exists,
    resume_persisted_enabled,
)
from telegram_bot.core.usage_meter import MODE_AUTONOMOUS

logger = logging.getLogger(__name__)

#: The one-turn resume prompt. The SDK requires a query to start a turn; the
#: CLI injects every pending queued item (including task notifications) as
#: synthetic user messages when that turn starts, so the minimal correct input
#: is a short, neutral, system-style instruction that (a) explains why the
#: session was resumed, (b) directs the model to report the injected results,
#: and (c) forbids opening new work — keeping the autonomous turn bounded.
WAKEUP_NUDGE = (
    "[bridge] This session was resumed because a background task finished "
    "while the session was inactive. Process the pending task notifications "
    "and report their results to the user. Do not start new work."
)

#: SessionStore key for the per-conversation wakeup attempt record.
WAKEUP_STATE_KEY = "dead_session_wakeup"

DEFAULT_COOLDOWN_SECONDS = 600.0
DEFAULT_MAX_ATTEMPTS_PER_SESSION = 2
#: One wakeup turn per scan tick keeps each tick cheap and naturally spreads
#: multiple eligible conversations across the (default 60s) scan interval.
DEFAULT_MAX_WAKEUPS_PER_SCAN = 1
DEFAULT_TURN_TIMEOUT_SECONDS = 600.0


@dataclass
class WakeupStats:
    scanned: int = 0
    triggered: int = 0
    delivered: int = 0
    failed: int = 0
    rejected: int = 0
    skipped_active: int = 0
    skipped_locked: int = 0
    skipped_quarantine: int = 0
    skipped_cooldown: int = 0
    skipped_attempts: int = 0
    skipped_budget: int = 0


@dataclass(frozen=True)
class WakeupCandidate:
    """Unconsumed terminal notifications found in one dead transcript."""

    count: int
    newest_enqueue_at: datetime


@dataclass(frozen=True)
class _WakeupPlan:
    storage_key: Any
    user_id: int
    chat_id: int
    session_id: str
    model: Optional[str]
    pending_count: int
    notification_age_seconds: float


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse one transcript ISO-8601 timestamp; ``None`` when unusable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def find_unconsumed_notifications(
    path: Path, session_id: str
) -> Optional[WakeupCandidate]:
    """Apply the module's unconsumed-notification rule to one transcript.

    Raises ``TranscriptRejected`` when the transcript fails the shared safety
    validation; returns ``None`` when nothing qualifies.
    """
    return unconsumed_candidate(replay_transcript_queue(Path(path), session_id))


def unconsumed_candidate(replay: TranscriptQueueReplay) -> Optional[WakeupCandidate]:
    """Apply the unconsumed-notification rule to one already-replayed FIFO."""
    last_assistant = _parse_timestamp(replay.last_assistant_timestamp)
    newest: Optional[datetime] = None
    count = 0
    for content, raw_timestamp in replay.pending:
        if _task_notification(content) is None:
            continue
        enqueued_at = _parse_timestamp(raw_timestamp)
        if enqueued_at is None:
            # Unknown enqueue time cannot be shown to be newer than the last
            # assistant output; skip rather than resume on an anomaly.
            continue
        if last_assistant is not None and enqueued_at <= last_assistant:
            continue
        count += 1
        if newest is None or enqueued_at > newest:
            newest = enqueued_at
    if count == 0 or newest is None:
        return None
    return WakeupCandidate(count=count, newest_enqueue_at=newest)


def _wakeup_state(record: Any, session_id: str) -> tuple[int, Optional[datetime]]:
    """Decode the persisted attempt record into ``(attempts, last_attempt_at)``.

    ``attempts`` counts only attempts against the *current* session id — a
    rotated session starts a fresh budget. ``last_attempt_at`` is returned for
    any recorded session so the cooldown is genuinely per-conversation.
    """
    if not isinstance(record, dict):
        return 0, None
    last_attempt_at = _parse_timestamp(record.get("last_attempt_at"))
    attempts_value = record.get("attempts")
    attempts = (
        attempts_value
        if isinstance(attempts_value, int) and not isinstance(attempts_value, bool)
        else 0
    )
    if record.get("session_id") != session_id:
        attempts = 0
    return max(0, attempts), last_attempt_at


def _live_agent_session_owned(handler: Any, user_id: int, chat_id: int) -> bool:
    """True when the runtime path holds a live session for this conversation.

    A live CLI process is tracked in ``_agent_sessions`` /
    ``_agent_active_sessions`` and will process its own notifications between
    turns (#601), so waking it from here would double-process.
    """
    route = getattr(handler, "_stream_key", None)
    key = route(user_id, chat_id) if callable(route) else (user_id, chat_id)
    if key in (getattr(handler, "_agent_active_sessions", None) or {}):
        return True
    return key in (getattr(handler, "_agent_sessions", None) or {})


def _live_conversation(handler: Any, user_id: int, chat_id: int) -> bool:
    return _live_agent_session_owned(handler, user_id, chat_id)


def recovery_should_defer_to_wakeup(
    project_handler: Any,
    current: Mapping[str, Any],
    session_id: str,
    replay: TranscriptQueueReplay,
    user_id: int,
    chat_id: int,
    *,
    usage_meter: Any = None,
    max_attempts_per_session: int = DEFAULT_MAX_ATTEMPTS_PER_SESSION,
) -> bool:
    """Should the P1 recovery raw replay defer to this wakeup feature? (#620)

    Wakeup-first with fallback: the recovery scanner calls this (only when
    ``CCC_DEAD_SESSION_WAKEUP`` is on) before raw-replaying a conversation's
    pending notifications, and skips the raw replay for that scan when the
    wakeup can still claim them itself — otherwise one stranded notification
    produces two user-facing messages (raw replay + autonomous report).

    True only while every claim precondition holds: resume is allowed, no
    live agent session owns the conversation, wakeup attempts remain for this
    session, the #388 autonomous budget permits the turn, and the replayed
    FIFO actually contains an unconsumed candidate under the wakeup detection
    rule. Every False answer keeps recovery's raw replay as the fail-safe
    fallback, so a notification can never become permanently undelivered:
    budget-blocked, attempts-exhausted (a failed wakeup turn consumes its
    attempt *before* running), or non-claimable conversations deliver raw
    exactly as before this feature existed.

    The cooldown window alone is deliberately NOT a fallback trigger:
    attempts remain and the wakeup will claim the notification once the
    cooldown expires, so recovery keeps deferring — bounded by the attempts
    cap, which durably increments before every wakeup turn.

    Quarantine needs no gate here: recovery only reaches its deferral point
    after the transcript parsed safely, having already lifted (and persisted)
    any stale quarantine record — and it disables deferral itself when that
    lift fails to persist.
    """
    if not resume_persisted_enabled():
        return False
    if _live_agent_session_owned(project_handler, user_id, chat_id):
        # The wakeup scan skips live conversations, so it would never claim
        # this one; keep recovery's behavior for live-adapter sessions as it
        # was before #620.
        return False
    attempts, _ = _wakeup_state(current.get(WAKEUP_STATE_KEY), session_id)
    if attempts >= max_attempts_per_session:
        return False
    if usage_meter is not None:
        # Mirror the wakeup's #388 gate, which fails closed on a meter error:
        # a wakeup that will not spend must not be deferred to.
        try:
            decision = usage_meter.check_autonomous_spend("claude")
        except Exception:
            return False
        if not decision.allowed:
            return False
    return unconsumed_candidate(replay) is not None


def _conversation_route(
    raw_key: Any, snapshot: Any, stats: WakeupStats
) -> Optional[tuple[Any, int, int, str]]:
    """Decode one persisted SessionStore entry into a validated Claude route."""
    try:
        storage_key, user_id, chat_id = parse_conversation_route(raw_key)
        if not isinstance(snapshot, dict):
            raise ValueError("invalid session entry")
        # Claude only: Codex sessions have no CLI transcript FIFO to replay
        # and no resume-with-neutral-turn contract (see module docstring).
        if not _uses_claude_transcript(snapshot):
            return None
        session_id = _validated_session_id(snapshot.get("session_id"))
    except (ValueError, TranscriptRejected):
        stats.rejected += 1
        return None
    return storage_key, user_id, chat_id, session_id


async def _current_claude_session(
    session_manager: Any, storage_key: Any, session_id: str, stats: WakeupStats
) -> Optional[dict]:
    """Re-read the session under the lock; ``None`` unless it is still ours."""
    try:
        current = await session_manager.get_session(storage_key)
    except Exception:
        stats.rejected += 1
        return None
    try:
        if not isinstance(current, dict) or not _uses_claude_transcript(current):
            return None
    except ValueError:
        stats.rejected += 1
        return None
    if current.get("session_id") != session_id:
        return None
    return current


def _passes_state_gates(
    *,
    current: dict,
    storage_key: Any,
    session_id: str,
    usage_meter: Any,
    stats: WakeupStats,
    cooldown_seconds: float,
    max_attempts_per_session: int,
    now: datetime,
) -> Optional[int]:
    """Quarantine, cooldown, attempts-cap and #388 budget gates.

    Returns the current attempt count when every gate passes, ``None`` (with
    the skip reason logged at INFO) otherwise.
    """
    if _valid_quarantine(current.get(QUARANTINE_KEY), session_id) is not None:
        stats.skipped_quarantine += 1
        logger.info(
            "Dead-session wakeup skipped for conversation %s: transcript "
            "quarantined (session=%s)",
            storage_key,
            session_id,
        )
        return None
    attempts, last_attempt_at = _wakeup_state(current.get(WAKEUP_STATE_KEY), session_id)
    if (
        last_attempt_at is not None
        and (now - last_attempt_at).total_seconds() < cooldown_seconds
    ):
        stats.skipped_cooldown += 1
        logger.info(
            "Dead-session wakeup skipped for conversation %s: cooldown "
            "(last attempt %.0fs ago < %.0fs)",
            storage_key,
            (now - last_attempt_at).total_seconds(),
            cooldown_seconds,
        )
        return None
    if attempts >= max_attempts_per_session:
        stats.skipped_attempts += 1
        logger.info(
            "Dead-session wakeup skipped for conversation %s: attempts cap "
            "reached (%d/%d for session=%s)",
            storage_key,
            attempts,
            max_attempts_per_session,
            session_id,
        )
        return None
    if usage_meter is not None:
        # #388 budget gate: enforce blocks AUTONOMOUS spend only. A meter
        # failure fails closed for this autonomous feature — interactive
        # traffic is untouched either way.
        try:
            decision = usage_meter.check_autonomous_spend("claude")
        except Exception:
            logger.warning(
                "Dead-session wakeup skipped for conversation %s: budget check failed",
                storage_key,
            )
            stats.failed += 1
            return None
        if not decision.allowed:
            stats.skipped_budget += 1
            logger.info(
                "Dead-session wakeup skipped for conversation %s: autonomous "
                "budget blocked (%s)",
                storage_key,
                decision.reason(),
            )
            return None
    return attempts


async def _detect_and_claim(
    *,
    session_manager: Any,
    conversations_dir: Path,
    storage_key: Any,
    user_id: int,
    chat_id: int,
    session_id: str,
    current: dict,
    attempts: int,
    max_attempts_per_session: int,
    stats: WakeupStats,
    now: datetime,
) -> Optional[_WakeupPlan]:
    """Scan the transcript and durably claim one wakeup attempt."""
    if not persisted_transcript_exists(conversations_dir, session_id):
        return None
    try:
        path = _safe_transcript_path(conversations_dir, session_id)
        candidate = await asyncio.to_thread(
            find_unconsumed_notifications, path, session_id
        )
    except TranscriptRejected as error:
        # Quarantine bookkeeping belongs to the recovery scanner; this module
        # only refuses to act on a transcript it cannot trust.
        stats.rejected += 1
        logger.warning(
            "Dead-session wakeup rejected transcript for conversation %s: %s",
            storage_key,
            error,
        )
        return None
    stats.scanned += 1
    if candidate is None:
        return None
    # Persist the attempt BEFORE running the turn: a wakeup that itself dies
    # must consume attempt budget, never loop.
    record = {
        "session_id": session_id,
        "attempts": attempts + 1,
        "last_attempt_at": now.isoformat().replace("+00:00", "Z"),
    }
    try:
        await session_manager.update_session(storage_key, {WAKEUP_STATE_KEY: record})
    except Exception:
        stats.failed += 1
        logger.warning(
            "Dead-session wakeup attempt record persistence failed for "
            "conversation %s; skipping wakeup",
            storage_key,
        )
        return None
    age_seconds = max(0.0, (now - candidate.newest_enqueue_at).total_seconds())
    logger.info(
        "Dead-session wakeup triggered for conversation %s: session=%s "
        "pending_notifications=%d newest_notification_age=%.0fs attempt=%d/%d",
        storage_key,
        session_id,
        candidate.count,
        age_seconds,
        attempts + 1,
        max_attempts_per_session,
    )
    model = current.get("model")
    return _WakeupPlan(
        storage_key=storage_key,
        user_id=user_id,
        chat_id=chat_id,
        session_id=session_id,
        model=model if isinstance(model, str) else None,
        pending_count=candidate.count,
        notification_age_seconds=age_seconds,
    )


async def _evaluate_conversation(
    *,
    session_manager: Any,
    project_handler: Any,
    conversations_dir: Path,
    raw_key: Any,
    snapshot: Any,
    usage_meter: Any,
    stats: WakeupStats,
    cooldown_seconds: float,
    max_attempts_per_session: int,
    lock_timeout: float,
    now: datetime,
) -> Optional[_WakeupPlan]:
    """Run every detection/safety gate for one conversation under its lock.

    Returns a plan only after the attempt record has been durably persisted;
    the caller runs the actual turn *outside* the conversation lock because
    ``process_message`` re-acquires it.
    """
    route = _conversation_route(raw_key, snapshot, stats)
    if route is None:
        return None
    storage_key, user_id, chat_id, session_id = route

    lock = project_handler._get_conversation_lock(user_id, chat_id)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=lock_timeout)
    except asyncio.TimeoutError:
        stats.skipped_locked += 1
        return None
    try:
        if _live_conversation(project_handler, user_id, chat_id):
            stats.skipped_active += 1
            return None
        current = await _current_claude_session(
            session_manager, storage_key, session_id, stats
        )
        if current is None:
            return None
        attempts = _passes_state_gates(
            current=current,
            storage_key=storage_key,
            session_id=session_id,
            usage_meter=usage_meter,
            stats=stats,
            cooldown_seconds=cooldown_seconds,
            max_attempts_per_session=max_attempts_per_session,
            now=now,
        )
        if attempts is None:
            return None
        return await _detect_and_claim(
            session_manager=session_manager,
            conversations_dir=conversations_dir,
            storage_key=storage_key,
            user_id=user_id,
            chat_id=chat_id,
            session_id=session_id,
            current=current,
            attempts=attempts,
            max_attempts_per_session=max_attempts_per_session,
            stats=stats,
            now=now,
        )
    finally:
        lock.release()


async def _run_wakeup_turn(
    *,
    bot: Any,
    project_handler: Any,
    plan: _WakeupPlan,
    stats: WakeupStats,
    turn_timeout: float,
    send_timeout: float,
) -> None:
    """Run the single autonomous turn and deliver its final content.

    The turn goes through the normal ``process_message`` path so mid-turn
    machinery (adapter unsolicited handler registration, ledger, metering)
    behaves exactly as for a user turn — but metered as AUTONOMOUS, and with
    ``notification_bot`` set so any between-turns continuation the CLI makes
    delivers via #601's unsolicited route. No approval callback is supplied on
    purpose: a wakeup turn must report existing results, not authorize new
    privileged work.
    """
    try:
        response = await asyncio.wait_for(
            project_handler.process_message(
                user_message=WAKEUP_NUDGE,
                user_id=plan.user_id,
                chat_id=plan.chat_id,
                session_id=plan.session_id,
                model=plan.model,
                new_session=False,
                notification_bot=bot,
                usage_mode=MODE_AUTONOMOUS,
            ),
            timeout=turn_timeout,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        stats.failed += 1
        logger.warning(
            "Dead-session wakeup turn failed for conversation %s: %s",
            plan.storage_key,
            type(error).__name__,
        )
        return
    stats.triggered += 1
    if not getattr(response, "success", False):
        stats.failed += 1
        logger.warning(
            "Dead-session wakeup turn returned an error for conversation %s: %s",
            plan.storage_key,
            getattr(response, "error", None),
        )
        return
    content = getattr(response, "content", "") or ""
    if not content or getattr(response, "streamed", False):
        return
    if len(content) > 4000:
        content = f"{content[:3960]}\n\n… (background result truncated)"
    try:
        await asyncio.wait_for(
            bot.send_message(chat_id=plan.chat_id, text=content),
            timeout=send_timeout,
        )
    except Exception as error:
        stats.failed += 1
        logger.warning(
            "Dead-session wakeup delivery failed for conversation %s: %s",
            plan.storage_key,
            type(error).__name__,
        )
        return
    stats.delivered += 1


async def run_dead_session_wakeup_scan(
    bot: Any,
    session_manager: Any,
    project_handler: Any,
    conversations_dir: Optional[Path],
    *,
    enabled: bool,
    usage_meter: Any = None,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    max_wakeups_per_scan: int = DEFAULT_MAX_WAKEUPS_PER_SCAN,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    max_attempts_per_session: int = DEFAULT_MAX_ATTEMPTS_PER_SESSION,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    turn_timeout: float = DEFAULT_TURN_TIMEOUT_SECONDS,
    send_timeout: float = DEFAULT_SEND_TIMEOUT,
    now: Optional[Callable[[], datetime]] = None,
) -> WakeupStats:
    """Scan persisted dead Claude sessions once and run bounded wakeup turns."""
    stats = WakeupStats()
    if not enabled:
        logger.debug(
            "Dead-session wakeup scan skipped: disabled (opt-in via "
            "CCC_DEAD_SESSION_WAKEUP)"
        )
        return stats
    if not conversations_dir:
        return stats
    if not resume_persisted_enabled():
        # Resuming a dead session against CCC_RESUME_PERSISTED_SESSIONS=false
        # would contradict the operator's explicit never-resume choice.
        logger.debug(
            "Dead-session wakeup scan skipped: CCC_RESUME_PERSISTED_SESSIONS "
            "is false"
        )
        return stats
    try:
        sessions = await session_manager.list_sessions()
    except Exception as error:
        logger.warning(
            "Dead-session wakeup could not enumerate sessions: %s",
            type(error).__name__,
        )
        stats.rejected += 1
        return stats
    if not isinstance(sessions, dict):
        stats.rejected += 1
        return stats

    clock = now or (lambda: datetime.now(timezone.utc))
    wakeups = 0
    for raw_key, snapshot in sorted(sessions.items(), key=lambda item: str(item[0]))[
        :max_sessions
    ]:
        if wakeups >= max_wakeups_per_scan:
            break
        plan = await _evaluate_conversation(
            session_manager=session_manager,
            project_handler=project_handler,
            conversations_dir=Path(conversations_dir),
            raw_key=raw_key,
            snapshot=snapshot,
            usage_meter=usage_meter,
            stats=stats,
            cooldown_seconds=cooldown_seconds,
            max_attempts_per_session=max_attempts_per_session,
            lock_timeout=lock_timeout,
            now=clock(),
        )
        if plan is None:
            continue
        wakeups += 1
        await _run_wakeup_turn(
            bot=bot,
            project_handler=project_handler,
            plan=plan,
            stats=stats,
            turn_timeout=turn_timeout,
            send_timeout=send_timeout,
        )
    return stats
