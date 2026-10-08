"""Fleet alert relay receiver (#2182): signed POST from each node's bridge → owner-only queue.

Runs on the relay host (Tailscale-internal bind). Each node's Matrix spool
notifier in relay mode (``CCC_PUSH_FLEET_RELAY_URL``) posts one
``ccc.fleet-alert.v1`` record at a time with ``X-Fleet-Node`` /
``X-Fleet-Timestamp`` / ``X-Fleet-Signature`` (bridge/core/fleet_alert_relay).
This receiver:

- knows a node only through ``<secrets-dir>/<node>`` — an owner-only (0600)
  file holding that node's shared secret. The directory listing *is* the
  allowlist; adding/rotating a node needs no restart and no config edit.
- verifies the HMAC in constant time, rejects timestamps outside ``--skew``
  seconds, and refuses bodies over ``--max-body`` bytes before parsing;
- requires the payload's ``node`` to equal the signed header node, so one
  node can never speak for another;
- queues the already-formatted ``text`` for the Matrix sender with a
  fingerprint of ``node + record`` — a node that retries after a lost 2xx
  gets ``202 duplicate`` instead of a second room message.

Status codes mirror the client contract: 202 accepted; 401 (signature /
timestamp / headers) and 403 (unknown node) and 400 (payload) are final for
that record; 413 for oversize; 503 when the queue cannot be written (the
node keeps the record and retries). Logs never contain alert bodies.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_alerts_outbox as outbox  # noqa: E402

from telegram_bot.core.fleet_alert_relay import (  # noqa: E402
    NODE_HEADER,
    SCHEMA,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    read_secret,
    verify,
)

ALERTS_PATH = "/v1/alerts"
HEALTH_PATH = "/healthz"
NODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
DEFAULT_SKEW_S = 300
DEFAULT_MAX_BODY = 65_536
log = logging.getLogger("fleet-alerts-receiver")


@dataclass(frozen=True)
class Decision:
    status: int
    reason: str
    node: str = ""
    payload: Optional[dict] = None


def node_secret(secrets_dir: Path, node: str) -> Optional[str]:
    """The node's secret, or ``None`` when the node is unknown or its file is unsafe."""
    if not NODE_RE.match(node):
        return None
    try:
        return read_secret(secrets_dir / node)
    except (FileNotFoundError, ValueError, OSError):
        return None


def check_request(
    headers: Mapping[str, str],
    body: bytes,
    *,
    secrets_dir: Path,
    now: float,
    skew: float = DEFAULT_SKEW_S,
    max_body: int = DEFAULT_MAX_BODY,
) -> Decision:
    """Pure admission check: headers + raw body → ``Decision`` (no I/O but the secret file)."""
    if len(body) > max_body:
        return Decision(413, "oversize")
    node = (headers.get(NODE_HEADER) or "").strip()
    ts_raw = (headers.get(TIMESTAMP_HEADER) or "").strip()
    sig = (headers.get(SIGNATURE_HEADER) or "").strip()
    if not node or not ts_raw or not sig:
        return Decision(401, "missing-headers")
    if not NODE_RE.match(node):
        return Decision(403, "bad-node")
    secret = node_secret(secrets_dir, node)
    if secret is None:
        return Decision(403, "unknown-node", node)
    try:
        ts = int(ts_raw)
    except ValueError:
        return Decision(401, "bad-timestamp", node)
    if abs(now - ts) > skew:
        return Decision(401, "stale-timestamp", node)
    if not verify(secret, ts, body, sig):
        return Decision(401, "bad-signature", node)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return Decision(400, "bad-json", node)
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return Decision(400, "bad-schema", node)
    if payload.get("node") != node:
        return Decision(400, "node-mismatch", node)
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > outbox.MAX_BODY_BYTES:
        return Decision(400, "bad-text", node)
    return Decision(202, "ok", node, payload)


