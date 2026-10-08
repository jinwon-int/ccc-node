"""Fleet alert bot (#2182): queue hygiene, receiver admission, HTTP round trip with the bridge client, sender loop.

Hermetic: a local ThreadingHTTPServer on 127.0.0.1 and a temp queue; no Matrix,
no network. Runs under ``python3 scripts/fleet-alerts/fleet_alerts_test.py``
(wired through fleet-alerts.test.sh) with ``PYTHONPATH=.github/pythonpath``.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_alerts_outbox as outbox  # noqa: E402
import fleet_alerts_receiver as rx  # noqa: E402
import fleet_alerts_sender as tx  # noqa: E402
from telegram_bot.core import fleet_alert_relay as R  # noqa: E402


def _secret_file(d: Path, node: str, value: str = "k") -> Path:
    f = d / node
    f.write_text(value, encoding="utf-8")
    os.chmod(f, 0o600)
    return f


def _signed(secret: str, node: str, payload: dict, ts: int) -> tuple[dict, bytes]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return (
        {R.NODE_HEADER: node, R.TIMESTAMP_HEADER: str(ts), R.SIGNATURE_HEADER: R.sign(secret, ts, body)},
        body,
    )


def _payload(node: str = "node-a", **over) -> dict:
    p = R.build_payload(node, "r1.json", {"event": "SelfUpdate", "text": "x"}, "🔔 formatted")
    p.update(over)
    return p


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        os.chmod(self.root, 0o700)
        self.secrets = self.root / "nodes"
        self.secrets.mkdir(mode=0o700)
        self.queue = self.root / "queue"
        _secret_file(self.secrets, "node-a", "ka")
        _secret_file(self.secrets, "node-b", "kb")

    def tearDown(self) -> None:
        self._tmp.cleanup()


class OutboxTest(_Base):
    def test_enqueue_pending_attempted_delivered_and_body_wiped(self) -> None:
        txn, dup = outbox.enqueue(self.queue, "hello", node="node-a", event="E", fingerprint=outbox.fingerprint("node-a", "r1"))
        self.assertFalse(dup)
        self.assertEqual(outbox.pending(self.queue), (txn, "hello"))
        self.assertEqual(outbox.pending_count(self.queue), 1)
        outbox.attempted(self.queue, txn)
        outbox.delivered(self.queue, txn, "$evt")
        self.assertIsNone(outbox.pending(self.queue))
        with outbox.database(self.queue) as conn:
            row = conn.execute("SELECT body,event_id,attempts,node,event FROM alerts WHERE txn=?", (txn,)).fetchone()
        self.assertEqual(row, ("", "$evt", 1, "node-a", "E"))

    def test_duplicate_fingerprint_inside_window_returns_existing_txn(self) -> None:
        fp = outbox.fingerprint("node-a", "r1.json")
        txn1, dup1 = outbox.enqueue(self.queue, "a", fingerprint=fp)
        txn2, dup2 = outbox.enqueue(self.queue, "a again", fingerprint=fp)
        self.assertEqual((dup1, dup2, txn1), (False, True, txn2))
        self.assertEqual(outbox.pending_count(self.queue), 1)
        txn3, dup3 = outbox.enqueue(self.queue, "later", fingerprint=fp, window=0)
        self.assertFalse(dup3)
        self.assertNotEqual(txn3, txn1)
        self.assertNotEqual(outbox.fingerprint("node-b", "r1.json"), fp)

    def test_delivered_requires_matrix_event_id_and_rejects_bad_input(self) -> None:
        txn, _ = outbox.enqueue(self.queue, "x")
        with self.assertRaises(ValueError):
            outbox.delivered(self.queue, txn, "nope")
        with self.assertRaises(ValueError):
            outbox.enqueue(self.queue, "   ")
        with self.assertRaises(ValueError):
            outbox.enqueue(self.queue, "x", fingerprint="short")
        with self.assertRaises(ValueError):
            outbox.enqueue(self.queue, "y" * (outbox.MAX_BODY_BYTES + 1))

    def test_queue_directory_must_be_private(self) -> None:
        q = self.root / "loose"
        q.mkdir()
        os.chmod(q, 0o755)  # explicit: the suite may run under umask 077
        with self.assertRaises(ValueError):
            outbox.pending(q)
        with self.assertRaises(ValueError):
            outbox.pending(Path("relative/queue"))

    def test_prune_removes_only_old_delivered_rows(self) -> None:
        txn, _ = outbox.enqueue(self.queue, "x")
        outbox.delivered(self.queue, txn, "$e")
        outbox.enqueue(self.queue, "pending")
        self.assertEqual(outbox.prune(self.queue, keep_seconds=3600), 0)
        self.assertEqual(outbox.prune(self.queue, keep_seconds=-1), 1)
        self.assertEqual(outbox.pending_count(self.queue), 1)


class AdmissionTest(_Base):
    def check(self, headers: dict, body: bytes, now: float | None = None, **kw) -> rx.Decision:
        return rx.check_request(headers, body, secrets_dir=self.secrets, now=now if now is not None else 1_700_000_000, **kw)

    def test_valid_request_is_accepted_with_payload(self) -> None:
        h, b = _signed("ka", "node-a", _payload(), 1_700_000_000)
        d = self.check(h, b)
        self.assertEqual((d.status, d.reason, d.node), (202, "ok", "node-a"))
        self.assertEqual(d.payload["record"], "r1.json")

    def test_unknown_or_unsafe_node_is_403(self) -> None:
        h, b = _signed("kc", "node-c", _payload("node-c"), 1_700_000_000)
        self.assertEqual(self.check(h, b).status, 403)
        h, b = _signed("ka", "Bad Node", _payload(), 1_700_000_000)
        self.assertEqual(self.check(h, b).status, 403)
        os.chmod(self.secrets / "node-a", 0o640)  # group-readable secret → node unknown
        h, b = _signed("ka", "node-a", _payload(), 1_700_000_000)
        self.assertEqual(self.check(h, b).reason, "unknown-node")

    def test_signature_timestamp_and_header_failures_are_401(self) -> None:
        h, b = _signed("kb", "node-a", _payload(), 1_700_000_000)  # wrong secret
        self.assertEqual(self.check(h, b).reason, "bad-signature")
        h, b = _signed("ka", "node-a", _payload(), 1_700_000_000 - 301)
        self.assertEqual(self.check(h, b).reason, "stale-timestamp")
        h, b = _signed("ka", "node-a", _payload(), 1_700_000_000)
        self.assertEqual(self.check(h, b + b" ").reason, "bad-signature")  # body tamper
        h[R.TIMESTAMP_HEADER] = "soon"
        self.assertEqual(self.check(h, b).reason, "bad-timestamp")
        self.assertEqual(self.check({}, b).reason, "missing-headers")

    def test_payload_failures_are_400_and_oversize_413(self) -> None:
        ts = 1_700_000_000
        h, b = _signed("ka", "node-a", _payload(node="node-b"), ts)
        self.assertEqual(self.check(h, b).reason, "node-mismatch")
        h, b = _signed("ka", "node-a", _payload(schema="other"), ts)
        self.assertEqual(self.check(h, b).reason, "bad-schema")
        h, b = _signed("ka", "node-a", _payload(text="  "), ts)
        self.assertEqual(self.check(h, b).reason, "bad-text")
        body = b"not json"
        h = {R.NODE_HEADER: "node-a", R.TIMESTAMP_HEADER: str(ts), R.SIGNATURE_HEADER: R.sign("ka", ts, body)}
        self.assertEqual(self.check(h, body).reason, "bad-json")
        self.assertEqual(self.check(h, b"x" * 70_000).status, 413)


class NodeLabelTest(_Base):
    def test_label_prefix_from_file_with_fallback_and_reload(self) -> None:
        f = self.root / "labels.json"
        f.write_text(json.dumps({"node-a": "에이전트A", "node-c": ""}), encoding="utf-8")
        labels = rx.NodeLabels(f)
        self.assertEqual(labels.label("node-a"), "에이전트A")
        self.assertEqual(labels.label("node-b"), "node-b")  # unknown → id
        self.assertEqual(labels.label("node-c"), "node-c")  # empty name → id
        txn, _ = rx.queue_payload(self.queue, _payload(), labels)
        self.assertEqual(outbox.pending(self.queue), (txn, "[에이전트A] 🔔 formatted"))
        f.write_text(json.dumps({"node-a": "새이름"}), encoding="utf-8")
        os.utime(f, (time.time() + 5, time.time() + 5))  # force a different mtime
        self.assertEqual(labels.label("node-a"), "새이름")

    def test_missing_or_invalid_label_file_shows_node_id(self) -> None:
        self.assertEqual(rx.NodeLabels(self.root / "nope.json").label("node-a"), "node-a")
        self.assertEqual(rx.NodeLabels(None).label("node-a"), "node-a")
        bad = self.root / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        self.assertEqual(rx.NodeLabels(bad).label("node-a"), "node-a")
        txn, _ = rx.queue_payload(self.queue, _payload(), None)
        self.assertEqual(outbox.pending(self.queue), (txn, "[node-a] 🔔 formatted"))


class HttpRoundTripTest(_Base):
    """The bridge's FleetAlertRelay client (PR 1) against this receiver, end to end."""

    def setUp(self) -> None:
        super().setUp()
        self.receiver = rx.Receiver(secrets_dir=self.secrets, queue_dir=self.queue)
        self.httpd = rx.serve(self.receiver, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}{rx.ALERTS_PATH}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def test_client_post_lands_in_queue_and_retry_is_deduplicated(self) -> None:
        relay = R.FleetAlertRelay(self.url, "ka", "node-a", timeout=5)
        payload = _payload()
        relay.post(payload)
        relay.post(payload)  # lost-2xx retry
        self.assertEqual(outbox.pending_count(self.queue), 1)
        self.assertEqual(outbox.pending(self.queue)[1], "[node-a] 🔔 formatted")
        relay.post(R.build_payload("node-a", "r2.json", {"event": "x", "text": "t"}, "second"))
        self.assertEqual(outbox.pending_count(self.queue), 2)

    def test_wrong_secret_is_rejected_for_good_and_unknown_node_too(self) -> None:
        with self.assertRaises(R.RelayRejected):
            R.FleetAlertRelay(self.url, "wrong", "node-a", timeout=5).post(_payload())
        with self.assertRaises(R.RelayRejected):
            R.FleetAlertRelay(self.url, "kc", "node-c", timeout=5).post(_payload("node-c"))
        self.assertEqual(outbox.pending_count(self.queue), 0)

    def test_health_and_unknown_paths(self) -> None:
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{self.httpd.server_port}{rx.HEALTH_PATH}", timeout=5) as r:
            self.assertEqual(json.loads(r.read()), {"ok": True})
        self.assertEqual(self.receiver.handle("GET", rx.ALERTS_PATH, {}, b"")[0], 404)
        self.assertEqual(self.receiver.handle("POST", "/v1/other", {}, b"")[0], 404)

    def test_unwritable_queue_is_503_so_the_node_keeps_the_record(self) -> None:
        bad = rx.Receiver(secrets_dir=self.secrets, queue_dir=self.root / "no-such" / "deep")
        h, b = _signed("ka", "node-a", _payload(), int(time.time()))
        status, obj = bad.handle("POST", rx.ALERTS_PATH, h, b)
        self.assertEqual((status, obj["error"]), (503, "queue-unavailable"))


