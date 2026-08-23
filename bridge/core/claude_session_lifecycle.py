"""Start, close, and body-free transport diagnostics for one Claude session."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Sequence
import logging
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .claude_runtime import ClaudeRuntime, ClaudeSession, SdkClient


# Preserve the established body-free trace channel after this pure move.
logger = logging.getLogger("telegram_bot.core.claude_runtime")

_STDERR_TAIL_LINES = 20
_STDERR_LINE_CHARS = 400


def _classify_cli_stderr(lines: Sequence[str]) -> str | None:
    """Body-free error class for CLI stderr — never leak the payload.

    Mirrors ``external_wait_monitor._classify_gh_error``: a stall report needs
    the SHAPE of the failure, not its text. Provider stderr can echo prompts,
    filesystem paths, or a credential the CLI was handed, and a log line is the
    one place none of that may land.

    ``None`` means the process said nothing at all — itself the most telling
    answer when a turn produced no first event.
    """
    if not lines:
        return None
    text = " ".join(lines)[-_STDERR_LINE_CHARS:].casefold()
    if "rate limit" in text or "ratelimit" in text or "too many requests" in text:
        return "rate-limit"
    if (
        "not logged in" in text
        or "unauthorized" in text
        or "authentication" in text
        or "invalid api key" in text
    ):
        return "auth"
    if "certificate" in text or "ssl" in text or "tls handshake" in text:
        return "tls"
    if (
        "econnreset" in text
        or "econnrefused" in text
        or "enotfound" in text
        or "etimedout" in text
        or "socket hang up" in text
    ):
        return "network"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "out of memory" in text or "enomem" in text or "heap" in text:
        return "oom"
    return "other"


class ClaudeSessionLifecycleMixin:
    """Own session start, single-flight close, and transport diagnostics."""

    _client: SdkClient | None
    _close_task: asyncio.Task[None] | None
    _closed: bool
    _reader_task: asyncio.Task[None] | None
    _runtime: ClaudeRuntime
    _session_id: str | None
    _session_ready: asyncio.Event
    _stderr_tail: deque[str]
    _turn_lock: asyncio.Lock | None
    _fail_active_turn: Callable[[str, str], None]

    if TYPE_CHECKING:
        async def _read_frames(self, client: SdkClient) -> None: ...

    async def _start(self, client: SdkClient, *, timeout_seconds: float) -> None:
        self._client = client
        try:
            await client.connect()
            self._reader_task = asyncio.create_task(self._read_frames(client))
            if self._session_id is None:
                # A new session's stable id is the SDK session id, announced by
                # the first system frame the CLI emits at startup.
                try:
                    await asyncio.wait_for(self._session_ready.wait(), timeout_seconds)
                except TimeoutError:
                    raise RuntimeError(
                        "Claude session id was not announced before the timeout"
                    ) from None
                if self._session_id is None:
                    raise RuntimeError("Claude session ended before announcing a session id")
            else:
                # Resume preserves the requested id as the stable neutral id.
                self._session_ready.set()
            self._turn_lock = self._runtime._session_lock(self._session_id)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        close_task = self._begin_close()
        if close_task is not None:
            await asyncio.shield(close_task)

    def _begin_close(self) -> asyncio.Task[None] | None:
        """Synchronously seal the session and start its single cleanup task."""

        if self._close_task is None:
            if self._closed:
                return None
            self._closed = True
            self._fail_active_turn("claude_runtime_closed", "Claude runtime closed")
            reader_task = self._reader_task
            if reader_task is not None:
                reader_task.cancel()
                self._reader_task = None
            client = self._client
            if reader_task is None and client is None:
                return None
            # Keep cleanup in a separate single-flight task. Project-chat puts
            # stalled-turn abort behind asyncio.wait_for(); cancelling that
            # caller must not cancel the SDK's bounded TERM/KILL escalation.
            # A later close() joins the same task instead of trusting the
            # already-set _closed flag and abandoning a half-closed client.
            self._close_task = asyncio.create_task(
                self._finish_close(reader_task, client)
            )
        return self._close_task

    async def _finish_close(
        self,
        reader_task: asyncio.Task[None] | None,
        client: SdkClient | None,
    ) -> None:
        if reader_task is not None:
            await asyncio.gather(reader_task, return_exceptions=True)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                logger.exception("Claude SDK client disconnect failed during close")
            finally:
                if self._client is client:
                    self._client = None
                self._runtime._forget_session(cast("ClaudeSession", self))

    # -- transport diagnostics ---------------------------------------------

    def _record_stderr(self, line: object) -> None:
        """Sink for the CLI's stderr.

        Registering this is what makes the stderr exist at all: the SDK
        transport pipes stderr only when a callback is set
        (``stderr_dest = PIPE if self._options.stderr is not None else None``),
        so without it the kernel discards whatever the process said on its way
        out — the exact evidence an admission timeout needs (#846).

        Runs on the SDK's reader task, so it must never disturb the turn. The
        SDK already swallows callback exceptions; this does not rely on that.
        """
        try:
            text = str(line).strip()
        except Exception:  # pragma: no cover - defensive
            return
        if text:
            self._stderr_tail.append(text[:_STDERR_LINE_CHARS])

    def transport_diagnostics(self) -> dict[str, object]:
        """Why the runtime went quiet, in body-free form.

        Read this BEFORE the session is dropped: closing the client clears the
        SDK transport, and the process exit code goes with it.

        The exit code comes through private SDK attributes because the public
        surface exposes it only by raising ``ProcessError`` on a call we are not
        making here. Every hop is guarded — a diagnostic that raises while
        reporting a failure is worse than no diagnostic.
        """
        lines = tuple(self._stderr_tail)
        exit_code: object = None
        try:
            transport = getattr(self._client, "_transport", None)
            exit_code = getattr(getattr(transport, "_process", None), "returncode", None)
            if exit_code is None:
                exit_error = getattr(transport, "_exit_error", None)
                exit_code = getattr(exit_error, "exit_code", None)
        except Exception:  # pragma: no cover - defensive
            exit_code = None
        return {
            "exit_code": exit_code,
            "stderr_class": _classify_cli_stderr(lines),
            "stderr_lines": len(lines),
        }
