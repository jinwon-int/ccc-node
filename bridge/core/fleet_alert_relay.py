"""Fleet alert relay client — forwards push-spool records to the fleet alert bot (#2182).

Why: every node's Matrix frontend used to post the push spool (self-update,
agent-cron, fleet-watch, health alerts) into the owner's direct room *as the
agent*, between the agent's own progress bubbles, so a notice could bury the
work in flight. With ``CCC_PUSH_FLEET_RELAY_URL`` set, the Matrix spool
notifier hands each record to a central relay instead; a dedicated bot
(``@fleet-alerts``) posts it into the owner's "🔔 플릿 알림" room and the agent
rooms stay quiet.

Contract (``ccc.fleet-alert.v1``):

- ``POST <url>`` with a JSON body and three headers — ``X-Fleet-Node`` (this
  node's name), ``X-Fleet-Timestamp`` (unix seconds, integer) and
  ``X-Fleet-Signature: sha256=<hex>`` where hex is
  ``HMAC-SHA256(secret, f"{timestamp}." + body)``. The receiver checks the
  signature, the node allowlist and a replay window.
- 2xx = accepted (the caller archives the record). 4xx other than 408/429 =
  the relay refuses this record for good (``RelayRejected`` → archive, not
  retried forever). Everything else (5xx, 408/429, connection errors,
  timeouts) is transient (``RelayError`` → keep the record, retry next cycle).

The shared secret lives in an owner-only file (``CCC_PUSH_FLEET_RELAY_SECRET_FILE``)
— never in the environment or a record — and is never logged. This module
has no Matrix or Telegram dependency so it can be unit-tested with a local
HTTP server.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

SCHEMA = "ccc.fleet-alert.v1"
NODE_HEADER = "X-Fleet-Node"
TIMESTAMP_HEADER = "X-Fleet-Timestamp"
SIGNATURE_HEADER = "X-Fleet-Signature"
DEFAULT_TIMEOUT_SECONDS = 10.0
# Record keys forwarded verbatim (everything else in a spool record is dropped).
RAW_KEYS = ("ts", "event", "node", "text", "dedup", "chatId", "recipient")


class RelayError(Exception):
    """Transient delivery failure — keep the record and retry next cycle."""


class RelayRejected(Exception):
    """The relay refused this record for good (4xx) — archive it, do not retry."""


def default_node_name() -> str:
    """``CCC_NODE`` if set, else the short hostname (what self-update records use)."""
    name = (os.environ.get("CCC_NODE") or "").strip()
    if name:
        return name
    return socket.gethostname().split(".")[0] or "node"


def read_secret(path: Path) -> str:
    """Read the shared secret from an owner-only file.

    Fails closed on a missing/empty file. A file readable by group/others is
    refused too: the secret authenticates this node to the fleet relay, so a
    world-readable copy would let any local account forge fleet alerts.
    """
    p = Path(path).expanduser()
    st = p.lstat()
    if stat.S_ISLNK(st.st_mode):
        raise ValueError(f"fleet relay secret file must not be a symlink: {p}")
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError(f"fleet relay secret file must be owner-only (0600): {p}")
    secret = p.read_text(encoding="utf-8").strip()
    if not secret:
        raise ValueError(f"fleet relay secret file is empty: {p}")
    return secret


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{int(timestamp)}.".encode("ascii") + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(secret: str, timestamp: int, body: bytes, signature: str) -> bool:
    """Constant-time check of a ``sha256=<hex>`` signature (used by the receiver)."""
    try:
        return hmac.compare_digest(sign(secret, timestamp, body), str(signature))
    except (TypeError, ValueError):
        return False


def build_payload(node: str, record_name: str, data: dict, formatted_text: str) -> dict:
    """The JSON body for one spool record.

    ``text`` is the already-formatted notice (what the owner would have read in
    the agent room); ``raw`` keeps the record's own fields so the relay can
    group by event/dedup across nodes. ``dedup`` is namespaced by node.
    """
    raw = {k: data[k] for k in RAW_KEYS if k in data}
    dedup = str(data.get("dedup") or "").strip()
    return {
        "schema": SCHEMA,
        "node": node,
        "record": record_name,
        "event": str(data.get("event") or ""),
        "ts": data.get("ts"),
        "text": formatted_text,
        "dedup": f"{node}:{dedup}" if dedup else "",
        "raw": raw,
    }


class FleetAlertRelay:
    """Signed HTTP forwarder for one node. ``post`` is synchronous (call it off-loop)."""

    def __init__(
        self,
        url: str,
        secret: str,
        node: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Optional[Callable[..., Any]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not str(url).startswith(("http://", "https://")):
            raise ValueError("fleet relay url must be http(s)")
        self.url = str(url)
        self._secret = secret
        self.node = node
        self.timeout = float(timeout)
        self._opener = opener or urllib.request.urlopen
        self._clock = clock

    def post(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ts = int(self._clock())
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                NODE_HEADER: self.node,
                TIMESTAMP_HEADER: str(ts),
                SIGNATURE_HEADER: sign(self._secret, ts, body),
            },
        )
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                status = int(getattr(resp, "status", 200) or 200)
        except urllib.error.HTTPError as e:
            status = int(e.code)
            if 400 <= status < 500 and status not in (408, 429):
                raise RelayRejected(f"fleet relay refused record: HTTP {status}") from None
            raise RelayError(f"fleet relay HTTP {status}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise RelayError(f"fleet relay unreachable: {e.__class__.__name__}") from None
        if 200 <= status < 300:
            return
        if 400 <= status < 500 and status not in (408, 429):
            raise RelayRejected(f"fleet relay refused record: HTTP {status}")
        raise RelayError(f"fleet relay HTTP {status}")


def relay_from_settings(settings: Any) -> Optional[FleetAlertRelay]:
    """Build the relay from settings, or ``None`` when relay mode is off.

    Raises ``ValueError`` when the mode is on but misconfigured (no secret
    file, unsafe mode, empty secret): the caller must then *not* fall back to
    posting into the agent room — the whole point of relay mode is that the
    agent room stays quiet — and should keep the records for retry instead.
    """
    url = (getattr(settings, "push_fleet_relay_url", None) or "").strip()
    if not url:
        return None
    secret_file = getattr(settings, "push_fleet_relay_secret_file", None)
    if not secret_file:
        raise ValueError("CCC_PUSH_FLEET_RELAY_URL is set but CCC_PUSH_FLEET_RELAY_SECRET_FILE is not")
    secret = read_secret(Path(secret_file))
    node = (getattr(settings, "push_fleet_node", None) or "").strip() or default_node_name()
    timeout = float(getattr(settings, "push_fleet_relay_timeout", DEFAULT_TIMEOUT_SECONDS) or DEFAULT_TIMEOUT_SECONDS)
    return FleetAlertRelay(url, secret, node, timeout=timeout)
