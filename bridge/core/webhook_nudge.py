"""GitHub webhook *nudge* listener for durable external waits (#1222).

#740's ExternalWait watches PR checks by polling ``gh`` with exponential
backoff (30s doubling to a 300s cap, ``external_wait_monitor``), so a CI run
that finishes late in the backoff window can sit undetected for up to five
minutes. This module closes that latency gap without touching the trust
model:

- A webhook delivery is UNTRUSTED input and never a state source. A valid,
  HMAC-authenticated delivery only pulls the matching waits'
  ``next_poll_epoch`` forward (and resets their backoff) so the monitor
  re-reads GitHub through its authenticated ``gh`` transport on the next
  tick instead of at the next backoff slot. Exact-head validation, terminal
  classification, wake journaling, and resume budgets all stay in
  ``external_wait_monitor`` unchanged.
- A lost or undelivered webhook changes nothing: polling remains the
  loss-free fallback path.
- Fail-closed: a bad signature is rejected before any registry access, and
  enabling the listener without a secret refuses to start the listener
  while the bridge itself boots normally.

The HTTP surface is deliberately tiny and strict: one POST path,
Content-Length required (chunked rejected), bounded header/body sizes, the
connection closed after every response. Payload bodies are parsed in memory
and never persisted (#740's redaction contract). Public ingress (tunnel or
reverse proxy from GitHub to this listener) is a per-node operational
decision outside this module; the default bind is loopback-only.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from telegram_bot.core.external_wait import STATE_MONITORING, ExternalWaitRegistry

logger = logging.getLogger(__name__)

NUDGE_PATH = "/nudge"
SIGNATURE_HEADER = "x-hub-signature-256"
EVENT_HEADER = "x-github-event"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791
DEFAULT_MAX_BODY_BYTES = 1_048_576  # 1 MiB
MIN_MAX_BODY_BYTES = 4_096
MAX_MAX_BODY_BYTES = 16_777_216  # 16 MiB
MAX_HEADER_BYTES = 16_384
MAX_HEADER_COUNT = 64
REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_RATE_LIMIT_PER_MINUTE = 120

# Nudged waits restart from the registry's freshest cadence so a mid-run
# event (one workflow done, the rollup still pending) keeps polling briskly
# instead of resuming the 300s crawl.
NUDGE_POLL_INTERVAL_SECONDS = 30.0

_HANDLED_EVENTS = frozenset({"workflow_run", "check_suite", "pull_request"})


@dataclass(frozen=True, slots=True)
class NudgeTarget:
    """One (repo, PR, head) hint extracted from a webhook payload."""

    repo: str
    pr_numbers: Tuple[int, ...]
    head_sha: str  # lowercase hex, may be empty


def verify_signature(secret: str, body: bytes, header: Optional[str]) -> bool:
    """Constant-time check of GitHub's ``X-Hub-Signature-256`` header."""
    if not secret or not header:
        return False
    value = header.strip().lower()
    if not value.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(value[len("sha256=") :], expected)


def _pr_numbers(items: Any) -> Tuple[int, ...]:
    numbers: List[int] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            raw = item.get("number")
            if raw is None:
                continue
            try:
                numbers.append(int(raw))
            except (TypeError, ValueError):
                continue
    return tuple(numbers)


def parse_nudge_targets(event: str, payload: Dict[str, Any]) -> List[NudgeTarget]:
    """Extract nudge hints from one delivery; unknown events yield nothing.

    Only identity fields (repo full name, PR numbers, head SHA) are read.
    Nothing else in the payload is inspected or retained.
    """
    if event not in _HANDLED_EVENTS or not isinstance(payload, dict):
        return []
    repository = payload.get("repository")
    repo = ""
    if isinstance(repository, dict):
        repo = str(repository.get("full_name") or "").strip()
    if not repo or "/" not in repo:
        return []

    if event == "pull_request":
        pull = payload.get("pull_request")
        if not isinstance(pull, dict):
            return []
        raw_number = pull.get("number")
        if raw_number is None:
            return []
        try:
            number = int(raw_number)
        except (TypeError, ValueError):
            return []
        head = pull.get("head")
        sha = ""
        if isinstance(head, dict):
            sha = str(head.get("sha") or "").strip().lower()
        return [NudgeTarget(repo=repo, pr_numbers=(number,), head_sha=sha)]

    inner = payload.get(event)
    if not isinstance(inner, dict):
        return []
    sha = str(inner.get("head_sha") or "").strip().lower()
    numbers = _pr_numbers(inner.get("pull_requests"))
    if not sha and not numbers:
        return []
    return [NudgeTarget(repo=repo, pr_numbers=numbers, head_sha=sha)]


