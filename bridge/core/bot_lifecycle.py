import asyncio
import json
import logging
import os
import signal
import shutil
import subprocess
from typing import Optional

import telegram.error
from telegram import Update
from telegram.ext import Application
from telegram.request import BaseRequest, HTTPXRequest

from telegram_bot.core import crash_policy, media
from telegram_bot.core.bot_shared import _PollingRestart, enforce_access_control
from telegram_bot.core.tool_policy import (
    EXECUTION_OWNER_OPERATOR,
    effective_bash_policy,
    resolve_bash_policy,
    resolve_execution_profile,
)
from telegram_bot.core.session_isolation import apply_subprocess_session_isolation
from telegram_bot.utils.health import health_reporter
from telegram_bot.core.task_ledger import (
    INTERRUPTED_NOTICE_TEXT,
    TaskLedger,
    ledger_path_for,
)
from telegram_bot.core.dead_session_recovery import (
    recover_dead_session_notifications,
    run_periodic_dead_session_recovery,
)
from telegram_bot.core.dead_session_wakeup import (
    recovery_should_defer_to_wakeup,
    run_dead_session_wakeup_scan,
)
from telegram_bot.utils.heartbeat_store import drain_heartbeats, store_path_for
from telegram_bot.utils.orphan_reaper import (
    run_periodic_reaper,
    sweep_orphaned_claude_processes,
)

logger = logging.getLogger(__name__)


