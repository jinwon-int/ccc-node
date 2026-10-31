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
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Optional, Protocol, Tuple

from telegram_bot.core.external_wait import (
    SOURCE_GITHUB_ACTIONS_RUN,
    SOURCE_GITHUB_MERGE_QUEUE,
    SOURCE_GITHUB_PR_CHECKS,
    UNPOLLED_SOURCES,
    TERMINAL_CANCELLED,
    TERMINAL_CLOSED,
    TERMINAL_EVICTED,
    TERMINAL_EXPIRED,
    TERMINAL_FAILURE,
    TERMINAL_MERGED,
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
#: A merge-queue wait whose PR was never seen in the queue is only judged
#: evicted after this grace (the agent may register a moment before GitHub
#: shows the entry); afterwards it ends ``evicted`` with reason never-enqueued.
NOT_ENQUEUED_GRACE_SECONDS = 180.0
#: Merge-queue polls back off to this ceiling (group runs take minutes).
MERGE_QUEUE_MAX_POLL_INTERVAL_SECONDS = 120.0
#: Actions-run polls back off to this ceiling (#2590): builds and release
#: uploads take minutes, a 5-minute ceiling would add most of a poll to every
#: wake.
ACTIONS_RUN_MAX_POLL_INTERVAL_SECONDS = 120.0
#: Live run statuses (#2590). ``waiting`` is an environment approval gate
#: (deployment review) — NOT terminal: the run continues once approved.
ACTIONS_RUN_PENDING_STATUSES = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})
#: Completed-run conclusions mapped to a terminal status. ``neutral`` is a
#: deliberate non-failure; everything not listed (including unknown future
#: conclusions) is treated as a failure so a promise never silently reports
#: green for a run that did not succeed.
ACTIONS_RUN_SUCCESS_CONCLUSIONS = frozenset({"success", "neutral"})
ACTIONS_RUN_CANCELLED_CONCLUSIONS = frozenset({"cancelled"})
#: When the base branch's required check set cannot be read, a green rollup
#: must repeat on this many consecutive polls before it counts (#2507): the
#: first poll after a push often sees only the fast CodeQL checks.
UNKNOWN_REQUIRED_SETTLE_POLLS = 2
#: Required-context sets change rarely; re-read them at most this often.
REQUIRED_CONTEXTS_TTL_SECONDS = 600.0

_MERGE_QUEUE_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
    "{pullRequest(number:$number){state headRefOid isInMergeQueue}}}"
)


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
    #: False when the base branch's required check set could not be read; a
    #: success is then only provisional and must settle (#2507).
    required_known: bool = True


@dataclass(frozen=True, slots=True)
class MergeQueueState:
    """Body-free merge-queue picture for one PR (#2118)."""

    head_sha: str
    pr_state: str  # OPEN | MERGED | CLOSED
    in_queue: bool


@dataclass(frozen=True, slots=True)
class ActionsRunState:
    """Body-free picture of one GitHub Actions workflow run (#2590)."""

    status: str  # queued | in_progress | waiting | requested | pending | completed
    conclusion: str  # "" until completed; success | failure | cancelled | …
    head_sha: str = ""


class WaitTransport(Protocol):
    async def fetch_pr_state(self, repo: str, pr_number: int) -> PrState: ...


class ActionsRunTransport(Protocol):
    async def fetch_actions_run(self, repo: str, run_id: int) -> ActionsRunState: ...


class MergeQueueTransport(Protocol):
    async def fetch_merge_queue_state(self, repo: str, pr_number: int) -> MergeQueueState: ...

    async def fetch_failed_queue_run(self, repo: str, pr_number: int) -> Optional[int]: ...


async def _run_gh(*args: str, timeout: float = 60) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            "gh",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        raise TransportError("gh-unavailable")
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        raise TransportError("gh-timeout")
    if process.returncode != 0:
        raise TransportError(_classify_gh_error(stderr))
    return stdout