def _head_matches(recorded: str, delivered: str) -> bool:
    """Mirror the monitor's legacy short-SHA tolerance (#961)."""
    if not recorded or not delivered:
        return False
    recorded = recorded.lower()
    if recorded == delivered:
        return True
    return len(recorded) < 40 and delivered.startswith(recorded)


def apply_nudge(
    registry: ExternalWaitRegistry,
    targets: List[NudgeTarget],
    *,
    now: Optional[float] = None,
    clock: Callable[[], float] = time.time,
) -> int:
    """Pull matching monitoring waits' next poll to now; returns the count.

    Purely a scheduling accelerator: no wait state, head, or terminal field
    is derived from the delivery. Idempotent — repeating the same delivery
    finds ``next_poll_epoch`` already due and rewrites nothing further.
    """
    if not targets:
        return 0
    now = clock() if now is None else float(now)
    nudged = 0
    for record in registry.records():
        if record.get("state") != STATE_MONITORING:
            continue
        repo = str(record.get("repo") or "")
        matched = False
        for target in targets:
            if repo.lower() != target.repo.lower():
                continue
            try:
                pr_number = int(record.get("pr_number"))
            except (TypeError, ValueError):
                pr_number = -1
            if pr_number in target.pr_numbers or _head_matches(
                str(record.get("head_sha") or ""), target.head_sha
            ):
                matched = True
                break
        if not matched:
            continue
        if float(record.get("next_poll_epoch") or 0.0) <= now:
            continue  # already due — nothing to accelerate
        registry.reschedule(
            str(record["wait_id"]),
            next_poll_epoch=now,
            poll_interval_seconds=NUDGE_POLL_INTERVAL_SECONDS,
        )
        nudged += 1
    return nudged


