"""Heartbeat / health-alerts / task-ledger settings domain (#584 P2-3).

Long-running task heartbeat, runtime health probe, and persistent task
ledger configuration extracted verbatim from ``utils/config.py``.
``Config`` inherits this mixin, so every field name, env alias, and
default behaves exactly as before.

The mixin is intentionally a plain class (no pydantic base): pydantic v2
collects annotated fields from non-model bases when building ``Config``,
which keeps the MRO simple and avoids merging multiple ``model_config``
definitions.

Standalone-config contract: this module must stay importable as a leaf of
the synthetic ``telegram_bot.utils`` package that
``tests/test_config_voice_provider.py`` builds in a fresh process, so it
may only import stdlib and pydantic.
"""

from pathlib import Path
from typing import Optional

from pydantic import Field


class HeartbeatSettingsMixin:
    """Heartbeat, health-alert, and task-ledger configuration."""

    heartbeat_enabled: bool = Field(
        default=True,
        alias="CCC_HEARTBEAT_ENABLED",
        description="Enable fail-open long-running task heartbeat messages.",
    )
    heartbeat_threshold_seconds: float = Field(
        default=15.0,
        alias="CCC_HEARTBEAT_THRESHOLD_SECONDS",
        description="Seconds before sending the first long-running task heartbeat.",
    )
    heartbeat_update_interval_seconds: float = Field(
        default=15.0,
        alias="CCC_HEARTBEAT_UPDATE_INTERVAL_SECONDS",
        description="Minimum seconds between heartbeat message edits.",
    )
    heartbeat_suppress_when_streaming_progress: bool = Field(
        default=True,
        alias="CCC_HEARTBEAT_SUPPRESS_WHEN_STREAMING_PROGRESS",
        description="Suppress heartbeat while live streaming drafts recently showed progress.",
    )
    heartbeat_delete_on_done: bool = Field(
        default=True,
        alias="CCC_HEARTBEAT_DELETE_ON_DONE",
        description="Delete transient heartbeat messages when a task completes or is cancelled.",
    )
    heartbeat_store_path: Optional[Path] = Field(
        default=None,
        alias="CCC_HEARTBEAT_STORE_PATH",
        description=(
            "Optional path to the JSON registry of live heartbeat message ids. "
            "On startup the bridge deletes any survivors listed here — heartbeats "
            "from a run that was SIGTERM-killed mid-request, whose '⏳ Working' "
            "message would otherwise linger forever. Defaults to "
            "BOT_DATA_DIR/heartbeats.json."
        ),
    )
    task_ledger_path: Optional[Path] = Field(
        default=None,
        alias="CCC_TASK_LEDGER_PATH",
        description=(
            "Optional path to the persistent task ledger (Hermes-style explicit "
            "task lifecycle for bridge requests). Every request gets a record "
            "with an explicit state; the '⏳ Working' status message is a "
            "projection of it, terminal cleanup is retried until it lands, and "
            "startup reconciles records orphaned by a dead process. Defaults to "
            "BOT_DATA_DIR/tasks.json."
        ),
    )
    task_interrupted_notice: bool = Field(
        default=True,
        alias="CCC_TASK_INTERRUPTED_NOTICE",
        description=(
            "When a restart interrupts an in-flight request, edit its status "
            "message into a short 'interrupted — please resend' notice instead "
            "of deleting it silently. Set false to delete."
        ),
    )
    heartbeat_stall_seconds: float = Field(
        default=300.0,
        alias="CCC_HEARTBEAT_STALL_SECONDS",
        description=(
            "Delete the transient heartbeat message when no SDK event has arrived "
            "for this many seconds. A request that stalls (e.g. a bridge restart "
            "left it in flight, or the SDK stream hangs) never reaches the "
            "terminal ResultMessage that normally removes the heartbeat, so the "
            "growing '⏳ Working — Nm' line would otherwise linger as the last "
            "chat message. It reappears automatically if SDK activity resumes. "
            "Set 0 to disable. NOTE: a legitimately long single tool call emits "
            "no intermediate SDK events while it runs, so if it exceeds this its "
            "heartbeat is removed too — raise this when you run such tools."
        ),
    )
    health_alerts_enabled: bool = Field(
        default=True,
        alias="CCC_HEALTH_ALERTS_ENABLED",
        description=(
            "Run the detection-only runtime health probe (#389): every interval "
            "it exports session-liveness, heartbeat-age, notification-backlog, "
            "and orphan-child signals to health.json and evaluates alert "
            "thresholds. Alerts are queued through the owner-only push-notifier "
            "spool, so a real Telegram send additionally requires "
            "CCC_PUSH_ENABLED; with push disabled alerts surface in logs and "
            "health.json only."
        ),
    )
    health_alerts_interval_seconds: float = Field(
        default=60.0,
        alias="CCC_HEALTH_ALERTS_INTERVAL_SECONDS",
        description="Seconds between runtime health probe ticks.",
    )
    health_alerts_cooldown_seconds: float = Field(
        default=1800.0,
        alias="CCC_HEALTH_ALERTS_COOLDOWN_SECONDS",
        description=(
            "Per-alert-code cooldown: a persistent condition re-alerts only "
            "after this long (a cleared condition re-arms immediately)."
        ),
    )
    alert_heartbeat_age_factor: float = Field(
        default=1.0,
        alias="CCC_ALERT_HEARTBEAT_AGE_FACTOR",
        description=(
            "Alert when the oldest in-flight request exceeds this multiple of "
            "CLAUDE_PROCESS_TIMEOUT — nothing should outlive its own request "
            "lifetime (#307 regression guard). 0 disables this check."
        ),
    )
    alert_max_dead_streams: int = Field(
        default=1,
        alias="CCC_ALERT_MAX_DEAD_STREAMS",
        description="Alert when at least this many registered streams have a dead reader.",
    )
    alert_max_pending_notifications: int = Field(
        default=10,
        alias="CCC_ALERT_MAX_PENDING_NOTIFICATIONS",
        description="Alert when the push-notifier spool backlog reaches this size.",
    )
    alert_max_orphan_children: int = Field(
        default=1,
        alias="CCC_ALERT_MAX_ORPHAN_CHILDREN",
        description="Alert when at least this many orphan node-claude processes survive.",
    )
    terminal_stall_seconds: float = Field(
        default=300.0,
        alias="CCC_TERMINAL_STALL_SECONDS",
        description=(
            "Release a request whose agent produced answer text but whose "
            "terminal event (Claude ResultMessage / provider completion) never "
            "arrives (#411 C). After this many seconds of total stream silence "
            "following the last assistant text — with no tool running and no "
            "approval pending — the buffered text is delivered once with a "
            "stall notice, the turn is interrupted, and the conversation FIFO "
            "is released so queued messages proceed. Without it the request "
            "would hold the conversation until the full process timeout "
            "(default 21600s). Set 0 to disable and fall back to the process "
            "timeout only."
        ),
    )
    heartbeat_duration_log_enabled: bool = Field(
        default=True,
        alias="CCC_HEARTBEAT_DURATION_LOG_ENABLED",
        description="Append local request duration samples for later heartbeat forecasts.",
    )
    heartbeat_duration_log_path: Optional[Path] = Field(
        default=None,
        alias="CCC_HEARTBEAT_DURATION_LOG_PATH",
        description="Optional JSONL duration log path. Defaults to BOT_DATA_DIR/duration.jsonl.",
    )
    heartbeat_duration_log_max_lines: int = Field(
        default=10000,
        alias="CCC_HEARTBEAT_DURATION_LOG_MAX_LINES",
        description="Maximum JSONL duration samples to retain locally.",
    )
    heartbeat_forecast_enabled: bool = Field(
        default=True,
        alias="CCC_HEARTBEAT_FORECAST_ENABLED",
        description=(
            "Show a remaining-time ETA in heartbeat messages. Recomputed every "
            "heartbeat tick as the conditional median of past request durations "
            "that exceed the current elapsed time (so it tracks long-running "
            "tasks instead of going stale); hidden when too few comparable "
            "samples remain."
        ),
    )
    heartbeat_forecast_min_samples: int = Field(
        default=10,
        alias="CCC_HEARTBEAT_FORECAST_MIN_SAMPLES",
        description="Minimum local duration samples required before showing a forecast.",
    )
