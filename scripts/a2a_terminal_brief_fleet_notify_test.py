"""Hermetic tests for scripts/a2a-terminal-brief-fleet-notify.py (ccc-node#2498).

A fake broker serves the terminal outbox with the real broker's cursor
semantics: rows after ``after_id``, and an unknown (aged-out) ``after_id``
replays the outbox from the start.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "fleet_notify", Path(__file__).resolve().parent / "a2a-terminal-brief-fleet-notify.py"
)
notify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notify)

EDGE = "test-edge-secret-value"


def iso(offset_s: float) -> str:
    moment = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_s)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def row(row_id: str, created: str, **payload) -> dict:
    base = {"taskId": row_id.split(":")[-1], "status": "succeeded", "worker": "workerzeta",
            "createdAt": created, "updatedAt": created}
    base.update(payload)
    return {"id": row_id, "kind": "task.terminal", "taskEventId": 1, "createdAt": created,
            "payload": base, "receipt": {"status": "accepted", "updatedAt": created}, "attempts": 0}


class FakeBroker:
    def __init__(self):
        self.events: list[dict] = []
        self.receipts: list[dict] = []
        self.headers: list[dict] = []
        self.fail_receipts = False
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                broker.headers.append({k.lower(): v for k, v in self.headers.items()})
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/a2a/tasks/terminal-outbox":
                    return self._send(404, {})
                query = urllib.parse.parse_qs(parsed.query)
                after = (query.get("after_id") or [None])[0]
                limit = int((query.get("limit") or ["1000"])[0])
                ids = [event["id"] for event in broker.events]
                start = ids.index(after) + 1 if after in ids else 0  # unknown cursor -> from the start
                events = broker.events[start:start + limit]
                self._send(200, {"kind": "task.terminal.outbox", "count": len(events), "events": events})

            def do_POST(self):
                broker.headers.append({k.lower(): v for k, v in self.headers.items()})
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                if self.path == "/a2a/tasks/terminal-outbox/receipt":
                    if broker.fail_receipts:
                        return self._send(500, {})
                    broker.receipts.append(body)
                    return self._send(200, {"event": {}})
                self._send(404, {})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FleetNotifyTest(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.spool = root / "spool"
        self.state = root / "state" / "state.json"
        secret = root / "edge"
        secret.write_text(EDGE + "\n")
        os.chmod(secret, 0o600)
        self.env = {
            "HOME": str(root),
            "A2A_TB_NOTIFY_BROKER_URL": self.broker.url,
            "A2A_TB_NOTIFY_EDGE_SECRET_FILE": str(secret),
            "A2A_TB_NOTIFY_STATE_FILE": str(self.state),
            "CCC_PUSH_SPOOL": str(self.spool),
            "A2A_TB_NOTIFY_NODE": "seoseo",
        }

    def tearDown(self):
        self.broker.close()
        self.tmp.cleanup()

    def run_once(self, **extra) -> dict:
        out = io.StringIO()
        self.assertEqual(notify.run({**self.env, **extra}, out=out), 0)
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def spool_records(self) -> list[dict]:
        if not self.spool.exists():
            return []
        return [json.loads(path.read_text()) for path in sorted(self.spool.glob("*.json"))]

    def test_first_run_pins_the_cursor_and_never_sends_the_backlog(self):
        self.broker.events = [row(f"terminal:old-{i}", iso(-3600 + i)) for i in range(450)]
        summary = self.run_once()
        self.assertEqual(summary["kind"], "a2a-terminal-brief-fleet-notify.init")
        self.assertEqual(summary["skippedBacklog"], 450)
        self.assertEqual(summary["cursor"], "terminal:old-449")
        self.assertEqual(self.spool_records(), [])
        self.assertEqual(self.broker.receipts, [])
        self.assertEqual(oct(self.state.stat().st_mode & 0o777), "0o600")
        # A later run with nothing new sends nothing.
        self.assertEqual(self.run_once()["sent"], 0)

    def test_new_operator_facing_rows_reach_the_spool_and_get_a_produced_receipt(self):
        self.broker.events = [row("terminal:old", iso(-60))]
        self.run_once()
        self.broker.events += [
            row("terminal:new-op", iso(5), terminalBriefTitle="A2A Terminal Brief 완료: workerzeta(완료 2/4)",
                taskBrief="fix flaky test", doneUrl="https://github.com/acme/x/issues/1#issuecomment-2"),
            row("terminal:new-evidence", iso(6), notificationOwnership={"scope": "parent-broker-only", "ownerBrokerId": "gwakga"}),
        ]
        summary = self.run_once()
        self.assertEqual((summary["sent"], summary["skipped"]), (1, 1))
        [record] = self.spool_records()
        self.assertEqual(record["event"], "A2ATerminalBrief")
        self.assertEqual(record["node"], "seoseo")
        self.assertEqual(record["dedup"], "A2ATerminalBrief:seoseo:terminal:new-op")
        self.assertEqual(record["text"].splitlines(), [
            "A2A Terminal Brief 완료: workerzeta(완료 2/4)",
            "fix flaky test",
            "https://github.com/acme/x/issues/1#issuecomment-2",
            "broker seoseo · task new-op",
        ])
        self.assertEqual(self.broker.receipts, [{"id": "terminal:new-op", "receipt": {
            "status": "produced", "note": "queued to the Matrix fleet-alert room via the ccc push spool"}}])
        spool_file = next(self.spool.glob("*.json"))
        self.assertEqual(oct(spool_file.stat().st_mode & 0o777), "0o600")
        self.assertEqual(list(self.spool.glob(".*")), [], "no temp file left behind")
        self.assertTrue(all(h.get("x-a2a-edge-secret") == EDGE for h in self.broker.headers))
        self.assertTrue(all(h.get("x-a2a-requester-role") == "operator" for h in self.broker.headers))
        # Re-running does not resend.
        self.assertEqual(self.run_once()["sent"], 0)
        self.assertEqual(len(self.spool_records()), 1)

    def test_an_aged_out_cursor_replays_the_outbox_but_neither_backlog_nor_sent_rows_are_resent(self):
        self.broker.events = [row("terminal:backlog-1", iso(-120)), row("terminal:backlog-2", iso(-110))]
        self.run_once()
        self.broker.events.append(row("terminal:new-1", iso(5)))
        self.assertEqual(self.run_once()["sent"], 1)
        # Retention evicts every row the cursor knew; the broker now serves from the start.
        self.broker.events = [row("terminal:backlog-1", iso(-120)), row("terminal:new-1", iso(5)), row("terminal:new-2", iso(6))]
        state = json.loads(self.state.read_text())
        state["cursor"] = "terminal:evicted"
        self.state.write_text(json.dumps(state))
        summary = self.run_once()
        self.assertEqual(summary["sent"], 1)
        self.assertEqual([r["dedup"].rsplit(":", 1)[-1] for r in self.spool_records()], ["new-1", "new-2"])

    def test_the_per_run_cap_leaves_the_rest_for_the_next_run(self):
        self.broker.events = [row("terminal:seed", iso(-10))]
        self.run_once()
        self.broker.events += [row(f"terminal:n{i}", iso(5 + i)) for i in range(3)]
        self.assertEqual(self.run_once(A2A_TB_NOTIFY_MAX_PER_RUN="2")["sent"], 2)
        self.assertEqual(self.run_once(A2A_TB_NOTIFY_MAX_PER_RUN="2")["sent"], 1)
        self.assertEqual(len(self.spool_records()), 3)

    def test_relay_fallback_rows_carry_the_failure_and_unsafe_urls_are_dropped(self):
        self.broker.events = [row("terminal:seed", iso(-10))]
        self.run_once()
        self.broker.events.append(row(
            "terminal:fallback", iso(5),
            terminalBriefTitle="A2A Terminal Brief relay failure: workerzeta (missing_parent)",
            relayFallback={"lastErrorCode": "missing_parent", "parentBrokerId": "seoseo", "parentRoundId": "r-1"},
            doneUrl="http://insecure.example/x",
        ))
        self.run_once()
        [record] = self.spool_records()
        lines = record["text"].splitlines()
        self.assertIn("relay failure: missing_parent (parent seoseo, round r-1)", lines)
        self.assertFalse(any(line.startswith("http://") for line in lines))

    def test_dry_run_writes_nothing(self):
        self.broker.events = [row("terminal:seed", iso(-10))]
        summary = self.run_once(**{})  # real init
        self.broker.events.append(row("terminal:new", iso(5)))
        out = io.StringIO()
        notify.run(self.env, dry_run=True, out=out)
        self.assertEqual(json.loads(out.getvalue())["sent"], 1)
        self.assertEqual(self.spool_records(), [])
        self.assertEqual(self.broker.receipts, [])
        self.assertEqual(json.loads(self.state.read_text())["cursor"], summary["cursor"])

    def test_a_failed_receipt_does_not_lose_or_repeat_the_notice(self):
        self.broker.events = [row("terminal:seed", iso(-10))]
        self.run_once()
        self.broker.fail_receipts = True
        self.broker.events.append(row("terminal:new", iso(5)))
        summary = self.run_once()
        self.assertEqual((summary["sent"], summary["receiptErrors"]), (1, 1))
        self.assertEqual(self.run_once()["sent"], 0)

    def test_broker_unavailable_exits_nonzero_and_keeps_state(self):
        self.broker.events = [row("terminal:seed", iso(-10))]
        self.run_once()
        before = self.state.read_text()
        self.broker.close()
        env = {**self.env}
        old = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(env)
            self.assertEqual(notify.main([]), 1)
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(self.state.read_text(), before)
        self.broker = FakeBroker()  # tearDown closes a live server

    def test_edge_secret_comes_from_the_broker_env_file_and_a_loose_secret_file_is_refused(self):
        env_file = Path(self.tmp.name) / "broker.env"
        env_file.write_text("FOO=1\nexport A2A_EDGE_SECRET='from-env-file'\n")
        self.assertEqual(notify.read_edge_secret({"A2A_TB_NOTIFY_BROKER_ENV_FILE": str(env_file)}), "from-env-file")
        loose = Path(self.tmp.name) / "loose"
        loose.write_text("x")
        os.chmod(loose, 0o644)
        with self.assertRaises(notify.ConfigError):
            notify.read_edge_secret({"A2A_TB_NOTIFY_EDGE_SECRET_FILE": str(loose)})


if __name__ == "__main__":
    unittest.main()
