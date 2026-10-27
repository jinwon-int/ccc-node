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
import fleet_alerts_policy as policy  # noqa: E402
import fleet_alerts_receiver as rx  # noqa: E402
import fleet_alerts_receipt as rcpt  # noqa: E402
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
        self.assertEqual(outbox.pending(self.queue)[0], txn)
        self.assertRegex(outbox.pending(self.queue)[1], r"^\[에이전트A\] #[0-9a-z]{6} 🔔 formatted$")
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
        self.assertEqual(outbox.pending(self.queue)[0], txn)
        self.assertRegex(outbox.pending(self.queue)[1], r"^\[node-a\] #[0-9a-z]{6} 🔔 formatted$")


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
        self.assertRegex(outbox.pending(self.queue)[1], r"^\[node-a\] #[0-9a-z]{6} 🔔 formatted$")
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
        t1, _ = outbox.enqueue(self.queue, "first", tier="action")
        t2, _ = outbox.enqueue(self.queue, "second", tier="action")
        fake = _FakeTransport()

        async def run() -> list[bool]:
            return [await tx.deliver_one(self.queue, fake, "!room:hs", msgtype="m.notice") for _ in range(3)]

        self.assertEqual(asyncio.run(run()), [True, True, False])
        self.assertEqual([s[2] for s in fake.sent], [t1, t2])
        self.assertEqual(fake.sent[0][:2] + (fake.sent[0][3],), ("!room:hs", "first", "m.notice"))
        self.assertEqual(fake.pinned, 2)
        self.assertEqual(outbox.pending_count(self.queue), 0)

    def test_gate_failure_raises_and_keeps_the_alert_pending(self) -> None:
        txn, _ = outbox.enqueue(self.queue, "keep me", tier="action")
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