class BotLifecycleMixin:
    async def _on_ready(self, application: Application):
        """Called after application.initialize() — sets up commands and cleanup."""
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._image_dir.mkdir(parents=True, exist_ok=True)
        document_directory_fd = media.open_private_document_directory(self._document_dir)
        os.close(document_directory_fd)
        removed = await self._cleanup_stale_audio_files(
            self._audio_dir, max_age_seconds=self._STALE_AUDIO_SECONDS
        )
        removed_images = await self._cleanup_stale_audio_files(
            self._image_dir, max_age_seconds=self._STALE_AUDIO_SECONDS
        )
        removed_documents = media.cleanup_stale_document_files(
            self._document_dir, max_age_seconds=self._STALE_AUDIO_SECONDS
        )
        if removed:
            logger.info("Startup audio cleanup removed %s stale file(s)", removed)
        if removed_images:
            logger.info("Startup image cleanup removed %s stale file(s)", removed_images)
        if removed_documents:
            logger.info("Startup document cleanup removed %s stale file(s)", removed_documents)

        # Reap any node-claude orphans left over from a previous bridge run.
        # On Android/Termux there is no systemd cgroup to clean up child
        # processes automatically when the bridge exits, so PPID=1 orphans can
        # accumulate across restarts (see jinwon-int/ccc-node#303).
        killed = sweep_orphaned_claude_processes()
        if killed:
            logger.info(
                "Startup orphan sweep: signalled %d orphan node-claude process(es) — PIDs %s",
                len(killed),
                killed,
            )
        else:
            logger.debug("Startup orphan sweep: no orphans found")

        # Delete '⏳ Working' heartbeat messages orphaned by a previous run that
        # was SIGTERM-killed mid-request (exit 143). Their ids were persisted on
        # creation; the owning _PendingRequest died with that process, so this
        # restart is the only thing that can remove the now-frozen messages.
        await self._sweep_orphaned_heartbeats(application)

        # Task-ledger reconciliation (Hermes model): every non-terminal task
        # record was written by a previous process, so it died mid-flight —
        # transition it to `interrupted` and clean (or annotate) its status
        # message. Then drain any terminal ops left pending by failed cleanups.
        await self._reconcile_task_ledger(application)
        await self._recover_dead_session_notifications(application)

        await self._set_bot_commands()
        logger.info("Bot initialization complete")

    async def _recover_dead_session_notifications(self, application: Application) -> None:
        stats = await recover_dead_session_notifications(
            application.bot,
            self._session_manager,
            self._project_chat,
            self._project_chat.conversations_dir,
            max_delivery_attempts_per_scan=3,
            send_timeout=5.0,
            wakeup_defer=self._build_dead_session_wakeup_defer(),
        )
        self._record_recovery_stats(stats)
        if stats.delivered or stats.failed or stats.rejected or stats.deferred_wakeup:
            logger.info(
                "Dead-session recovery: scanned=%d delivered=%d duplicate=%d failed=%d "
                "rejected=%d quarantined=%d quarantine_skipped=%d deferred_wakeup=%d "
                "active=%d locked=%d",
                stats.scanned,
                stats.delivered,
                stats.duplicate,
                stats.failed,
                stats.rejected,
                stats.quarantined,
                stats.quarantine_skipped,
                stats.deferred_wakeup,
                stats.skipped_active,
                stats.skipped_locked,
            )

    def _record_recovery_stats(self, stats) -> None:
        """Surface recovery counters in health.json (fail-open)."""
        try:
            if getattr(stats, "quarantined", 0):
                health_reporter.record_transcript_quarantined(stats.quarantined)
            if getattr(stats, "hard_quarantined", 0):
                health_reporter.record_transcript_hard_quarantined(stats.hard_quarantined)
        except Exception as exc:
            logger.debug("Recovery stats health recording failed: %s", type(exc).__name__)

    def _lifecycle_task_ledger(self):
        path = ledger_path_for(
            getattr(self._config, "bot_data_dir", None),
            getattr(self._config, "task_ledger_path", None),
        )
        return TaskLedger(path) if path else None

    async def _reconcile_task_ledger(self, application: Application) -> None:
        ledger = self._lifecycle_task_ledger()
        if ledger is None:
            return
        op_kind = "notice" if getattr(self._config, "task_interrupted_notice", True) else "delete"
        interrupted = ledger.reconcile_interrupted(op_kind=op_kind)
        if interrupted:
            logger.info(
                "Task ledger reconciliation: %d task(s) from a previous run marked interrupted",
                interrupted,
            )
        await self._drain_terminal_ops(application.bot, ledger)

    async def _drain_terminal_ops(self, bot, ledger=None) -> None:
        """Retry pending terminal cleanups (the ledger's mini terminal-outbox)."""
        ledger = ledger or self._lifecycle_task_ledger()
        if ledger is None:
            return
        for task_id, op in ledger.pending_terminal_ops():
            chat_id = op.get("chat_id")
            message_id = op.get("message_id")
            if not chat_id or not message_id:
                ledger.resolve_terminal_op(task_id, success=True)
                continue
            try:
                if op.get("kind") == "notice":
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=INTERRUPTED_NOTICE_TEXT,
                    )
                else:
                    await bot.delete_message(chat_id=chat_id, message_id=message_id)
                ledger.resolve_terminal_op(task_id, success=True)
            except telegram.error.BadRequest:
                # Message already gone / not editable — nothing left to clean.
                ledger.resolve_terminal_op(task_id, success=True)
            except Exception as exc:
                logger.debug(
                    "Terminal op retry failed for task %s (%s/%s): %s",
                    task_id,
                    chat_id,
                    message_id,
                    type(exc).__name__,
                )
                ledger.resolve_terminal_op(task_id, success=False)

    async def _sweep_orphaned_heartbeats(self, application: Application) -> None:
        """Delete heartbeat messages left frozen by a previous killed run."""
        store_path = store_path_for(
            getattr(self._config, "bot_data_dir", None),
            getattr(self._config, "heartbeat_store_path", None),
        )
        if store_path is None:
            return
        leftovers = drain_heartbeats(store_path)
        if not leftovers:
            return
        deleted = 0
        for chat_id, message_id in leftovers:
            try:
                await application.bot.delete_message(chat_id=chat_id, message_id=message_id)
                deleted += 1
            except Exception as exc:
                # Best-effort: message may be >48h old, already gone, or the chat
                # unreachable. Nothing more we can do — it stays as-is.
                logger.debug(
                    "Heartbeat sweep: could not delete %s/%s: %s",
                    chat_id,
                    message_id,
                    type(exc).__name__,
                )
        logger.info(
            "Startup heartbeat sweep: removed %d/%d stale heartbeat message(s)",
            deleted,
            len(leftovers),
        )

    def build(self):
        """Build the application (no post_init — lifecycle managed manually)."""
        self.application = (
            self._application_builder_factory()
            .token(self._config.telegram_bot_token)
            .concurrent_updates(True)
            .get_updates_request(self._build_get_updates_request())
            .request(self._build_default_request())
            .build()
        )
        self._setup_handlers()
        self.application.add_error_handler(self._error_handler)

    def _build_default_request(self) -> BaseRequest:
        """Build default request for all non-getUpdates API calls."""
        proxy_url = (
            os.environ.get("PROXY_URL")
            or os.environ.get("https_proxy")
            or os.environ.get("http_proxy")
        )
        return HTTPXRequest(
            connection_pool_size=8,
            pool_timeout=3.0,
            read_timeout=10.0,
            write_timeout=10.0,
            connect_timeout=5.0,
            proxy=proxy_url,
            http_version="1.1",
        )

    def _build_get_updates_request(self) -> BaseRequest:
        """Build dedicated request for getUpdates polling."""
        proxy_url = (
            os.environ.get("PROXY_URL")
            or os.environ.get("https_proxy")
            or os.environ.get("http_proxy")
        )
        return HTTPXRequest(
            connection_pool_size=4,  # Increased from 2 to handle long polling
            pool_timeout=5.0,
            read_timeout=35.0,
            write_timeout=10.0,
            connect_timeout=5.0,
            proxy=proxy_url,
            http_version="1.1",
        )

    # Crash/rapid-restart thresholds come from the single shared policy
    # (crash-policy.env via core.crash_policy) so this in-process guard can never
    # silently diverge from the process supervisor in start.sh (#445). Values are
    # unchanged: in-process min-uptime 30s, 5 strikes.
    _MIN_UPTIME = crash_policy.INPROCESS_MIN_UPTIME_SECONDS  # polling exits faster → count as crash
    _MAX_RAPID_CRASHES = crash_policy.MAX_RAPID_CRASHES
    _WORKLOAD_INTERVAL = 10  # seconds between in-flight workload snapshots
    # Transport-only reconnect policy (issue #411): a transient Telegram
    # NetworkError/TimedOut must not tear down the Application — that would
    # couple the polling transport to in-flight agent turns. Retry the
    # updater alone with bounded exponential backoff before escalating to
    # the full rebuild path.
    _RECONNECT_ATTEMPTS = 5
    _RECONNECT_BASE_DELAY = 1.0  # seconds, doubled per attempt
    _RECONNECT_MAX_DELAY = 30.0
    # Reconnected polling that dies again within _MIN_UPTIME this many times in
    # a row means the transport problem is not transient — escalate to the full
    # rebuild path (which owns the rapid-crash SystemExit accounting).
    _MAX_RAPID_RECONNECT_CYCLES = 3
    # getUpdates errors that must fail closed instead of being retried forever.
    # PTB's polling loop (network_retry_loop, max_retries=-1) only invokes the
    # error callback and keeps updater.running True, so without an explicit
    # callback a post-start Conflict/Forbidden would never surface anywhere.
    _PERMANENT_POLLING_ERRORS = (
        telegram.error.InvalidToken,
        telegram.error.Conflict,
        telegram.error.Forbidden,
    )
    # Set by _on_polling_error / _polling_task_failure when a permanent
    # getUpdates error surfaces; consumed (and reset to None) by the
    # supervise/reconnect paths.
    _fatal_polling_error: Optional[telegram.error.TelegramError] = None

    def validate_runtime_paths(self) -> None:
        """Validate runtime paths before logging creates artifacts."""
        self._session_manager.validate_storage_path()
        distill_journal = getattr(self, "_distill_journal", None)
        if distill_journal is not None:
            distill_journal.validate_path()

    def run(self):
        """Run the bot with in-process polling restart capability."""
        settings = self._config
        execution_profile = resolve_execution_profile(
            getattr(settings, "execution_profile", "strict-project"),
            allowed_user_ids=getattr(settings, "allowed_user_ids", []),
            require_allowlist=getattr(settings, "require_allowlist", True),
        )
        bash_policy = effective_bash_policy(
            resolve_bash_policy(getattr(settings, "bash_policy", None)), execution_profile
        )
        logger.info(
            "bridge_execution_policy execution_profile=%s bash_policy=%s host_scope=%s",
            execution_profile,
            bash_policy,
            str(execution_profile == EXECUTION_OWNER_OPERATOR).lower(),
        )
        enforce_access_control(settings)
        self._session_manager.initialize()
        distill_journal = getattr(self, "_distill_journal", None)
        if distill_journal is not None:
            distill_journal.initialize()
        exit_reason = "Bot stopped"
        try:
            health_reporter.initialize_process()
            health_reporter.mark_starting("initializing bot")
            asyncio.run(self._run_async())
        except KeyboardInterrupt:
            exit_reason = "Stopped by signal"
            raise
        except SystemExit as exc:
            if exc.code not in (None, 0):
                exit_reason = str(exc.code)
            raise
        except Exception:
            exit_reason = "Unexpected error in bot run loop"
            logger.exception("Unexpected error in bot run loop")
            raise
        finally:
            health_reporter.mark_unavailable(exit_reason)
            health_reporter.cleanup_runtime_files()

    def _probe_claude_readiness(self) -> tuple[bool, str]:
        cli_path = (
            str(self._config.claude_cli_path) if self._config.claude_cli_path else shutil.which("claude") or ""
        )
        if not cli_path:
            return False, "claude command not found"

        try:
            proc = subprocess.run(
                [cli_path, "auth", "status", "--json"],
                text=True,
                capture_output=True,
                timeout=float(os.getenv("CLAUDE_AUTH_STATUS_TIMEOUT", "15")),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, "claude auth status timed out"
        except Exception as exc:
            return False, f"claude auth status failed: {exc}"

        raw = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            preview = raw.replace("\n", " ")[:200]
            return (
                False,
                f"invalid claude auth response (cli={cli_path}, exit={proc.returncode}): {preview}",
            )

        if data.get("loggedIn") is True:
            return True, ""

        return False, "claude authentication unavailable"

    def _probe_codex_readiness(self) -> tuple[bool, str]:
        configured_path = str(getattr(self._config, "codex_cli_path", "codex")).strip()
        cli_path = shutil.which(configured_path) or ""
        if not cli_path:
            return False, "codex command not found"

        try:
            proc = subprocess.run(
                [cli_path, "login", "status"],
                text=True,
                capture_output=True,
                timeout=float(os.getenv("CODEX_AUTH_STATUS_TIMEOUT", "15")),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, "codex login status timed out"
        except Exception as exc:
            return False, f"codex login status failed: {exc}"

        if proc.returncode == 0:
            return True, ""
        return False, "codex authentication unavailable"

    def _probe_agent_readiness(self) -> tuple[bool, str]:
        provider = str(getattr(self._config, "agent_provider", "claude")).lower()
        if provider == "codex":
            return self._probe_codex_readiness()
        return self._probe_claude_readiness()

    async def _run_async(self):  # noqa: C901 -- #348 baseline hotspot
        """Async entry: manage Application lifecycle and polling restart loop."""
        # Isolate child claude/bash/pytest process trees into their own session so a
        # SIGTERM/SIGINT from work the bot itself launched cannot propagate back and
        # stop the bot (see core/session_isolation.py).
        apply_subprocess_session_isolation()
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        rapid_crash_count = 0

        while not stop_event.is_set():
            if not self.application:
                self.build()

            logger.info("Starting...")
            start_time = self._clock.time()
            health_reporter.mark_starting("initializing telegram polling")

            try:
                await self.application.initialize()
            except telegram.error.InvalidToken:
                message = (
                    "Invalid Telegram Bot Token. "
                    "Please check TELEGRAM_BOT_TOKEN in your .env file.\n"
                    "   Get a valid token from @BotFather on Telegram."
                )
                health_reporter.record_telegram_error(message, consecutive_failures=1)
                self._record_transport_teardown("invalid telegram token")
                raise SystemExit(message)
            except telegram.error.Conflict:
                message = (
                    "Another bot instance is already running with the same token.\n"
                    "   Use --stop to stop it first, or check for duplicate processes."
                )
                health_reporter.record_telegram_error(message, consecutive_failures=1)
                self._record_transport_teardown("telegram getUpdates conflict")
                raise SystemExit(message)
            except telegram.error.TimedOut as e:
                # PoolTimeout is converted to TimedOut, need force cleanup
                health_reporter.record_telegram_error(
                    f"telegram timeout error: {e}",
                    consecutive_failures=1,
                )
                logger.warning(
                    "TimedOut error during initialization (likely PoolTimeout): %s, retrying...",
                    e,
                )
                # Force cleanup to release leaked connections from pool
                await self._graceful_shutdown(force=True)
                await asyncio.sleep(5)
                continue
            except telegram.error.NetworkError as e:
                health_reporter.record_telegram_error(
                    f"telegram startup error: {e}",
                    consecutive_failures=1,
                )
                logger.warning("Network error during initialization: %s, retrying...", e)
                # Force cleanup to release leaked connections from pool
                await self._graceful_shutdown(force=True)
                await asyncio.sleep(5)
                continue

            await self._on_ready(self.application)

            watchdog_task = None
            push_task = None
            reaper_task = None
            workload_task = None
            dead_session_recovery_task = None
            distill_snapshot_task = None
            distill_extraction_task = None
            distill_local_sink_task = None
            distill_wiki_sink_task = None
            distill_honcho_sink_task = None
            health_alerts_task = None
            try:
                await self.application.start()
                await self.application.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    # Drop the backlog only on the process's very first polling
                    # start (deliberate stale-flood protection at boot). In-process
                    # restarts must NOT drop updates: messages sent during a
                    # transport outage would be silently lost. The 20-minute
                    # stale-message guard in _check_access still bounds backlogs.
                    drop_pending_updates=self._consume_initial_polling_start(),
                    error_callback=self._on_polling_error,
                )

                logger.info("Bot is running")
                health_reporter.record_telegram_ok()
                agent_ready, agent_reason = await asyncio.to_thread(
                    self._probe_agent_readiness
                )
                if agent_ready:
                    health_reporter.record_agent_ok()
                else:
                    health_reporter.record_agent_error(agent_reason)

                watchdog_task = asyncio.create_task(self._polling_watchdog(stop_event))
                push_task = asyncio.create_task(
                    self._push_notifier.run(self.application, stop_event)
                )
                reaper_task = asyncio.create_task(run_periodic_reaper(), name="orphan-reaper")
                workload_task = asyncio.create_task(
                    self._workload_reporter(stop_event), name="workload-reporter"
                )
                dead_session_recovery_task = asyncio.create_task(
                    self._periodic_dead_session_recovery(stop_event),
                    name="dead-session-recovery",
                )
                health_alerts_task = asyncio.create_task(
                    self._health_alerts_probe(stop_event), name="health-alerts"
                )
                distill_extraction_task = None
                if (
                    self._distill_snapshot_worker is not None
                    and self._distill_journal is not None
                ):
                    distill_snapshot_task = asyncio.create_task(
                        self._distill_snapshot_loop(stop_event),
                        name="distill-snapshot",
                    )
                if (
                    self._distill_extraction_worker is not None
                    and self._distill_journal is not None
                ):
                    distill_extraction_task = asyncio.create_task(
                        self._distill_extraction_loop(stop_event),
                        name="distill-extraction",
                    )
                distill_local_sink_task = None
                if (
                    self._distill_local_sink_worker is not None
                    and self._distill_journal is not None
                ):
                    distill_local_sink_task = asyncio.create_task(
                        self._distill_local_sink_loop(stop_event),
                        name="distill-local-sink",
                    )
                distill_wiki_sink_task = None
                if (
                    self._distill_wiki_sink_worker is not None
                    and self._distill_journal is not None
                ):
                    distill_wiki_sink_task = asyncio.create_task(
                        self._distill_wiki_sink_loop(stop_event),
                        name="distill-wiki-sink",
                    )
                distill_honcho_sink_task = None
                if (
                    self._distill_honcho_sink_worker is not None
                    and self._distill_journal is not None
                ):
                    distill_honcho_sink_task = asyncio.create_task(
                        self._distill_honcho_sink_loop(stop_event),
                        name="distill-honcho-sink",
                    )

                await self._supervise_polling(stop_event)

            except _PollingRestart:
                health_reporter.mark_starting("restarting polling after connection loss")
                uptime = self._clock.time() - start_time
                if uptime < self._MIN_UPTIME:
                    rapid_crash_count += 1
                    if rapid_crash_count >= self._MAX_RAPID_CRASHES:
                        # Deliberate escalation to layer 2 (#445): this non-zero
                        # SystemExit ends the process, and start.sh's supervisor
                        # counts that as exactly ONE process crash against its own
                        # CCC_PROCESS_CRASH_WINDOW_SECONDS/CCC_MAX_RAPID_CRASHES
                        # budget. The in-process layer absorbs transient churn;
                        # the supervisor bounds the process lifetime.
                        raise SystemExit(
                            f"Polling restarted {self._MAX_RAPID_CRASHES} times within "
                            f"{self._MIN_UPTIME}s each. Giving up."
                        )
                    logger.warning(
                        "Polling restart after only %.1fs (crash %d/%d)",
                        uptime,
                        rapid_crash_count,
                        self._MAX_RAPID_CRASHES,
                    )
                else:
                    rapid_crash_count = 0

                logger.warning("Polling restart triggered, restarting...")
                continue
            except telegram.error.TimedOut as e:
                # PoolTimeout is converted to TimedOut, need force cleanup
                health_reporter.record_telegram_error(
                    f"telegram timeout error: {e}",
                    consecutive_failures=1,
                )
                logger.warning("TimedOut error during runtime (likely PoolTimeout): %s", e)
                # Force cleanup to release leaked connections from pool
                await self._graceful_shutdown(force=True)
                continue
            except telegram.error.NetworkError as e:
                health_reporter.record_telegram_error(
                    f"telegram runtime error: {e}",
                    consecutive_failures=1,
                )
                logger.warning("Network error during startup: %s", e)
                # Force cleanup to release leaked connections from pool
                await self._graceful_shutdown(force=True)
                continue
            except telegram.error.Conflict:
                # Surfaced by _on_polling_error via the supervisor: another
                # instance started polling the same token after we did.
                message = (
                    "Another bot instance is already running with the same token.\n"
                    "   Use --stop to stop it first, or check for duplicate processes."
                )
                health_reporter.record_telegram_error(message, consecutive_failures=1)
                self._record_transport_teardown("telegram getUpdates conflict")
                raise SystemExit(message)
            except telegram.error.InvalidToken:
                message = (
                    "Invalid Telegram Bot Token. "
                    "Please check TELEGRAM_BOT_TOKEN in your .env file.\n"
                    "   Get a valid token from @BotFather on Telegram."
                )
                health_reporter.record_telegram_error(message, consecutive_failures=1)
                self._record_transport_teardown("invalid telegram token")
                raise SystemExit(message)
            except telegram.error.Forbidden as e:
                message = (
                    f"Bot token was revoked or bot is blocked: {e}\n"
                    "   Create a new token via @BotFather on Telegram."
                )
                health_reporter.record_telegram_error(message, consecutive_failures=1)
                self._record_transport_teardown("telegram token revoked or bot blocked")
                raise SystemExit(message)
            finally:
                for _task in (
                    watchdog_task,
                    push_task,
                    reaper_task,
                    workload_task,
                    dead_session_recovery_task,
                    health_alerts_task,
                    distill_snapshot_task,
                    distill_extraction_task,
                    distill_local_sink_task,
                    distill_wiki_sink_task,
                    distill_honcho_sink_task,
                ):
                    if _task and not _task.done():
                        _task.cancel()
                        try:
                            await _task
                        except (asyncio.CancelledError, _PollingRestart):
                            pass
                await self._graceful_shutdown()
                if stop_event.is_set():
                    await self._enqueue_shutdown_distills()
                    close_project_chat = getattr(self._project_chat, "close", None)
                    if close_project_chat is not None:
                        await close_project_chat()

        logger.info("Bot stopped")

    def _consume_initial_polling_start(self) -> bool:
        """True exactly once per process: only the first start drops the backlog."""
        first = not getattr(self, "_polling_started_once", False)
        self._polling_started_once = True
        return first

    def _on_polling_error(self, exc: telegram.error.TelegramError) -> None:
        """Synchronous getUpdates error callback for PTB's polling retry loop.

        PTB (22.x) retries polling errors indefinitely in a background task and
        keeps ``updater.running`` True; its default callback only logs. A
        permanent ``Conflict``/``Forbidden`` raised *after* polling started would
        therefore never reach the fail-closed handlers in ``_run_async``. Flag
        permanent errors for the polling supervisor to fail closed; transient
        errors only mark telegram health degraded (PTB already logs and retries
        them, and the watchdog escalates prolonged outages).

        Must never raise: PTB aborts the retry loop when the callback raises.
        """
        try:
            if isinstance(exc, self._PERMANENT_POLLING_ERRORS):
                self._fatal_polling_error = exc
                health_reporter.record_telegram_error(
                    f"permanent polling failure: {exc}", consecutive_failures=1
                )
                logger.error(
                    "Permanent polling failure (%s): %s — failing closed",
                    type(exc).__name__,
                    exc,
                )
            else:
                health_reporter.record_telegram_error(str(exc))
        except Exception:
            logger.exception("Polling error callback failed")

    async def _supervise_polling(self, stop_event: asyncio.Event) -> None:
        """Wait for polling exit; recover transient exits transport-only.

        A stopped updater (watchdog-triggered or an unexpected polling exit)
        first gets a bounded transport-only reconnect that preserves the
        Application, its bot request pools, and every in-flight agent turn.
        ``_PollingRestart`` escalates to the caller's full teardown/rebuild
        path when the reconnect fails outright, or when reconnected polling
        keeps dying within ``_MIN_UPTIME`` — a hot reconnect loop would
        otherwise bypass the rapid-crash accounting entirely.
        """
        rapid_cycles = 0
        while True:
            entered_at = self._clock.time()
            try:
                await self._wait_for_polling_exit(stop_event)
                return
            except _PollingRestart:
                if stop_event.is_set():
                    return
                fatal = getattr(self, "_fatal_polling_error", None)
                if fatal is not None:
                    # A permanent getUpdates failure (Conflict/Forbidden/token)
                    # must not be "reconnected around": surface the original
                    # error so _run_async's fail-closed handlers terminate with
                    # explicit attribution.
                    self._fatal_polling_error = None
                    raise fatal
                if self._clock.time() - entered_at < self._MIN_UPTIME:
                    rapid_cycles += 1
                    if rapid_cycles >= self._MAX_RAPID_RECONNECT_CYCLES:
                        logger.warning(
                            "Polling died %d times within %ds of reconnecting; "
                            "escalating to full application rebuild",
                            rapid_cycles,
                            self._MIN_UPTIME,
                        )
                        raise
                else:
                    rapid_cycles = 0
                if not await self._reconnect_polling(stop_event):
                    raise

    async def _reconnect_polling(self, stop_event: asyncio.Event) -> bool:
        """Bounded transport-only polling reconnect (issue #411).

        Restarts only the Telegram updater with exponential backoff. The
        Application object — and with it the bot request pools, handler state,
        conversation FIFO, and in-flight agent turns — is left untouched, so a
        turn that finishes mid-outage still delivers through the surviving bot
        once polling is back. Never drops pending updates. Returns True when
        polling is running again; False after ``_RECONNECT_ATTEMPTS`` failures
        or when a stop was requested.
        """
        app = self.application
        updater = getattr(app, "updater", None) if app else None
        if updater is None:
            return False
        health_reporter.mark_starting("reconnecting telegram polling")
        for attempt in range(1, self._RECONNECT_ATTEMPTS + 1):
            if stop_event.is_set():
                return False
            try:
                if updater.running:
                    await asyncio.wait_for(updater.stop(), timeout=15)
                await updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=False,
                    error_callback=self._on_polling_error,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                health_reporter.record_telegram_error(f"polling reconnect failed: {exc}")
                if attempt >= self._RECONNECT_ATTEMPTS:
                    break
                delay = min(
                    self._RECONNECT_BASE_DELAY * (2 ** (attempt - 1)),
                    self._RECONNECT_MAX_DELAY,
                )
                logger.warning(
                    "Polling reconnect attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt,
                    self._RECONNECT_ATTEMPTS,
                    type(exc).__name__,
                    delay,
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                    return False  # stop requested during backoff
                except asyncio.TimeoutError:
                    continue
            else:
                health_reporter.record_transport_reconnect()
                health_reporter.record_telegram_ok()
                logger.info(
                    "Polling transport reconnected (attempt %d/%d) — "
                    "in-flight agent turns preserved",
                    attempt,
                    self._RECONNECT_ATTEMPTS,
                )
                return True
        logger.warning(
            "Transport-only reconnect failed after %d attempt(s); "
            "escalating to full application rebuild",
            self._RECONNECT_ATTEMPTS,
        )
        return False

    def _record_transport_teardown(self, reason: str) -> None:
        """Attribute in-flight requests terminated by a transport-caused exit.

        Permanent Telegram failures (revoked token, getUpdates conflict) keep
        their fail-closed SystemExit, but the requests they take down must be
        visible: count them in ``health.transport.cancelled_by_transport`` so a
        silent-cancellation regression shows up in monitoring. The task ledger's
        startup reconciliation still marks each record ``interrupted`` and posts
        the owner-facing retry notice.
        """
        try:
            now = asyncio.get_running_loop().time()
            count, _ = self._project_chat.workload_snapshot(now)
        except Exception:
            return
        if count > 0:
            health_reporter.record_cancelled_by_transport(count)
            logger.warning(
                "Permanent transport failure terminates %d in-flight request(s): %s",
                count,
                reason,
            )

    async def _polling_watchdog(self, stop_event: asyncio.Event):
        """Monitor Telegram API reachability; restart polling if hung."""
        consecutive_failures = 0

        while not stop_event.is_set():
            await asyncio.sleep(self._WATCHDOG_INTERVAL)

            updater = self.application.updater if self.application else None
            if not self.application or not updater or not updater.running:
                continue

            try:
                await asyncio.wait_for(self.application.bot.get_me(), timeout=10)
                if consecutive_failures > 0:
                    logger.info(
                        "Telegram API reachable again after %d failure(s)",
                        consecutive_failures,
                    )
                consecutive_failures = 0
                health_reporter.record_telegram_ok()
            except Exception as e:
                consecutive_failures += 1
                total_down = consecutive_failures * self._WATCHDOG_INTERVAL
                health_reporter.record_telegram_error(
                    str(e), consecutive_failures=consecutive_failures
                )
                logger.warning("Telegram API unreachable (%ds): %s", total_down, e)

                if total_down >= self._NETWORK_FAILURE_THRESHOLD:
                    logger.warning(
                        "Network down for %ds, restarting polling...",
                        total_down,
                    )
                    try:
                        await asyncio.wait_for(updater.stop(), timeout=15)
                    except asyncio.TimeoutError:
                        logger.error("updater.stop() timed out, forcing process exit")
                        os._exit(1)
                    # The stopped updater makes _supervise_polling run the
                    # transport-only reconnect. Stay alive instead of raising:
                    # after a transport-only reconnect no full rebuild recreates
                    # this task, so dying here would leave polling unmonitored
                    # (issue #411).
                    consecutive_failures = 0

    async def _periodic_dead_session_recovery(self, stop_event: asyncio.Event) -> None:
        if not self.application:
            return
        await run_periodic_dead_session_recovery(
            self.application.bot,
            self._session_manager,
            self._project_chat,
            self._project_chat.conversations_dir,
            stop_event,
            on_stats=self._record_recovery_stats,
            wakeup_tick=self._build_dead_session_wakeup_tick(),
            wakeup_defer=self._build_dead_session_wakeup_defer(),
        )

    def _build_dead_session_wakeup_defer(self):
        """Per-scan recovery→wakeup deferral predicate (#620); None when opted out.

        With ``CCC_DEAD_SESSION_WAKEUP`` off (the default) this returns None
        and both the startup and periodic recovery scans behave exactly as
        before. With the flag on, recovery skips the raw replay for
        conversations the wakeup can still claim (wakeup-first), and delivers
        raw as before whenever the wakeup cannot (fallback).
        """
        if not bool(getattr(self._config, "dead_session_wakeup", False)):
            return None

        def wakeup_defer(current, session_id, replay, user_id, chat_id) -> bool:
            return recovery_should_defer_to_wakeup(
                self._project_chat,
                current,
                session_id,
                replay,
                user_id,
                chat_id,
                usage_meter=getattr(self._project_chat, "usage_meter", None),
            )

        return wakeup_defer

    def _build_dead_session_wakeup_tick(self):
        """Per-tick dead-session wakeup runner (#364 P2); None when opted out.

        Rides the recovery loop's cadence instead of adding a second periodic
        scanner. With the flag off (the default) this returns None and the
        recovery loop behaves exactly as before.
        """
        if not bool(getattr(self._config, "dead_session_wakeup", False)):
            logger.info(
                "Dead-session wakeup disabled (opt-in via CCC_DEAD_SESSION_WAKEUP)"
            )
            return None

        async def wakeup_tick() -> None:
            stats = await run_dead_session_wakeup_scan(
                self.application.bot,
                self._session_manager,
                self._project_chat,
                self._project_chat.conversations_dir,
                enabled=True,
                usage_meter=getattr(self._project_chat, "usage_meter", None),
            )
            if stats.triggered or stats.failed or stats.rejected:
                logger.info(
                    "Dead-session wakeup: scanned=%d triggered=%d delivered=%d "
                    "failed=%d rejected=%d budget=%d cooldown=%d attempts=%d "
                    "quarantine=%d active=%d locked=%d",
                    stats.scanned,
                    stats.triggered,
                    stats.delivered,
                    stats.failed,
                    stats.rejected,
                    stats.skipped_budget,
                    stats.skipped_cooldown,
                    stats.skipped_attempts,
                    stats.skipped_quarantine,
                    stats.skipped_active,
                    stats.skipped_locked,
                )

        return wakeup_tick

    async def _distill_extraction_loop(self, stop_event: asyncio.Event) -> None:
        """Drive the budget-gated distill worker over ready snapshot jobs.

        This is the production scheduler for the retained worker (#388): each
        sweep runs every ready (snapshot_done) job through extract_once, whose
        prospective reservation gate defers capped work before any provider
        call. Trigger policy that *creates* jobs remains #465's phase, so on
        nodes without queued jobs each sweep is a no-op. Fail-open: sweep
        errors are logged and never end the loop or the bridge.
        """

        from telegram_bot.memory.distill_types import DistillJobStatus

        worker = self._distill_extraction_worker
        journal = self._distill_journal
        interval = float(
            getattr(self._config, "distill_extraction_poll_interval", 300.0) or 300.0
        )
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(journal.recover_stale_running)
                jobs = await asyncio.to_thread(journal.list_jobs)
                for job in jobs:
                    if stop_event.is_set():
                        break
                    # Ready set: fresh snapshots AND transiently failed
                    # extractions — claim_extraction accepts both, and the
                    # worker's max-attempts gate bounds the retries.
                    if job.status not in (
                        DistillJobStatus.SNAPSHOT_DONE,
                        DistillJobStatus.EXTRACTION_RETRYABLE_FAILED,
                    ):
                        continue
                    await worker.extract_once(job_id=job.job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Distill extraction sweep failed; continuing", exc_info=True
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except (TimeoutError, asyncio.TimeoutError):
                continue

    async def _distill_snapshot_loop(self, stop_event: asyncio.Event) -> None:
        """Recover queued/stale snapshot jobs through their bound Codex route."""

        from telegram_bot.memory.distill_types import DistillJobStatus

        worker = self._distill_snapshot_worker
        journal = self._distill_journal
        interval = float(
            getattr(self._config, "distill_extraction_poll_interval", 300.0) or 300.0
        )
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(journal.recover_stale_running)
                jobs = await asyncio.to_thread(journal.list_jobs)
                for job in jobs:
                    if stop_event.is_set():
                        break
                    if job.status not in (
                        DistillJobStatus.QUEUED,
                        DistillJobStatus.RETRYABLE_FAILED,
                    ):
                        continue
                    await worker.snapshot_once(job_id=job.job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Distill snapshot sweep failed; continuing", exc_info=True
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except (TimeoutError, asyncio.TimeoutError):
                continue

    async def _distill_local_sink_loop(self, stop_event: asyncio.Event) -> None:
        """Drive independently leased local write-back without re-extraction."""

        from telegram_bot.memory.distill_types import DistillLocalSinkStatus

        worker = self._distill_local_sink_worker
        journal = self._distill_journal
        interval = float(
            getattr(self._config, "distill_extraction_poll_interval", 300.0) or 300.0
        )
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(journal.recover_stale_running)
                jobs = await asyncio.to_thread(journal.list_jobs)
                for job in jobs:
                    if stop_event.is_set():
                        break
                    if job.local_sink_status not in (
                        DistillLocalSinkStatus.PENDING,
                        DistillLocalSinkStatus.RETRYABLE_FAILED,
                    ):
                        continue
                    await worker.write_once(job_id=job.job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Distill local-sink sweep failed; continuing", exc_info=True
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except (TimeoutError, asyncio.TimeoutError):
                continue

    async def _distill_wiki_sink_loop(self, stop_event: asyncio.Event) -> None:
        """Drive the local human-review queue without any Wiki write or PR."""

        from telegram_bot.memory.distill_types import DistillWikiSinkStatus

        worker = self._distill_wiki_sink_worker
        journal = self._distill_journal
        interval = float(
            getattr(self._config, "distill_extraction_poll_interval", 300.0) or 300.0
        )
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(journal.recover_stale_running)
                jobs = await asyncio.to_thread(journal.list_jobs)
                for job in jobs:
                    if stop_event.is_set():
                        break
                    if job.wiki_sink_status not in (
                        DistillWikiSinkStatus.PENDING,
                        DistillWikiSinkStatus.RETRYABLE_FAILED,
                    ):
                        continue
                    await worker.write_once(job_id=job.job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Distill Wiki-sink sweep failed; continuing", exc_info=True
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except (TimeoutError, asyncio.TimeoutError):
                continue

    async def _distill_honcho_sink_loop(self, stop_event: asyncio.Event) -> None:
        """Drive independently leased Honcho delivery from the durable outbox."""

        from telegram_bot.memory.distill_types import DistillHonchoSinkStatus

        worker = self._distill_honcho_sink_worker
        journal = self._distill_journal
        interval = float(
            getattr(self._config, "distill_extraction_poll_interval", 300.0) or 300.0
        )
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(journal.recover_stale_running)
                for job in await asyncio.to_thread(journal.list_jobs):
                    if stop_event.is_set():
                        break
                    if job.honcho_sink_status not in (
                        DistillHonchoSinkStatus.PENDING,
                        DistillHonchoSinkStatus.RETRYABLE_FAILED,
                    ):
                        continue
                    await worker.write_once(job_id=job.job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Distill Honcho-sink sweep failed; continuing", exc_info=True
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except (TimeoutError, asyncio.TimeoutError):
                continue

    async def _health_alerts_probe(self, stop_event: asyncio.Event) -> None:
        """Detection-only runtime health probe + threshold alerts (#389).

        Every tick exports the four structured signals to ``health.json`` and
        evaluates alert thresholds. Fired alerts are logged and queued through
        the owner-only push-notifier spool — the actual Telegram send stays
        behind the notifier's ``CCC_PUSH_ENABLED`` opt-in, so this task never
        contacts a provider on its own. No remediation is performed here.
        """
        from telegram_bot.utils.health_alerts import (
            AlertGate,
            AlertThresholds,
            HealthProbe,
            evaluate_alerts,
            probe_interval,
            write_alert_spool,
        )

        settings = self._config
        if not getattr(settings, "health_alerts_enabled", True):
            return
        # Defensive clamp: a non-positive configured interval would make
        # wait_for time out immediately and spin this loop hot (#430 review).
        interval = probe_interval(getattr(settings, "health_alerts_interval_seconds", None))
        probe = HealthProbe(
            project_chat=self._project_chat,
            spool_dir=self._push_notifier.spool_dir,
            thresholds=AlertThresholds(
                heartbeat_age_factor=float(
                    getattr(settings, "alert_heartbeat_age_factor", 1.0)
                ),
                max_pending_notifications=int(
                    getattr(settings, "alert_max_pending_notifications", 10)
                ),
                max_orphan_children=int(
                    getattr(settings, "alert_max_orphan_children", 1)
                ),
            ),
        )
        gate = AlertGate(
            cooldown_seconds=float(
                getattr(settings, "health_alerts_cooldown_seconds", 1800.0)
            )
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
                health_reporter.record_health_signals(
                    signals.as_dict(), alerts_fired=len(fired)
                )
                for alert in fired:
                    logger.warning("Health alert [%s]: %s", alert.code, alert.message)
                    if push_enabled:
                        write_alert_spool(self._push_notifier.spool_dir, alert)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # detection must never hurt the bridge
                logger.debug("Health probe tick failed: %s", type(exc).__name__)

    async def _workload_reporter(self, stop_event: asyncio.Event):
        """Publish in-flight request count to health.json on a fixed interval.

        The self-update procedure reads this to defer restarting the bridge
        while it is serving a request, so an in-flight ``claude`` child is not
        SIGTERM-killed mid-task (exit 143).
        """
        while not stop_event.is_set():
            try:
                now = asyncio.get_event_loop().time()
                count, oldest_age = self._project_chat.workload_snapshot(now)
                health_reporter.record_workload(count, oldest_age)
            except Exception as exc:
                logger.debug("Workload reporter tick failed: %s", type(exc).__name__)
            # Retry any terminal cleanups that failed at transition time (the
            # ledger's terminal-outbox) — normally an empty, cheap read.
            try:
                if self.application:
                    await self._drain_terminal_ops(self.application.bot)
            except Exception as exc:
                logger.debug("Terminal op drain failed: %s", type(exc).__name__)
            await asyncio.sleep(self._WORKLOAD_INTERVAL)

    def _polling_task_failure(self) -> Optional[BaseException]:
        """Return the exception that killed PTB's polling task, if it died.

        PTB's polling retry loop re-raises ``InvalidToken`` *without* invoking
        the error callback, and a crashed polling task does not clear
        ``updater.running`` — a zombie state no public Updater API reflects.
        Reach the name-mangled task defensively: if PTB internals drift, this
        returns None and the get_me watchdog remains the fallback detector.
        """
        app = self.application
        updater = getattr(app, "updater", None) if app else None
        task = getattr(updater, "_Updater__polling_task", None)
        if task is None or not task.done() or task.cancelled():
            return None
        try:
            return task.exception()
        except (asyncio.CancelledError, asyncio.InvalidStateError):
            return None

    async def _wait_for_polling_exit(self, stop_event: asyncio.Event):
        """Block until stop signal, polling exit, or a fatal polling error."""
        while not stop_event.is_set():
            # PTB keeps updater.running True while retrying getUpdates forever,
            # so a permanent failure flagged by _on_polling_error must be
            # checked explicitly — it never shows up as a stopped updater.
            if getattr(self, "_fatal_polling_error", None) is not None:
                logger.warning("Permanent polling failure flagged, leaving polling wait")
                raise _PollingRestart()
            failure = self._polling_task_failure()
            if failure is not None:
                # InvalidToken kills the polling task without reaching the
                # error callback while updater.running stays True. Route
                # permanent failures to the fail-closed handlers; anything
                # else gets the transport-only reconnect.
                if isinstance(failure, self._PERMANENT_POLLING_ERRORS):
                    self._fatal_polling_error = failure
                logger.warning(
                    "Polling task died (%s), triggering restart",
                    type(failure).__name__,
                )
                raise _PollingRestart()
            if (
                self.application
                and self.application.updater
                and not self.application.updater.running
            ):
                logger.warning("Polling exited unexpectedly, triggering restart")
                raise _PollingRestart()
            await asyncio.sleep(1)

    async def _graceful_shutdown(self, force: bool = False):
        """Tear down the current Application so the next loop iteration is clean.

        Args:
            force: If True, skip graceful stop and immediately cleanup.
                   Use when connection pool is exhausted or timed out.
        """
        self._deny_codex_approvals()
        if not self.application:
            return

        try:
            if force:
                logger.warning("Force shutdown requested, skipping graceful stop")
            else:
                # Give graceful shutdown 5 seconds max
                await asyncio.wait_for(
                    self._do_graceful_stop(),
                    timeout=5.0,
                )
        except asyncio.TimeoutError:
            logger.warning("Graceful shutdown timed out after 5s, forcing cleanup")
        except Exception:
            logger.exception("Error during graceful shutdown")
        finally:
            # Always clear the reference so next build() creates fresh connections
            self.application = None

    async def _do_graceful_stop(self):
        """Actual graceful shutdown logic with proper resource cleanup."""
        if self.application.updater and self.application.updater.running:
            await self.application.updater.stop()
        if self.application.running:
            await self.application.stop()
        await self.application.shutdown()
