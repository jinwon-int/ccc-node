"""Fleet runtime health signals and threshold alerts (issue #389).

Detection-only: this module observes and reports, it never remediates.

Runtime signals are exported to ``health.json`` on every probe tick. The three
alert groups evaluated against configurable thresholds are:

1. **Heartbeat age vs request lifetime** — the oldest in-flight request's age
   compared to the configured process timeout. A request older than its own
   lifetime means the lifecycle leaked (the #307 class): nothing should outlive
   ``CLAUDE_PROCESS_TIMEOUT`` now that the terminal-stall guard (#411 C)
   releases silent turns much earlier.
2. **Pending / dropped notifications** — the push-notifier spool backlog plus
   the cumulative quarantined-transcript counter (notifications recovery gave
   up on, #411 B).
3. **Orphan children** — PPID-1 ``node claude`` processes from the read-only
   orphan probe (#303).

The legacy session-liveness signal (streams with a dead reader task) retired
with the direct Claude SDK stream path (#584 slice C-2); runtime-path session
death surfaces through turn errors and the recovery/wakeup scanners instead.
Body-free resident-session counts, process-tree RSS, guard evictions, Codex
attachments, and runtime recycles are exported for fleet diagnostics but do
not independently fire alerts: the resource guard owns bounded remediation.

Alert delivery reuses the owner-only push-notifier spool: alerts are written as
ordinary spool records, so the existing redaction, dedup, rate-limit, and the
``CCC_PUSH_ENABLED`` opt-in all apply. With push disabled (the default) alerts
surface only in logs and ``health.json`` — real Telegram delivery is a rollout
decision, exactly as #389 scopes it. Alert payloads carry only the node name,
signal code, and numeric values: never tokens, prompts, or filesystem paths.

Threshold rationale (documented for #389's acceptance):

- ``alert_heartbeat_age_factor`` (default 1.0): fire when the oldest in-flight
  request exceeds ``factor × CLAUDE_PROCESS_TIMEOUT``. Aligned with the request
  lifetime by construction, so the #307 "heartbeat outlives its request"
  regression is caught at the first multiple of the lifetime.
- ``alert_max_pending_notifications`` (default 10): the spool normally drains
  within seconds; a double-digit backlog means delivery is stuck, not busy.
- ``alert_max_orphan_children`` (default 1): the startup/periodic reaper keeps
  this at zero; any survivor indicates the reaper itself is not running.
"""

from __future__ import annotations

import json
import logging
import math
import platform
import socket
import ssl
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

DEFAULT_PROBE_INTERVAL_SECONDS = 60.0
DEFAULT_ALERT_COOLDOWN_SECONDS = 1800.0
MIN_PROBE_INTERVAL_SECONDS = 5.0
MAX_PROBE_INTERVAL_SECONDS = 3600.0


def probe_interval(value: Any, default: float = DEFAULT_PROBE_INTERVAL_SECONDS) -> float:
    """Resolve the probe interval defensively.

    A non-positive or unparsable configured interval must never reach the
    probe loop: ``asyncio.wait_for(..., timeout<=0)`` times out immediately and
    the loop would spin hot, hammering health.json and /proc every iteration.
    Invalid values fall back to the default; valid ones are clamped to
    [MIN_PROBE_INTERVAL_SECONDS, MAX_PROBE_INTERVAL_SECONDS].
    """
    try:
        interval = float(value)
    except (TypeError, ValueError):
        return default
    # NaN passes every comparison guard (all NaN comparisons are False) and
    # min/max propagate it, so wait_for(timeout=NaN) would still time out
    # immediately — reject every non-finite value outright (#430 review).
    if not math.isfinite(interval) or interval <= 0:
        return default
    return min(max(interval, MIN_PROBE_INTERVAL_SECONDS), MAX_PROBE_INTERVAL_SECONDS)


@dataclass(frozen=True)
class HealthSignals:
    """One probe tick's structured runtime-health snapshot."""

    active_requests: int = 0
    waiting_for_turn: int = 0
    oldest_request_age_seconds: float = 0.0
    request_lifetime_seconds: float = 0.0
    pending_notifications: int = 0
    dropped_notifications: int = 0
    orphan_children: int = 0
    resident_sessions: int = 0
    active_sessions: int = 0
    session_tree_rss_mb: float = 0.0
    session_guard_evictions: int = 0
    runtime_recycles: int = 0
    codex_attachments: int = 0
    orphan_tool_loop_recent: int = 0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["oldest_request_age_seconds"] = int(self.oldest_request_age_seconds)
        data["request_lifetime_seconds"] = int(self.request_lifetime_seconds)
        data["session_tree_rss_mb"] = int(self.session_tree_rss_mb)
        return data