class ContractV2Test(_Base):
    """#2413 stage 1: alert id + tier on every row, per-channel receipts, v1 still accepted."""

    def test_enqueue_assigns_unique_6char_alert_id_and_tier(self) -> None:
        t1, _ = outbox.enqueue(self.queue, "a", tier="action")
        t2, _ = outbox.enqueue(self.queue, "b", tier="bogus")
        a1, a2 = outbox.alert_id_of(self.queue, t1), outbox.alert_id_of(self.queue, t2)
        self.assertRegex(a1, r"^[0-9a-z]{6}$")
        self.assertNotEqual(a1, a2)
        self.assertEqual(outbox.lookup(self.queue, a1)["tier"], "action")
        self.assertEqual(outbox.lookup(self.queue, a2)["tier"], "info")  # unknown tier → info
        self.assertIsNone(outbox.alert_id_of(self.queue, "fleet-missing"))

    def test_body_with_id_puts_the_id_into_the_stored_text(self) -> None:
        txn, _ = outbox.enqueue(self.queue, "x", body_with_id=lambda i: f"[n] #{i} x")
        aid = outbox.alert_id_of(self.queue, txn)
        self.assertEqual(outbox.pending(self.queue), (txn, f"[n] #{aid} x"))
        with self.assertRaises(ValueError):
            outbox.enqueue(self.queue, "x", body_with_id=lambda i: "   ")

    def test_matrix_delivery_writes_a_strong_receipt_and_lookup_reports_notified(self) -> None:
        txn, _ = outbox.enqueue(self.queue, "x", node="node-a", event="AgentCronRun", tier="action")
        aid = outbox.alert_id_of(self.queue, txn)
        self.assertFalse(outbox.lookup(self.queue, aid)["notified"])
        outbox.delivered(self.queue, txn, "$evt")
        info = outbox.lookup(self.queue, aid)
        self.assertEqual([(r["channel"], r["kind"], r["ref"]) for r in info["receipts"]], [("matrix", "matrix-event", "$evt")])
        self.assertTrue(info["notified"])
        self.assertEqual((info["node"], info["event"], info["tier"], info["txn"]), ("node-a", "AgentCronRun", "action", txn))
        self.assertNotIn("body", info)
        self.assertEqual(outbox.lookup(self.queue, txn)["alert_id"], aid)  # txn also resolves
        self.assertIsNone(outbox.lookup(self.queue, "zzzzzz"))

    def test_weak_receipt_alone_is_not_notified_and_one_receipt_per_channel(self) -> None:
        txn, _ = outbox.enqueue(self.queue, "x")
        outbox.receipt(self.queue, txn, "hande", "socket-write")
        info = outbox.lookup(self.queue, txn)
        self.assertEqual(len(info["receipts"]), 1)
        self.assertFalse(info["notified"])
        outbox.receipt(self.queue, txn, "hande", "hande-ack", "ev-1")  # later ack replaces the weak one
        info = outbox.lookup(self.queue, txn)
        self.assertEqual([(r["kind"], r["ref"]) for r in info["receipts"]], [("hande-ack", "ev-1")])
        self.assertTrue(info["notified"])
        with self.assertRaises(ValueError):
            outbox.receipt(self.queue, txn, "", "x")

    def test_prune_drops_receipts_with_their_alert(self) -> None:
        txn, _ = outbox.enqueue(self.queue, "x")
        outbox.delivered(self.queue, txn, "$e")
        self.assertEqual(outbox.prune(self.queue, keep_seconds=-1), 1)
        with outbox.database(self.queue) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)

    def test_pre_v2_queue_is_migrated_in_place(self) -> None:
        q = self.root / "old"
        q.mkdir(mode=0o700)
        import sqlite3
        conn = sqlite3.connect(q / "queue.sqlite3")
        conn.execute("""CREATE TABLE alerts (txn TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL,
            fingerprint TEXT, node TEXT NOT NULL DEFAULT '', event TEXT NOT NULL DEFAULT '', delivered REAL,
            event_id TEXT, attempts INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("INSERT INTO alerts(txn,body,created) VALUES ('fleet-old','legacy',1.0)")
        conn.commit()
        conn.close()
        os.chmod(q / "queue.sqlite3", 0o600)
        self.assertEqual(outbox.pending(q), ("fleet-old", "legacy"))  # opening migrates
        info = outbox.lookup(q, "fleet-old")
        self.assertEqual((info["alert_id"], info["tier"], info["receipts"]), (None, "info", []))
        txn, _ = outbox.enqueue(q, "new")
        self.assertRegex(outbox.alert_id_of(q, txn), r"^[0-9a-z]{6}$")

    def test_receiver_accepts_v1_and_v2_prefixes_id_and_returns_tier(self) -> None:
        receiver = rx.Receiver(secrets_dir=self.secrets, queue_dir=self.queue)
        for schema, tier, want in ((R.SCHEMA_V1, None, "info"), (R.SCHEMA, "critical", "critical"), (R.SCHEMA, "weird", "info")):
            payload = _payload(record=f"r-{schema}-{tier}.json")
            payload["schema"] = schema
            if tier is None:
                payload.pop("tier", None)
            else:
                payload["tier"] = tier
            headers, body = _signed("ka", "node-a", payload, int(time.time()))
            status, obj = receiver.handle("POST", rx.ALERTS_PATH, headers, body)
            self.assertEqual(status, 202, obj)
            self.assertEqual(obj["tier"], want)
            self.assertRegex(obj["alertId"], r"^[0-9a-z]{6}$")
            self.assertEqual(outbox.lookup(self.queue, obj["alertId"])["tier"], want)
        texts = []
        while (item := outbox.pending(self.queue)) is not None:
            texts.append(item[1])
            outbox.delivered(self.queue, item[0], "$x")
        self.assertEqual(len(texts), 3)
        for t in texts:
            self.assertRegex(t, r"^\[node-a\] #[0-9a-z]{6} 🔔 formatted$")
        payload = _payload(record="r-bad.json")
        payload["schema"] = "ccc.fleet-alert.v9"
        headers, body = _signed("ka", "node-a", payload, int(time.time()))
        self.assertEqual(receiver.handle("POST", rx.ALERTS_PATH, headers, body)[0], 400)

    def test_duplicate_retry_keeps_the_first_alert_id(self) -> None:
        receiver = rx.Receiver(secrets_dir=self.secrets, queue_dir=self.queue)
        payload = _payload(record="same.json")
        headers, body = _signed("ka", "node-a", payload, int(time.time()))
        s1, o1 = receiver.handle("POST", rx.ALERTS_PATH, headers, body)
        s2, o2 = receiver.handle("POST", rx.ALERTS_PATH, headers, body)
        self.assertEqual((s1, s2, o1["duplicate"], o2["duplicate"]), (202, 202, False, True))
        self.assertEqual(o1["alertId"], o2["alertId"])

    def test_client_payload_carries_tier_from_the_spool_record(self) -> None:
        self.assertEqual(R.build_payload("n", "r", {"text": "t"}, "t")["tier"], "info")
        self.assertEqual(R.build_payload("n", "r", {"text": "t", "tier": "ACTION"}, "t")["tier"], "action")
        p = R.build_payload("n", "r", {"text": "t", "tier": "critical"}, "t")
        self.assertEqual((p["schema"], p["raw"]["tier"]), (R.SCHEMA, "critical"))

    def test_receipt_cli_prints_metadata_and_exits_3_when_unknown(self) -> None:
        import contextlib
        import io
        txn, _ = outbox.enqueue(self.queue, "secret body text", tier="action")
        aid = outbox.alert_id_of(self.queue, txn)
        outbox.delivered(self.queue, txn, "$ev")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(rcpt.main(["--queue", str(self.queue), f"#{aid}"]), 0)
        out = json.loads(buf.getvalue())
        self.assertEqual((out["alert_id"], out["notified"], out["tier"]), (aid, True, "action"))
        self.assertNotIn("secret body", buf.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rcpt.main(["--queue", str(self.queue), "nope00"]), 3)


def _kst(y, m, d, hh, mm=0) -> float:
    import datetime as dt
    return dt.datetime(y, m, d, hh, mm, tzinfo=policy.KST).timestamp()


class PolicyTest(_Base):
    """#2413 stage 2: info digest at 09:00 KST, action/critical re-notify once after 15 min."""

    def test_info_is_held_action_and_critical_leave_at_once(self) -> None:
        outbox.enqueue(self.queue, "quiet", tier="info")
        self.assertIsNone(policy.pending_deliverable(self.queue))
        ta, _ = outbox.enqueue(self.queue, "look", tier="action")
        self.assertEqual(policy.pending_deliverable(self.queue)[0], ta)
        fake = _FakeTransport()
        # today's digest already made → the held info row must not leave on its own
        policy.meta_set(self.queue, policy.META_DIGEST_DAY, policy.kst_day(time.time()))
        self.assertTrue(asyncio.run(tx.deliver_one(self.queue, fake, "!r")))
        self.assertFalse(asyncio.run(tx.deliver_one(self.queue, fake, "!r")))  # info still held
        self.assertEqual(outbox.pending_count(self.queue), 1)
        self.assertEqual([t[2] for t in fake.sent], [ta])

    def test_digest_due_window_and_once_per_kst_day(self) -> None:
        self.assertFalse(policy.digest_due(_kst(2026, 10, 27, 8, 59), None))
        self.assertTrue(policy.digest_due(_kst(2026, 10, 27, 9, 0), None))
        self.assertTrue(policy.digest_due(_kst(2026, 10, 27, 23, 30), "2026-10-26"))
        self.assertFalse(policy.digest_due(_kst(2026, 10, 27, 12, 0), "2026-10-27"))

    def test_make_digest_bundles_held_info_rows_and_marks_them_digested(self) -> None:
        now = _kst(2026, 10, 27, 9, 5)
        t1, _ = outbox.enqueue(self.queue, "x", node="node-a", body_with_id=lambda i: f"[A] #{i} self-update ok\nmore")
        t2, _ = outbox.enqueue(self.queue, "y", node="node-b", body_with_id=lambda i: f"[B] #{i} startup banner")
        ta, _ = outbox.enqueue(self.queue, "act", tier="action")
        dtxn = policy.make_digest(self.queue, now)
        self.assertIsNotNone(dtxn)
        info = outbox.lookup(self.queue, dtxn)
        self.assertEqual((info["tier"], info["event"], info["node"]), ("digest", "FleetAlertDigest", "relay"))
        body = policy.pending_deliverable(self.queue)
        # action row is older → first; the digest row follows
        self.assertEqual(body[0], ta)
        with outbox.database(self.queue) as conn:
            dbody = conn.execute("SELECT body FROM alerts WHERE txn=?", (dtxn,)).fetchone()[0]
        self.assertRegex(dbody, r"^#[0-9a-z]{6} 📋 플릿 알림 다이제스트 2026-10-27 — info 2건\n• \d\d:\d\d \[A\] #[0-9a-z]{6} self-update ok\n• ")
        self.assertNotIn("more", dbody)  # first line only
        for t in (t1, t2):
            i = outbox.lookup(self.queue, t)
            self.assertIsNotNone(i["matrix_delivered"])
            self.assertEqual([(r["channel"], r["kind"], r["ref"]) for r in i["receipts"]], [("digest", "digested", info["alert_id"])])
        self.assertEqual(policy.meta_get(self.queue, policy.META_DIGEST_DAY), "2026-10-27")
        self.assertIsNone(policy.make_digest(self.queue, now))  # nothing held now
        with outbox.database(self.queue) as conn:
            self.assertEqual(conn.execute("SELECT body FROM alerts WHERE txn=?", (t1,)).fetchone()[0], "")

    def test_tick_makes_digest_only_when_due_and_only_once(self) -> None:
        outbox.enqueue(self.queue, "x", tier="info")
        self.assertIsNone(policy.tick(self.queue, _kst(2026, 10, 27, 8, 0))["digest"])
        d = policy.tick(self.queue, _kst(2026, 10, 27, 9, 1))["digest"]
        self.assertIsNotNone(d)
        outbox.enqueue(self.queue, "later", tier="info")
        self.assertIsNone(policy.tick(self.queue, _kst(2026, 10, 27, 15, 0))["digest"])  # waits for tomorrow
        self.assertIsNotNone(policy.tick(self.queue, _kst(2026, 10, 28, 9, 0))["digest"])

    def test_renotify_once_after_15_minutes_unless_replied(self) -> None:
        t0 = _kst(2026, 10, 27, 10, 0)
        ta, _ = outbox.enqueue(self.queue, "act", node="node-a", tier="action")
        tc, _ = outbox.enqueue(self.queue, "crit", node="node-b", tier="critical")
        tr, _ = outbox.enqueue(self.queue, "replied", node="node-c", tier="action")
        for t in (ta, tc, tr):
            outbox.delivered(self.queue, t, "$" + t)
        with outbox.database(self.queue) as conn:
            conn.execute("UPDATE alerts SET delivered=? WHERE txn IN (?,?,?)", (t0, ta, tc, tr))
        outbox.receipt(self.queue, tr, "reply", "reply", "ok")
        self.assertEqual(policy.make_renotify(self.queue, t0 + 14 * 60), [])
        made = policy.make_renotify(self.queue, t0 + 16 * 60)
        self.assertEqual(len(made), 2)
        bodies = []
        for m in made:
            with outbox.database(self.queue) as conn:
                bodies.append(conn.execute("SELECT body, tier FROM alerts WHERE txn=?", (m,)).fetchone())
        self.assertTrue(all(tier == "renotify" for _, tier in bodies))
        aid_a, aid_c = outbox.alert_id_of(self.queue, ta), outbox.alert_id_of(self.queue, tc)
        self.assertTrue(any(f"⏰ 재통지 #{aid_a} (action, 16분 전" in b for b, _ in bodies))
        self.assertTrue(any(f"🚨 재통지 #{aid_c} (critical" in b for b, _ in bodies))
        self.assertTrue(any(f"`ok {aid_a}` 또는 `hold {aid_a}`" in b for b, _ in bodies))
        self.assertEqual(policy.make_renotify(self.queue, t0 + 60 * 60), [])  # only once
        self.assertEqual([r["kind"] for r in outbox.lookup(self.queue, ta)["receipts"]], ["matrix-event", "renotify"])
        self.assertEqual([r["kind"] for r in outbox.lookup(self.queue, tr)["receipts"]], ["matrix-event", "reply"])
        # derived rows leave at once and are strong-receipted like any other row
        self.assertEqual(policy.pending_deliverable(self.queue)[0], made[0])

    def test_payload_cannot_inject_derived_tiers(self) -> None:
        self.assertEqual(R.normalize_tier("digest"), "info")
        self.assertEqual(outbox.normalize_tier("renotify"), "info")
        txn, _ = outbox.enqueue(self.queue, "x", tier="digest")  # internal callers only
        self.assertEqual(outbox.lookup(self.queue, txn)["tier"], "digest")

    def test_tick_never_raises(self) -> None:
        out = policy.tick(Path("relative/not/allowed"), _kst(2026, 10, 27, 9, 0))
        self.assertEqual(out, {"digest": None, "renotify": 0})


if __name__ == "__main__":
    unittest.main()
