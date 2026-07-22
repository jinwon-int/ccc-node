"""
Project Chat Handler - Integrates Telegram with Claude Code SDK.
"""

import json
import os
import time
import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from claude_agent_sdk import (
    RateLimitEvent,
    ResultMessage,
)

from telegram_bot.utils.config import config
from telegram_bot.core.task_ledger import (
    TaskLedger,
    ledger_path_for,
)
from telegram_bot.core.heartbeat import (
    compose_heartbeat_text,
    has_recent_visible_progress,
    should_update_heartbeat,
)
from telegram_bot.utils.duration_log import (
    default_duration_log_path,
    forecast_samples,
    remaining_ms,
)
from telegram_bot.core.usage import (
    SNAPSHOT_TTL_SECONDS,
    UsageSnapshot,
    load_claude_status_snapshot,
    local_claude_environment_snapshot,
    merge_usage,
    parse_claude_rate_limit_event,
    parse_claude_result,
    synthesize_service_windows,
)
from telegram_bot.core.usage_meter import MODE_INTERACTIVE, UsageMeter
from telegram_bot.memory.distill_worker import CodexDistillExtractionWorker
from telegram_bot.core.conversation_paths import claude_project_dir_name
from telegram_bot.core.session_scope import stream_key

logger = logging.getLogger(__name__)


from telegram_bot.core.tool_policy import (  # noqa: E402
    EXECUTION_OWNER_OPERATOR,
    EXECUTION_STRICT_PROJECT,
    claude_unrestricted_enabled,
    effective_bash_policy,
    resolve_bash_policy,
    resolve_execution_profile,
    running_as_root,
)

PROCESS_TIMEOUT = int(os.getenv("CLAUDE_PROCESS_TIMEOUT", "21600"))

# Compatibility knobs for legacy direct-construction tests. Production always
# injects Settings and therefore never reads these module values.
EXECUTION_PROFILE = EXECUTION_STRICT_PROJECT
BASH_POLICY = "auto-approve"
CLAUDE_UNRESTRICTED = False


# Pure SDK-stream / text helpers live in core/sdk_text.py (error classification,
# stream-delta extraction, AskUserQuestion formatting, numbered-option detection).
# Re-exported here so existing call sites and
# `from telegram_bot.core.project_chat import _is_...` imports (tests) keep working.
from telegram_bot.core.sdk_text import (  # noqa: E402,F401
    RESTART_INTERRUPT_NOTICE,
    TASK_TERMINATED_NOTICE,
    TERMINAL_STALL_NOTICE,
    CANCEL_REASON_WINDOW_S,
    describe_cancel_reason,
    _is_shutdown_signal_error,
    _is_retryable_sdk_error,
    _format_ask_user_question,
    _extract_stream_text_delta,
    _detect_numbered_options,
)


from telegram_bot.core.project_chat_types import (  # noqa: E402,F401
    AgentSessionEntry,
    ChatResponse,
    AgentApprovalCallback,
    PermissionCallback,
    StatusCallback,
    TypingCallback,
    _PendingRequest,
)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring invalid integer env %s=%r; using %s", name, raw, default)
        return default


TYPING_INTERVAL = 4  # Telegram typing status expires after ~5s
TYPING_MAX_NO_PROGRESS_SECONDS = _env_int("CCC_TYPING_MAX_NO_PROGRESS_SECONDS", 600)

from telegram_bot.core.project_chat_history import ProjectChatHistoryMixin  # noqa: E402
from telegram_bot.core.project_chat_process import ProjectChatProcessMixin  # noqa: E402
from telegram_bot.core.project_chat_state import ProjectChatStateMixin  # noqa: E402


