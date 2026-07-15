import errno
import json
import os
import stat
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, List
from dotenv import dotenv_values
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BOT_PACKAGE_DIR = Path(__file__).resolve().parent.parent

# Compatibility defaults for direct ``Config(...)`` construction. Runtime
# settings are loaded lazily below; importing this module must not require a
# project root or touch the process environment.
PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", os.curdir)).resolve()
BOT_DATA_DIR = PROJECT_ROOT / ".telegram_bot"
ENV_FILE_PATH = BOT_DATA_DIR / ".env"  # project config (priority)
BOT_ENV_FILE_PATH = BOT_PACKAGE_DIR / ".env"  # global fallback (e.g. CLAUDE_CLI_PATH)

_PLACEHOLDER_TOKENS = {"your_bot_token_here", ""}
_IMPORT_ENV = dict(os.environ)

LOGS_DIR = BOT_DATA_DIR / "logs"
SESSION_STORE_PATH = BOT_DATA_DIR / "sessions.json"
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


class Config(BaseSettings):
    """Bot configuration"""

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=[str(ENV_FILE_PATH), str(BOT_ENV_FILE_PATH)],
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def load(
        cls,
        *,
        project_root: Path | str | None = None,
        environ: Mapping[str, str] | None = None,
        bot_env_file: Path | str | None = None,
    ) -> "Config":
        """Load settings without mutating the process environment.

        Precedence is explicit: process environment, project ``.env``, then the
        package fallback ``.env``.
        """
        process_values = dict(os.environ if environ is None else environ)
        root_value = project_root if project_root is not None else process_values.get("PROJECT_ROOT")
        if root_value is None or not str(root_value).strip():
            raise ValueError("PROJECT_ROOT must be non-empty to load runtime settings")
        root = Path(root_value).expanduser().resolve()
        fallback_path = Path(bot_env_file or BOT_ENV_FILE_PATH).expanduser()
        project_path = root / ".telegram_bot" / ".env"

        fallback_values = {
            key: value
            for key, value in dotenv_values(fallback_path).items()
            if value is not None
        }
        project_values = {
            key: value
            for key, value in dotenv_values(project_path).items()
            if value is not None
        }
        process_token = process_values.get("TELEGRAM_BOT_TOKEN")
        fallback_has_token = "TELEGRAM_BOT_TOKEN" in fallback_values
        if process_token in _PLACEHOLDER_TOKENS:
            process_values.pop("TELEGRAM_BOT_TOKEN", None)
            if fallback_has_token:
                project_values.pop("TELEGRAM_BOT_TOKEN", None)
        elif (
            project_values.get("TELEGRAM_BOT_TOKEN") in _PLACEHOLDER_TOKENS
            and fallback_has_token
        ):
            project_values.pop("TELEGRAM_BOT_TOKEN", None)
        merged = {**fallback_values, **project_values, **process_values}

        values: dict[str, Any] = {}
        for name, field in cls.model_fields.items():
            environment_name = field.alias or name.upper()
            if environment_name in merged:
                values[field.alias or name] = merged[environment_name]

        data_dir = root / ".telegram_bot"
        home = Path(process_values.get("HOME", str(Path.home()))).expanduser()
        claude_root = Path(process_values.get("CCC_CLAUDE_DIR", str(home / ".claude"))).expanduser()
        values["project_root"] = root
        values.setdefault("bot_data_dir", data_dir)
        values.setdefault("logs_dir", data_dir / "logs")
        values.setdefault("session_store_path", data_dir / "sessions.json")
        values.setdefault("claude_settings_path", claude_root / "settings.json")
        values.setdefault("CCC_CODEX_CLI_PATH", str(claude_root / "hooks" / "ccc-codex"))
        values.setdefault(
            "CCC_CODEX_MEMORY_MATERIALIZER_PATH",
            str(claude_root / "hooks" / "ccc_codex_memory.py"),
        )
        values.setdefault(
            "CCC_PUSH_SPOOL", home / ".claude" / "state" / "telegram-spool"
        )
        return cls.model_validate(values)

    agent_provider: Literal["claude", "codex"] = Field(
        default="claude",
        alias="CCC_AGENT_PROVIDER",
        description="Agent provider used by ProjectChat.",
    )
    node_isolation_profile: Literal["fleet", "external"] = Field(
        default="fleet",
        alias="CCC_NODE_ISOLATION_PROFILE",
        description="Root policy inherited by Claude memory hooks.",
    )
    wiki_memory_enabled: bool = Field(
        default=True,
        alias="CCC_WIKI_MEMORY_ENABLED",
        description="Family Wiki memory source/sink toggle; external profile always overrides off.",
    )
    memory_user_label: str = Field(
        default="Seo Jin On / 서진원",
        alias="CCC_MEMORY_USER_LABEL",
        description="Prompt-only user identity label for memory injection/distill.",
    )
    memory_assistant_label: str = Field(
        default="dungae, a Hermes Team2 worker",
        alias="CCC_MEMORY_ASSISTANT_LABEL",
        description="Prompt-only assistant identity label for memory distill.",
    )

    def hook_policy_environment(self) -> dict[str, str]:
        """Return validated, non-secret policy fields inherited by Claude hooks."""
        profile = self.node_isolation_profile
        return {
            "CCC_NODE_ISOLATION_PROFILE": profile,
            "CCC_WIKI_MEMORY_ENABLED": (
                "0" if profile == "external" else ("1" if self.wiki_memory_enabled else "0")
            ),
            "CCC_MEMORY_USER_LABEL": self.memory_user_label,
            "CCC_MEMORY_ASSISTANT_LABEL": self.memory_assistant_label,
        }

    codex_cli_path: str = Field(
        default_factory=lambda: str(Path.home() / ".claude" / "hooks" / "ccc-codex"),
        alias="CCC_CODEX_CLI_PATH",
        description="ccc-node Codex launcher path.",
    )
    codex_memory_materializer_path: str = Field(
        default_factory=lambda: str(Path.home() / ".claude" / "hooks" / "ccc_codex_memory.py"),
        alias="CCC_CODEX_MEMORY_MATERIALIZER_PATH",
        description="Body-free Codex memory materializer path.",
    )
    codex_memory_bootstrap_timeout_seconds: float = Field(
        default=14.0,
        ge=0.1,
        le=30.0,
        alias="CCC_CODEX_MEMORY_BOOTSTRAP_TIMEOUT_SEC",
        description="Timeout for each Codex memory materialize/status command.",
    )
    claude_cli_path: Optional[Path] = Field(
        default=None,
        description="Optional absolute path to Claude CLI binary (defaults to system PATH)",
    )
    claude_settings_path: Path = Field(
        default=CLAUDE_SETTINGS_PATH, description="Path to Claude Code settings.json"
    )

    # Telegram Bot
    telegram_bot_token: str = Field(..., description="Telegram Bot API Token")
    network_retry_attempts: int = Field(
        default=3, description="Number of retry attempts for network errors"
    )
    network_retry_delay: int = Field(
        default=5, description="Delay in seconds between retry attempts"
    )
    polling_timeout: int = Field(default=30, description="Telegram polling timeout in seconds")

    @field_validator("telegram_bot_token", mode="before")
    @classmethod
    def validate_bot_token(cls, v):
        if not v or v.strip() in _PLACEHOLDER_TOKENS:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is not configured. "
                "Set it in the project .env or bot source .env file."
            )
        return v.strip()

    @field_validator("memory_user_label", "memory_assistant_label", mode="before")
    @classmethod
    def validate_memory_label(cls, v):
        value = " ".join(str(v).split())[:80]
        if not value:
            raise ValueError("memory identity labels must be non-empty")
        return value

    @field_validator("codex_cli_path", "codex_memory_materializer_path", mode="before")
    @classmethod
    def validate_codex_runtime_path(cls, v):
        value = str(v).strip()
        if not value:
            raise ValueError("Codex runtime paths must be non-empty")
        return value

    # Runtime data
    project_root: Path = Field(default=PROJECT_ROOT, description="Bound project root")
    bot_data_dir: Path = Field(default=BOT_DATA_DIR, description="Runtime data directory")
    logs_dir: Path = Field(default=LOGS_DIR, description="Runtime logs directory")
    session_store_path: Path = Field(
        default=SESSION_STORE_PATH,
        description="Local session JSON storage path",
    )
    auto_new_session_after_hours: Optional[float] = Field(
        default=24.0,
        description=(
            "Automatically start a new Claude session when the gap since the "
            "previous user message exceeds this many hours. Set to 0, false, "
            "or off to disable."
        ),
    )

    # Access Control - comma-separated list of allowed user IDs (if empty, allow all)
    allowed_user_ids: Annotated[List[int], NoDecode] = Field(
        default_factory=list,
        description=(
            "List of allowed Telegram user IDs. Empty means allow all, but the "
            "bot refuses to start while empty unless CCC_REQUIRE_ALLOWLIST=false."
        ),
    )
    # Fail-closed guard: when true (default), the bot REFUSES to start with an
    # empty allowed_user_ids, preventing an accidental open-to-everyone bridge.
    # Set CCC_REQUIRE_ALLOWLIST=false only to intentionally run an open bridge.
    require_allowlist: bool = Field(
        default=True,
        alias="CCC_REQUIRE_ALLOWLIST",
        description="Refuse to start when ALLOWED_USER_IDS is empty (fail-closed access control).",
    )
    execution_profile: str = Field(
        default="strict-project",
        alias="CCC_BRIDGE_EXECUTION_PROFILE",
        description=(
            "SDK execution boundary: strict-project (default), owner-operator "
            "(single allowlisted owner only), or disabled."
        ),
    )
    bash_policy: str = Field(
        default="auto-approve",
        alias="CCC_BRIDGE_BASH_POLICY",
        description="Bash approval UX: auto-approve, approve-each, or disabled.",
    )

    # ccc-node push notifier (owner-only outbound Claude Code lifecycle notifications).
    # DISABLED by default — opt-in only. See core/push_notifier.py for the approval boundary.
    push_enabled: bool = Field(
        default=False,
        alias="CCC_PUSH_ENABLED",
        description="Enable owner-only push delivery of Claude Code notifications (opt-in).",
    )
    push_chat_id: Optional[int] = Field(
        default=None,
        alias="CCC_PUSH_CHAT_ID",
        description="Explicit owner chat id for push. Falls back to the sole ALLOWED_USER_IDS.",
    )
    push_spool_dir: Path = Field(
        default=Path.home() / ".claude" / "state" / "telegram-spool",
        alias="CCC_PUSH_SPOOL",
        description="Spool directory the Claude Code notify hook writes summaries into.",
    )
    push_poll_interval: float = Field(
        default=3.0,
        alias="CCC_PUSH_POLL_INTERVAL",
        description="Seconds between spool drains.",
    )
    push_max_per_minute: int = Field(
        default=10,
        alias="CCC_PUSH_MAX_PER_MINUTE",
        description="Rate limit: max push messages delivered per minute.",
    )

    @field_validator("push_enabled", mode="before")
    @classmethod
    def parse_push_enabled(cls, v):
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)

    @field_validator("require_allowlist", mode="before")
    @classmethod
    def parse_require_allowlist(cls, v):
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            normalized = v.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        elif isinstance(v, int) and v in {0, 1}:
            return bool(v)
        raise ValueError(
            "CCC_REQUIRE_ALLOWLIST must be a boolean: true/false, yes/no, on/off, or 1/0"
        )

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def parse_allowed_user_ids(cls, v):
        """Parse allowed_user_ids from string or list"""
        if isinstance(v, str):
            value = v.strip()
            if not value:
                return []
            if value.startswith("["):
                parsed = json.loads(value)
                if not isinstance(parsed, list):
                    raise ValueError("ALLOWED_USER_IDS JSON value must be a list")
                return [int(x) for x in parsed]
            return [int(x.strip()) for x in value.split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v

    @field_validator("auto_new_session_after_hours", mode="before")
    @classmethod
    def parse_auto_new_session_after_hours(cls, v):
        if v is None:
            return 24.0
        if isinstance(v, bool):
            if not v:
                return None
            raise ValueError(
                "AUTO_NEW_SESSION_AFTER_HOURS must be a positive number, or 0/off/false to disable."
            )
        if isinstance(v, str):
            value = v.strip().lower()
            if not value:
                return 24.0
            if value in {"0", "false", "off", "no", "disable", "disabled"}:
                return None
            try:
                parsed = float(value)
            except ValueError as exc:
                raise ValueError(
                    "AUTO_NEW_SESSION_AFTER_HOURS must be a positive number, "
                    "or 0/off/false to disable."
                ) from exc
        else:
            parsed = float(v)

        if parsed == 0:
            return None
        if parsed < 0:
            raise ValueError(
                "AUTO_NEW_SESSION_AFTER_HOURS must be greater than 0, or 0/off/false to disable."
            )
        return parsed

    # Streaming configuration
    draft_update_min_chars: int = Field(
        default=150,
        description="Minimum characters to accumulate before sending draft update",
    )
    draft_update_interval: float = Field(
        default=1.0, description="Minimum seconds between draft updates"
    )
    enable_streaming_tool_calls: bool = Field(
        default=False,
        description="Whether to show tool calls in Telegram streaming messages",
    )
    enable_readable_renderer: bool = Field(
        default=True,
        alias="CCC_TELEGRAM_READABLE_RENDERER",
        description=(
            "Normalize final Telegram text for mobile readability before the "
            "MarkdownV2 conversion (GitHub issue #34). Default on — the "
            "transform is content-preserving, idempotent, and fail-open, so it "
            "only adjusts whitespace/blank-line layout. Set "
            "CCC_TELEGRAM_READABLE_RENDERER=false to disable."
        ),
    )
    enable_part_headers: bool = Field(
        default=True,
        alias="CCC_TELEGRAM_PART_HEADERS",
        description=(
            "Prefix multi-chunk Telegram responses with a compact 'k/N' part "
            "marker (GitHub issue #34). Default on; only applies when a response "
            "is split into more than one message. Set "
            "CCC_TELEGRAM_PART_HEADERS=false to disable."
        ),
    )
    enable_loose_spacing: bool = Field(
        default=True,
        alias="CCC_TELEGRAM_LOOSE_SPACING",
        description=(
            "Add vertical breathing room by inserting a blank line between "
            "adjacent list items so dense bullet/numbered lists are easier to "
            "read on mobile. Telegram has no line-height control, so blank lines "
            "are the only lever. Prose lines stay attached and fenced code is "
            "untouched. Applies only when the readable renderer is enabled. "
            "Default on; set CCC_TELEGRAM_LOOSE_SPACING=false for compact output."
        ),
    )
    spacing_lines: int = Field(
        default=2,
        alias="CCC_TELEGRAM_SPACING_LINES",
        description=(
            "Number of blank lines to use for each vertical gap when the readable "
            "renderer normalizes layout: paragraph, section, and (in loose mode) "
            "list-item gaps are all widened to this many blank lines so output is "
            "less dense on mobile. Telegram has no line-height control, so blank "
            "lines are the only lever. Clamped to [1, 3]. Default 2 (roomy); set "
            "CCC_TELEGRAM_SPACING_LINES=1 for the historical compact single-blank "
            "layout. Applies only when the readable renderer is on."
        ),
    )

    @field_validator("spacing_lines", mode="before")
    @classmethod
    def clamp_spacing_lines(cls, v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 1
        return max(1, min(n, 3))

    telegram_max_bubble_chars: int = Field(
        default=1200,
        alias="CCC_TELEGRAM_MAX_BUBBLE_CHARS",
        description=(
            "Maximum characters per Telegram message ('bubble'). Long replies "
            "are split into multiple messages at this size during streaming so no "
            "single bubble is overwhelming. Telegram's hard limit is 4096; values "
            "are clamped to [200, 4000]. Default 1200."
        ),
    )

    @field_validator("telegram_max_bubble_chars", mode="before")
    @classmethod
    def clamp_max_bubble_chars(cls, v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 1200
        return max(200, min(n, 4000))

    enable_option_buttons: bool = Field(
        default=False,
        alias="CCC_TELEGRAM_OPTION_BUTTONS",
        description=(
            "Render multiple-choice questions as tappable Telegram inline "
            "keyboard buttons. Default OFF: the question and its numbered options "
            "are shown as plain text and the user just types their choice. Set "
            "CCC_TELEGRAM_OPTION_BUTTONS=true to bring back the tap-to-select "
            "buttons."
        ),
    )

    enable_entity_renderer: bool = Field(
        default=True,
        alias="CCC_TELEGRAM_ENTITY_RENDERER",
        description=(
            "Send final Telegram output as (text + MessageEntity[]) instead of a "
            "MarkdownV2 string, avoiding escape failures (GitHub issue #34). "
            "Default on; fail-open — falls back to MarkdownV2 if the entity API "
            "is unavailable or a send fails. Set "
            "CCC_TELEGRAM_ENTITY_RENDERER=false to disable."
        ),
    )
    enable_streaming: bool = Field(
        default=False,
        alias="CCC_TELEGRAM_STREAMING",
        description=(
            "Master switch for live response streaming (the progressively-edited "
            "Telegram draft). Default OFF: replies are delivered as complete "
            "message(s) when generation finishes, which is more reliable than "
            "the live draft. Set CCC_TELEGRAM_STREAMING=true to re-enable the "
            "live draft (then CCC_PARTIAL_STREAMING controls token-level vs "
            "whole-block updates)."
        ),
    )
    enable_partial_streaming: bool = Field(
        default=True,
        alias="CCC_PARTIAL_STREAMING",
        description=(
            "Token-level streaming, used only when CCC_TELEGRAM_STREAMING is on. "
            "When enabled, the SDK is asked for partial message events "
            "(include_partial_messages) and the reader loop drives the live "
            "Telegram draft from incremental text deltas (true typewriter), vs a "
            "single whole-block update. Draft edit cadence is still throttled by "
            "draft_update_min_chars / draft_update_interval."
        ),
    )
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

    # Voice message configuration
    transcription_provider: str = Field(
        default="whisper",
        description=("Voice transcription provider. Supported values: whisper, volcengine"),
    )
    openai_api_key: Optional[str] = Field(
        default=None, description="OpenAI API key used for Whisper transcription"
    )
    openai_base_url: Optional[str] = Field(
        default=None,
        description="Optional OpenAI-compatible API base URL for Whisper transcription",
    )
    whisper_model: str = Field(
        default="whisper-1", description="Whisper model name for voice transcription"
    )
    max_voice_duration: int = Field(
        default=300, description="Maximum accepted voice duration in seconds"
    )
    max_document_size_mb: int = Field(
        default=20,
        ge=1,
        le=50,
        alias="CCC_MAX_DOCUMENT_SIZE_MB",
        description="Maximum inbound Telegram document size in MiB.",
    )
    ffmpeg_path: Optional[str] = Field(
        default=None,
        description="Optional absolute path to ffmpeg binary (defaults to system PATH)",
    )
    voice_reply_persona: str = Field(
        default="Tingting",
        description="Default persona name for voice replies",
    )
    # Volcengine bigmodel file ASR fields (v3 submit/query)
    volcengine_app_id: Optional[str] = Field(
        default=None, description="Volcengine appid for bigmodel file ASR"
    )
    volcengine_token: Optional[str] = Field(
        default=None, description="Volcengine token for bigmodel file ASR"
    )
    volcengine_access_key: Optional[str] = Field(
        default=None, description="Volcengine Access Key for TOS upload"
    )
    volcengine_secret_access_key: Optional[str] = Field(
        default=None, description="Volcengine Secret Access Key for TOS upload"
    )
    volcengine_tos_bucket_name: Optional[str] = Field(
        default=None, description="Volcengine TOS bucket name for staging voice files"
    )
    volcengine_tos_endpoint: str = Field(default="", description="Volcengine TOS endpoint URL")
    volcengine_tos_region: str = Field(default="cn-beijing", description="Volcengine TOS region")
    volcengine_tos_signed_url_ttl_seconds: int = Field(
        default=900,
        description="Signed URL TTL in seconds for Volcengine ASR to fetch staged voice file",
    )
    volcengine_cluster: str = Field(
        default="volc_auc_common",
        description="Volcengine cluster (reserved for compatibility)",
    )
    volcengine_resource_id: str = Field(
        default="volc.bigasr.auc",
        description="Volcengine X-Api-Resource-Id for bigmodel file ASR",
    )
    volcengine_model_name: str = Field(
        default="bigmodel",
        description="Volcengine request.model_name for bigmodel file ASR",
    )
    volcengine_submit_endpoint: str = Field(
        default="https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit",
        description="Volcengine bigmodel ASR submit endpoint URL",
    )
    volcengine_query_endpoint: str = Field(
        default="https://openspeech.bytedance.com/api/v3/auc/bigmodel/query",
        description="Volcengine bigmodel ASR query endpoint URL",
    )
    volcengine_timeout_seconds: float = Field(
        default=20.0,
        description="Volcengine request timeout in seconds",
    )
    volcengine_max_retries: int = Field(
        default=3,
        description="Maximum retry attempts for Volcengine transcription",
    )
    volcengine_initial_backoff: float = Field(
        default=1.0,
        description="Initial retry backoff seconds for Volcengine transcription",
    )
    volcengine_poll_interval_seconds: float = Field(
        default=2.0,
        description="Polling interval in seconds for Volcengine query",
    )
    volcengine_max_poll_seconds: float = Field(
        default=300.0,
        description="Maximum polling duration in seconds for Volcengine query",
    )

    @field_validator("transcription_provider", mode="before")
    @classmethod
    def normalize_transcription_provider(cls, v):
        provider = str(v or "whisper").strip().lower()
        allowed = {"whisper", "volcengine"}
        if provider not in allowed:
            raise ValueError("TRANSCRIPTION_PROVIDER must be one of: whisper, volcengine.")
        return provider

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def normalize_openai_key(cls, v):
        if v is None:
            return None
        value = str(v).strip()
        return value or None

    @field_validator("openai_base_url", mode="before")
    @classmethod
    def normalize_openai_base_url(cls, v):
        if v is None:
            return None
        value = str(v).strip()
        return value or None

    @field_validator("voice_reply_persona")
    @classmethod
    def normalize_voice_reply_text(cls, v):
        value = str(v or "").strip()
        if not value:
            raise ValueError("VOICE_REPLY_PERSONA must not be empty.")
        return value

    @field_validator(
        "volcengine_app_id",
        "volcengine_token",
        "volcengine_access_key",
        "volcengine_secret_access_key",
        "volcengine_tos_bucket_name",
        "volcengine_cluster",
        mode="before",
    )
    @classmethod
    def normalize_volcengine_secret(cls, v, info):
        if info.field_name == "volcengine_cluster":
            value = str(v or "").strip()
            return value or "volc_auc_common"
        if v is None:
            return None
        value = str(v).strip()
        return value or None

    @field_validator(
        "volcengine_submit_endpoint",
        "volcengine_query_endpoint",
        "volcengine_resource_id",
        "volcengine_model_name",
        "volcengine_tos_region",
    )
    @classmethod
    def validate_volcengine_required_text(cls, v, info):
        value = str(v).strip()
        if not value:
            env_name = info.field_name.upper()
            raise ValueError(f"{env_name} must not be empty.")
        return value

    @field_validator("max_voice_duration")
    @classmethod
    def validate_max_voice_duration(cls, v):
        if v <= 0:
            raise ValueError("MAX_VOICE_DURATION must be a positive integer.")
        return v

    @field_validator("volcengine_timeout_seconds")
    @classmethod
    def validate_volcengine_timeout_seconds(cls, v):
        if v <= 0:
            raise ValueError("VOLCENGINE_TIMEOUT_SECONDS must be positive.")
        return v

    @field_validator("volcengine_max_retries")
    @classmethod
    def validate_volcengine_max_retries(cls, v):
        if v <= 0:
            raise ValueError("VOLCENGINE_MAX_RETRIES must be a positive integer.")
        return v

    @field_validator("volcengine_initial_backoff")
    @classmethod
    def validate_volcengine_initial_backoff(cls, v):
        if v <= 0:
            raise ValueError("VOLCENGINE_INITIAL_BACKOFF must be positive.")
        return v

    @field_validator("volcengine_poll_interval_seconds")
    @classmethod
    def validate_volcengine_poll_interval_seconds(cls, v):
        if v <= 0:
            raise ValueError("VOLCENGINE_POLL_INTERVAL_SECONDS must be positive.")
        return v

    @field_validator("volcengine_max_poll_seconds")
    @classmethod
    def validate_volcengine_max_poll_seconds(cls, v):
        if v <= 0:
            raise ValueError("VOLCENGINE_MAX_POLL_SECONDS must be positive.")
        return v

    @field_validator("volcengine_tos_signed_url_ttl_seconds")
    @classmethod
    def validate_volcengine_tos_signed_url_ttl_seconds(cls, v):
        if v <= 0:
            raise ValueError("VOLCENGINE_TOS_SIGNED_URL_TTL_SECONDS must be positive.")
        return v

    @model_validator(mode="after")
    def validate_provider_specific_config(self):
        if self.transcription_provider != "volcengine":
            return self

        missing = []
        if not self.volcengine_app_id:
            missing.append("VOLCENGINE_APP_ID")
        if not self.volcengine_token:
            missing.append("VOLCENGINE_TOKEN")
        if not self.volcengine_access_key:
            missing.append("VOLCENGINE_ACCESS_KEY")
        if not self.volcengine_secret_access_key:
            missing.append("VOLCENGINE_SECRET_ACCESS_KEY")
        if not self.volcengine_tos_bucket_name:
            missing.append("VOLCENGINE_TOS_BUCKET_NAME")
        if not self.volcengine_tos_endpoint:
            missing.append("VOLCENGINE_TOS_ENDPOINT")
        if missing:
            raise ValueError(
                "Volcengine transcription provider requires: " + ", ".join(missing) + "."
            )
        return self

    # Logging
    log_level: str = Field("INFO", description="Logging level")
    log_format: str = Field(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s", description="Log format"
    )


Settings = Config


def _load_legacy_config() -> Config:
    """Load the compatibility singleton only when a runtime consumer needs it."""
    return Config.load(
        project_root=PROJECT_ROOT,
        environ=_IMPORT_ENV,
        bot_env_file=BOT_ENV_FILE_PATH,
    )


class _LazyConfig:
    """Compatibility proxy that keeps module import side-effect free."""

    def __init__(self) -> None:
        object.__setattr__(self, "_loaded", None)

    def bind(self, settings: Config) -> None:
        object.__setattr__(self, "_loaded", settings)

    def _get(self) -> Config:
        loaded = object.__getattribute__(self, "_loaded")
        if loaded is None:
            loaded = _load_legacy_config()
            object.__setattr__(self, "_loaded", loaded)
        return loaded

    def __getattr__(self, name):
        return getattr(self._get(), name)

    def __setattr__(self, name, value) -> None:
        setattr(self._get(), name, value)

    def __delattr__(self, name) -> None:
        delattr(self._get(), name)


config = _LazyConfig()


def bind_config(settings: Config) -> None:
    """Bind validated settings before importing runtime components."""
    config.bind(settings)


def _open_private_log_directory(path: Path) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise RuntimeError("secure logging requires O_NOFOLLOW and O_DIRECTORY support")
    flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(f"log parent is not a directory: {path}")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError(f"log parent is not owned by the current user: {path}")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise PermissionError(f"log parent is writable by group or others: {path}")
        return fd
    except Exception:
        os.close(fd)
        raise


def _private_log_handler_at(dir_fd: int, name: str, display_path: Path) -> logging.FileHandler:
    """Open a final log component relative to an already validated directory fd."""
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError(f"invalid log file name: {name!r}")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RuntimeError("secure logging requires O_NOFOLLOW support")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | nofollow
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK, errno.ENXIO}:
            raise PermissionError(f"refusing unsafe log file: {display_path}") from exc
        raise

    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise PermissionError(f"log path is not a regular file: {display_path}")
        if file_stat.st_nlink != 1:
            raise PermissionError(f"log file has multiple hard links: {display_path}")
        if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
            raise PermissionError(f"log file is not owned by the current user: {display_path}")
        os.fchmod(fd, 0o600)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            raise PermissionError(f"could not enforce owner-only log mode: {display_path}")

        stream = os.fdopen(fd, "a", encoding="utf-8")
        fd = -1
        handler = logging.FileHandler(display_path, encoding="utf-8", delay=True)
        handler.stream = stream
        return handler
    finally:
        if fd >= 0:
            os.close(fd)


def _private_log_handler(path: Path) -> logging.FileHandler:
    """Compatibility wrapper that anchors the final open to the parent directory."""
    dir_fd = _open_private_log_directory(path.parent)
    try:
        return _private_log_handler_at(dir_fd, path.name, path)
    finally:
        os.close(dir_fd)


def _private_log_handlers(logs_dir: Path, names: tuple[str, ...]) -> list[logging.FileHandler]:
    """Open all handlers against one directory identity or publish none of them."""
    dir_fd = _open_private_log_directory(logs_dir)
    handlers: list[logging.FileHandler] = []
    created: list[str] = []
    try:
        for name in names:
            try:
                os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                existed = True
            except FileNotFoundError:
                existed = False
            handler = _private_log_handler_at(dir_fd, name, logs_dir / name)
            handlers.append(handler)
            if not existed:
                created.append(name)
        return handlers
    except Exception:
        for handler in handlers:
            handler.close()
        for name in created:
            try:
                os.unlink(name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            except OSError:
                logging.getLogger(__name__).warning(
                    "Failed to roll back newly-created log artifact %s", logs_dir / name
                )
        raise
    finally:
        os.close(dir_fd)


def setup_logging(settings: Config | None = None) -> None:
    """Setup logging configuration with console and private file output."""
    runtime_config: Any = config if settings is None else settings
    logs_dir = runtime_config.logs_dir
    from datetime import datetime

    from telegram_bot.session.store import ensure_private_directory

    ensure_private_directory(logs_dir)
    error_name = f"error_{datetime.now().strftime('%Y-%m-%d')}.log"
    fh, efh = _private_log_handlers(logs_dir, ("bot.log", error_name))

    log_level = getattr(logging, runtime_config.log_level.upper())
    formatter = logging.Formatter(runtime_config.log_format)
    is_debug = os.environ.get("BOT_DEBUG")
    console_level = log_level if is_debug else logging.WARNING

    try:
        logging.basicConfig(level=log_level, format=runtime_config.log_format)
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)
        for handler in root_logger.handlers:
            handler.setLevel(console_level)

        fh.setLevel(log_level)
        fh.setFormatter(formatter)
        root_logger.addHandler(fh)

        sep = "=" * 60
        efh.setLevel(logging.ERROR)
        efh.setFormatter(
            logging.Formatter(
                f"\n{sep}\n[%(asctime)s] %(name)s - %(levelname)s\n%(message)s\n{sep}"
            )
        )
        root_logger.addHandler(efh)

        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("telegram").setLevel(logging.WARNING)
        logging.getLogger("telegram.ext").setLevel(logging.WARNING)
        logging.getLogger("telegram.ext.ExtBot").setLevel(logging.WARNING)
    except Exception:
        root_logger = logging.getLogger()
        for handler in (fh, efh):
            root_logger.removeHandler(handler)
            handler.close()
        raise