class _HttpError(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


_STATUS_TEXT = {
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    411: "Length Required",
    413: "Payload Too Large",
    429: "Too Many Requests",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
}


class WebhookNudgeServer:
    """Loopback-bound, fail-closed HTTP listener that only accelerates polls."""

    def __init__(
        self,
        registry: ExternalWaitRegistry,
        *,
        secret: str,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not secret:
            raise ValueError("webhook nudge requires a non-empty secret")
        self._registry = registry
        self._secret = secret
        self._host = host
        self._port = int(port)
        self._max_body_bytes = max(
            MIN_MAX_BODY_BYTES, min(MAX_MAX_BODY_BYTES, int(max_body_bytes))
        )
        self._rate_limit = max(1, int(rate_limit_per_minute))
        self._clock = clock
        self._server: Optional[asyncio.Server] = None
        self._window_start = 0.0
        self._window_count = 0

    @property
    def port(self) -> int:
        """The actual bound port (useful when constructed with port 0)."""
        if self._server and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self._port

    async def start(self) -> bool:
        """Bind and serve; False (with a log) instead of raising on bind errors."""
        try:
            self._server = await asyncio.start_server(
                self._handle_connection, self._host, self._port
            )
        except OSError as exc:
            logger.error(
                "Webhook nudge listener failed to bind %s:%d (%s) — "
                "continuing without it; polling remains the fallback",
                self._host,
                self._port,
                exc.strerror or exc,
            )
            self._server = None
            return False
        logger.info(
            "Webhook nudge listener on %s:%d (path %s, body cap %d bytes)",
            self._host,
            self.port,
            NUDGE_PATH,
            self._max_body_bytes,
        )
        return True

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        try:
            await self._server.wait_closed()
        finally:
            self._server = None

    # -- request handling ---------------------------------------------------------

    def _rate_limited(self) -> bool:
        now = self._clock()
        if now - self._window_start >= 60.0:
            self._window_start = now
            self._window_count = 0
        self._window_count += 1
        return self._window_count > self._rate_limit

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        status, body = 500, ""
        try:
            status, body = await asyncio.wait_for(
                self._process(reader), timeout=REQUEST_TIMEOUT_SECONDS
            )
        except _HttpError as exc:
            status, body = exc.status, exc.reason
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
            status, body = 400, "malformed request"
        except Exception:
            logger.exception("Webhook nudge request failed")
            status, body = 500, "internal error"
        finally:
            try:
                await self._respond(writer, status, body)
            except (ConnectionError, OSError):
                pass

    async def _process(self, reader: asyncio.StreamReader) -> Tuple[int, str]:
        request_line = await reader.readline()
        if len(request_line) > 2_048:
            raise _HttpError(431, "request line too long")
        parts = request_line.decode("latin-1", "replace").split()
        if len(parts) < 3:
            raise _HttpError(400, "malformed request line")
        method, path = parts[0], parts[1]
        headers = await self._read_headers(reader)

        if self._rate_limited():
            raise _HttpError(429, "rate limited")
        if path.split("?", 1)[0] != NUDGE_PATH:
            raise _HttpError(404, "unknown path")
        if method.upper() != "POST":
            raise _HttpError(405, "POST only")
        if "chunked" in headers.get("transfer-encoding", "").lower():
            raise _HttpError(411, "chunked not accepted")
        try:
            content_length = int(headers.get("content-length", ""))
        except ValueError:
            raise _HttpError(411, "content-length required")
        if content_length < 0 or content_length > self._max_body_bytes:
            raise _HttpError(413, "body too large")

        body = await reader.readexactly(content_length)
        if not verify_signature(self._secret, body, headers.get(SIGNATURE_HEADER)):
            # Reject before any payload parse or registry access; never log
            # header or body material.
            raise _HttpError(401, "signature mismatch")

        event = headers.get(EVENT_HEADER, "").strip().lower()
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _HttpError(400, "invalid json")
        targets = parse_nudge_targets(event, payload if isinstance(payload, dict) else {})
        nudged = apply_nudge(self._registry, targets, clock=self._clock)
        if nudged:
            logger.info(
                "Webhook nudge accelerated %d wait(s) (event=%s)", nudged, event
            )
        return 204, ""

    async def _read_headers(self, reader: asyncio.StreamReader) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        total = 0
        for _ in range(MAX_HEADER_COUNT + 1):
            line = await reader.readline()
            total += len(line)
            if total > MAX_HEADER_BYTES:
                raise _HttpError(431, "headers too large")
            if line in (b"\r\n", b"\n", b""):
                return headers
            name, sep, value = line.decode("latin-1", "replace").partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        raise _HttpError(431, "too many headers")

    async def _respond(
        self, writer: asyncio.StreamWriter, status: int, body: str
    ) -> None:
        payload = body.encode("utf-8")
        text = _STATUS_TEXT.get(status, "Error")
        head = (
            f"HTTP/1.1 {status} {text}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(head.encode("latin-1") + payload)
        try:
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


def build_from_env(
    registry_factory: Callable[[], ExternalWaitRegistry],
    *,
    environ: Optional[Dict[str, str]] = None,
) -> Optional[WebhookNudgeServer]:
    """Env-gated constructor; None when disabled or misconfigured (fail-closed)."""
    env = os.environ if environ is None else environ
    raw = (env.get("CCC_WEBHOOK_NUDGE_ENABLED") or "").strip().lower()
    if raw not in {"1", "true", "yes", "on"}:
        return None
    secret = (env.get("CCC_WEBHOOK_NUDGE_SECRET") or "").strip()
    if not secret:
        logger.error(
            "CCC_WEBHOOK_NUDGE_ENABLED is set but CCC_WEBHOOK_NUDGE_SECRET is "
            "empty — refusing to start the listener (fail-closed)"
        )
        return None
    host = (env.get("CCC_WEBHOOK_NUDGE_HOST") or DEFAULT_HOST).strip() or DEFAULT_HOST
    try:
        port = int(env.get("CCC_WEBHOOK_NUDGE_PORT", "") or DEFAULT_PORT)
    except ValueError:
        port = DEFAULT_PORT
    try:
        max_body = int(
            env.get("CCC_WEBHOOK_NUDGE_MAX_BODY_BYTES", "") or DEFAULT_MAX_BODY_BYTES
        )
    except ValueError:
        max_body = DEFAULT_MAX_BODY_BYTES
    return WebhookNudgeServer(
        registry_factory(),
        secret=secret,
        host=host,
        port=port,
        max_body_bytes=max_body,
    )


__all__ = [
    "NUDGE_PATH",
    "NudgeTarget",
    "WebhookNudgeServer",
    "apply_nudge",
    "build_from_env",
    "parse_nudge_targets",
    "verify_signature",
]
