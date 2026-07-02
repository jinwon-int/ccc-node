import asyncio
import json
import logging
import os
import signal
import shutil
import subprocess
import time

import telegram.error
from telegram import Update
from telegram.ext import Application
from telegram.request import BaseRequest, HTTPXRequest

from telegram_bot.core.bot_shared import _PollingRestart, enforce_access_control
from telegram_bot.core.session_isolation import apply_subprocess_session_isolation
from telegram_bot.utils.config import config
from telegram_bot.utils.health import health_reporter

logger = logging.getLogger(__name__)


class BotLifecycleMixin:
    async def _on_ready(self, application: Application):
        """Called after application.initialize() — sets up commands and cleanup."""
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._image_dir.mkdir(parents=True, exist_ok=True)
        removed = await self._cleanup_stale_audio_files(
            self._audio_dir, max_age_seconds=self._STALE_AUDIO_SECONDS
        )
        removed_images = await self._cleanup_stale_audio_files(
            self._image_dir, max_age_seconds=self._STALE_AUDIO_SECONDS
        )
        if removed:
            logger.info("Startup audio cleanup removed %s stale file(s)", removed)
        if removed_images:
            logger.info("Startup image cleanup removed %s stale file(s)", removed_images)
        await self._set_bot_commands()
        logger.info("Bot initialization complete")

    def build(self):
        """Build the application (no post_init — lifecycle managed manually)."""
        self.application = (
            Application.builder()
            .token(config.telegram_bot_token)
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

    _MIN_UPTIME = 30  # seconds — polling exits faster → count as crash
    _MAX_RAPID_CRASHES = 5

    def run(self):
        """Run the bot with in-process polling restart capability."""
        enforce_access_control(config)
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
            str(config.claude_cli_path)
            if config.claude_cli_path
            else shutil.which("claude") or ""
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

    async def _run_async(self):
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
            start_time = time.time()
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
                raise SystemExit(message)
            except telegram.error.Conflict:
                message = (
                    "Another bot instance is already running with the same token.\n"
                    "   Use --stop to stop it first, or check for duplicate processes."
                )
                health_reporter.record_telegram_error(message, consecutive_failures=1)
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
                logger.warning(
                    "Network error during initialization: %s, retrying...", e
                )
                # Force cleanup to release leaked connections from pool
                await self._graceful_shutdown(force=True)
                await asyncio.sleep(5)
                continue

            await self._on_ready(self.application)

            watchdog_task = None
            push_task = None
            try:
                await self.application.start()
                await self.application.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,
                )

                logger.info("Bot is running")
                health_reporter.record_telegram_ok()
                claude_ready, claude_reason = self._probe_claude_readiness()
                if claude_ready:
                    health_reporter.record_claude_ok()
                else:
                    health_reporter.record_claude_error(claude_reason)

                watchdog_task = asyncio.create_task(self._polling_watchdog(stop_event))
                push_task = asyncio.create_task(
                    self._push_notifier.run(self.application, stop_event)
                )

                await self._wait_for_polling_exit(stop_event)

            except _PollingRestart:
                health_reporter.mark_starting(
                    "restarting polling after connection loss"
                )
                uptime = time.time() - start_time
                if uptime < self._MIN_UPTIME:
                    rapid_crash_count += 1
                    if rapid_crash_count >= self._MAX_RAPID_CRASHES:
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
                logger.warning(
                    "TimedOut error during runtime (likely PoolTimeout): %s", e
                )
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
            except telegram.error.Forbidden as e:
                message = (
                    f"Bot token was revoked or bot is blocked: {e}\n"
                    "   Create a new token via @BotFather on Telegram."
                )
                health_reporter.record_telegram_error(message, consecutive_failures=1)
                raise SystemExit(message)
            finally:
                for _task in (watchdog_task, push_task):
                    if _task and not _task.done():
                        _task.cancel()
                        try:
                            await _task
                        except (asyncio.CancelledError, _PollingRestart):
                            pass
                await self._graceful_shutdown()

        logger.info("Bot stopped")

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
                    raise _PollingRestart()

    async def _wait_for_polling_exit(self, stop_event: asyncio.Event):
        """Block until stop signal or polling exits unexpectedly."""
        while not stop_event.is_set():
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
