"""GitHub CI watch loop for durable external waits (#740).

The monitor is deliberately small and bridge-owned:

- It polls the registry's due waits through an injectable transport (the
  production default shells out to the authenticated ``gh`` CLI; tests drive
  a fake), normalizing check state into pending / success / failure /
  cancelled rollups, always pinned to the registered exact head SHA.
- A terminal rollup is journaled in the registry *before* any wake
  (``finish``), and the wake itself is delivered through the journaled
  pending-wake drain — duplicate polls, restarts, and late events can never
  produce a second notification or continuation turn.
- The loop never touches the interactive turn FIFO: every wake goes through
  the caller-provided notifier/resumer callbacks, and GitHub read errors
  are bounded per-wait before failing closed with a body-free owner notice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol

from telegram_bot.core.external_wait import (
    TERMINAL_CANCELLED,
    TERMINAL_EXPIRED,
    TERMINAL_FAILURE,
    TERMINAL_MONITOR_ERROR,
    TERMINAL_SUPERSEDED,
    TERMINAL_SUCCESS,
    ExternalWaitRegistry,
)

logger = logging.getLogger(__name__)

ROLLUP_PENDING = "pending"
ROLLUP_SUCCESS = "success"
ROLLUP_FAILURE = "failure"
ROLLUP_CANCELLED = "cancelled"

DEFAULT_TICK_SECONDS = 5.0
DEFAULT_POLL_INTERVAL_SECONDS = 30.0
MAX_POLL_INTERVAL_SECONDS = 300.0
MAX_TRANSPORT_ERRORS = 3
DEFAULT_TIMEOUT_SECONDS = 6 * 60 * 60
DEFAULT_RESUME_DAILY_CAP = 10


class TransportError(Exception):
    """A body-free GitHub read failure (auth, rate-limit, network, shape)."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class PrState:
    """The normalized, body-free PR check picture for one exact head."""

    head_sha: str
    rollup: str  # pending | success | failure | cancelled


class WaitTransport(Protocol):
    async def fetch_pr_state(self, repo: str, pr_number: int) -> PrState: ...