@dataclass(frozen=True)
class AlertThresholds:
    heartbeat_age_factor: float = 1.0
    max_pending_notifications: int = 10
    max_orphan_children: int = 1
    max_orphan_tool_loop: int = 10


@dataclass(frozen=True)
class Alert:
    code: str
    message: str  # constant template + numbers only; redaction-safe by construction
    # Re-alert stage of a persistent condition (#2086). Stage 0 keeps the
    # historical dedup key; later stages get their own key so a staged
    # reminder is never collapsed into the first alert by a spool consumer.
    stage: int = 0

    def dedup_key(self) -> str:
        if self.stage > 0:
            return f"health-alert:{self.code}:stage{self.stage}"
        return f"health-alert:{self.code}"


def evaluate_alerts(signals: HealthSignals, thresholds: AlertThresholds) -> list[Alert]:
    """Pure threshold evaluation over one signals snapshot."""
    alerts: list[Alert] = []
    lifetime = signals.request_lifetime_seconds
    if (
        lifetime > 0
        and thresholds.heartbeat_age_factor > 0
        and signals.oldest_request_age_seconds
        >= lifetime * thresholds.heartbeat_age_factor
    ):
        alerts.append(
            Alert(
                code="request_outlived_lifetime",
                message=(
                    f"Oldest in-flight request is {int(signals.oldest_request_age_seconds)}s "
                    f"old, beyond its {int(lifetime)}s lifetime — the request "
                    "lifecycle leaked (#307 class)."
                ),
            )
        )
    if signals.pending_notifications >= max(1, thresholds.max_pending_notifications):
        alerts.append(
            Alert(
                code="notification_backlog",
                message=(
                    f"{signals.pending_notifications} owner notification(s) are "
                    "queued undelivered in the push spool."
                ),
            )
        )
    if signals.dropped_notifications > 0:
        alerts.append(
            Alert(
                code="notifications_dropped",
                message=(
                    f"{signals.dropped_notifications} background notification(s) "
                    "were quarantined as unrecoverable."
                ),
            )
        )
    if signals.orphan_tool_loop_recent >= max(1, thresholds.max_orphan_tool_loop):
        alerts.append(
            Alert(
                code="orphan_tool_loop",
                message=(
                    f"{signals.orphan_tool_loop_recent} 'Custom tool call output "
                    "is missing' stderr lines in 5 min — the engine is stuck "
                    "in an orphan tool-call loop."
                ),
            )
        )
    if signals.orphan_children >= max(1, thresholds.max_orphan_children):
        alerts.append(
            Alert(
                code="orphan_claude_children",
                message=(
                    f"{signals.orphan_children} orphaned node-claude process(es) "
                    "survive outside any bridge session."
                ),
            )
        )
    return alerts


# -- staged re-alerts for a persistent outage (#2086) ---------------------------
#
# A retry-loop alert used to fire once per outage episode. During a 10-hour
# node DNS outage that meant one alert at minute one and silence for the next
# 7,000 failures. A still-ongoing outage is now re-announced at fixed offsets
# from its start, then periodically. Stage 0 is the first alert (raised by the
# caller's own threshold); stage N >= 1 is the N-th reminder.

#: Elapsed seconds since the outage began at which reminders 1, 2, ... fire.
OUTAGE_REALERT_OFFSETS_SECONDS: tuple[float, ...] = (600.0, 3600.0)
#: After the last fixed offset, one more reminder every this many seconds.
OUTAGE_REALERT_INTERVAL_SECONDS = 6 * 3600.0


