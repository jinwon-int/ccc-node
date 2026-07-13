"""Fleet runtime health signals and threshold alerts (issue #389).

Detection-only: this module observes and reports, it never remediates.

Four structured signals are exported to ``health.json`` on every probe tick and
evaluated against configurable thresholds:

1. **Session liveness** — streams whose reader task has died while the stream
   is still registered (a dead session that can no longer deliver anything).
2. **Heartbeat age vs request lifetime** — the oldest in-flight request's age
   compared to the configured process timeout. A request older than its own
   lifetime means the lifecycle leaked (the #307 class): nothing should outlive
   ``CLAUDE_PROCESS_TIMEOUT`` now that the terminal-stall guard (#411 C)
   releases silent turns much earlier.
3. **Pending / dropped notifications** — the push-notifier spool backlog plus
   the cumulative quarantined-transcript counter (notifications recovery gave
   up on, #411 B).
4. **Orphan children** — PPID-1 ``node claude`` processes from the read-only
   orphan probe (#303).

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
- ``alert_max_dead_streams`` (default 1): any registered stream with a dead
  reader is already a delivery outage for that conversation.
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

    active_streams: int = 0
    dead_streams: int = 0
    active_requests: int = 0
    oldest_request_age_seconds: float = 0.0
    request_lifetime_seconds: float = 0.0
    pending_notifications: int = 0
    dropped_notifications: int = 0
    orphan_children: int = 0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["oldest_request_age_seconds"] = int(self.oldest_request_age_seconds)
        data["request_lifetime_seconds"] = int(self.request_lifetime_seconds)
        return data


@dataclass(frozen=True)
class AlertThresholds:
    heartbeat_age_factor: float = 1.0
    max_dead_streams: int = 1
    max_pending_notifications: int = 10
    max_orphan_children: int = 1


@dataclass(frozen=True)
class Alert:
    code: str
    message: str  # constant template + numbers only; redaction-safe by construction

    def dedup_key(self) -> str:
        return f"health-alert:{self.code}"


def evaluate_alerts(signals: HealthSignals, thresholds: AlertThresholds) -> list[Alert]:
    """Pure threshold evaluation over one signals snapshot."""
    alerts: list[Alert] = []
    if signals.dead_streams >= max(1, thresholds.max_dead_streams):
        alerts.append(
            Alert(
                code="dead_session_stream",
                message=(
                    f"{signals.dead_streams} conversation stream(s) have a dead "
                    "reader and cannot deliver replies until recreated."
                ),
            )
        )
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

    def collect(self, now: float) -> HealthSignals:
        active_streams = 0
        dead_streams = 0
        streams = getattr(self.project_chat, "_streams", {}) or {}
        for state in streams.values():
            reader = getattr(state, "reader_task", None)
            if reader is not None and reader.done():
                dead_streams += 1
            else:
                active_streams += 1

        try:
            active_requests, oldest_age = self.project_chat.workload_snapshot(now)
        except Exception:
            active_requests, oldest_age = 0, 0.0

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

        return HealthSignals(
            active_streams=active_streams,
            dead_streams=dead_streams,
            active_requests=int(active_requests),
            oldest_request_age_seconds=float(oldest_age),
            request_lifetime_seconds=lifetime,
            pending_notifications=count_spool_backlog(self.spool_dir),
            dropped_notifications=dropped,
            orphan_children=len(orphans),
        )