def queue_payload(queue_dir: Path, payload: dict) -> tuple[str, bool]:
    node = str(payload.get("node") or "")
    key = str(payload.get("record") or payload.get("dedup") or "")
    fp = outbox.fingerprint(node, key) if key else None
    return outbox.enqueue(
        queue_dir,
        payload["text"],
        node=node,
        event=str(payload.get("event") or ""),
        fingerprint=fp,
    )


class Receiver:
    """Request handling without the HTTP plumbing, so it can be unit-tested directly."""

    def __init__(
        self,
        *,
        secrets_dir: Path,
        queue_dir: Path,
        skew: float = DEFAULT_SKEW_S,
        max_body: int = DEFAULT_MAX_BODY,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.secrets_dir = secrets_dir
        self.queue_dir = queue_dir
        self.skew = skew
        self.max_body = max_body
        self.clock = clock

    def handle(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict]:
        if method == "GET" and path == HEALTH_PATH:
            return 200, {"ok": True}
        if method != "POST" or path != ALERTS_PATH:
            return 404, {"error": "not-found"}
        d = check_request(
            headers, body, secrets_dir=self.secrets_dir, now=self.clock(), skew=self.skew, max_body=self.max_body
        )
        if d.status != 202:
            log.warning("refused node=%s reason=%s status=%d", d.node or "?", d.reason, d.status)
            return d.status, {"error": d.reason}
        assert d.payload is not None
        try:
            txn, dup = queue_payload(self.queue_dir, d.payload)
        except Exception as exc:  # queue unsafe/unwritable → node keeps the record
            log.error("queue failure node=%s reason=%s", d.node, exc.__class__.__name__)
            return 503, {"error": "queue-unavailable"}
        log.info("accepted node=%s event=%s txn=%s duplicate=%s", d.node, d.payload.get("event", ""), txn, dup)
        return 202, {"accepted": True, "txn": txn, "duplicate": dup}


def make_handler(receiver: Receiver) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "fleet-alerts/1"
        sys_version = ""

        def _reply(self, status: int, obj: dict) -> None:
            data = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> Optional[bytes]:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if n < 0 or n > receiver.max_body:
                return None
            return self.rfile.read(n)

        def do_GET(self) -> None:  # noqa: N802
            status, obj = receiver.handle("GET", self.path, self.headers, b"")
            self._reply(status, obj)

        def do_POST(self) -> None:  # noqa: N802
            body = self._body()
            if body is None:
                self._reply(413, {"error": "oversize"})
                return
            status, obj = receiver.handle("POST", self.path, self.headers, body)
            self._reply(status, obj)

        def log_message(self, *args: Any) -> None:  # body-free logging is done by Receiver
            pass

    return Handler


def serve(receiver: Receiver, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(receiver))
    httpd.daemon_threads = True
    return httpd


def _parse_bind(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError("--bind must be host:port")
    return host, int(port)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bind", type=_parse_bind, default=("127.0.0.1", 8795), help="host:port (default 127.0.0.1:8795)")
    parser.add_argument(
        "--secrets-dir", dest="nodes_dir", required=True, type=Path, help="directory of <node> secret files (0600)"
    )
    parser.add_argument("--queue", required=True, type=Path, help="owner-only queue directory shared with the sender")
    parser.add_argument("--skew", type=float, default=DEFAULT_SKEW_S, help="max |now - X-Fleet-Timestamp| in seconds")
    parser.add_argument("--max-body", type=int, default=DEFAULT_MAX_BODY)
    args = parser.parse_args(argv)
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.nodes_dir.is_dir():
        # The directory path is not sensitive (its *contents* are, and are never logged).
        log.error("node key directory missing (--secrets-dir)")
        return 2
    outbox.pending_count(args.queue)  # fail fast on an unsafe queue directory
    receiver = Receiver(secrets_dir=args.nodes_dir, queue_dir=args.queue, skew=args.skew, max_body=args.max_body)
    host, port = args.bind
    httpd = serve(receiver, host, port)
    log.info("fleet alert receiver listening on %s:%d", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