class _FakeTransport:
    def __init__(self, fail_gate: bool = False) -> None:
        self.matrix_lock = asyncio.Lock()
        self.sent: list[tuple[str, str, str, str]] = []
        self.pinned = 0
        self.fail_gate = fail_gate

    async def pin_devices(self) -> None:
        self.pinned += 1

    async def room_gate(self, room: str) -> bool:
        return not self.fail_gate

    async def encrypted_send(self, room: str, text: str, txn: str, *, msgtype: str = "m.text") -> str:
        self.sent.append((room, text, txn, msgtype))
        return "$" + txn


class SenderLoopTest(_Base):
    def test_deliver_one_sends_oldest_marks_delivered_and_stops_on_empty(self) -> None:
        t1, _ = outbox.enqueue(self.queue, "first")
        t2, _ = outbox.enqueue(self.queue, "second")
        fake = _FakeTransport()

        async def run() -> list[bool]:
            return [await tx.deliver_one(self.queue, fake, "!room:hs", msgtype="m.notice") for _ in range(3)]

        self.assertEqual(asyncio.run(run()), [True, True, False])
        self.assertEqual([s[2] for s in fake.sent], [t1, t2])
        self.assertEqual(fake.sent[0][:2] + (fake.sent[0][3],), ("!room:hs", "first", "m.notice"))
        self.assertEqual(fake.pinned, 2)
        self.assertEqual(outbox.pending_count(self.queue), 0)

    def test_gate_failure_raises_and_keeps_the_alert_pending(self) -> None:
        txn, _ = outbox.enqueue(self.queue, "keep me")
        fake = _FakeTransport(fail_gate=True)
        with self.assertRaises(RuntimeError):
            asyncio.run(tx.deliver_one(self.queue, fake, "!room:hs"))
        self.assertEqual(outbox.pending(self.queue), (txn, "keep me"))
        with outbox.database(self.queue) as conn:
            self.assertEqual(conn.execute("SELECT attempts FROM alerts WHERE txn=?", (txn,)).fetchone()[0], 1)

    def test_config_requires_exactly_one_room_and_no_family(self) -> None:
        tx.validate_config({"rooms": ["!a:hs"]})
        with self.assertRaises(ValueError):
            tx.validate_config({"rooms": []})
        with self.assertRaises(ValueError):
            tx.validate_config({"rooms": ["!a:hs", "!b:hs"]})
        with self.assertRaises(ValueError):
            tx.validate_config({"rooms": ["!a:hs"], "family": {"rooms": ["!f:hs"]}})


if __name__ == "__main__":
    unittest.main()
