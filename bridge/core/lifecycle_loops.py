"""Channel-neutral background loops shared by the Telegram and Matrix frontends (#1825).

``BotLifecycleMixin`` (Telegram) used to own these loops as methods that read
``self._config``. The Matrix frontend does not inherit that mixin and keeps
its *Matrix* config in ``self._config``, so it could not reuse them — which is
how the health-alert probe, the session resource guard and the skill-candidate
collector ended up running on Telegram only.

Each loop here takes its collaborators explicitly (``settings`` is the bridge
``Settings``, never a frontend config) and runs until ``stop_event`` is set.
The Telegram mixin methods are thin delegators, so their behaviour and their
existing tests are unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

from telegram_bot.utils.health import health_reporter

logger = logging.getLogger(__name__)

SweepJobs = Callable[..., Awaitable[Iterable[Any]]]


async def run_skill_candidate_collector(
    worker: Any,
    sweep_jobs: SweepJobs,
    settings: Any,
    stop_event: asyncio.Event,
) -> None:
    """Stage provider-bound skill candidates from distill snapshots (#667, #749).

    Read-only against the distill journal: it only reads jobs that already
    carry a snapshot and stages via the idempotent sink. Never mutates a
    distill job, so the memory-distill pipeline is unaffected. Provider
    attempts per sweep are hard-bounded to avoid a first-start backlog burst.
    """

    # Lazy: keep the collector's memory stack out of this module's import path.
    from telegram_bot.memory.skill_candidate_worker import (
        skill_candidate_failure_fields,
    )

    collector_provider = worker.provider
    interval = float(getattr(settings, "distill_extraction_poll_interval", 300.0) or 300.0)
    max_jobs = int(getattr(settings, "codex_skill_collector_max_jobs_per_sweep", 1) or 1)
    while not stop_event.is_set():
        try:
            jobs = await sweep_jobs(interval, recover=False)
            attempted = 0
            for job in jobs:
                if stop_event.is_set() or attempted >= max_jobs:
                    break
                if getattr(job, "snapshot", None) is None:
                    continue
                if getattr(job, "provider", None) != collector_provider:
                    continue
                if not await asyncio.to_thread(worker.should_collect, job_id=job.job_id):
                    continue
                attempted += 1
                try:
                    await worker.collect_once(job_id=job.job_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Body-free diagnostics only: the classified code and the
                    # provider exit status, never stdout/stderr bytes.
                    error_code, exit_status = skill_candidate_failure_fields(exc)
                    logger.warning(
                        "Skill-candidate job failed; backing off job_id=%s "
                        "code=%s exit_status=%s",
                        job.job_id,
                        error_code,
                        exit_status,
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Skill-candidate collector sweep failed; continuing", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except (TimeoutError, asyncio.TimeoutError):
            continue


async def run_health_alerts_probe(
    settings: Any,
    project_chat: Any,
    stop_event: asyncio.Event,
    *,
    spool_dir: Path,
    write_spool_dir: Optional[Path] = None,
) -> None:
    """Detection-only runtime health probe + threshold alerts (#389).

    Every tick exports the structured signals to ``health.json`` and evaluates
    alert thresholds. Fired alerts are logged and queued through the
    owner-only push-notifier spool — delivery stays behind the notifier's
    ``CCC_PUSH_ENABLED`` opt-in, so this task never contacts a provider on its
    own. No remediation is performed here.

    ``spool_dir`` is the spool this process *consumes* (its backlog counts);
    ``write_spool_dir`` is where this process's own writers queue records
    when that differs (the receiving side of a push fan-out). Alerts are
    written to ``write_spool_dir`` when given, else to ``spool_dir``.
    """

    from telegram_bot.utils.health_alerts import (
        AlertGate,
        AlertThresholds,
        HealthProbe,
        evaluate_alerts,
        probe_interval,
        write_alert_spool,
    )

    if not getattr(settings, "health_alerts_enabled", True):
        return
    # Defensive clamp: a non-positive configured interval would make wait_for
    # time out immediately and spin this loop hot (#430 review).
    interval = probe_interval(getattr(settings, "health_alerts_interval_seconds", None))
    probe = HealthProbe(
        project_chat=project_chat,
        spool_dir=spool_dir,
        extra_spool_dirs=tuple(
            d for d in (write_spool_dir,) if d is not None and d != spool_dir
        ),
        thresholds=AlertThresholds(
            heartbeat_age_factor=float(getattr(settings, "alert_heartbeat_age_factor", 1.0)),
            max_pending_notifications=int(
                getattr(settings, "alert_max_pending_notifications", 10)
            ),
            max_orphan_children=int(getattr(settings, "alert_max_orphan_children", 1)),
        ),
    )
    gate = AlertGate(
        cooldown_seconds=float(getattr(settings, "health_alerts_cooldown_seconds", 1800.0))
    )
    push_enabled = bool(getattr(settings, "push_enabled", False))
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        try:
            now = asyncio.get_running_loop().time()
            signals = probe.collect(now)
            fired = gate.admit(evaluate_alerts(signals, probe.thresholds))
            health_reporter.record_health_signals(signals.as_dict(), alerts_fired=len(fired))
            for alert in fired:
                logger.warning("Health alert [%s]: %s", alert.code, alert.message)
                if push_enabled:
                    write_alert_spool(write_spool_dir or spool_dir, alert)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # detection must never hurt the bridge
            logger.debug("Health probe tick failed: %s", type(exc).__name__)


def session_guard_interval(settings: Any) -> float:
    """``session_guard_interval_seconds`` clamped to [10, 3600] (default 60)."""

    raw_interval = getattr(settings, "session_guard_interval_seconds", 60.0)
    try:
        interval = float(raw_interval)
    except (TypeError, ValueError):
        interval = 60.0
    if not math.isfinite(interval):
        interval = 60.0
    return min(max(interval, 10.0), 3600.0)


async def run_session_resource_guard(
    settings: Any,
    project_chat: Any,
    stop_event: asyncio.Event,
) -> None:
    """Periodically release idle provider and MCP process trees.

    Enforcement is delegated to ProjectChat, which owns the active-session
    registry and can therefore guarantee that no in-flight request is
    interrupted. Failures are logged and retried on the next bounded tick.
    """

    interval = session_guard_interval(settings)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await project_chat.enforce_session_resource_limits()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Session resource guard sweep failed; continuing", exc_info=True)


__all__ = [
    "run_health_alerts_probe",
    "run_session_resource_guard",
    "run_skill_candidate_collector",
    "session_guard_interval",
]
