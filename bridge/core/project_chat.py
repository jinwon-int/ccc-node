"""
Project Chat Handler - Integrates Telegram with Claude Code SDK.
"""

import json
import os
import time
import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
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
from telegram_bot.core.request_lifecycle import RequestPhase
from telegram_bot.core.heartbeat import (
    compose_heartbeat_text,
    has_recent_visible_progress,
    should_update_heartbeat,
)
from telegram_bot.utils.duration_log import (
    append_duration_sample,
    default_duration_log_path,
    forecast_samples,
    remaining_ms,
)
from telegram_bot.core.usage import (
    SNAPSHOT_TTL_SECONDS,
    UsageSnapshot,
    delta_from_snapshots,
    load_claude_status_snapshot,
    local_claude_environment_snapshot,
    local_piri_environment_snapshot,
    merge_usage,
    parse_claude_rate_limit_event,
    parse_claude_result,
    synthesize_service_windows,
)
from telegram_bot.core.usage_cost_ledger import CostLedger
from telegram_bot.core.usage_meter import MODE_INTERACTIVE, UsageMeter
from telegram_bot.core.async_completion_event import (
    NormalizedAsyncCompletionEvent,
)
from telegram_bot.core.async_completion_journal import AsyncCompletionJournal
from telegram_bot.core.async_completion_delivery import (
    AsyncCompletionDeliveryCoordinator,
    AsyncCompletionReclaimer,
)
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
    ChatResponse,
    AgentApprovalCallback,
    PermissionCallback,
    StatusCallback,
    TypingCallback,
    _PendingRequest,
)
from telegram_bot.core.agent_session_registry import AgentSessionRegistry  # noqa: E402


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
from telegram_bot.core.lifecycle_audit import build_lifecycle_observer  # noqa: E402
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
        policy = BASH_POLICY if compatibility_mode else getattr(self._config, "bash_policy", None)
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
        if provider in {"codex", "piri"} and agent_runtime is None:
            raise ValueError(f"{provider.title()} ProjectChat requires an injected AgentRuntime")
        # Every provider runs through the provider-neutral AgentRuntime seam
        # (#584 slice C-2 removed the legacy direct Claude SDK path). The
        # composition root always injects a runtime; direct construction
        # without one is a unit-test convenience for pure helpers and fails
        # fast in process_message via _require_runtime.
        self._agent_runtime = agent_runtime
        self._agent_session_registry = AgentSessionRegistry()
        self._agent_runtime_closed = False
        # Durable, body-free journal for out-of-turn completions (#646 slice
        # 1). Construction stays in-memory only to preserve the composition
        # root's deferred-initialization invariant (no filesystem access
        # before the first observation); initialization, stale-claim recovery,
        # and listener registration are all fail-open so any failure keeps
        # the degraded no-delivery boundary and only loses the evidence trail.
        self._async_completion_journal: Optional[AsyncCompletionJournal] = None
        self._async_completion_recovery_done = False
        # Delivery seams for durable-capable runtimes (#646 slice 2). The
        # sender is attached by the lifecycle layer once the bot exists; the
        # coordinator is built lazily from wired seams on first use.
        self._async_completion_sender: Any = None
        self._async_completion_delivery: Optional[AsyncCompletionDeliveryCoordinator] = None
        self._async_completion_reclaimer: Optional[AsyncCompletionReclaimer] = None
        # Strong references to in-flight delivery tasks: the loop keeps only
        # weak references, so an untracked task may be collected mid-flight
        # (#1479). Deliberately never cancelled — the journal owns recovery.
        self._async_completion_tasks: set[asyncio.Task[None]] = set()
        try:
            self._async_completion_journal = AsyncCompletionJournal(
                self.project_root / ".telegram_bot" / "async-completions"
            )
            # Observer registration is mode-exclusive (#646): a runtime that
            # declares durable delivery gets only the promotion observer —
            # the degraded observer would otherwise journal the identity as
            # evidence-only first, and the promotion observe would then be
            # rejected as a duplicate. Degraded runtimes keep the slice-1
            # observer and never promote.
            if self._runtime_declares_durable_delivery():
                durable_setter = getattr(
                    self._agent_runtime,
                    "set_durable_completion_listener",
                    None,
                )
                if callable(durable_setter):
                    durable_setter(self._observe_durable_codex_completion)
            else:
                listener_setter = getattr(
                    self._agent_runtime,
                    "set_unowned_completion_listener",
                    None,
                )
                if callable(listener_setter):
                    listener_setter(self._observe_unowned_codex_completion)
        except Exception:
            self._async_completion_journal = None
            logger.exception(
                "Async completion journal unavailable; continuing without "
                "durable completion evidence"
            )
        self._shutdown_draining = False
        self._agent_interrupt_timeout_seconds = 10.0
        self._session_guard_lock = asyncio.Lock()
        # Orders the offloaded task-ledger writes (see _run_ledger_write).
        self._task_ledger_write_lock = asyncio.Lock()
        self._session_guard_evictions = 0
        self._session_guard_runtime_recycles = 0
        self._session_guard_last_tree_rss_mb = 0.0
        self._agent_session_attachments = 0
        self._agent_runtime_recycle_pending = False
        self._clock = clock or time
        # Opt-in lifecycle audit observer (#645); None on a default node.
        self._lifecycle_observer = build_lifecycle_observer(self._config)
        # Production always injects validated Settings. The fallback preserves
        # legacy lightweight test adapters that pass a partial namespace.
        self._process_timeout_seconds = int(
            getattr(self._config, "process_timeout_seconds", 21600)
        )
        self._typing_interval_seconds = TYPING_INTERVAL
        self._conversation_locks: Dict[Tuple[int, int], asyncio.Lock] = {}
        # Requests admitted to a conversation but not yet finished, including
        # the one holding the lock. A value above 1 means later messages are
        # already waiting behind the current turn, which is the only in-process
        # signal that a turn's empty output was coalesced rather than lost
        # (#1128). Kept here beside the lock it mirrors so both share a key.
        self._conversation_pending: Dict[Tuple[int, int], int] = {}
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
        self._cost_ledger: Optional[CostLedger] = None
        if getattr(self._config, "usage_meter_enabled", True):
            try:
                self._usage_meter = UsageMeter(
                    self.project_root / ".telegram_bot" / "usage-meter.json",
                    budgets={
                        "claude": int(getattr(self._config, "usage_budget_tokens_claude", 0) or 0),
                        "codex": int(getattr(self._config, "usage_budget_tokens_codex", 0) or 0),
                        "piri": int(getattr(self._config, "usage_budget_tokens_piri", 0) or 0),
                    },
                    warn_percent=int(getattr(self._config, "usage_budget_warn_percent", 80) or 80),
                    alert_sink=self._write_usage_alert_spool,
                )
            except Exception:
                logger.exception("Usage meter unavailable; continuing without local metering")
            try:
                self._cost_ledger = CostLedger(
                    self.project_root / ".telegram_bot" / "usage-cost-ledger.jsonl",
                    clock=self._clock.time,
                )
            except Exception:
                logger.exception("Cost ledger unavailable; continuing without spend tracking")
        if self._usage_meter is not None and self._agent_runtime is not None:
            set_usage_recorder = getattr(self._agent_runtime, "set_usage_recorder", None)
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

    def render_cost_report(self, *, days: int = 7) -> str:
        """Render the additive per-model spend block for ``/usage``.

        Backed by :class:`CostLedger`; returns an empty string when the ledger
        is disabled or has no cost data for the window. Never raises.
        """

        if self._cost_ledger is None:
            return ""
        try:
            return self._cost_ledger.render_report(days=days)
        except Exception:
            logger.debug("Cost ledger report failed", exc_info=True)
            return ""

    def _write_usage_alert_spool(self, message: str) -> None:
        """Queue one budget alert for owner push delivery (#388).

        Reuses the opt-in push-notifier spool contract: token isolation,
        owner-only target resolution, dedup, and rate limiting all stay in
        PushNotifier. When push is disabled the alert stays log-only (the
        meter already logged it) and nothing accumulates on disk.
        """

        self._write_owner_notice_spool(
            "usage-budget", message, f"usage-budget:{message}"
        )

    def _write_owner_notice_spool(
        self, event: str, message: str, dedup: str
    ) -> None:
        """Queue one body-free owner push notice via the spool contract."""

        if not bool(getattr(self._config, "push_enabled", False)):
            return
        spool_dir = Path(
            getattr(self._config, "push_spool_dir", None)
            or (Path.home() / ".claude" / "state" / "telegram-spool")
        )
        payload = {
            "event": event,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "text": message,
            "dedup": dedup,
        }
        try:
            spool_dir.mkdir(parents=True, exist_ok=True)
            target = spool_dir / f"{event}-{time.time_ns()}.json"
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except OSError:
            logger.warning("Owner notice spool write failed; notice stays log-only")

    def _runtime_declares_durable_delivery(self) -> bool:
        """Whether the injected runtime claims durable delivery (#646)."""

        capability_getter = getattr(
            self._agent_runtime, "async_completion_capability", None
        )
        if not callable(capability_getter):
            return False
        try:
            capability = capability_getter()
        except Exception:
            return False
        return bool(getattr(capability, "supports_durable_delivery", False))

    def set_async_completion_sender(self, sender: Any) -> None:
        """Wire the conversation delivery seam for durable completions (#646).

        ``sender(user_id, chat_id, text) -> bool`` is built by the lifecycle
        layer once the bot handle exists.  Until it is wired, durable-capable
        observations fall back to the slice-1 owner notice — the capability
        declaration alone never enables delivery.
        """

        self._async_completion_sender = sender
        # A newly wired sender invalidates any coordinator built without one.
        self._async_completion_delivery = None
        self._async_completion_reclaimer = None

    def _init_async_completion_journal(self) -> AsyncCompletionJournal | None:
        """Initialize the journal once; return ``None`` when unavailable.

        Shared fail-open initialization for both #646 observer seams: any
        failure keeps the conversation path working and only loses the
        durable evidence trail.
        """

        journal = self._async_completion_journal
        if journal is None:
            return None
        try:
            journal.initialize()
            if not self._async_completion_recovery_done:
                self._async_completion_recovery_done = True
                recovered = journal.recover_stale_claimed()
                if recovered:
                    logger.warning(
                        "Recovered %d stale claimed async completion record(s)",
                        len(recovered),
                    )
        except Exception:
            self._async_completion_journal = None
            logger.exception(
                "Async completion journal initialization failed; continuing "
                "without durable completion evidence"
            )
            return None
        return journal

    def _delivery_for(self, journal: AsyncCompletionJournal) -> Any:
        """Build (once) the delivery coordinator from wired seams."""

        if self._async_completion_delivery is not None:
            return self._async_completion_delivery
        sender = self._async_completion_sender
        if sender is None:
            return None
        self._async_completion_delivery = AsyncCompletionDeliveryCoordinator(
            journal,
            lock_factory=self._get_conversation_lock,
            sender=sender,
            generation_probe=lambda user_id, chat_id: (
                self._agent_session_registry.generation_high_water(
                    (user_id, chat_id)
                )
            ),
        )
        return self._async_completion_delivery

    def _reclaimer_for(self, journal: AsyncCompletionJournal) -> Any:
        """Build (once) the next-turn reclaimer from wired seams."""

        if self._async_completion_reclaimer is not None:
            return self._async_completion_reclaimer
        sender = self._async_completion_sender
        if sender is None:
            return None
        self._async_completion_reclaimer = AsyncCompletionReclaimer(
            journal,
            sender=sender,
        )
        return self._async_completion_reclaimer

    async def _maybe_reclaim_async_completions(
        self, user_id: int, chat_id: int, usage_mode: str
    ) -> None:
        """Drain route-bound queued completions on a user turn (#646 slice 3).

        Called inside the conversation turn (the caller holds the conversation
        lock), before the turn's response machinery starts, so the body-free
        reclaim notice arrives before the reply.  Interactive turns only —
        autonomous wakeup turns must not consume the user's reclaim window.
        Fail-open: any failure keeps the turn path working.
        """

        if usage_mode != MODE_INTERACTIVE:
            return
        # Route-bound records can only exist under a runtime that declares
        # durable delivery, so a degraded runtime (every production provider
        # today) skips the journal scan entirely — the per-turn hot path
        # stays free of filesystem work.
        if not self._runtime_declares_durable_delivery():
            return
        journal = self._init_async_completion_journal()
        if journal is None:
            return
        reclaimer = self._reclaimer_for(journal)
        if reclaimer is None:
            return
        try:
            reclaimed = await reclaimer.reclaim_for_route(user_id, chat_id)
        except Exception:
            logger.warning(
                "Async completion reclaim failed; turn continues",
                exc_info=True,
            )
            return
        if reclaimed:
            logger.info(
                "Reclaimed %d route-bound async completion(s) for user %s "
                "chat %s (body-free next-turn notice)",
                reclaimed,
                user_id,
                chat_id,
            )

    def _observe_unowned_codex_completion(
        self, thread_id: str, turn_id: str
    ) -> None:
        """Journal one validated unowned Codex completion (#646 slice 1).

        Runs inline on the runtime's notification path, so it must stay
        synchronous and fast. Attribution is fail-closed: without a resident
        cached session owning the thread, or without a lifecycle generation
        for that route, no normalized identity is fabricated and only the
        runtime's own degraded counters remember the observation. The owner
        fallback notice is at-most-once per identity (``mark_noticed`` first,
        spool second) so a restart can never re-notify an old record.
        """

        journal = self._init_async_completion_journal()
        if journal is None:
            return
        key = self._agent_session_registry.find_route_by_session_id(thread_id)
        if key is None:
            return
        generation = self._agent_session_registry.generation_high_water(key)
        if generation < 1:
            return
        user_id, chat_id = key
        conversation_route_id = (
            str(user_id) if user_id == chat_id else f"{user_id}:{chat_id}"
        )
        event = NormalizedAsyncCompletionEvent(
            provider="codex",
            thread_id=thread_id,
            conversation_route_id=conversation_route_id,
            session_generation=generation,
            turn_id=turn_id,
        )
        if not journal.observe(event):
            return
        journal.mark_noticed(event.idempotency_key)
        logger.warning(
            "Unowned Codex completion recorded (degraded, no delivery); "
            "owner fallback notice queued"
        )
        self._write_owner_notice_spool(
            "async-completion",
            "Unowned Codex turn completed on a live thread — recorded at "
            "the degraded boundary (no auto-delivery; #646)",
            event.idempotency_key,
        )

    def _observe_durable_codex_completion(
        self, thread_id: str, turn_id: str, text: str | None
    ) -> None:
        """Promote one validated unowned completion to durable delivery.

        Invoked by the runtime only when its capability declares
        ``supports_durable_delivery`` (#646 slice 2).  The journal is the
        exactly-once gate: only the first observation of an identity schedules
        a delivery task, and every failure mode stays inside the journal state
        machine.  Without a wired sender or a running loop the observation
        degrades to the slice-1 owner-notice path — capability alone never
        delivers.
        """

        journal = self._init_async_completion_journal()
        if journal is None:
            return
        key = self._agent_session_registry.find_route_by_session_id(thread_id)
        if key is None:
            return
        generation = self._agent_session_registry.generation_high_water(key)
        if generation < 1:
            return
        user_id, chat_id = key
        conversation_route_id = (
            str(user_id) if user_id == chat_id else f"{user_id}:{chat_id}"
        )
        event = NormalizedAsyncCompletionEvent(
            provider="codex",
            thread_id=thread_id,
            conversation_route_id=conversation_route_id,
            session_generation=generation,
            turn_id=turn_id,
        )
        coordinator = self._delivery_for(journal)
        if coordinator is None:
            # No delivery seam wired (e.g. lifecycle never attached the bot):
            # keep the evidence and use the at-most-once owner fallback.
            if journal.observe(event):
                journal.mark_noticed(event.idempotency_key)
                logger.warning(
                    "Durable-capable Codex completion observed without a "
                    "delivery seam; owner fallback notice queued"
                )
                self._write_owner_notice_spool(
                    "async-completion",
                    "Codex async completion recorded without a delivery "
                    "seam — no conversation delivery (no auto-delivery; #646)",
                    event.idempotency_key,
                )
            return
        if not journal.observe(event, deliverable=True):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "Durable completion observed outside an event loop; "
                "delivery skipped and record kept queued"
            )
            return
        task = loop.create_task(
            self._deliver_durable_codex_completion(
                journal, coordinator, event, user_id, chat_id, text
            )
        )
        self._async_completion_tasks.add(task)
        task.add_done_callback(self._async_completion_tasks.discard)

    async def _deliver_durable_codex_completion(
        self,
        journal: AsyncCompletionJournal,
        coordinator: Any,
        event: NormalizedAsyncCompletionEvent,
        user_id: int,
        chat_id: int,
        text: str | None,
    ) -> None:
        """Run one delivery to the end (never raises to the caller's loop)."""

        try:
            delivered = await coordinator.deliver(
                event.idempotency_key,
                user_id=user_id,
                chat_id=chat_id,
                session_generation=event.session_generation,
                text=text,
            )
        except Exception:
            logger.exception(
                "Async completion delivery task crashed; record kept for "
                "stale-claim recovery"
            )
            return
        if delivered:
            logger.info("Async completion delivered to conversation (#646)")
            return
        record = journal.get(event.idempotency_key)
        if record is None or record.noticed_at is not None:
            return
        journal.mark_noticed(event.idempotency_key)
        self._write_owner_notice_spool(
            "async-completion",
            "Codex async completion could not be delivered to its "
            "conversation — terminal at the delivery boundary (#646)",
            event.idempotency_key,
        )

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
            raise ValueError("usage_meter is injected by the composition root; do not pass it")
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

    def _meter_claude_tokens(self, delta: Tuple[int, int], mode: str = MODE_INTERACTIVE) -> None:
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
        provider = getattr(self._config, "agent_provider", "claude")
        if provider not in {"claude", "piri"}:
            return
        if callable(getattr(self._agent_runtime, "set_turn_attempt_recorder", None)):
            return
        try:
            self._usage_meter.record(provider, mode, requests=1)
        except Exception:
            logger.exception("Runtime request metering failed; turn continues")

    def record_claude_adapter_result(self, event: Any, mode: str = MODE_INTERACTIVE) -> None:
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

    def record_claude_result_snapshot(self, user_id: int, chat_id: int, msg: ResultMessage) -> None:
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
        cost_ledger = getattr(self, "_cost_ledger", None)
        if cost_ledger is not None:
            # The SDK's ResultMessage totals are session-cumulative running
            # totals, zeroed on ConversationResetMessage — appending them raw
            # re-counted every earlier turn in the window (#1205 D-3). Write
            # the per-turn delta against the previous cached snapshot for this
            # session key instead; a reset surfaces as a new key (no previous)
            # or a backwards total, both handled inside delta_from_snapshots.
            previous = self._claude_usage.get(key)
            cost_ledger.record_snapshot(
                delta_from_snapshots(previous, snapshot),
                provider="claude",
                session_id=session_id,
            )
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

    async def get_usage(self, user_id: int, chat_id: int, session_id: str | None) -> UsageSnapshot:
        """Return provider usage already observed for this exact conversation."""

        if self._agent_runtime is not None:
            runtime = self._require_runtime()
            get_usage = getattr(runtime, "get_usage", None)
            if get_usage is not None:
                return await asyncio.wait_for(get_usage(session_id), timeout=7.0)
            provider = str(getattr(self._config, "agent_provider", "claude"))
            if provider == "piri":
                # PiriRuntime exposes no usage endpoint and Piri reports no
                # token/quota telemetry, so the only usable signal is the
                # local meter — but synthesis is keyed by service, which the
                # bare snapshot lacks. Name it, then fill from the meter.
                return self._fill_local_service_windows(
                    local_piri_environment_snapshot()
                )
            if provider != "claude":
                return UsageSnapshot(provider=provider)
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
                "CCC_STATE_DIR",
                str(Path(self._config.claude_settings_path).parent / "state"),
            )
        )
        status = load_claude_status_snapshot(state_root / "usage", session_id, now=now)
        if status is not None:
            result = merge_usage(result, status)
        # Global, not session-scoped by design — see `_claude_rate_limit`.
        # getattr guards test fixtures that build the handler via __new__
        # without running __init__.
        rate_limit = getattr(self, "_claude_rate_limit", None)
        if rate_limit is not None:
            result = merge_usage(result, rate_limit)
        return self._fill_local_service_windows(result)

    def _fill_local_service_windows(self, result: UsageSnapshot) -> UsageSnapshot:
        """Fill empty rate-limit windows from the local meter estimate.

        Third-party services (e.g. Kimi Code) publish no quota data, so no
        observed window ever arrives; fall back to the meter's local
        rolling-window estimate so /usage is not stuck on "unavailable".
        Real observed windows always win — synthesis only fills an empty set,
        and a snapshot without a service is returned untouched.
        """
        if result.service is None or result.windows:
            return result
        meter = getattr(self, "_usage_meter", None)
        if meter is None:
            return result
        try:
            rolling = meter.rolling_usage().get(result.provider)
            period = getattr(meter, "period_usage", None)
            weekly = period(days=7).get(result.provider) if period is not None else None
            windows = synthesize_service_windows(result.service, rolling, weekly)
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

    def _enter_conversation_queue(self, key: Tuple[int, int]) -> None:
        """Count this request against ``key`` before it contends for the lock.

        Called before acquisition, not after, so a request still waiting is
        visible to the turn currently holding the lock. That ordering is the
        whole point: the holder needs to know someone is behind it *while* it
        is finishing, not afterwards.
        """
        self._conversation_pending[key] = self._conversation_pending.get(key, 0) + 1

    def _leave_conversation_queue(self, key: Tuple[int, int]) -> None:
        """Drop this request's claim on ``key``, forgetting empty keys."""
        remaining = self._conversation_pending.get(key, 0) - 1
        if remaining > 0:
            self._conversation_pending[key] = remaining
        else:
            self._conversation_pending.pop(key, None)

    def _conversation_followers(self, key: Tuple[int, int]) -> int:
        """Requests queued behind the current holder of ``key`` (never < 0).

        The holder counts itself, so subtract it. A caller that never
        registered (older tests, direct invocations) reads 0 rather than -1.
        """
        return max(0, self._conversation_pending.get(key, 0) - 1)

    @asynccontextmanager
    async def _conversation_turn(self, user_id: int, chat_id: int):
        """Hold the conversation lock while staying countable.

        Wraps ``_get_conversation_lock`` rather than replacing it so the
        existing callers that only need mutual exclusion (dead-session
        recovery, wakeup) are untouched. Registration happens before the
        acquire and is released in ``finally``, so a request that is cancelled
        while waiting does not leak a phantom follower.
        """
        key = self._stream_key(user_id, chat_id)
        self._enter_conversation_queue(key)
        try:
            async with self._get_conversation_lock(user_id, chat_id):
                yield
        finally:
            self._leave_conversation_queue(key)

    def workload_snapshot(self, now: float) -> tuple[int, float]:
        """Return ``(in_flight_count, oldest_request_age_seconds)``.

        Exposes bridge busyness so an external supervisor (the self-update
        procedure) can avoid restarting the bridge mid-request — a restart
        SIGTERM-kills the in-flight agent child process and destroys the
        user's work. ``now`` must come from the event loop clock so it is
        comparable to the recorded turn start times.
        """
        metrics = self._agent_session_registry.metrics()
        count = metrics.active_sessions
        oldest_started = metrics.oldest_started_at
        oldest_age = (now - oldest_started) if oldest_started is not None else 0.0
        oldest_age = max(0.0, oldest_age)

        # A provider may own tracked work after the interactive turn has
        # returned (Claude's run-in-background Bash tasks are the motivating
        # case).  Keep this optional so Codex and future runtimes retain the
        # same provider-neutral session contract.
        for entry in self._agent_session_registry.resident_entries_snapshot():
            snapshot = getattr(entry.session, "background_workload_snapshot", None)
            if not callable(snapshot):
                continue
            try:
                background_count, background_oldest_age = snapshot(now)
            except Exception:
                logger.debug(
                    "Provider background workload snapshot failed: provider=%s",
                    type(entry.session).__name__,
                )
                continue
            count += max(0, int(background_count))
            oldest_age = max(oldest_age, max(0.0, float(background_oldest_age)))
        return count, oldest_age

    def foreground_workload_snapshot(self, now: float) -> tuple[int, float]:
        """Return ``(active_turn_count, oldest_active_turn_age_seconds)``.

        Same registry metrics :meth:`workload_snapshot` starts from, but
        WITHOUT folding provider background-task ages (#1291). Background
        tasks (Claude run-in-background Bash) deliberately outlive their
        interactive turn and are NOT bounded by ``_process_timeout_seconds``
        — the ``wait_for`` that enforces that lifetime covers only the turn
        stream. Consumers must pick the snapshot matching what bounds their
        comparison: restart gating wants background work counted (a SIGTERM
        would destroy it), while the health probe's request-lifetime alert
        compares against exactly that per-turn timeout and must see only
        interactive turns. Clock contract is identical: ``now`` must come
        from the event-loop clock so it is comparable to recorded turn start
        times.
        """
        metrics = self._agent_session_registry.metrics()
        oldest_started = metrics.oldest_started_at
        oldest_age = (now - oldest_started) if oldest_started is not None else 0.0
        return metrics.active_sessions, max(0.0, oldest_age)

    def begin_drain(self) -> bool:
        """Atomically close admission for new provider turns.

        Returns true only for the transition into drain.  Existing turns and
        tracked provider background work remain visible to
        :meth:`workload_snapshot` until they finish or the supervisor's bounded
        shutdown window expires.
        """

        first = not self._shutdown_draining
        self._shutdown_draining = True
        return first

    @property
    def is_draining(self) -> bool:
        return self._shutdown_draining

    def waiting_for_turn_snapshot(self) -> int:
        """Requests registered by the bridge but not admitted by a runtime."""

        return self._agent_session_registry.metrics().waiting_for_turn

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

    async def _run_ledger_write(
        self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Any:
        """Run one fsync-backed ledger mutation in a worker thread (#1479).

        Every ``TaskLedger`` verb re-reads, re-serializes, and atomically
        rewrites ``tasks.json`` with a file + directory fsync, so it must not run
        on the event loop. The per-handler asyncio lock keeps writes issued
        from the loop landing in issue order even when two coroutines of the
        same request (approval callback vs. event consumer) project a phase
        concurrently — the default executor gives no FIFO guarantee across
        threads, and a stale projection must never overwrite a newer one.
        """

        lock = getattr(self, "_task_ledger_write_lock", None)
        if lock is None:
            lock = self._task_ledger_write_lock = asyncio.Lock()
        async with lock:
            return await asyncio.to_thread(fn, *args, **kwargs)

    async def _ledger_create(
        self,
        user_id: int,
        chat_id: int,
        *,
        initial_state: str = RequestPhase.WORKING.value,
    ) -> Optional[str]:
        led = self._task_ledger
        if not led:
            return None
        try:
            return await self._run_ledger_write(
                led.create, user_id, chat_id, initial_state=initial_state
            )
        except Exception as exc:
            logger.warning("Task ledger create failed: %s", type(exc).__name__)
            return None

    async def _project_request_phase(self, req: _PendingRequest) -> None:
        """Best-effort durable projection of the in-memory request phase."""

        led = self._task_ledger
        if not led or not req.task_id or req.lifecycle.is_terminal:
            return
        # Snapshot on the loop so the write carries the phase as of this call,
        # exactly like the former synchronous projection did.
        phase = req.lifecycle.phase.value
        try:
            await self._run_ledger_write(led.set_state, req.task_id, phase)
        except Exception as exc:
            logger.warning("Task ledger phase projection failed: %s", type(exc).__name__)

    async def _ledger_finish(
        self, req: _PendingRequest, state: str, *, cleanup_done: bool
    ) -> None:
        led = self._task_ledger
        if led and req.task_id:
            try:
                await self._run_ledger_write(
                    led.finish, req.task_id, state, cleanup_done=cleanup_done
                )
            except Exception as exc:
                logger.warning("Task ledger finish failed: %s", type(exc).__name__)

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
                try:
                    await self._run_ledger_write(led.set_status_message, req.task_id, None)
                except Exception as exc:
                    logger.warning(
                        "Task ledger heartbeat cleanup projection failed: %s",
                        type(exc).__name__,
                    )
        return cleaned

    async def _maybe_update_heartbeat(self, req: _PendingRequest, now: float) -> None:
        """Send or edit a fail-open long-running task heartbeat."""
        if not getattr(config, "heartbeat_enabled", True):
            return
        if not req.status_callback or req.future.done() or req.lifecycle.is_terminal:
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
                    await self._run_ledger_write(led.set_status_message, req.task_id, message_id)
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
                getattr(self._config, "bot_data_dir", None) or self.project_root / ".telegram_bot"
            )
            return default_duration_log_path(Path(bot_data_dir))
        return Path(path)

    def _append_duration_log(
        self,
        req: _PendingRequest,
        *,
        session_id: Optional[str],
        duration_ms: int,
        success: bool,
    ) -> None:
        """Record one terminal request without prompt or response content."""

        if not getattr(self._config, "heartbeat_duration_log_enabled", True):
            return
        append_duration_sample(
            path=self._duration_log_path(),
            user_id=req.user_id,
            chat_id=req.chat_id,
            session_id=session_id or req.requested_session_id,
            model=req.model,
            duration_ms=max(0, int(duration_ms)),
            success=success,
            max_lines=int(
                getattr(self._config, "heartbeat_duration_log_max_lines", 10000)
            ),
        )

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
        if req.future.done() or req.lifecycle.is_terminal or req.awaiting_permission:
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