def outage_realert_due_stage(elapsed_seconds: float) -> int:
    """Highest reminder stage whose offset ``elapsed_seconds`` has reached.

    0 = no reminder due yet. With the defaults: 10 min → 1, 1 h → 2,
    7 h → 3, 13 h → 4, ...
    """
    try:
        elapsed = float(elapsed_seconds)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(elapsed) or elapsed < 0:
        return 0
    stage = 0
    for offset in OUTAGE_REALERT_OFFSETS_SECONDS:
        if elapsed < offset:
            return stage
        stage += 1
    last = OUTAGE_REALERT_OFFSETS_SECONDS[-1] if OUTAGE_REALERT_OFFSETS_SECONDS else 0.0
    return stage + int((elapsed - last) // OUTAGE_REALERT_INTERVAL_SECONDS)


def outage_realert_stage(elapsed_seconds: float, last_stage: int) -> Optional[int]:
    """The reminder stage to announce now, or ``None`` when none is due.

    ``last_stage`` is the stage most recently announced (0 = only the first
    alert). A slow retry loop that skipped several offsets announces once, at
    the highest due stage, instead of a burst of catch-up reminders.
    """
    due = outage_realert_due_stage(elapsed_seconds)
    return due if due > int(last_stage) else None


# -- failure-cause classification (#2086) ---------------------------------------

FAILURE_DNS = "dns"
FAILURE_TLS = "tls"
FAILURE_CONNECTION_REFUSED = "connection_refused"
FAILURE_HTTP_ERROR = "http_error"
FAILURE_TIMEOUT = "timeout"
FAILURE_OTHER = "other"

_DNS_TYPE_NAMES = frozenset({"gaierror", "ClientConnectorDNSError"})
_DNS_MARKERS = (
    "temporary failure in name resolution",
    "name or service not known",
    "no address associated with hostname",
    "nodename nor servname",
    "getaddrinfo failed",
    "could not resolve host",
    "eai_again",
    "eai_noname",
    "[errno -2]",
    "[errno -3]",
    "[errno -5]",
)
_TLS_MARKERS = ("certificate_verify_failed", "[ssl", "ssl:", "tlsv1 alert")
_REFUSED_MARKERS = ("connection refused", "[errno 111]", "[errno 61]")
_HTTP_TYPE_NAMES = frozenset({"HTTPStatusError", "ClientResponseError"})
_HTTP_MARKERS = (
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "internal server error",
)
_TIMEOUT_MARKERS = ("timed out", "timeout")
_MAX_CHAIN = 32


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """``exc`` plus every exception reachable through cause/context/groups."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending and len(chain) < _MAX_CHAIN:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        linked: list[Any] = [current.__cause__, current.__context__]
        # aiohttp's ClientConnectorError keeps the socket error on .os_error.
        linked.append(getattr(current, "os_error", None))
        group = getattr(current, "exceptions", None)
        if isinstance(group, (tuple, list)):
            linked.extend(group)
        pending.extend(e for e in linked if isinstance(e, BaseException))
    return chain


def _text(exc: BaseException) -> str:
    try:
        return str(exc).lower()
    except Exception:
        return ""


def _is_dns(exc: BaseException) -> bool:
    return (
        isinstance(exc, socket.gaierror)
        or type(exc).__name__ in _DNS_TYPE_NAMES
        or any(m in _text(exc) for m in _DNS_MARKERS)
    )


def _is_tls(exc: BaseException) -> bool:
    name = type(exc).__name__
    return (
        isinstance(exc, ssl.SSLError)
        or "SSL" in name
        or "Certificate" in name
        or any(m in _text(exc) for m in _TLS_MARKERS)
    )


def _is_refused(exc: BaseException) -> bool:
    return isinstance(exc, ConnectionRefusedError) or any(
        m in _text(exc) for m in _REFUSED_MARKERS
    )


def _is_http_error(exc: BaseException) -> bool:
    return type(exc).__name__ in _HTTP_TYPE_NAMES or any(
        m in _text(exc) for m in _HTTP_MARKERS
    )


def _is_timeout(exc: BaseException) -> bool:
    name = type(exc).__name__
    return (
        isinstance(exc, TimeoutError)
        or name == "TimedOut"
        or name.endswith("Timeout")
        or "TimeoutError" in name
        or any(m in _text(exc) for m in _TIMEOUT_MARKERS)
    )


# Most specific first: a DNS failure that ends in a connect timeout is "dns".
_CLASSIFIERS = (
    (FAILURE_DNS, _is_dns),
    (FAILURE_TLS, _is_tls),
    (FAILURE_CONNECTION_REFUSED, _is_refused),
    (FAILURE_HTTP_ERROR, _is_http_error),
    (FAILURE_TIMEOUT, _is_timeout),
)


def classify_network_failure(exc: BaseException) -> tuple[str, str]:
    """Classify a transport failure as ``(kind, exception type name)``.

    ``kind`` is one of ``dns``, ``tls``, ``connection_refused``,
    ``http_error``, ``timeout`` or ``other``. The whole ``__cause__`` /
    ``__context__`` chain (and exception-group members) is searched, because
    the interesting error is usually wrapped: PTB's ``NetworkError`` wraps
    ``httpx.ConnectError`` wraps ``socket.gaierror``. The type name is that of
    the exception that decided the kind (the innermost one for ``other``).
    Only the kind and a class name ever reach an alert — never the message.
    """
    chain = _exception_chain(exc)
    for kind, matches in _CLASSIFIERS:
        for item in chain:
            if matches(item):
                return kind, type(item).__name__
    return FAILURE_OTHER, type(chain[-1]).__name__


def format_failure_cause(cause: Optional[tuple[str, str]]) -> str:
    """`` Cause: dns (gaierror).`` or empty — appended to alert messages."""
    if not cause:
        return ""
    kind, type_name = cause
    return f" Cause: {kind} ({type_name})."


def _format_duration(seconds: float) -> str:
    """``61s (1m01s)`` / ``36033s (10h00m)``; the raw seconds always lead."""
    total = int(max(0.0, seconds)) if math.isfinite(seconds) else 0
    if total < 60:
        return f"{total}s"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    human = f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"
    return f"{total}s ({human})"


def init_retry_loop_alert(
    failures: int,
    elapsed_seconds: float,
    *,
    cause: Optional[tuple[str, str]] = None,
    stage: int = 0,
) -> Alert:
    """Bridge stuck retrying ``Application.initialize()`` — not receiving messages.

    Raised by the polling lifecycle (core/bot_lifecycle.py): stage 0 once the
    streak of consecutive transient initialize() failures reaches
    ``CCC_ALERT_INIT_FAILURES``, then a reminder per
    :func:`outage_realert_stage` while the outage lasts. Numbers, a failure
    kind and an exception class name only.
    """
    duration = _format_duration(elapsed_seconds)
    if stage > 0:
        message = (
            f"Telegram polling is still failing to initialize: {int(failures)} "
            f"failed attempts over {duration} since the outage began "
            f"(reminder {int(stage)}) — the bridge is not receiving messages."
        )
    else:
        message = (
            f"Telegram polling failed to initialize {int(failures)} times in a row "
            f"over {duration} — the bridge is stuck in its "
            "transport retry loop and is not receiving messages."
        )
    return Alert(
        code="telegram_init_retry_loop",
        message=message + format_failure_cause(cause),
        stage=max(0, int(stage)),
    )


def init_retry_recovered_alert(
    failures: int,
    elapsed_seconds: float,
    *,
    cause: Optional[tuple[str, str]] = None,
) -> Alert:
    """Companion notice: initialize() succeeded after an alerted retry streak.

    Carries the outage's total duration, failure count and last failure kind.
    """
    message = (
        f"Telegram polling initialized after {int(failures)} failed attempt(s) "
        f"over {_format_duration(elapsed_seconds)} — messages are being received again."
    )
    if cause:
        message += f" Last failure cause: {cause[0]} ({cause[1]})."
    return Alert(code="telegram_init_recovered", message=message)


class AlertGate:
    """Edge-triggered per-code cooldown so a persistent condition alerts once.

    A code re-fires only after ``cooldown_seconds`` (or after the condition
    cleared and returned). The push spool's own 5-minute dedup is a second,
    independent layer.
    """

    def __init__(self, cooldown_seconds: float = DEFAULT_ALERT_COOLDOWN_SECONDS) -> None:
        self._cooldown = max(0.0, float(cooldown_seconds))
        self._last_fired: dict[str, float] = {}

    def admit(self, alerts: Iterable[Alert], now: Optional[float] = None) -> list[Alert]:
        current = time.monotonic() if now is None else now
        fired: list[Alert] = []
        seen = set()
        for alert in alerts:
            seen.add(alert.code)
            last = self._last_fired.get(alert.code)
            if last is not None and current - last < self._cooldown:
                continue
            self._last_fired[alert.code] = current
            fired.append(alert)
        # A cleared condition re-arms immediately so its next occurrence alerts.
        for code in list(self._last_fired):
            if code not in seen:
                self._last_fired.pop(code, None)
        return fired


def write_alert_spool(spool_dir: Path, alert: Alert, *, node: Optional[str] = None) -> bool:
    """Queue one alert as an owner-only push-notifier spool record.

    Delivery (and therefore any real Telegram send) remains entirely behind the
    notifier's ``CCC_PUSH_ENABLED`` opt-in and owner-only target resolution.
    """
    record = {
        "event": "health-alert",
        "node": node or platform.node() or "ccc-node",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "text": alert.message,
        "dedup": alert.dedup_key(),
    }
    try:
        spool_dir.mkdir(parents=True, exist_ok=True)
        path = spool_dir / f"health-alert-{alert.code}-{int(time.time() * 1000)}.json"
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    except OSError as error:
        logger.warning("Health alert spool write failed: %s", type(error).__name__)
        return False
    return True


def count_spool_backlog(spool_dir: Path) -> int:
    """Pending (not yet delivered) notification files in the push spool."""
    try:
        return sum(1 for p in spool_dir.glob("*.json") if p.is_file())
    except OSError:
        return 0


@dataclass
class HealthProbe:
    """Collect signals from live bridge collaborators; every input injectable."""

    project_chat: Any
    spool_dir: Path
    orphan_probe: Any = None  # () -> list[int]; defaults to the read-only reaper scan
    health_snapshot: Any = None  # () -> dict; defaults to health_reporter.snapshot
    thresholds: AlertThresholds = field(default_factory=AlertThresholds)
    # Other spool dirs whose backlog also counts — on the receiving side of a
    # push fan-out, the primary spool this process writes into (the consumer
    # of that dir is another process with no probe of its own).
    extra_spool_dirs: tuple = ()

    def _pending_notifications(self) -> int:
        seen: set = set()
        total = 0
        for d in (self.spool_dir, *self.extra_spool_dirs):
            key = str(Path(d))
            if key in seen:
                continue
            seen.add(key)
            total += count_spool_backlog(Path(d))
        return total

    def collect(self, now: float) -> HealthSignals:
        try:
            # Foreground turns only (#1291): workload_snapshot() folds in
            # provider background-task ages, but those are not bounded by
            # _process_timeout_seconds — only the turn stream is. A healthy
            # long-running background Bash task therefore read as a request
            # lifecycle leak here and re-armed every probe tick.
            active_requests, oldest_age = self.project_chat.foreground_workload_snapshot(
                now
            )
        except Exception:
            active_requests, oldest_age = 0, 0.0

        try:
            waiting_for_turn = int(self.project_chat.waiting_for_turn_snapshot())
        except Exception:
            waiting_for_turn = 0

        lifetime = float(getattr(self.project_chat, "_process_timeout_seconds", 0) or 0)

        dropped = 0
        try:
            snapshot = (
                self.health_snapshot() if self.health_snapshot is not None else None
            )
            if snapshot is None:
                from telegram_bot.utils.health import health_reporter

                snapshot = health_reporter.snapshot()
            recovery = snapshot.get("recovery") or {}
            dropped = int(recovery.get("quarantined_transcripts", 0) or 0)
        except Exception:
            dropped = 0

        orphans: list[int] = []
        try:
            if self.orphan_probe is not None:
                orphans = list(self.orphan_probe())
            else:
                from telegram_bot.utils.orphan_reaper import find_orphaned_claude_pids

                orphans = find_orphaned_claude_pids()
        except Exception:
            orphans = []

        resources: dict[str, Any] = {}
        try:
            resources = dict(self.project_chat.session_resource_snapshot())
        except Exception:
            resources = {}

        orphan_tool_loop_recent = 0
        try:
            from telegram_bot.core.turn_stall import orphan_tool_loop_tracker

            orphan_tool_loop_recent = orphan_tool_loop_tracker.recent_count(now=now)
        except Exception:
            orphan_tool_loop_recent = 0

        return HealthSignals(
            active_requests=int(active_requests),
            waiting_for_turn=max(0, waiting_for_turn),
            oldest_request_age_seconds=float(oldest_age),
            request_lifetime_seconds=lifetime,
            pending_notifications=self._pending_notifications(),
            dropped_notifications=dropped,
            orphan_children=len(orphans),
            resident_sessions=max(0, int(resources.get("resident_sessions", 0))),
            active_sessions=max(0, int(resources.get("active_sessions", 0))),
            session_tree_rss_mb=max(0.0, float(resources.get("tree_rss_mb", 0.0))),
            session_guard_evictions=max(0, int(resources.get("evictions", 0))),
            runtime_recycles=max(0, int(resources.get("runtime_recycles", 0))),
            codex_attachments=max(0, int(resources.get("codex_attachments", 0))),
            orphan_tool_loop_recent=max(0, int(orphan_tool_loop_recent)),
        )