class GhCliTransport:
    """Production transport: authenticated ``gh`` CLI, body-free parsing."""

    async def fetch_pr_state(self, repo: str, pr_number: int) -> PrState:
        try:
            process = await asyncio.create_subprocess_exec(
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "headRefOid,statusCheckRollup",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            raise TransportError("gh-unavailable")
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except TimeoutError:
            process.kill()
            raise TransportError("gh-timeout")
        if process.returncode != 0:
            raise TransportError(_classify_gh_error(stderr))
        try:
            payload = json.loads(stdout.decode("utf-8", "replace"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TransportError("malformed-response")
        head_sha = str(payload.get("headRefOid") or "").strip().lower()
        if not head_sha:
            raise TransportError("malformed-response")
        return PrState(head_sha=head_sha, rollup=_normalize_rollup(payload))


def _classify_gh_error(stderr: bytes) -> str:
    """Body-free error class: never leak stderr payloads into logs/records."""
    text = stderr.decode("utf-8", "replace")[:400].lower()
    if "rate limit" in text or "ratelimit" in text:
        return "rate-limit"
    if "auth" in text or "login" in text or "403" in text or "401" in text:
        return "auth"
    return "gh-error"


def _normalize_rollup(payload: Dict[str, Any]) -> str:
    checks = payload.get("statusCheckRollup")
    if not isinstance(checks, list) or not checks:
        # No registered checks yet: still in-flight for our purposes.
        return ROLLUP_PENDING
    states = []
    for entry in checks:
        if not isinstance(entry, dict):
            continue
        conclusion = str(entry.get("conclusion") or "").upper()
        status = str(entry.get("status") or "").upper()
        if conclusion:
            states.append(conclusion)
        elif status:
            states.append(status)
    if any(state in {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"} for state in states):
        return ROLLUP_FAILURE
    if any(state == "CANCELLED" for state in states):
        return ROLLUP_CANCELLED
    if any(state in {"", "PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"} for state in states):
        return ROLLUP_PENDING
    # SUCCESS / NEUTRAL / SKIPPED only.
    return ROLLUP_SUCCESS


_TERMINAL_BY_ROLLUP = {
    ROLLUP_SUCCESS: TERMINAL_SUCCESS,
    ROLLUP_FAILURE: TERMINAL_FAILURE,
    ROLLUP_CANCELLED: TERMINAL_CANCELLED,
}

_WAKE_HEADLINE = {
    TERMINAL_SUCCESS: "✅ CI green",
    TERMINAL_FAILURE: "❌ CI failed",
    TERMINAL_CANCELLED: "⚠️ CI cancelled",
    TERMINAL_SUPERSEDED: "🔀 Watched head moved",
    TERMINAL_EXPIRED: "⏰ CI watch expired",
    TERMINAL_MONITOR_ERROR: "⚠️ CI watch failed (GitHub read error)",
}


_SKIP_REASON_TEXT = {
    "session_moved": "the conversation moved to another session",
    "daily_cap": "the daily auto-resume cap is used up",
    "resume_disabled": "auto-resume is switched off",
    "non_terminal_rollup": "the result says nothing about the watched CI",
    "no_promise_recorded": "no next step was recorded",
    "resume_failed": "the continuation turn did not run",
}


def wake_notification_text(
    record: Dict[str, Any], *, resumed: bool, skip_reason: Optional[str] = None
) -> str:
    """Bounded, body-free owner notification for one terminal wait.

    A skipped continuation states itself. Previously the only signal was the
    *absence* of "Continuing automatically.", which reads exactly like a wake
    that did continue — so a dropped promise looked like a kept one.
    """
    headline = _WAKE_HEADLINE.get(record.get("terminal_status"), "ℹ️ CI watch ended")
    ref = f"{record.get('repo')}#{record.get('pr_number')} @ {str(record.get('head_sha') or '')[:8]}"
    lines = [f"{headline} — {ref}"]
    summary = str(record.get("summary") or "").strip()
    if summary:
        lines.append(f"Next: {summary}")
    if resumed:
        lines.append("Continuing automatically.")
    else:
        why = _SKIP_REASON_TEXT.get(skip_reason or "", "auto-resume did not run")
        lines.append(f"NOT continued — {why}. Reply to continue.")
    return "\n".join(lines)


def resume_prompt_text(record: Dict[str, Any]) -> str:
    """Bridge-owned continuation input, clearly not user-authored (#740)."""
    summary = str(record.get("summary") or "").strip() or "continue the promised next step"
    return (
        f"[external_event: github_pr_checks terminal={record.get('terminal_status')} "
        f"repo={record.get('repo')} pr={record.get('pr_number')} "
        f"head={str(record.get('head_sha') or '')[:8]}]\n{summary}"
    )


NotifierCallback = Callable[[int, str], Awaitable[bool]]
ResumerCallback = Callable[[Dict[str, Any], str], Awaitable[bool]]
SessionLookup = Callable[[int, int], Awaitable[Optional[str]]]


class ExternalWaitMonitor:
    """Bounded async watch loop over the durable registry."""

    def __init__(
        self,
        registry: ExternalWaitRegistry,
        *,
        transport: WaitTransport,
        notifier: NotifierCallback,
        resumer: Optional[ResumerCallback] = None,
        session_lookup: Optional[SessionLookup] = None,
        resume_enabled: bool = True,
        resume_daily_cap: int = DEFAULT_RESUME_DAILY_CAP,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._registry = registry
        self._transport = transport
        self._notifier = notifier
        self._resumer = resumer
        self._session_lookup = session_lookup
        self._resume_enabled = resume_enabled
        self._resume_daily_cap = max(0, int(resume_daily_cap))
        self._tick_seconds = max(1.0, float(tick_seconds))
        self._clock = clock
        self._transport_errors: Dict[str, int] = {}
        self._resume_count = 0
        self._resume_day = ""

    # -- configuration helpers ---------------------------------------------------
    @staticmethod
    def env_flag(name: str, *, default: bool) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return raw.strip().lower() not in {"0", "false", "no", "off"}

    @staticmethod
    def env_int(name: str, *, default: int, minimum: int = 0) -> int:
        try:
            return max(minimum, int(os.environ.get(name, "") or default))
        except ValueError:
            return default

    def _resume_budget_ok(self, day: str) -> bool:
        if self._resume_day != day:
            self._resume_day = day
            self._resume_count = 0
        return self._resume_count < self._resume_daily_cap

    # -- main loop ------------------------------------------------------------------
    async def run(self, stop_event: asyncio.Event) -> None:
        rearmed = self._registry.reconcile_on_start()
        if rearmed:
            logger.info("External-wait monitor re-armed %d wait(s) after restart", rearmed)
        while not stop_event.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("External-wait monitor tick failed (continuing)")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        for record in self._registry.due(now=self._clock()):
            await self._poll_one(record)
        for record in self._registry.pending_wakes():
            await self._deliver_wake(record)

    # -- polling ------------------------------------------------------------------
    async def _poll_one(self, record: Dict[str, Any]) -> None:
        wait_id = record["wait_id"]
        now = self._clock()
        if now >= float(record.get("expires_epoch") or 0):
            self._registry.finish(wait_id, TERMINAL_EXPIRED, now=now)
            return
        try:
            state = await self._transport.fetch_pr_state(record["repo"], int(record["pr_number"]))
        except TransportError as exc:
            errors = self._transport_errors.get(wait_id, 0) + 1
            self._transport_errors[wait_id] = errors
            logger.warning(
                "External-wait GitHub read failed (%s, attempt %d/%d): wait=%s",
                exc.kind,
                errors,
                MAX_TRANSPORT_ERRORS,
                wait_id,
            )
            if errors >= MAX_TRANSPORT_ERRORS:
                self._registry.finish(wait_id, TERMINAL_MONITOR_ERROR, now=now)
            else:
                self._reschedule(record)
            return
        self._transport_errors.pop(wait_id, None)
        if state.head_sha != record.get("head_sha"):
            # The PR moved on: never report the stale run as the watched one.
            self._registry.finish(wait_id, TERMINAL_SUPERSEDED, now=now)
            return
        terminal = _TERMINAL_BY_ROLLUP.get(state.rollup)
        if terminal is not None:
            self._registry.finish(wait_id, terminal, now=now)
            return
        self._reschedule(record)

    def _reschedule(self, record: Dict[str, Any]) -> None:
        interval = min(
            MAX_POLL_INTERVAL_SECONDS,
            float(record.get("poll_interval_seconds") or DEFAULT_POLL_INTERVAL_SECONDS) * 2,
        )
        self._registry.reschedule(
            record["wait_id"],
            next_poll_epoch=self._clock() + interval,
            poll_interval_seconds=interval,
        )

    # -- wake delivery -------------------------------------------------------------
    async def _deliver_wake(self, record: Dict[str, Any]) -> None:
        wait_id = record["wait_id"]
        resumed, skip_reason = await self._maybe_resume(record)
        try:
            delivered = await self._notifier(
                int(record["chat_id"]),
                wake_notification_text(record, resumed=resumed, skip_reason=skip_reason),
            )
        except Exception:
            logger.warning("External-wait notifier raised: wait=%s", wait_id)
            delivered = False
        self._registry.mark_wake(
            wait_id,
            delivered=bool(delivered),
            resumed=bool(resumed),
            skip_reason=skip_reason,
        )

    async def _maybe_resume(self, record: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """(resumed, skip_reason). The reason is what the owner has to act on.

        Every non-resuming path names itself: a silently-False return made a
        dropped promise look the same as a fulfilled one in both the ledger and
        the notification (#740 follow-up, 2026-07-30).
        """
        if not self._resume_enabled or self._resumer is None:
            return False, "resume_disabled"
        # Only a genuine terminal rollup continues the promised next step:
        # a superseded head, an expired watch, or a GitHub read failure says
        # nothing about the watched CI, so the owner decides instead (#740).
        if record.get("terminal_status") not in {
            TERMINAL_SUCCESS,
            TERMINAL_FAILURE,
            TERMINAL_CANCELLED,
        }:
            return False, "non_terminal_rollup"
        summary = str(record.get("summary") or "").strip()
        if not summary:
            return False, "no_promise_recorded"  # nothing was promised
        day = time.strftime("%Y-%m-%d", time.gmtime(self._clock()))
        if not self._resume_budget_ok(day):
            logger.warning("External-wait resume daily cap reached; notification only")
            return False, "daily_cap"
        registered_session = record.get("session_id")
        if registered_session and self._session_lookup is not None:
            try:
                current = await self._session_lookup(int(record["user_id"]), int(record["chat_id"]))
            except Exception:
                current = None
            if current != registered_session:
                # /new, provider switch, or a restarted conversation: never
                # inject a stale promise into the new session (#740).
                logger.info(
                    "External-wait resume skipped (session moved on): wait=%s",
                    record["wait_id"],
                )
                return False, "session_moved"
        try:
            resumed = await self._resumer(record, resume_prompt_text(record))
        except Exception:
            logger.warning("External-wait resumer raised: wait=%s", record["wait_id"])
            resumed = False
        if resumed:
            self._resume_count += 1
            return True, None
        return False, "resume_failed"


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_RESUME_DAILY_CAP",
    "DEFAULT_TICK_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "ExternalWaitMonitor",
    "GhCliTransport",
    "MAX_TRANSPORT_ERRORS",
    "PrState",
    "TransportError",
    "WaitTransport",
    "resume_prompt_text",
    "wake_notification_text",
]