class GhCliTransport:
    """Production transport: authenticated ``gh`` CLI, body-free parsing."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._required_cache: Dict[Tuple[str, str], Tuple[float, Optional[FrozenSet[str]]]] = {}

    async def required_contexts(self, repo: str, base: str) -> Optional[FrozenSet[str]]:
        """Required status contexts of ``base``: classic protection + rulesets.

        Mirrors ``skills/gh-pr-flow/ci-watch.sh`` (#2200). ``None`` means the
        set could not be read at all (both sources failed or were empty), so
        the caller must not treat "nothing pending" as complete (#2507).
        """
        key = (repo, base)
        now = self._clock()
        cached = self._required_cache.get(key)
        if cached is not None and now - cached[0] < REQUIRED_CONTEXTS_TTL_SECONDS:
            return cached[1]
        names: set = set()
        readable = False
        try:
            classic = json.loads((await _run_gh("api", f"repos/{repo}/branches/{base}")).decode("utf-8", "replace"))
            protection = classic.get("protection") if isinstance(classic, dict) else None
            checks = (protection or {}).get("required_status_checks") or {}
            for name in checks.get("contexts") or []:
                if isinstance(name, str) and name:
                    names.add(name)
            readable = True
        except (TransportError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            pass
        try:
            rules = json.loads((await _run_gh("api", f"repos/{repo}/rules/branches/{base}")).decode("utf-8", "replace"))
            for rule in rules if isinstance(rules, list) else []:
                if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
                    continue
                for item in (rule.get("parameters") or {}).get("required_status_checks") or []:
                    name = item.get("context") if isinstance(item, dict) else None
                    if isinstance(name, str) and name:
                        names.add(name)
            readable = True
        except (TransportError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            pass
        result: Optional[FrozenSet[str]] = frozenset(names) if readable and names else None
        self._required_cache[key] = (now, result)
        return result

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
                "headRefOid,statusCheckRollup,baseRefName",
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
        base = str(payload.get("baseRefName") or "").strip()
        required = await self.required_contexts(repo, base) if base else None
        return PrState(
            head_sha=head_sha,
            rollup=_normalize_rollup(payload, required),
            required_known=required is not None,
        )

    async def fetch_merge_queue_state(self, repo: str, pr_number: int) -> MergeQueueState:
        owner, name = repo.split("/", 1)
        stdout = await _run_gh(
            "api",
            "graphql",
            "-f",
            f"query={_MERGE_QUEUE_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={int(pr_number)}",
        )
        try:
            payload = json.loads(stdout.decode("utf-8", "replace"))
            pr = payload["data"]["repository"]["pullRequest"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            raise TransportError("malformed-response")
        return _merge_queue_state_from(pr)

    async def fetch_failed_queue_run(self, repo: str, pr_number: int) -> Optional[int]:
        """Newest failed ``merge_group`` run for this PR's queue branch, if any."""
        stdout = await _run_gh(
            "run",
            "list",
            "--repo",
            repo,
            "--event",
            "merge_group",
            "--limit",
            "30",
            "--json",
            "databaseId,headBranch,conclusion",
        )
        try:
            runs = json.loads(stdout.decode("utf-8", "replace"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TransportError("malformed-response")
        return _failed_queue_run_id(runs, pr_number)


    async def fetch_actions_run(self, repo: str, run_id: int) -> ActionsRunState:
        """One run's status/conclusion via ``gh api`` (#2590), body-free."""
        stdout = await _run_gh("api", f"repos/{repo}/actions/runs/{int(run_id)}")
        try:
            payload = json.loads(stdout.decode("utf-8", "replace"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TransportError("malformed-response")
        return _actions_run_state_from(payload)


def _actions_run_state_from(payload: Any) -> ActionsRunState:
    if not isinstance(payload, dict):
        raise TransportError("malformed-response")
    status = str(payload.get("status") or "").strip().lower()
    conclusion = str(payload.get("conclusion") or "").strip().lower()
    if not status or len(status) > 24 or len(conclusion) > 24:
        raise TransportError("malformed-response")
    head_sha = str(payload.get("head_sha") or "").strip().lower()
    return ActionsRunState(status=status, conclusion=conclusion, head_sha=head_sha)


def actions_run_terminal(state: ActionsRunState) -> Optional[str]:
    """Terminal status for a run, or ``None`` while it is still going (#2590)."""
    if state.status != "completed":
        # ``waiting`` (environment approval) and unknown live statuses keep
        # the watch open; expiry still bounds a run nobody approves.
        return None
    if state.conclusion in ACTIONS_RUN_SUCCESS_CONCLUSIONS:
        return TERMINAL_SUCCESS
    if state.conclusion in ACTIONS_RUN_CANCELLED_CONCLUSIONS:
        return TERMINAL_CANCELLED
    return TERMINAL_FAILURE


def _merge_queue_state_from(pr: Any) -> MergeQueueState:
    if not isinstance(pr, dict):
        raise TransportError("malformed-response")
    head_sha = str(pr.get("headRefOid") or "").strip().lower()
    pr_state = str(pr.get("state") or "").upper()
    if not head_sha or pr_state not in {"OPEN", "MERGED", "CLOSED"}:
        raise TransportError("malformed-response")
    return MergeQueueState(head_sha=head_sha, pr_state=pr_state, in_queue=pr.get("isInMergeQueue") is True)


def _failed_queue_run_id(runs: Any, pr_number: int) -> Optional[int]:
    """``gh run list`` is newest-first; the queue branch embeds ``/pr-<n>-``."""
    if not isinstance(runs, list):
        return None
    marker = f"/pr-{int(pr_number)}-"
    for run in runs:
        if not isinstance(run, dict):
            continue
        branch = str(run.get("headBranch") or "")
        if not branch.startswith("gh-readonly-queue/") or marker not in branch:
            continue
        if str(run.get("conclusion") or "").lower() not in {
            "failure",
            "timed_out",
            "cancelled",
            "startup_failure",
        }:
            continue
        run_id = run.get("databaseId")
        if isinstance(run_id, int) and run_id > 0:
            return run_id
    return None


def _classify_gh_error(stderr: bytes) -> str:
    """Body-free error class: never leak stderr payloads into logs/records."""
    text = stderr.decode("utf-8", "replace")[:400].lower()
    if "rate limit" in text or "ratelimit" in text:
        return "rate-limit"
    if "not found" in text or "404" in text:
        return "not-found"
    if "auth" in text or "login" in text or "403" in text or "401" in text:
        return "auth"
    return "gh-error"


def _normalize_rollup(payload: Dict[str, Any], required: Optional[FrozenSet[str]] = None) -> str:
    checks = payload.get("statusCheckRollup")
    if not isinstance(checks, list) or not checks:
        # No registered checks yet: still in-flight for our purposes.
        return ROLLUP_PENDING
    states = []
    names = set()
    for entry in checks:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("context")
        if isinstance(name, str) and name:
            names.add(name)
        conclusion = str(entry.get("conclusion") or "").upper()
        status = str(entry.get("status") or "").upper()
        # Legacy commit statuses (StatusContext) report only ``state``; a
        # pending one used to be dropped and read as green (#2507).
        legacy = str(entry.get("state") or "").upper()
        if conclusion:
            states.append(conclusion)
        elif status:
            states.append(status)
        elif legacy:
            states.append(legacy)
        else:
            states.append("")
    if any(state in {"FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"} for state in states):
        return ROLLUP_FAILURE
    if any(state == "CANCELLED" for state in states):
        return ROLLUP_CANCELLED
    if any(state in {"", "PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED"} for state in states):
        return ROLLUP_PENDING
    # A required context that has not registered yet means its workflow has
    # not started: the visible checks are only the fast ones (#2507, #2200).
    if required and not required.issubset(names):
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
    TERMINAL_MERGED: "✅ Merged via merge queue",
    TERMINAL_EVICTED: "❌ Dropped from merge queue",
    TERMINAL_CLOSED: "⚠️ PR closed without merging",
}

#: Actions-run wording (#2590); terminals not listed fall back to the CI map.
_ACTIONS_RUN_HEADLINE = {
    TERMINAL_SUCCESS: "✅ Actions run succeeded",
    TERMINAL_FAILURE: "❌ Actions run failed",
    TERMINAL_CANCELLED: "⚠️ Actions run cancelled",
    TERMINAL_EXPIRED: "⏰ Actions run watch expired",
    TERMINAL_MONITOR_ERROR: "⚠️ Actions run watch failed (GitHub read error)",
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
    if record.get("source") == SOURCE_GITHUB_ACTIONS_RUN:
        headline = _ACTIONS_RUN_HEADLINE.get(record.get("terminal_status")) or _WAKE_HEADLINE.get(
            record.get("terminal_status"), "ℹ️ Actions run watch ended"
        )
    else:
        headline = _WAKE_HEADLINE.get(record.get("terminal_status"), "ℹ️ CI watch ended")
    ref = _wait_ref(record)
    lines = [f"{headline} — {ref}"]
    detail_line = _detail_text(record)
    if detail_line:
        lines.append(detail_line)
    summary = str(record.get("summary") or "").strip()
    if summary:
        lines.append(f"Next: {summary}")
    if resumed:
        lines.append("Continuing automatically.")
    else:
        why = _SKIP_REASON_TEXT.get(skip_reason or "", "auto-resume did not run")
        lines.append(f"NOT continued — {why}. Reply to continue.")
    return "\n".join(lines)


def _wait_ref(record: Dict[str, Any]) -> str:
    """``repo#pr @ head`` — or ``repo Actions run <id> (<label>)`` (#2590)."""
    sha = str(record.get("head_sha") or "")[:8]
    if record.get("source") == SOURCE_GITHUB_ACTIONS_RUN:
        label = str(record.get("run_label") or "").strip()
        number = record.get("run_number")
        ref = f"{record.get('repo')} Actions run {record.get('run_id')}"
        if label:
            ref += f" ({label}{f' #{number}' if number else ''})"
        return f"{ref} @ {sha}" if sha else ref
    return f"{record.get('repo')}#{record.get('pr_number')} @ {sha}"


def _detail_text(record: Dict[str, Any]) -> str:
    detail = record.get("terminal_detail")
    if not isinstance(detail, dict):
        return ""
    if detail.get("conclusion"):
        return f"Run conclusion: {detail['conclusion']}"
    if detail.get("failed_run_id"):
        return f"Failed merge-group run: {detail['failed_run_id']}"
    if detail.get("reason") == "never-enqueued":
        return "The PR was never seen in the merge queue."
    return ""


def resume_prompt_text(record: Dict[str, Any]) -> str:
    """Bridge-owned continuation input, clearly not user-authored (#740)."""
    summary = str(record.get("summary") or "").strip() or "continue the promised next step"
    source = str(record.get("source") or SOURCE_GITHUB_PR_CHECKS)
    extra = ""
    detail = record.get("terminal_detail")
    if isinstance(detail, dict):
        if detail.get("failed_run_id"):
            extra += f" failed_run={detail['failed_run_id']}"
        if detail.get("reason"):
            extra += f" reason={detail['reason']}"
        if detail.get("conclusion"):
            extra += f" conclusion={detail['conclusion']}"
    if source == SOURCE_GITHUB_ACTIONS_RUN:
        target = f"run={record.get('run_id')}"
    else:
        target = f"pr={record.get('pr_number')}"
    return (
        f"[external_event: {source} terminal={record.get('terminal_status')} "
        f"repo={record.get('repo')} {target} "
        f"head={str(record.get('head_sha') or '')[:8]}{extra}]\n{summary}"
    )


NotifierCallback = Callable[[int, str], Awaitable[bool]]
ResumerCallback = Callable[[Dict[str, Any], str], Awaitable[bool]]
SessionLookup = Callable[[int, int], Awaitable[Optional[str]]]
#: ``(user_id, chat_id)`` -> refresh that route's wait status message (#2081).
StatusSyncer = Callable[[int, int], Awaitable[None]]


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
        status_syncer: Optional[StatusSyncer] = None,
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
        self._status_syncer = status_syncer
        self._resume_enabled = resume_enabled
        self._resume_daily_cap = max(0, int(resume_daily_cap))
        self._tick_seconds = max(1.0, float(tick_seconds))
        self._clock = clock
        self._transport_errors: Dict[str, int] = {}
        self._provisional_greens: Dict[str, int] = {}
        self._resume_count = 0
        self._resume_day = ""
        self._idle_identity: Optional[tuple] = None

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
        # Idle fast path: a previous tick proved the registry holds no
        # monitoring record and no pending wake. Time alone cannot create
        # work in that state, so until the file identity changes (any
        # register/mutate rewrites it atomically), the tick is one os.stat
        # instead of two full read+parse passes — this loop runs every 5s
        # forever, and the registry is empty most of a node's life.
        idle = getattr(self._registry, "idle_snapshot_identity", None)
        if idle is not None and self._idle_identity is not None:
            if self._idle_identity == self._registry.state_file_identity():
                return
        for record in self._registry.due(now=self._clock()):
            await self._poll_one(record)
        for record in self._registry.pending_wakes():
            await self._deliver_wake(record)
        self._idle_identity = idle() if idle is not None else None

    # -- polling ------------------------------------------------------------------
    async def _poll_one(self, record: Dict[str, Any]) -> None:
        wait_id = record["wait_id"]
        now = self._clock()
        if record.get("source") in UNPOLLED_SOURCES:
            # A local-task wait (#2605) has no transport: the harness
            # continues the session itself and the task tracker closes the
            # record. The monitor only sees it at its deadline, and a deadline
            # must not resume the conversation a second time — expire without
            # a wake and refresh the route's status line.
            if now >= float(record.get("expires_epoch") or 0):
                if self._registry.finish(wait_id, TERMINAL_EXPIRED, now=now, wake=False):
                    await self._sync_status(record)
            else:
                self._registry.reschedule(
                    wait_id,
                    next_poll_epoch=float(record.get("expires_epoch") or now),
                    poll_interval_seconds=float(record.get("poll_interval_seconds") or 5.0),
                )
            return
        if now >= float(record.get("expires_epoch") or 0):
            self._registry.finish(wait_id, TERMINAL_EXPIRED, now=now)
            return
        if record.get("source") == SOURCE_GITHUB_MERGE_QUEUE:
            await self._poll_merge_queue(record, now)
            return
        if record.get("source") == SOURCE_GITHUB_ACTIONS_RUN:
            await self._poll_actions_run(record, now)
            return
        try:
            state = await self._transport.fetch_pr_state(record["repo"], int(record["pr_number"]))
        except TransportError as exc:
            self._transport_failed(record, exc, now)
            return
        self._transport_errors.pop(wait_id, None)
        recorded = str(record.get("head_sha") or "")
        if state.head_sha != recorded:
            if recorded and len(recorded) < 40 and state.head_sha.startswith(recorded):
                # Legacy short-SHA registration watching the same head: heal
                # the record to the full SHA instead of dropping the promise
                # as superseded (#961). A genuinely moved head still ends the
                # wait below.
                self._registry.correct_head_sha(wait_id, state.head_sha)
            else:
                # The PR moved on: never report the stale run as the watched one.
                self._registry.finish(wait_id, TERMINAL_SUPERSEDED, now=now)
                return
        if state.rollup == ROLLUP_SUCCESS and not state.required_known:
            # Required set unknown: one green poll may be the registration gap
            # (#2507). Count consecutive greens; anything else resets.
            greens = self._provisional_greens.get(wait_id, 0) + 1
            if greens < UNKNOWN_REQUIRED_SETTLE_POLLS:
                self._provisional_greens[wait_id] = greens
                self._reschedule(record)
                return
        self._provisional_greens.pop(wait_id, None)
        terminal = _TERMINAL_BY_ROLLUP.get(state.rollup)
        if terminal is not None:
            self._registry.finish(wait_id, terminal, now=now)
            return
        self._reschedule(record)

    def _transport_failed(self, record: Dict[str, Any], exc: TransportError, now: float) -> None:
        wait_id = record["wait_id"]
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

    async def _poll_merge_queue(self, record: Dict[str, Any], now: float) -> None:
        """Watch one queued PR until it lands, is dropped, or closes (#2118).

        An eviction changes neither the check rollup nor the PR state — the
        entry just disappears — so the queue membership itself is polled.
        """
        wait_id = record["wait_id"]
        repo = record["repo"]
        pr_number = int(record["pr_number"])
        fetch = getattr(self._transport, "fetch_merge_queue_state", None)
        if fetch is None:
            self._transport_failed(record, TransportError("unsupported-source"), now)
            return
        try:
            state = await fetch(repo, pr_number)
        except TransportError as exc:
            self._transport_failed(record, exc, now)
            return
        self._transport_errors.pop(wait_id, None)
        if state.pr_state == "MERGED":
            self._registry.finish(wait_id, TERMINAL_MERGED, now=now)
            return
        if state.pr_state == "CLOSED":
            self._registry.finish(wait_id, TERMINAL_CLOSED, now=now)
            return
        recorded = str(record.get("head_sha") or "")
        if state.head_sha != recorded:
            # A push removes the PR from the queue and needs a fresh approval;
            # the watched head will never land.
            self._registry.finish(wait_id, TERMINAL_SUPERSEDED, now=now)
            return
        if state.in_queue:
            if not record.get("queue_seen"):
                self._registry.mark_queue_seen(wait_id)
            self._reschedule(record, ceiling=MERGE_QUEUE_MAX_POLL_INTERVAL_SECONDS)
            return
        if not record.get("queue_seen"):
            if now - float(record.get("created_epoch") or now) < NOT_ENQUEUED_GRACE_SECONDS:
                self._reschedule(record, ceiling=MERGE_QUEUE_MAX_POLL_INTERVAL_SECONDS)
                return
            self._registry.finish(
                wait_id, TERMINAL_EVICTED, now=now, detail={"reason": "never-enqueued"}
            )
            return
        detail: Dict[str, Any] = {"reason": "dropped"}
        lookup = getattr(self._transport, "fetch_failed_queue_run", None)
        if lookup is not None:
            try:
                run_id = await lookup(repo, pr_number)
            except TransportError:
                run_id = None
            if run_id:
                detail["failed_run_id"] = int(run_id)
        self._registry.finish(wait_id, TERMINAL_EVICTED, now=now, detail=detail)

    async def _poll_actions_run(self, record: Dict[str, Any], now: float) -> None:
        """Watch one workflow run until it completes (#2590).

        ``waiting`` (an environment approval gate) stays open and is recorded
        as the run's live status so the wait-status line can say so; the
        status line is refreshed whenever that live status changes.
        """
        wait_id = record["wait_id"]
        fetch = getattr(self._transport, "fetch_actions_run", None)
        if fetch is None:
            self._transport_failed(record, TransportError("unsupported-source"), now)
            return
        try:
            state = await fetch(record["repo"], int(record["run_id"]))
        except TransportError as exc:
            self._transport_failed(record, exc, now)
            return
        except (KeyError, TypeError, ValueError):
            self._transport_failed(record, TransportError("malformed-record"), now)
            return
        self._transport_errors.pop(wait_id, None)
        terminal = actions_run_terminal(state)
        if terminal is not None:
            detail = {"conclusion": state.conclusion} if state.conclusion else None
            self._registry.finish(wait_id, terminal, now=now, detail=detail)
            return
        if self._registry.note_run_status(wait_id, state.status):
            refreshed = self._registry.get(wait_id) or record
            await self._sync_status(refreshed)
        self._reschedule(record, ceiling=ACTIONS_RUN_MAX_POLL_INTERVAL_SECONDS)

    def _reschedule(self, record: Dict[str, Any], *, ceiling: float = MAX_POLL_INTERVAL_SECONDS) -> None:
        interval = min(
            ceiling,
            float(record.get("poll_interval_seconds") or DEFAULT_POLL_INTERVAL_SECONDS) * 2,
        )
        self._registry.reschedule(
            record["wait_id"],
            next_poll_epoch=self._clock() + interval,
            poll_interval_seconds=interval,
        )

    # -- wake delivery -------------------------------------------------------------
    # ccc-side-effect: external_wait.wake_resume
    async def _deliver_wake(self, record: Dict[str, Any]) -> None:
        wait_id = record["wait_id"]
        skip_reason = await self._resume_skip_reason(record)
        if skip_reason is not None:
            delivered = await self._notify_wake(
                record,
                resumed=False,
                skip_reason=skip_reason,
            )
            self._registry.mark_wake(
                wait_id,
                delivered=delivered,
                resumed=False,
                skip_reason=skip_reason,
            )
            await self._sync_status(record)
            return

        # Notify the owner as soon as the terminal result is known.  A Codex
        # continuation can be admitted into an already-running native Goal
        # turn and then block for minutes; putting the resumer first made a
        # prompt CI poll look like a missing GitHub event (#740 follow-up).
        delivered = await self._notify_wake(record, resumed=True)
        await self._sync_status(record)
        if not delivered:
            # Do not run a continuation whose terminal notification did not
            # land.  Leave the wake pending so the next drain retries the
            # notification, then resumes exactly once after delivery.
            self._registry.mark_wake(wait_id, delivered=False)
            return

        resumed = await self._run_resume(record)
        if resumed:
            self._registry.mark_wake(wait_id, delivered=True, resumed=True)
            return

        # The first notification truthfully announced an attempt, not its
        # outcome.  Correct it promptly when the continuation could not run;
        # the durable ledger remains authoritative even if this best-effort
        # follow-up notification itself fails.
        await self._notify_wake(record, resumed=False, skip_reason="resume_failed")
        self._registry.mark_wake(
            wait_id,
            delivered=True,
            resumed=False,
            skip_reason="resume_failed",
        )

    async def _sync_status(self, record: Dict[str, Any]) -> None:
        """Refresh the route's status message after a terminal transition (#2081).

        Best-effort and strictly after the wake notification: the durable
        wake journal stays authoritative, a status hiccup never blocks it.
        """
        if self._status_syncer is None:
            return
        try:
            await self._status_syncer(int(record["user_id"]), int(record["chat_id"]))
        except Exception:
            logger.debug("External-wait status syncer raised: wait=%s", record.get("wait_id"))

    async def _notify_wake(
        self,
        record: Dict[str, Any],
        *,
        resumed: bool,
        skip_reason: Optional[str] = None,
    ) -> bool:
        try:
            return bool(
                await self._notifier(
                    int(record["chat_id"]),
                    wake_notification_text(record, resumed=resumed, skip_reason=skip_reason),
                )
            )
        except Exception:
            logger.warning("External-wait notifier raised: wait=%s", record["wait_id"])
            return False

    async def _resume_skip_reason(self, record: Dict[str, Any]) -> Optional[str]:
        """Return why auto-resume must not run, or ``None`` when eligible."""
        if not self._resume_enabled or self._resumer is None:
            return "resume_disabled"
        # Only a genuine terminal rollup continues the promised next step:
        # a superseded head, an expired watch, or a GitHub read failure says
        # nothing about the watched CI, so the owner decides instead (#740).
        if record.get("terminal_status") not in {
            TERMINAL_SUCCESS,
            TERMINAL_FAILURE,
            TERMINAL_CANCELLED,
            TERMINAL_MERGED,
            TERMINAL_EVICTED,
        }:
            return "non_terminal_rollup"
        summary = str(record.get("summary") or "").strip()
        if not summary:
            return "no_promise_recorded"  # nothing was promised
        day = time.strftime("%Y-%m-%d", time.gmtime(self._clock()))
        if not self._resume_budget_ok(day):
            logger.warning("External-wait resume daily cap reached; notification only")
            return "daily_cap"
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
                return "session_moved"
        return None

    async def _run_resume(self, record: Dict[str, Any]) -> bool:
        """Attempt one eligible continuation and account for a success."""
        assert self._resumer is not None
        try:
            resumed = await self._resumer(record, resume_prompt_text(record))
        except Exception:
            logger.warning("External-wait resumer raised: wait=%s", record["wait_id"])
            resumed = False
        if resumed:
            self._resume_count += 1
            return True
        return False


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_RESUME_DAILY_CAP",
    "DEFAULT_TICK_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "ExternalWaitMonitor",
    "GhCliTransport",
    "MAX_TRANSPORT_ERRORS",
    "MergeQueueState",
    "MergeQueueTransport",
    "NOT_ENQUEUED_GRACE_SECONDS",
    "PrState",
    "StatusSyncer",
    "TransportError",
    "WaitTransport",
    "resume_prompt_text",
    "wake_notification_text",
]
