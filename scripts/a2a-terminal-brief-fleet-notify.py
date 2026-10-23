#!/usr/bin/env python3
"""A2A Terminal Brief -> Matrix fleet alerts (ccc-node#2498, a2a-nexus#2460).

Why this exists: the broker writes one operator-facing row per finished task to
its terminal outbox (including parent-round ``n/N`` aggregates, cross-broker
children, and relay-failure notices). The OpenClaw Gateway notifier that used
to deliver those rows retired on 2026-08-09, and nothing consumed the outbox
afterwards. This script is the replacement consumer. It runs on a broker host,
reads the local broker's outbox through a cursor, and drops one record per new
operator-facing row into the ccc-node push spool. The Matrix bridge then
relays the record to the "🔔 플릿 알림" room. No new messenger credential is
involved.

Owner decision (2026-10-23): rows that already existed are never sent, only
rows created from now on.
- The first run pins the cursor at the current end of the outbox and sends
  nothing.
- When the cursor row ages out of the broker's bounded outbox, the broker
  serves the outbox from the start again (unknown ``after_id`` -> index 0).
  Two guards keep that from replaying the backlog: a row created before the
  first run is never sent, and processed ids are remembered.

Only rows without ``notificationOwnership`` are operator-facing.
Parent-broker-only rows are either child-side rows the relay delivers to the
parent, or aggregation evidence. After queueing a record, the script stores
receipt ``produced`` on the row: the notice was produced and handed to the
delivery path. That is not a Terminal Brief ACK, which needs operator-visible
evidence.

Configuration (environment):
  A2A_TB_NOTIFY_BROKER_URL        default http://127.0.0.1:8787
  A2A_TB_NOTIFY_EDGE_SECRET_FILE  0600 file holding the broker edge secret, or
  A2A_TB_NOTIFY_BROKER_ENV_FILE   the broker's .env (EDGE_SECRET / A2A_EDGE_SECRET read),
                                  default /root/work/a2a/a2a-nexus/packages/broker/.env
  A2A_TB_NOTIFY_REQUESTER_ID      default ccc-terminal-brief-notifier (role operator)
  A2A_TB_NOTIFY_STATE_FILE        default ~/.claude/state/a2a-terminal-brief-notify/state.json
  CCC_PUSH_SPOOL                  default ~/.claude/state/telegram-spool
  A2A_TB_NOTIFY_NODE              label for the record and text, default the short hostname
  A2A_TB_NOTIFY_MAX_PER_RUN       default 8 (the bridge forwards at most 10 records a minute)
  A2A_TB_NOTIFY_RECORD_RECEIPT    default true

Usage: a2a-terminal-brief-fleet-notify.py [--dry-run]   (run once; a timer repeats it)
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

EVENT = "A2ATerminalBrief"
STATE_VERSION = 1
MAX_PROCESSED_IDS = 5000
PAGE_LIMIT = 200
MAX_TEXT_CHARS = 1500
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ConfigError(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_edge_secret(env: dict[str, str]) -> str | None:
    secret_file = env.get("A2A_TB_NOTIFY_EDGE_SECRET_FILE")
    if secret_file:
        path = Path(secret_file)
        if path.stat().st_mode & 0o077:
            raise ConfigError(f"A2A_TB_NOTIFY_EDGE_SECRET_FILE must not be group/other accessible: {path}")
        value = path.read_text().strip()
        if not value:
            raise ConfigError(f"edge secret file is empty: {path}")
        return value
    env_file = Path(env.get("A2A_TB_NOTIFY_BROKER_ENV_FILE", "/root/work/a2a/a2a-nexus/packages/broker/.env"))
    if not env_file.is_file():
        return None
    values: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        match = re.match(r"\s*(?:export\s+)?(EDGE_SECRET|A2A_EDGE_SECRET)\s*=\s*(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip("'\"")
    return values.get("EDGE_SECRET") or values.get("A2A_EDGE_SECRET") or None


class Broker:
    def __init__(self, base_url: str, edge_secret: str | None, requester_id: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.edge_secret = edge_secret
        self.requester_id = requester_id
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {
            "content-type": "application/json",
            "x-a2a-requester-id": self.requester_id,
            "x-a2a-requester-role": "operator",
        }
        if self.edge_secret:
            headers["x-a2a-edge-secret"] = self.edge_secret
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - local broker URL
                return json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as error:
            error.close()  # release the response body; callers only need the failure
            raise

    def outbox(self, after_id: str | None, limit: int) -> list[dict]:
        query = {"limit": str(limit)}
        if after_id:
            query["after_id"] = after_id
        payload = self._request("GET", "/a2a/tasks/terminal-outbox?" + urllib.parse.urlencode(query))
        events = payload.get("events")
        return events if isinstance(events, list) else []

    def record_produced(self, event_id: str) -> None:
        self._request("POST", "/a2a/tasks/terminal-outbox/receipt", {
            "id": event_id,
            "receipt": {"status": "produced", "note": "queued to the Matrix fleet-alert room via the ccc push spool"},
        })


def load_state(path: Path) -> dict | None:
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION or not parse_iso(state.get("initializedAt")):
        raise ConfigError(f"state file is not a v{STATE_VERSION} notifier state: {path}")
    state.setdefault("processed", [])
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state["processed"] = state["processed"][-MAX_PROCESSED_IDS:]
    fd, tmp = tempfile.mkstemp(prefix=".state-", dir=str(path.parent))
    with os.fdopen(fd, "w") as handle:
        json.dump(state, handle, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def is_operator_facing(event: dict) -> bool:
    payload = event.get("payload")
    return (
        event.get("kind") == "task.terminal"
        and isinstance(payload, dict)
        and not payload.get("notificationOwnership")
    )


def clean(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = CONTROL_CHARS.sub(" ", value).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_text(event: dict, broker_label: str) -> str:
    payload = event.get("payload") or {}
    status = clean(payload.get("status"), 20) or "unknown"
    worker = clean(payload.get("worker"), 80) or clean(payload.get("taskId"), 120)
    title = clean(payload.get("terminalBriefTitle") or payload.get("title"), 240) or f"A2A Terminal Brief {status}: {worker}"
    lines = [title]
    brief = clean(payload.get("taskBrief") or payload.get("taskDescription"), 200)
    if brief and brief != title:
        lines.append(brief)
    summary = clean(payload.get("testSummary"), 300)
    if summary and summary not in (brief, title):
        lines.append(summary)
    fallback = payload.get("relayFallback")
    if isinstance(fallback, dict):
        lines.append(
            f"relay failure: {clean(fallback.get('lastErrorCode'), 40) or 'unknown'}"
            f" (parent {clean(fallback.get('parentBrokerId'), 80)}, round {clean(fallback.get('parentRoundId'), 120)})"
        )
    for key in ("doneUrl", "prUrl", "blockUrl"):
        url = clean(payload.get(key), 300)
        if url.startswith("https://"):
            lines.append(url)
            break
    lines.append(f"broker {broker_label} · task {clean(payload.get('taskId'), 160)}")
    text = "\n".join(lines)
    return text if len(text) <= MAX_TEXT_CHARS else text[: MAX_TEXT_CHARS - 1] + "…"


def write_spool_record(spool: Path, node: str, broker_label: str, event: dict) -> Path:
    spool.mkdir(parents=True, exist_ok=True)
    ts = now_iso()
    record = {
        "ts": ts,
        "event": EVENT,
        "node": node,
        "text": render_text(event, broker_label),
        "dedup": f"{EVENT}:{broker_label}:{event['id']}",
    }
    digest = hashlib.sha256(event["id"].encode()).hexdigest()[:12]
    name = f"{re.sub(r'[^0-9]', '', ts)[:17]}-{EVENT}-{os.getpid()}-{digest}.json"
    fd, tmp = tempfile.mkstemp(prefix=".a2a-tb-", suffix=".tmp", dir=str(spool))
    with os.fdopen(fd, "w") as handle:
        json.dump(record, handle, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    final = spool / name
    os.replace(tmp, final)
    return final


def pin_cursor_at_end(broker: Broker) -> tuple[str | None, int]:
    """Walk to the end of the outbox without sending anything."""
    cursor: str | None = None
    seen = 0
    while True:
        events = broker.outbox(cursor, PAGE_LIMIT)
        if not events:
            return cursor, seen
        seen += len(events)
        cursor = events[-1].get("id") or cursor
        if len(events) < PAGE_LIMIT:
            return cursor, seen


def run(env: dict[str, str], dry_run: bool = False, out=sys.stdout) -> int:
    node = env.get("A2A_TB_NOTIFY_NODE") or socket.gethostname().split(".")[0]
    broker_label = node
    home = Path(env.get("HOME", str(Path.home())))
    state_path = Path(env.get("A2A_TB_NOTIFY_STATE_FILE", str(home / ".claude/state/a2a-terminal-brief-notify/state.json")))
    spool = Path(env.get("CCC_PUSH_SPOOL", str(home / ".claude/state/telegram-spool")))
    max_per_run = max(1, int(env.get("A2A_TB_NOTIFY_MAX_PER_RUN", "8")))
    record_receipt = env.get("A2A_TB_NOTIFY_RECORD_RECEIPT", "true").lower() not in ("0", "false", "no")
    broker = Broker(
        env.get("A2A_TB_NOTIFY_BROKER_URL", "http://127.0.0.1:8787"),
        read_edge_secret(env),
        env.get("A2A_TB_NOTIFY_REQUESTER_ID", "ccc-terminal-brief-notifier"),
    )

    state = load_state(state_path)
    if state is None:
        cursor, backlog = pin_cursor_at_end(broker)
        state = {"version": STATE_VERSION, "cursor": cursor, "initializedAt": now_iso(), "processed": []}
        if not dry_run:
            save_state(state_path, state)
        print(json.dumps({"kind": "a2a-terminal-brief-fleet-notify.init", "cursor": cursor,
                          "skippedBacklog": backlog, "dryRun": dry_run}), file=out)
        return 0

    initialized_at = parse_iso(state["initializedAt"])
    processed = set(state["processed"])
    sent = skipped = receipt_errors = 0
    events = broker.outbox(state.get("cursor"), PAGE_LIMIT)
    for event in events:
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id:
            continue
        created = parse_iso(event.get("createdAt"))
        # Already handled, or created before the first run: the backlog is
        # never sent, even when an aged-out cursor makes the broker replay
        # the outbox from the start.
        if event_id in processed or created is None or created < initialized_at or not is_operator_facing(event):
            skipped += event_id not in processed
            state["cursor"] = event_id
            if event_id not in processed:
                state["processed"].append(event_id)
                processed.add(event_id)
            continue
        if sent >= max_per_run:
            break  # leave the cursor here; the next run continues
        if not dry_run:
            write_spool_record(spool, node, broker_label, event)
            if record_receipt:
                try:
                    broker.record_produced(event_id)
                except (urllib.error.URLError, OSError, ValueError):
                    receipt_errors += 1  # the notice is queued; the receipt is best effort
        sent += 1
        state["cursor"] = event_id
        state["processed"].append(event_id)
        processed.add(event_id)
        if not dry_run:
            save_state(state_path, state)
    if not dry_run:
        save_state(state_path, state)
    print(json.dumps({"kind": "a2a-terminal-brief-fleet-notify.poll", "fetched": len(events), "sent": sent,
                      "skipped": skipped, "receiptErrors": receipt_errors, "cursor": state.get("cursor"),
                      "dryRun": dry_run}), file=out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="read and decide, but write no spool record, receipt, or state")
    args = parser.parse_args(argv)
    try:
        return run(dict(os.environ), dry_run=args.dry_run)
    except ConfigError as error:
        print(f"a2a-terminal-brief-fleet-notify: {error}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError, ValueError) as error:
        # Transient (broker restart, network): the timer retries; nothing was skipped.
        print(f"a2a-terminal-brief-fleet-notify: broker unavailable: {error.__class__.__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
