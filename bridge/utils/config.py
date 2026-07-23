import json
import re
import os
from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, List
from dotenv import dotenv_values
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from telegram_bot.utils.settings_heartbeat import HeartbeatSettingsMixin
from telegram_bot.utils.settings_memory import MemorySettingsMixin
from telegram_bot.utils.settings_voice import VoiceSettingsMixin

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

# ``Settings.load`` computes the full precedence chain (process env > project
# .env > package fallback .env) itself and validates the merged mapping.
# pydantic-core invokes the custom ``BaseSettings.__init__`` even from
# ``model_validate``, so without intervention that validation would ALSO
# consult ``os.environ`` and the import-time ``model_config.env_file`` —
# silently leaking machine state (e.g. a live node's project ``.env`` or an
# operator's exported profile) into loads that passed explicit ``environ`` /
# ``bot_env_file`` arguments. While this flag is set,
# ``settings_customise_sources`` keeps only the init source, so ``load``
# validates exactly the values it merged. Direct ``Config(...)`` construction
# is unaffected.
_LOAD_EXPLICIT_VALUES_ONLY: ContextVar[bool] = ContextVar(
    "ccc_config_load_explicit_values_only", default=False
)

LOGS_DIR = BOT_DATA_DIR / "logs"
SESSION_STORE_PATH = BOT_DATA_DIR / "sessions.json"
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