class ProjectChatHandler(
    ProjectChatProcessMixin,
    ProjectChatStateMixin,
    ProjectChatHistoryMixin,
):
    """
    Handles Telegram messages through the provider-neutral AgentRuntime seam.

    Requests for the same Telegram conversation are serialized per conversation
    lock until the runtime turn completes, preserving the bridge's one-turn-at-
    a-time contract per Telegram conversation.
    """

    def __init__(
        self,
        settings: Any = None,
        *,
        agent_runtime: Any = None,
        clock: Any = None,
    ):
        # ``settings=None`` is retained only for legacy unit-test adapters. The
        # production composition root always injects the validated Settings.
        compatibility_mode = settings is None
        self._config = config if compatibility_mode else settings
        root_value = getattr(
            self._config,
            "project_root",
            os.environ.get("PROJECT_ROOT", Path.cwd()),
        )
        self.project_root = Path(root_value).resolve()
        project_dir_name = claude_project_dir_name(self.project_root)
        self.conversations_dir = Path.home() / ".claude" / "projects" / project_dir_name
        profile = (
            EXECUTION_PROFILE
            if compatibility_mode
            else getattr(self._config, "execution_profile", EXECUTION_STRICT_PROJECT)
        )
        policy = (
            BASH_POLICY
            if compatibility_mode
            else getattr(self._config, "bash_policy", None)
        )
        if compatibility_mode:
            self._execution_profile = profile
        else:
            self._execution_profile = resolve_execution_profile(
                profile,
                allowed_user_ids=getattr(self._config, "allowed_user_ids", []),
                require_allowlist=getattr(self._config, "require_allowlist", True),
            )
        self._bash_policy = effective_bash_policy(
            resolve_bash_policy(policy),
            self._execution_profile,
        )
        unrestricted_flag = (
            CLAUDE_UNRESTRICTED
            if compatibility_mode
            else getattr(self._config, "claude_unrestricted", False)
        )
        is_root = running_as_root()
        self._claude_unrestricted = claude_unrestricted_enabled(
            unrestricted_flag, self._execution_profile, is_root=is_root
        )
        if (
            unrestricted_flag is True
            and self._execution_profile == EXECUTION_OWNER_OPERATOR
            and is_root
            and not self._claude_unrestricted
        ):
            logger.warning(
                "CCC_BRIDGE_CLAUDE_UNRESTRICTED is set but ignored under root: "
                "Claude Code refuses bypassPermissions with root/sudo "
                "privileges. Keeping the guard boundary; run the bridge as a "
                "non-root user to enable unrestricted execution."
            )
        provider = getattr(self._config, "agent_provider", "claude")
        if provider == "codex" and agent_runtime is None:
            raise ValueError("Codex ProjectChat requires an injected AgentRuntime")
        # Every provider runs through the provider-neutral AgentRuntime seam
        # (#584 slice C-2 removed the legacy direct Claude SDK path). The
        # composition root always injects a runtime; direct construction
        # without one is a unit-test convenience for pure helpers and fails
        # fast in process_message via _require_runtime.
        self._agent_runtime = agent_runtime
        self._agent_sessions: Dict[Tuple[int, int], AgentSessionEntry] = {}
        self._agent_active_sessions: Dict[Tuple[int, int], Any] = {}
        self._agent_active_generations: Dict[Tuple[int, int], int] = {}
        self._agent_generation_counters: Dict[Tuple[int, int], int] = {}
        self._agent_started_at: Dict[Tuple[int, int], float] = {}
        self._agent_waiting_for_turn: set[Tuple[int, int]] = set()
        self._agent_runtime_closed = False
        self._agent_interrupt_timeout_seconds = 10.0
        self._clock = clock or time
        self._process_timeout_seconds = PROCESS_TIMEOUT
        self._typing_interval_seconds = TYPING_INTERVAL
        self._conversation_locks: Dict[Tuple[int, int], asyncio.Lock] = {}
        self._claude_usage: Dict[Tuple[int, int, str], UsageSnapshot] = {}
        # Rate-limit windows are a property of the single underlying Claude
        # subscription/OAuth credential this node authenticates with, not of
        # any one conversation, so — unlike `_claude_usage` above — this is
        # intentionally NOT scoped per (user_id, chat_id, session_id). It is
        # populated from the SDK's native `RateLimitEvent` stream messages
        # (see `_record_claude_rate_limit`), which fire regardless of which
        # chat's stream happens to be open when the CLI emits them.
        self._claude_rate_limit: Optional[UsageSnapshot] = None
        self._usage_meter: Optional[UsageMeter] = None
        if getattr(self._config, "usage_meter_enabled", True):
            try:
                self._usage_meter = UsageMeter(
                    self.project_root / ".telegram_bot" / "usage-meter.json",
                    budgets={
                        "claude": int(
                            getattr(self._config, "usage_budget_tokens_claude", 0) or 0
                        ),
                        "codex": int(
                            getattr(self._config, "usage_budget_tokens_codex", 0) or 0
                        ),
                    },
                    warn_percent=int(
                        getattr(self._config, "usage_budget_warn_percent", 80) or 80
                    ),
                    alert_sink=self._write_usage_alert_spool,
                )
            except Exception:
                logger.exception(
                    "Usage meter unavailable; continuing without local metering"
                )
        if self._usage_meter is not None and self._agent_runtime is not None:
            set_usage_recorder = getattr(
                self._agent_runtime, "set_usage_recorder", None
            )
            if callable(set_usage_recorder):
                set_usage_recorder(self._usage_meter.record_codex_thread_usage)
            set_turn_attempt_recorder = getattr(
                self._agent_runtime, "set_turn_attempt_recorder", None
            )
            if callable(set_turn_attempt_recorder):
                # The runtime invokes this at its spend boundary (provider
                # accepted turn/start), so cancelled-before-first-event turns
                # still count and pre-boundary failures charge nothing.
                set_turn_attempt_recorder(self.record_agent_turn_request)
        logger.info(f"ProjectChatHandler initialized for {self.project_root}")

    @property
    def usage_meter(self) -> Optional[UsageMeter]:
        """Node-local durable usage meter, when enabled (#388)."""

        return self._usage_meter

    def _write_usage_alert_spool(self, message: str) -> None:
        """Queue one budget alert for owner push delivery (#388).

        Reuses the opt-in push-notifier spool contract: token isolation,
        owner-only target resolution, dedup, and rate limiting all stay in
        PushNotifier. When push is disabled the alert stays log-only (the
        meter already logged it) and nothing accumulates on disk.
        """

        if not bool(getattr(self._config, "push_enabled", False)):
            return
        spool_dir = Path(
            getattr(self._config, "push_spool_dir", None)
            or (Path.home() / ".claude" / "state" / "telegram-spool")
        )
        payload = {
            "event": "usage-budget",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "text": message,
            "dedup": f"usage-budget:{message}",
        }
        try:
            spool_dir.mkdir(parents=True, exist_ok=True)
            target = spool_dir / f"usage-budget-{time.time_ns()}.json"
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except OSError:
            logger.warning("Usage budget alert spool write failed; alert stays log-only")

    def build_distill_extraction_worker(
        self,
        journal: Any,
        backend: Any,
        **worker_kwargs: Any,
    ) -> CodexDistillExtractionWorker:
        """Composition root for distill extraction workers (#465 scheduling).

        Always injects this handler's shared usage meter so autonomous
        extraction spend is gated by the same daily budget that meters
        interactive turns (#388). Callers must not supply their own
        ``usage_meter`` — the gate is a composition invariant, not an option.
        """

        if "usage_meter" in worker_kwargs:
            raise ValueError(
                "usage_meter is injected by the composition root; do not pass it"
            )
        return CodexDistillExtractionWorker(
            journal,
            backend,
            usage_meter=self._usage_meter,
            **worker_kwargs,
        )

    def _claude_usage_totals(self, message: Any) -> Tuple[int, int]:
        snapshot = parse_claude_result(message, observed_at=self._clock.time())
        input_total = snapshot.context_used
        if input_total is None:
            input_total = snapshot.input_tokens or 0
        return input_total, snapshot.output_tokens or 0

    def _meter_claude_tokens(
        self, delta: Tuple[int, int], mode: str = MODE_INTERACTIVE
    ) -> None:
        if self._usage_meter is None:
            return
        try:
            self._usage_meter.record(
                "claude",
                mode,
                input_tokens=delta[0],
                output_tokens=delta[1],
            )
        except Exception:
            logger.exception("Claude usage metering failed; turn continues")

    def record_agent_turn_request(self) -> None:
        """Count one completed interactive agent-runtime turn, fail-open."""

        if self._usage_meter is None:
            return
        provider = getattr(self._config, "agent_provider", "claude")
        try:
            self._usage_meter.record(provider, MODE_INTERACTIVE, requests=1)
        except Exception:
            logger.exception("Interactive usage metering failed; turn continues")

    def record_claude_adapter_attempt(self, mode: str = MODE_INTERACTIVE) -> None:
        """Meter one Claude adapter-path request at its spend boundary (#388).

        Claude adapter-path spend boundary (#584): the first runtime event of
        a turn proves the provider accepted the request, so cancellation after
        any output still charges exactly one request. Codex meters at its own
        runtime spend boundary via ``set_turn_attempt_recorder``; any runtime
        exposing that seam meters itself and this helper stays a no-op.

        ``mode`` distinguishes user turns (interactive, the default) from
        bridge-initiated turns such as the #364 dead-session wakeup, which
        meter as autonomous so the #388 budget gate governs them.
        """

        if self._usage_meter is None:
            return
        if getattr(self._config, "agent_provider", "claude") != "claude":
            return
        if callable(getattr(self._agent_runtime, "set_turn_attempt_recorder", None)):
            return
        try:
            self._usage_meter.record("claude", mode, requests=1)
        except Exception:
            logger.exception("Claude request metering failed; turn continues")

    def record_claude_adapter_result(
        self, event: Any, mode: str = MODE_INTERACTIVE
    ) -> None:
        """Meter Claude adapter-path tokens from the terminal ResultEvent (#388).

        ClaudeRuntime carries the SDK ResultMessage usage block in its
        ResultEvent payload, so the adapter path meters the validated
        input/output totals ``parse_claude_result`` derives (raw +
        cache-creation + cache-read input). Codex tokens meter through the
        runtime's ``set_usage_recorder`` seam and are excluded here by the
        provider check, so nothing double charges. Known gap: a turn that
        terminates in ErrorEvent emits no ResultEvent, so its tokens are not
        metered; the request itself is still counted at the spend boundary.
        """

        if self._usage_meter is None:
            return
        if getattr(self._config, "agent_provider", "claude") != "claude":
            return
        payload = getattr(event, "result", None)
        usage = payload.get("usage") if isinstance(payload, Mapping) else None
        if not isinstance(usage, Mapping):
            return
        from types import SimpleNamespace as _NS

        delta = self._claude_usage_totals(
            _NS(usage=dict(usage), model_usage={}, total_cost_usd=None)
        )
        if any(delta):
            self._meter_claude_tokens(delta, mode=mode)

    def _stream_key(self, user_id: int, chat_id: int) -> Tuple[int, int]:
        return stream_key(
            getattr(self._config, "telegram_session_scope", "per-user-chat"),
            user_id,
            chat_id,
        )

    def record_claude_result_snapshot(
        self, user_id: int, chat_id: int, msg: ResultMessage
    ) -> None:
        """Cache one terminal ResultMessage's usage/cost snapshot for /usage.

        Fed by the adapter path via the ``set_sdk_frame_observer`` seam (#584
        C-1 follow-up). This is what ``get_usage`` reads for the Context /
        Session tokens / Session cost lines.
        """

        session_id = msg.session_id
        if not isinstance(session_id, str) or not session_id:
            return
        key = (user_id, chat_id, session_id)
        snapshot = parse_claude_result(msg, observed_at=self._clock.time())
        self._claude_usage[key] = snapshot
        self._claude_usage = dict(tuple(self._claude_usage.items())[-128:])

    def _record_claude_rate_limit(self, msg: RateLimitEvent) -> None:
        parsed = parse_claude_rate_limit_event(msg, observed_at=self._clock.time())
        # Keep window-less, overage-less events out so they cannot dilute the
        # accumulated snapshot; overage-only events still carry state to keep.
        if not parsed.windows and parsed.overage_status is None:
            return
        self._claude_rate_limit = (
            merge_usage(self._claude_rate_limit, parsed)
            if self._claude_rate_limit is not None
            else parsed
        )

    async def get_usage(
        self, user_id: int, chat_id: int, session_id: str | None
    ) -> UsageSnapshot:
        """Return provider usage already observed for this exact conversation."""

        if self._agent_runtime is not None:
            runtime = self._require_runtime()
            get_usage = getattr(runtime, "get_usage", None)
            if get_usage is not None:
                return await asyncio.wait_for(get_usage(session_id), timeout=7.0)
            if getattr(self._config, "agent_provider", "claude") != "claude":
                return UsageSnapshot(provider="codex")
            # Claude adapter path (#584): ClaudeRuntime exposes no usage
            # endpoint, so fall through to the local aggregation below
            # (status-file snapshots and observed rate-limit windows).

        # Base carries non-secret local provider environment (service label,
        # configured model/effort/context cap) so third-party Claude-compatible
        # backends without rate-limit events still render meaningfully;
        # observed snapshots below always override it.
        result = local_claude_environment_snapshot()
        if not session_id:
            return result
        cached = self._claude_usage.get((user_id, chat_id, session_id))
        now = self._clock.time()
        if (
            cached is not None
            and cached.observed_at is not None
            and now - cached.observed_at <= SNAPSHOT_TTL_SECONDS
        ):
            result = merge_usage(result, cached)
        state_root = Path(
            os.environ.get(
                "CCC_STATE_DIR", str(Path(self._config.claude_settings_path).parent / "state")
            )
        )
        status = load_claude_status_snapshot(
            state_root / "usage", session_id, now=now
        )
        if status is not None:
            result = merge_usage(result, status)
        # Global, not session-scoped by design — see `_claude_rate_limit`.
        # getattr guards test fixtures that build the handler via __new__
        # without running __init__.
        rate_limit = getattr(self, "_claude_rate_limit", None)
        if rate_limit is not None:
            result = merge_usage(result, rate_limit)
        # Third-party services (e.g. Kimi Code) publish no quota data, so no
        # observed window ever arrives; fall back to the meter's local
        # rolling-window estimate so /usage is not stuck on "unavailable".
        # Real observed windows always win — synthesis only fills an empty set.
        if result.service is not None and not result.windows:
            meter = getattr(self, "_usage_meter", None)
            if meter is not None:
                try:
                    rolling = meter.rolling_usage().get(result.provider)
                    period = getattr(meter, "period_usage", None)
                    weekly = (
                        period(days=7).get(result.provider)
                        if period is not None
                        else None
                    )
                    windows = synthesize_service_windows(
                        result.service, rolling, weekly
                    )
                except Exception:
                    logger.debug("Local service window synthesis failed")
                    windows = ()
                if windows:
                    result = merge_usage(
                        result,
                        UsageSnapshot(provider=result.provider, windows=windows),
                    )
        return result

    def _get_conversation_lock(self, user_id: int, chat_id: int) -> asyncio.Lock:
        key = self._stream_key(user_id, chat_id)
        lock = self._conversation_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._conversation_locks[key] = lock
        return lock

    def workload_snapshot(self, now: float) -> tuple[int, float]:
        """Return ``(in_flight_count, oldest_request_age_seconds)``.

        Exposes bridge busyness so an external supervisor (the self-update
        procedure) can avoid restarting the bridge mid-request — a restart
        SIGTERM-kills the in-flight agent child process and destroys the
        user's work. ``now`` must come from the event loop clock so it is
        comparable to the recorded turn start times.
        """
        count = len(self._agent_active_sessions)
        oldest_started: Optional[float] = None
        for started_at in self._agent_started_at.values():
            if oldest_started is None or started_at < oldest_started:
                oldest_started = started_at
        oldest_age = (now - oldest_started) if oldest_started is not None else 0.0
        return count, max(0.0, oldest_age)

    def waiting_for_turn_snapshot(self) -> int:
        """Requests registered by the bridge but not admitted by a runtime."""

        return len(self._agent_waiting_for_turn)

    @property
    def _task_ledger(self):
        """Lazy persistent task ledger; None when no data dir is configured."""
        cached = getattr(self, "_task_ledger_cache", None)
        if cached is not None:
            return cached or None  # False sentinel = resolved to unavailable
        path = ledger_path_for(
            getattr(config, "bot_data_dir", None),
            getattr(config, "task_ledger_path", None),
        )
        self._task_ledger_cache = TaskLedger(path) if path else False
        return self._task_ledger_cache or None

    def _ledger_create(self, user_id: int, chat_id: int):
        led = self._task_ledger
        return led.create(user_id, chat_id) if led else None

    def _ledger_finish(self, req: _PendingRequest, state: str, *, cleanup_done: bool) -> None:
        led = self._task_ledger
        if led and req.task_id:
            led.finish(req.task_id, state, cleanup_done=cleanup_done)

    async def _cleanup_heartbeat(self, req: _PendingRequest) -> bool:
        """Delete/clear the transient heartbeat message for a request.

        Returns True when there is nothing left to clean (no message, or the
        delete went through) — False when the delete failed, so the caller's
        terminal transition keeps a retryable op in the task ledger.
        """
        if not req.status_callback or req.heartbeat_message_id is None:
            return True
        cleaned = False
        try:
            cleaned = (await req.status_callback(None, req.heartbeat_message_id)) is None
        except Exception as e:
            logger.warning(
                "Heartbeat cleanup failed for user %s chat %s: %s",
                req.user_id,
                req.chat_id,
                type(e).__name__,
            )
        if cleaned:
            req.heartbeat_message_id = None
            led = self._task_ledger
            if led and req.task_id:
                # Offload the (now fsync-backed) ledger write off the event loop
                # so a heartbeat-path mutation never stalls message delivery.
                await asyncio.to_thread(led.set_status_message, req.task_id, None)
        return cleaned

    async def _maybe_update_heartbeat(self, req: _PendingRequest, now: float) -> None:
        """Send or edit a fail-open long-running task heartbeat."""
        if not getattr(config, "heartbeat_enabled", True):
            return
        if not req.status_callback or req.future.done():
            return

        # Stall guard: if the SDK stream has gone silent for too long the request
        # is stuck (e.g. a bridge restart left it in flight, or the stream hung)
        # and will never reach the terminal ResultMessage that deletes the
        # heartbeat. Remove the dangling "⏳ Working — Nm" line rather than let it
        # tick up forever as the last chat message. It reappears if activity
        # resumes (last_event_at advances on the next SDK event).
        stall_seconds = float(getattr(config, "heartbeat_stall_seconds", 0.0) or 0.0)
        if stall_seconds > 0:
            last_event = req.last_event_at or req.started_at
            if last_event > 0 and now - last_event >= stall_seconds:
                if req.heartbeat_message_id is not None:
                    await self._cleanup_heartbeat(req)
                return

        threshold = float(getattr(config, "heartbeat_threshold_seconds", 15.0))
        interval = float(getattr(config, "heartbeat_update_interval_seconds", 15.0))
        if not should_update_heartbeat(
            now=now,
            started_at=req.started_at,
            last_update_at=req.heartbeat_last_update_at,
            threshold_seconds=threshold,
            update_interval_seconds=interval,
        ):
            return

        if (
            getattr(config, "heartbeat_suppress_when_streaming_progress", True)
            and req.streaming_handler
            and getattr(req.streaming_handler, "drafts", None)
            and has_recent_visible_progress(
                now=now,
                last_visible_progress_at=req.last_visible_progress_at,
                window_seconds=threshold,
            )
        ):
            return

        self._load_heartbeat_forecast(req)
        # Recompute the ETA on every tick as a REMAINING-time estimate
        # conditioned on the samples still longer than the current elapsed time
        # (see duration_log.remaining_ms) — a fixed total-median forecast goes
        # stale and reads absurd once elapsed exceeds it.
        elapsed = now - req.started_at
        forecast_remaining_ms = (
            remaining_ms(
                req.heartbeat_forecast_samples,
                elapsed_ms=int(elapsed * 1000),
            )
            if req.heartbeat_forecast_samples
            else None
        )
        text = compose_heartbeat_text(
            elapsed_seconds=elapsed,
            current_tool=req.current_tool_label,
            forecast_seconds=(forecast_remaining_ms / 1000.0)
            if forecast_remaining_ms is not None
            else None,
        )
        try:
            previous_id = req.heartbeat_message_id
            message_id = await req.status_callback(text, req.heartbeat_message_id)
            req.heartbeat_message_id = message_id
            req.heartbeat_last_update_at = now
            # Register the projection in the task ledger so a terminal
            # transition (or a restart's reconciliation) can always clean it.
            if message_id != previous_id:
                led = self._task_ledger
                if led and req.task_id:
                    # Offload the (now fsync-backed) ledger write off the event
                    # loop so a heartbeat-path mutation never stalls delivery.
                    await asyncio.to_thread(
                        led.set_status_message, req.task_id, message_id
                    )
        except Exception as e:
            logger.warning(
                "Heartbeat update failed for user %s chat %s: %s",
                req.user_id,
                req.chat_id,
                type(e).__name__,
            )

    def _duration_log_path(self) -> Path:
        path = getattr(self._config, "heartbeat_duration_log_path", None)
        if path is None:
            bot_data_dir = (
                getattr(self._config, "bot_data_dir", None)
                or self.project_root / ".telegram_bot"
            )
            return default_duration_log_path(
                Path(bot_data_dir)
            )
        return Path(path)

    def _load_heartbeat_forecast(self, req: _PendingRequest) -> None:
        """Load the duration samples the ETA conditions on (once per request).

        Only the sample list is cached — the remaining-time estimate itself is
        recomputed from it on every heartbeat tick so it tracks elapsed time.
        """
        if req.heartbeat_forecast_loaded:
            return
        req.heartbeat_forecast_loaded = True
        if not getattr(config, "heartbeat_forecast_enabled", False):
            return
        req.heartbeat_forecast_samples = forecast_samples(
            self._duration_log_path(),
            user_id=req.user_id,
            model=req.model,
            min_samples=int(getattr(config, "heartbeat_forecast_min_samples", 10)),
        )

    def _should_refresh_typing(self, req: _PendingRequest, now: float) -> bool:
        """Return whether Telegram typing should still be asserted for a request."""
        if req.future.done() or req.awaiting_permission:
            return False
        # After any visible draft/tool progress, stop typing entirely. Telegram
        # draft edits do not clear typing; progress/heartbeat should represent
        # the work from here instead of reasserting a stale chat action.
        if req.last_visible_progress_at > 0:
            return False
        if (
            TYPING_MAX_NO_PROGRESS_SECONDS > 0
            and req.started_at > 0
            and now - req.started_at >= TYPING_MAX_NO_PROGRESS_SECONDS
        ):
            return False
        return True
