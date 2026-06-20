"""Robust Telegram send helpers — retry, flood-control, timeout discrimination.

Ported from the Hermes gateway Telegram adapter
(``gateway/platforms/telegram.py``) for the ccc-node bridge.

The bridge's outbound send paths previously used a bare
``try: send(..., parse_mode) except Exception: send(...)`` pattern, which
unconditionally re-sends on *any* error. That risks DUPLICATE delivery: a
generic ``TimedOut`` may mean the request already reached Telegram, so blindly
re-sending posts the message twice.

``send_with_retry`` fixes this by discriminating error classes:
  * ``RetryAfter`` / flood control      -> honor ``retry_after``, then retry.
  * ``BadRequest`` (e.g. Markdown parse)-> permanent; raise so the caller can
                                            fall back to a plain-text send.
  * generic ``TimedOut``                -> may have reached Telegram; do NOT
                                            re-send (raise).
  * ``ConnectTimeout`` / ``PoolTimeout``-> provably NOT sent; safe to retry.
  * other ``NetworkError``              -> exponential backoff, then retry.
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


def looks_like_connect_timeout(error: BaseException) -> bool:
    """Return True when a Telegram TimedOut wraps a connect-timeout.

    A plain Telegram TimedOut may mean the request reached Telegram and should
    not be re-sent. A ConnectTimeout means the TCP connection was never
    established, so retrying is safe and prevents silent drops. We walk the
    ``__cause__`` / ``__context__`` chain so wrapped exceptions are detected.
    """
    seen = set()
    stack = [error]
    while stack:
        cur = stack.pop()
        ident = id(cur)
        if ident in seen:
            continue
        seen.add(ident)
        name = cur.__class__.__name__.lower()
        text = str(cur).lower()
        if "connecttimeout" in name or "connect timeout" in text or "connect timed out" in text:
            return True
        cause = getattr(cur, "__cause__", None)
        context = getattr(cur, "__context__", None)
        if cause is not None:
            stack.append(cause)
        if context is not None:
            stack.append(context)
    return False


def looks_like_pool_timeout(error: BaseException) -> bool:
    """Return True when a Telegram TimedOut wraps an httpx pool timeout.

    PTB converts ``httpx.PoolTimeout`` into ``telegram.error.TimedOut`` with a
    message stating the request was *not* sent to Telegram. Because the request
    never left the process, re-sending is safe and cannot duplicate -- the
    opposite of a generic TimedOut. We match the wrapped class name as well as
    the message string so the check survives PTB wording changes.
    """
    seen = set()
    stack = [error]
    while stack:
        cur = stack.pop()
        ident = id(cur)
        if ident in seen:
            continue
        seen.add(ident)
        name = cur.__class__.__name__.lower()
        text = str(cur).lower()
        if "pooltimeout" in name or "pool timeout" in text or (
            "connection pool" in text and "occupied" in text
        ):
            return True
        cause = getattr(cur, "__cause__", None)
        context = getattr(cur, "__context__", None)
        if cause is not None:
            stack.append(cause)
        if context is not None:
            stack.append(context)
    return False


async def send_with_retry(
    op_factory: Callable[[], Awaitable[Any]],
    *,
    name: str = "bridge",
    max_attempts: int = 3,
) -> Any:
    """Run a Telegram send/edit op with retry, flood-control, and safe-resend.

    ``op_factory`` must return a *fresh* awaitable each call (e.g.
    ``lambda: bot.send_message(...)``) so retries don't reuse a spent coroutine.

    Raises:
        telegram.error.BadRequest: permanent error (caller should plain-fallback).
        telegram.error.TimedOut: generic timeout that may have been delivered.
        Exception: the last error if all attempts are exhausted.
    """
    # Lazy import keeps this module importable without PTB at definition time
    # and tolerant of PTB version differences.
    from telegram.error import RetryAfter, TimedOut, NetworkError, BadRequest

    last_exc: BaseException = None
    for attempt in range(max_attempts):
        try:
            return await op_factory()
        except RetryAfter as e:
            last_exc = e
            if attempt == max_attempts - 1:
                raise
            wait = float(getattr(e, "retry_after", 0) or 1.0)
            logger.warning(
                "[%s] Telegram flood control (attempt %d/%d), retrying in %.1fs: %s",
                name, attempt + 1, max_attempts, wait, e,
            )
            await asyncio.sleep(wait)
        except BadRequest:
            # Permanent (e.g. Markdown parse error). Let the caller decide on a
            # plain-text fallback — never auto-retry an identical bad request.
            raise
        except TimedOut as e:
            last_exc = e
            # A generic TimedOut may have already reached Telegram -> do NOT
            # re-send (would duplicate). Only retry the provably-not-sent cases.
            if not looks_like_connect_timeout(e) and not looks_like_pool_timeout(e):
                raise
            if attempt == max_attempts - 1:
                raise
            wait = 2 ** attempt
            logger.warning(
                "[%s] Telegram connect/pool timeout (attempt %d/%d), retrying in %ds: %s",
                name, attempt + 1, max_attempts, wait, e,
            )
            await asyncio.sleep(wait)
        except NetworkError as e:
            last_exc = e
            if attempt == max_attempts - 1:
                raise
            wait = 2 ** attempt
            logger.warning(
                "[%s] Telegram network error (attempt %d/%d), retrying in %ds: %s",
                name, attempt + 1, max_attempts, wait, e,
            )
            await asyncio.sleep(wait)
        except Exception as e:  # noqa: BLE001 - catch flood worded as generic
            last_exc = e
            retry_after = getattr(e, "retry_after", None)
            if (retry_after is not None or "retry after" in str(e).lower()) and attempt < max_attempts - 1:
                wait = float(retry_after) if retry_after is not None else 1.0
                logger.warning(
                    "[%s] Telegram flood control (attempt %d/%d), retrying in %.1fs: %s",
                    name, attempt + 1, max_attempts, wait, e,
                )
                await asyncio.sleep(wait)
                continue
            raise
    if last_exc is not None:
        raise last_exc