# MemorySettingsMixin completes the #584 P2-3 domain split. Keep Config's
# existing docstring stable because pydantic exports it as JSON Schema metadata.
class Config(
    MemorySettingsMixin,
    VoiceSettingsMixin,
    HeartbeatSettingsMixin,
    BaseSettings,
):
    """Bot configuration.

    Domain field clusters live in per-domain mixins (#584 P2-3):
    ``VoiceSettingsMixin`` (whisper + Volcengine ASR) and
    ``HeartbeatSettingsMixin`` (heartbeat / health alerts / task ledger).
    pydantic v2 collects their annotated fields and validators through the
    MRO, so aliases, defaults, and validation behavior are unchanged.
    """

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=[str(ENV_FILE_PATH), str(BOT_ENV_FILE_PATH)],
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        if _LOAD_EXPLICIT_VALUES_ONLY.get():
            # ``load`` already merged every permitted source explicitly; do not
            # let pydantic-settings read os.environ or the import-time env_file.
            return (init_settings,)
        return (init_settings, env_settings, dotenv_settings, file_secret_settings)

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
        package fallback ``.env``. Only these three sources are consulted: a
        call with explicit ``environ`` / ``bot_env_file`` is hermetic and never
        reads ambient machine state (see ``_LOAD_EXPLICIT_VALUES_ONLY``). The
        fallback location may be redirected with ``CCC_BOT_ENV_FILE`` in the
        effective environment (used by subprocess tests to isolate a node's
        real package ``.env``); ``bot_env_file`` still wins when passed.
        """
        process_values = dict(os.environ if environ is None else environ)
        root_value = project_root if project_root is not None else process_values.get("PROJECT_ROOT")
        if root_value is None or not str(root_value).strip():
            raise ValueError("PROJECT_ROOT must be non-empty to load runtime settings")
        root = Path(root_value).expanduser().resolve()
        fallback_path = Path(
            bot_env_file or process_values.get("CCC_BOT_ENV_FILE") or BOT_ENV_FILE_PATH
        ).expanduser()
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
        token = _LOAD_EXPLICIT_VALUES_ONLY.set(True)
        try:
            return cls.model_validate(values)
        finally:
            _LOAD_EXPLICIT_VALUES_ONLY.reset(token)

    agent_provider: Literal["claude", "codex"] = Field(
        default="claude",
        alias="CCC_AGENT_PROVIDER",
        description="Agent provider used by ProjectChat.",
    )
    codex_cli_path: str = Field(
        default_factory=lambda: str(Path.home() / ".claude" / "hooks" / "ccc-codex"),
        alias="CCC_CODEX_CLI_PATH",
        description="ccc-node Codex launcher path.",
    )
    usage_meter_enabled: bool = Field(
        default=True,
        alias="CCC_USAGE_METER_ENABLED",
        description=(
            "Durable node-local usage metering (#388): body-free token/request "
            "counters per KST day x provider x interactive/autonomous mode in "
            ".telegram_bot/usage-meter.json. Metering never blocks turns."
        ),
    )
    usage_budget_tokens_claude: int = Field(
        default=0,
        ge=0,
        alias="CCC_USAGE_BUDGET_TOKENS_CLAUDE",
        description=(
            "Daily Claude token budget (input+output) for the usage meter. "
            "0 disables the budget. Crossing warn/enforce thresholds raises "
            "one alert each per day; enforce blocks autonomous spend only."
        ),
    )
    usage_budget_tokens_codex: int = Field(
        default=0,
        ge=0,
        alias="CCC_USAGE_BUDGET_TOKENS_CODEX",
        description=(
            "Daily Codex token budget (input+output) for the usage meter. "
            "0 disables the budget. Crossing warn/enforce thresholds raises "
            "one alert each per day; enforce blocks autonomous spend only."
        ),
    )
    usage_budget_warn_percent: int = Field(
        default=80,
        ge=1,
        le=99,
        alias="CCC_USAGE_BUDGET_WARN_PERCENT",
        description="Early-alarm percentage of a daily token budget.",
    )
    dead_session_wakeup: bool = Field(
        default=False,
        alias="CCC_DEAD_SESSION_WAKEUP",
        description=(
            "Opt-in dead-session wakeup (#364 P2): when a background task "
            "completes after its Claude session died, resume that session for "
            "one bounded autonomous turn so the CLI processes its pending "
            "task notifications and the result reaches the user. Metered as "
            "autonomous spend and gated by the daily budget; default off = "
            "no behavior change. Claude provider only."
        ),
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

    @field_validator("codex_cli_path", mode="before")
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
    claude_unrestricted: bool = Field(
        default=False,
        alias="CCC_BRIDGE_CLAUDE_UNRESTRICTED",
        description=(
            "Opt-in Codex-parity ungoverned Claude execution (owner-operator "
            "only; ignored on every other profile). When true, the Claude SDK "
            "path runs with permission_mode=bypassPermissions, no OS sandbox, "
            "and no host settings chain — so the node's host hooks/settings are "
            "not loaded — matching Codex's never + dangerFullAccess. Memory "
            "context is preserved. Default false keeps the node's normal governed "
            "path (host settings + audit trail); set per node and reversible."
        ),
    )
    telegram_session_scope: Literal[
        "per-user-chat", "shared-groups", "shared-all"
    ] = Field(
        default="per-user-chat",
        alias="CCC_TELEGRAM_SESSION_SCOPE",
        description=(
            "Telegram session boundary. per-user-chat isolates every sender/chat; "
            "shared-groups keeps DMs isolated but shares each group among allowlisted "
            "senders; shared-all routes every allowed DM and group to one conversation."
        ),
    )
    bridge_web_mcp_mode: Literal["off", "searxng-firecrawl"] = Field(
        default="off",
        alias="CCC_BRIDGE_WEB_MCP_MODE",
        description=(
            "Opt-in curated bridge web routing. searxng-firecrawl injects only the "
            "SearXNG search and Firecrawl scrape MCP tools without loading user settings."
        ),
    )
    bridge_searxng_url: Optional[str] = Field(
        default=None,
        alias="CCC_BRIDGE_SEARXNG_URL",
        description="HTTPS SearXNG endpoint used by curated bridge web routing.",
    )
    bridge_firecrawl_api_key: Optional[SecretStr] = Field(
        default=None,
        alias="CCC_BRIDGE_FIRECRAWL_API_KEY",
        description="Firecrawl API key used only by the curated Firecrawl MCP process.",
    )
    image_context_guard: bool = Field(
        default=False,
        alias="CCC_BRIDGE_IMAGE_CONTEXT_GUARD",
        description="Dedupe repeated image reads per request and enforce inbound image caps.",
    )
    telegram_max_image_bytes: int = Field(
        default=5 * 1024 * 1024,
        ge=64 * 1024,
        le=20 * 1024 * 1024,
        alias="CCC_TELEGRAM_MAX_IMAGE_BYTES",
        description="Maximum inbound image payload accepted when the image context guard is on.",
    )
    telegram_max_image_pixels: int = Field(
        default=4_000_000,
        ge=65_536,
        le=40_000_000,
        alias="CCC_TELEGRAM_MAX_IMAGE_PIXELS",
        description="Maximum Telegram photo variant pixel count when image guarding is on.",
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
    push_notify_allowed_chats: List[str] = Field(
        default_factory=list,
        alias="CCC_AGENT_CRON_NOTIFY_ALLOWED_CHATS",
        description=(
            "Allowlist of group/channel chat ids an agent-cron task may target "
            "with notify=telegram-chat* (#665). CSV or JSON list; empty = "
            "record-targeted chat delivery is disabled (fail-closed)."
        ),
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

    @field_validator("push_notify_allowed_chats", mode="before")
    @classmethod
    def parse_push_notify_allowed_chats(cls, v):
        """Parse the notify chat allowlist from CSV or JSON list into strings."""
        if isinstance(v, str):
            value = v.strip()
            if not value:
                return []
            if value.startswith("["):
                parsed = json.loads(value)
                if not isinstance(parsed, list):
                    raise ValueError(
                        "CCC_AGENT_CRON_NOTIFY_ALLOWED_CHATS JSON value must be a list"
                    )
                return [str(x).strip() for x in parsed if str(x).strip()]
            return [x.strip() for x in re.split(r"[,\s]+", value) if x.strip()]
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if str(x).strip()]
        return v

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
    # Memory lifecycle fields live in MemorySettingsMixin
    # (utils/settings_memory.py); heartbeat / health-alerts / task-ledger fields
    # live in HeartbeatSettingsMixin (utils/settings_heartbeat.py); voice /
    # transcription fields live in VoiceSettingsMixin (utils/settings_voice.py).

    # Inbound documents
    max_document_size_mb: int = Field(
        default=10,
        ge=1,
        le=20,
        alias="CCC_MAX_DOCUMENT_SIZE_MB",
        description="Maximum inbound Telegram document size in decimal megabytes",
    )

    @model_validator(mode="after")
    def validate_bridge_web_mcp_config(self):
        if self.bridge_web_mcp_mode == "off":
            return self
        url = str(self.bridge_searxng_url or "").strip().rstrip("/")
        key = self.bridge_firecrawl_api_key
        if not url.startswith("https://"):
            raise ValueError(
                "CCC_BRIDGE_WEB_MCP_MODE=searxng-firecrawl requires an HTTPS "
                "CCC_BRIDGE_SEARXNG_URL."
            )
        if key is None or not key.get_secret_value().strip():
            raise ValueError(
                "CCC_BRIDGE_WEB_MCP_MODE=searxng-firecrawl requires "
                "CCC_BRIDGE_FIRECRAWL_API_KEY."
            )
        self.bridge_searxng_url = url
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
