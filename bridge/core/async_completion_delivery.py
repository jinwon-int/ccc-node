"""Conversation delivery for durable async completions (#646 slice 2).

The coordinator turns one journal-observed, route-bound completion identity
into at most one conversation message.  It is the promotion half of the #646
flow — the degraded boundary (#646 slice 1) keeps journaling evidence and
notifying the owner, and this module is wired only when the runtime declares
``AsyncCompletionCapability.supports_durable_delivery``.

Delivery contracts:

- Exactly-once: the journal state machine is the only gate.  ``mark(claimed)``
  fails closed for any record that is not ``queued``/``retryable_failed``, so
  concurrent or duplicate coordinators cannot double-send.
- FIFO non-invasive: the message is sent directly to the conversation under
  the conversation lock, like dead-session recovery.  The turn FIFO, the
  active turn registry, and session generation state are never touched.
- Generation guard: before sending, the conversation's lifecycle generation
  high-water is compared to the observation's.  A rotated session (``/new``,
  provider switch, runtime recycle) transitions the record to
  ``terminal_failed('generation_mismatch')`` without sending — an old result
  can never land in a new session.
- Bounded bodies: provider payload text is trimmed to
  ``MAX_COMPLETION_TEXT_BYTES`` at extraction time and never reaches durable
  storage; only the send attempt sees it in memory.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from typing import Any, Protocol

from .async_completion_journal import AsyncCompletionJournal

logger = logging.getLogger(__name__)

MAX_COMPLETION_TEXT_BYTES = 4096
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_ATTEMPT_BACKOFF_SECONDS = 1.0
_DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
_DEFAULT_SEND_TIMEOUT_SECONDS = 15.0

Sender = Callable[[int, int, str], Awaitable[bool]]
LockFactory = Callable[[int, int], Any]
GenerationProbe = Callable[[int, int], int]


def bounded_completion_text(items: Any) -> str | None:
    """Extract bounded agent-message text from one turn's item list.

    Accepts the ``turn/completed`` ``items`` shape: a list of mappings whose
    ``type`` is ``agentMessage`` and whose ``text`` is a string.  Anything
    else — non-list, non-mapping items, foreign item types, oversized bodies —
    yields ``None`` or a bounded truncation, never a raise.  ``None`` means
    "no deliverable body": callers fall back to a body-free completion notice
    instead of guessing.
    """

    if not isinstance(items, list):
        return None
    chunks: list[str] = []
    remaining = MAX_COMPLETION_TEXT_BYTES
    for item in items:
        if remaining <= 0:
            break
        if not isinstance(item, dict):
            continue
        if item.get("type") != "agentMessage":
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text:
            continue
        encoded = text.encode("utf-8")
        if len(encoded) > remaining:
            encoded = encoded[:remaining]
            # Drop a possibly cut multibyte sequence tail.
            encoded = encoded.decode("utf-8", errors="ignore").encode("utf-8")
        chunk = encoded.decode("utf-8", errors="ignore")
        if not chunk:
            continue
        chunks.append(chunk)
        remaining -= len(encoded)
    joined = "\n".join(chunks).strip()
    return joined or None


class AsyncCompletionDeliveryCoordinator:
    """Claim, send, and mark one route-bound completion — exactly once."""

    def __init__(
        self,
        journal: AsyncCompletionJournal,
        *,
        lock_factory: LockFactory,
        sender: Sender,
        generation_probe: GenerationProbe,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        attempt_backoff_seconds: float = _DEFAULT_ATTEMPT_BACKOFF_SECONDS,
        lock_timeout_seconds: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
        send_timeout_seconds: float = _DEFAULT_SEND_TIMEOUT_SECONDS,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("delivery max_attempts must be positive")
        if attempt_backoff_seconds < 0:
            raise ValueError("delivery attempt backoff must be non-negative")
        self._journal = journal
        self._lock_factory = lock_factory
        self._sender = sender
        self._generation_probe = generation_probe
        self._max_attempts = max_attempts
        self._attempt_backoff_seconds = attempt_backoff_seconds
        self._lock_timeout_seconds = lock_timeout_seconds
        self._send_timeout_seconds = send_timeout_seconds

    async def deliver(
        self,
        idempotency_key: str,
        *,
        user_id: int,
        chat_id: int,
        session_generation: int,
        text: str | None,
        completion_count: int = 1,
    ) -> bool:
        """Deliver one observed completion; return the delivered verdict.

        Always returns without raising: every failure mode is folded into the
        journal state machine (``retryable_failed``/``terminal_failed``) and a
        ``False`` return.  The caller decides whether an owner fallback notice
        is warranted after a ``False`` verdict.
        """

        try:
            self._journal.mark(idempotency_key, "claimed")
        except ValueError:
            # Another coordinator claimed (or finished) this identity — the
            # exactly-once gate.  Never send on a lost claim race.
            logger.info(
                "Async completion delivery lost the claim race; skipping send"
            )
            return False

        try:
            route_generation = self._generation_probe(user_id, chat_id)
        except Exception:
            # A broken probe must fail closed: no verification, no send.
            logger.warning(
                "Async completion generation probe failed; dropping without "
                "delivery",
                exc_info=True,
            )
            self._terminal(idempotency_key, "generation_probe_failed")
            return False
        if route_generation != session_generation:
            self._terminal(
                idempotency_key,
                "generation_mismatch",
            )
            logger.warning(
                "Async completion dropped: conversation generation moved "
                "(observed=%d, current=%d); no cross-session delivery",
                session_generation,
                route_generation,
            )
            return False

        for attempt in range(1, self._max_attempts + 1):
            lock = self._lock_factory(user_id, chat_id)
            try:
                await asyncio.wait_for(
                    lock.acquire(), timeout=self._lock_timeout_seconds
                )
            except (asyncio.TimeoutError, TimeoutError):
                error_code = "lock_timeout"
            else:
                try:
                    sent = await self._send(
                        user_id, chat_id, text, completion_count
                    )
                except Exception:
                    sent = False
                    logger.warning(
                        "Async completion sender raised; attempt %d/%d failed",
                        attempt,
                        self._max_attempts,
                        exc_info=True,
                    )
                finally:
                    lock.release()
                error_code = "" if sent else "send_failed"
            if error_code == "":
                self._journal.mark(idempotency_key, "delivered")
                return True
            if attempt < self._max_attempts:
                await asyncio.sleep(self._attempt_backoff_seconds)
        self._terminal(idempotency_key, _final_error(error_code))
        return False

    async def _send(
        self,
        user_id: int,
        chat_id: int,
        text: str | None,
        completion_count: int,
    ) -> bool:
        message = format_completion_notice(text, completion_count)
        return bool(
            await asyncio.wait_for(
                self._sender(user_id, chat_id, message),
                timeout=self._send_timeout_seconds,
            )
        )

    def _terminal(self, idempotency_key: str, error_code: str) -> None:
        try:
            self._journal.mark(
                idempotency_key, "terminal_failed", error_code=error_code
            )
        except ValueError:
            logger.warning(
                "Async completion terminal transition raced; record kept as-is"
            )


def _final_error(last_error_code: str) -> str:
    return last_error_code or "send_failed"


def format_completion_notice(text: str | None, completion_count: int = 1) -> str:
    """Render the bounded, body-free-unless-available conversation message.

    The message never claims to be a turn response: it is visibly a
    background-completion notice so the interactive FIFO contract stays
    obvious to the user.  Without an extractable body it stays fully
    body-free.
    """

    count_prefix = "" if completion_count == 1 else f"({completion_count} completions)\n"
    if text:
        return f"⟳ Background task completed\n{count_prefix}{text}"
    suffix = "" if completion_count == 1 else f" x{completion_count}"
    return f"⟳ Background task completed{suffix} (no body available)"


class AsyncCompletionSender(Protocol):
    """Telegram seam signature wired by the composition/lifecycle layer."""

    async def __call__(self, user_id: int, chat_id: int, text: str) -> bool: ...


def build_telegram_sender(
    bot: Any,
    *,
    send_timeout: float = _DEFAULT_SEND_TIMEOUT_SECONDS,
) -> AsyncCompletionSender:
    """Build the conversation sender from a PTB bot handle.

    Mirrors the dead-session recovery idiom: fresh awaitable per attempt via
    ``send_with_retry``, hard-bounded by ``wait_for``.  Any failure becomes a
    ``False`` verdict for the coordinator's retry/terminal logic.
    """

    async def sender(user_id: int, chat_id: int, text: str) -> bool:
        from telegram_bot.utils.tg_robust import send_with_retry

        try:
            await asyncio.wait_for(
                send_with_retry(
                    lambda: bot.send_message(chat_id=chat_id, text=text),
                    name="async-completion",
                ),
                timeout=send_timeout,
            )
        except Exception as error:
            logger.warning(
                "Async completion send failed for chat %s: %s",
                chat_id,
                type(error).__name__,
            )
            return False
        return True

    return sender


__all__ = [
    "AsyncCompletionDeliveryCoordinator",
    "AsyncCompletionSender",
    "MAX_COMPLETION_TEXT_BYTES",
    "bounded_completion_text",
    "build_telegram_sender",
    "format_completion_notice",
]
