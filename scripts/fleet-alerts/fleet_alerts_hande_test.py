"""「한데」 lane of the fleet alert bot (#2413): mirror trigger, frames, pacing, socket loop.

Hermetic: temp queue + ``socket.socketpair`` standing in for the relay bot. No
network, no relay, no Matrix. Wired through fleet-alerts.test.sh.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_alerts_outbox as outbox  # noqa: E402
import fleet_alerts_sender as tx  # noqa: E402
import fleet_alerts_sender_hande as hande  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        os.chmod(self.root, 0o700)
        self.queue = self.root / "queue"
        hande.install(self.queue)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mirror_rows(self) -> list[tuple[str, str]]:
        with outbox.database(self.queue) as conn:
            return conn.execute("SELECT txn, body FROM hande_alerts ORDER BY id").fetchall()


class _FakeMatrix:
    def __init__(self) -> None:
        self.matrix_lock = asyncio.Lock()
        self.sent: list[str] = []

    async def pin_devices(self) -> None:
        pass

    async def room_gate(self, room: str) -> bool:
        return True

    async def encrypted_send(self, room: str, text: str, txn: str, *, msgtype: str = "m.text") -> str:
        self.sent.append(txn)
        return "$" + txn


class MirrorTest(_Base):
    def test_install_is_idempotent_and_mirrors_every_enqueue(self) -> None:
        hande.install(self.queue)
        self.assertTrue(hande.installed(self.queue))
        t1, _ = outbox.enqueue(self.queue, "first", node="node-a", event="AgentCronRun", tier="action")
        t2, _ = outbox.enqueue(self.queue, "second", node="node-b", event="SelfUpdate", tier="critical")
        self.assertEqual(self._mirror_rows(), [(t1, "first"), (t2, "second")])
        self.assertEqual(hande.backlog(self.queue), 2)

    def test_info_rows_are_not_mirrored_but_the_digest_row_is(self) -> None:
        import fleet_alerts_policy as policy
        outbox.enqueue(self.queue, "quiet info", tier="info")
        self.assertEqual(hande.backlog(self.queue), 0)
        import datetime as dt
        now = dt.datetime(2026, 10, 27, 9, 0, tzinfo=policy.KST).timestamp()
        dtxn = policy.make_digest(self.queue, now)
        self.assertEqual([txn for txn, _ in self._mirror_rows()], [dtxn])

    def test_notice_rows_are_mirrored_at_once(self) -> None:
        # #2628: the trigger copies every non-info insert, so a notice reaches 「한데」 immediately.
        txn, _ = outbox.enqueue(self.queue, "lotto notice", tier="notice")
        self.assertEqual([t for t, _ in self._mirror_rows()], [txn])

    def test_install_replaces_an_older_when_less_trigger(self) -> None:
        with outbox.database(self.queue) as conn:
            conn.execute("DROP TRIGGER IF EXISTS hande_mirror")
            conn.execute("CREATE TRIGGER hande_mirror AFTER INSERT ON alerts BEGIN INSERT INTO hande_alerts(txn, body, created) VALUES (NEW.txn, NEW.body, NEW.created); END")
        hande.install(self.queue)
        outbox.enqueue(self.queue, "info after reinstall", tier="info")
        self.assertEqual(hande.backlog(self.queue), 0)

    def test_dedup_duplicate_is_not_mirrored_twice(self) -> None:
        fp = outbox.fingerprint("node-a", "r1.json")
        outbox.enqueue(self.queue, "a", fingerprint=fp, tier="action")
        outbox.enqueue(self.queue, "a again", fingerprint=fp, tier="action")
        self.assertEqual(hande.backlog(self.queue), 1)

    def test_matrix_delivery_wipes_outbox_body_but_mirror_keeps_it(self) -> None:
        txn, _ = outbox.enqueue(self.queue, "keep for hande", tier="action")
        fake = _FakeMatrix()
        self.assertTrue(asyncio.run(tx.deliver_one(self.queue, fake, "!room:hs")))
        self.assertEqual(fake.sent, [txn])
        with outbox.database(self.queue) as conn:
            self.assertEqual(conn.execute("SELECT body FROM alerts WHERE txn=?", (txn,)).fetchone()[0], "")
        self.assertEqual(hande.next_alert(self.queue)[1:], (txn, "keep for hande"))

    def test_next_alert_is_oldest_forget_removes_and_expired_rows_are_dropped(self) -> None:
        t1, _ = outbox.enqueue(self.queue, "old", tier="action")
        t2, _ = outbox.enqueue(self.queue, "new", tier="action")
        with outbox.database(self.queue) as conn:
            conn.execute("UPDATE hande_alerts SET created=created-? WHERE txn=?", (hande.MAX_AGE_SECONDS + 5, t1))
        item = hande.next_alert(self.queue)
        self.assertEqual(item[1:], (t2, "new"))
        self.assertEqual(hande.backlog(self.queue), 1)
        hande.forget(self.queue, item[0])
        self.assertIsNone(hande.next_alert(self.queue))

    def test_trigger_prunes_mirror_rows_older_than_24h_on_insert(self) -> None:
        t1, _ = outbox.enqueue(self.queue, "stale", tier="action")
        with outbox.database(self.queue) as conn:
            conn.execute("UPDATE hande_alerts SET created=created-? WHERE txn=?", (hande.MAX_AGE_SECONDS + 5, t1))
        outbox.enqueue(self.queue, "fresh", tier="action")
        self.assertEqual([b for _, b in self._mirror_rows()], ["fresh"])

    def test_uninstall_drops_trigger_and_table_and_matrix_sender_is_unaffected(self) -> None:
        hande.uninstall(self.queue)
        self.assertFalse(hande.installed(self.queue))
        txn, _ = outbox.enqueue(self.queue, "matrix only", tier="action")
        with outbox.database(self.queue) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        self.assertNotIn("hande_alerts", names)
        self.assertNotIn("hande_mirror", names)
        fake = _FakeMatrix()
        self.assertTrue(asyncio.run(tx.deliver_one(self.queue, fake, "!room:hs")))
        self.assertEqual(fake.sent, [txn])

    def test_outbox_prune_of_delivered_rows_leaves_mirror_alone(self) -> None:
        txn, _ = outbox.enqueue(self.queue, "x", tier="action")
        outbox.delivered(self.queue, txn, "$e")
        self.assertEqual(outbox.prune(self.queue, keep_seconds=-1), 1)
        self.assertEqual(hande.backlog(self.queue), 1)


class FrameTest(unittest.TestCase):
    def test_turn_gets_empty_complete_result_everything_else_is_ignored(self) -> None:
        reply = hande.answer(json.dumps({"type": "turn", "turn_id": "t-1", "text": "hello?"}))
        self.assertEqual(json.loads(reply), {"type": "result", "turn_id": "t-1", "status": "complete", "text": ""})
        self.assertTrue(reply.endswith("\n"))
        for line in (
            json.dumps({"type": "approve", "turn_id": "t-1"}),
            json.dumps({"type": "tool-result", "turn_id": "t-1"}),
            json.dumps({"type": "turn", "turn_id": "bad id with spaces"}),
            json.dumps({"type": "turn"}),
            json.dumps(["turn"]),
            "not json",
        ):
            self.assertIsNone(hande.answer(line), line)

    def test_notice_frame_is_one_json_line_with_room_and_bounded_text(self) -> None:
        frame = hande.notice_frame("fleet-alerts", "  🔔 [node] alert body  ")
        obj = json.loads(frame)
        self.assertEqual(obj, {"type": "notice", "room": "fleet-alerts", "text": "🔔 [node] alert body"})
        self.assertTrue(frame.endswith("\n") and frame.count("\n") == 1)
        long = "한" * 5000  # 15000 bytes
        text = json.loads(hande.notice_frame("r", long))["text"]
        self.assertLessEqual(len(text.encode("utf-8")), hande.MAX_TEXT_BYTES)
        self.assertTrue(text.endswith(hande.TRUNCATION_MARKER))

    def test_notice_frame_carries_a_valid_id_when_given(self) -> None:
        obj = json.loads(hande.notice_frame("fleet-alerts", "body", "fleet-abc"))
        self.assertEqual(obj, {"type": "notice", "room": "fleet-alerts", "text": "body", "id": "fleet-abc"})
        for bad in ("", "has space", "x" * 129, "슬래시/"):
            with self.assertRaises(ValueError):
                hande.notice_frame("r", "body", bad)

    def test_notice_id_is_the_txn_or_a_row_id_fallback(self) -> None:
        self.assertEqual(hande.notice_id_of(7, "fleet-0123abcd"), "fleet-0123abcd")
        for txn in (None, "", "bad txn", "x" * 129):
            self.assertEqual(hande.notice_id_of(7, txn), "hande-7")
            self.assertTrue(hande.NOTICE_ID_RE.match(hande.notice_id_of(7, txn)))

    def test_notice_answer_parses_ack_and_nack_only(self) -> None:
        self.assertEqual(hande.notice_answer('{"type":"notice_ack","id":"fleet-1","seq":21}'), ("ack", "fleet-1", "21"))
        self.assertEqual(hande.notice_answer('{"type":"notice_ack","id":"fleet-1"}'), ("ack", "fleet-1", ""))
        self.assertEqual(hande.notice_answer('{"type":"notice_nack","id":"fleet-1","reason":"rate"}'),
                         ("nack", "fleet-1", "rate"))
        self.assertEqual(hande.notice_answer('{"type":"notice_nack","id":"fleet-1","reason":"Weird reason!"}'),
                         ("nack", "fleet-1", "invalid"))
        for line in ('{"type":"notice_ack","id":"bad id"}', '{"type":"notice_ack"}', '{"type":"turn","turn_id":"t"}',
                     '["notice_ack"]', "not json"):
            self.assertIsNone(hande.notice_answer(line), line)
        # an ack line is not a turn: answer() stays silent on it
        self.assertIsNone(hande.answer('{"type":"notice_ack","id":"fleet-1","seq":1}'))

    def test_pacer_allows_per_min_then_blocks_until_window_passes(self) -> None:
        p = hande.Pacer(2)
        self.assertTrue(p.ready(100.0))
        p.mark(100.0)
        self.assertTrue(p.ready(101.0))
        p.mark(101.0)
        self.assertFalse(p.ready(150.0))
        self.assertTrue(p.ready(161.0))


class SocketLoopTest(_Base):
    def _serve(self, bot_side_ready, **kw) -> tuple[str, socket.socket]:
        ours, bot = socket.socketpair()
        out: dict = {}

        def run() -> None:
            out["reason"] = hande.serve_connection(ours, self.queue, "fleet-alerts", kw.pop("pacer", hande.Pacer(9)),
                                                   poll=0.05, **kw)
            ours.close()

        th = threading.Thread(target=run, daemon=True)
        th.start()
        bot_side_ready(bot)
        th.join(timeout=5)
        self.assertFalse(th.is_alive(), "server loop did not finish")
        return out["reason"], bot

    def _read_lines(self, bot: socket.socket, n: int, timeout: float = 3.0) -> list[dict]:
        bot.settimeout(timeout)
        buf = b""
        while buf.count(b"\n") < n:
            chunk = bot.recv(65536)
            if not chunk:
                break
            buf += chunk
        return [json.loads(line) for line in buf.decode("utf-8").split("\n") if line]

    def test_legacy_mirrored_alerts_arrive_as_notice_frames_in_order_and_are_forgotten(self) -> None:
        outbox.enqueue(self.queue, "first alert", tier="action")
        outbox.enqueue(self.queue, "second alert", tier="action")
        frames: list[dict] = []

        def bot_side(bot: socket.socket) -> None:
            frames.extend(self._read_lines(bot, 2))

        reason, _ = self._serve(bot_side, max_notices=2, notice_ack=False)
        self.assertEqual(reason, "max notices reached")
        self.assertEqual([f["type"] for f in frames], ["notice", "notice"])
        self.assertTrue(all("id" not in f for f in frames))  # --no-notice-ack: the old frame shape
        self.assertEqual([f["text"] for f in frames], ["first alert", "second alert"])
        self.assertEqual({f["room"] for f in frames}, {"fleet-alerts"})
        self.assertEqual(hande.backlog(self.queue), 0)
        # #2413: each hand-off leaves a weak receipt (no notice ack in legacy mode).
        with outbox.database(self.queue) as conn:
            kinds = conn.execute("SELECT channel, kind FROM receipts ORDER BY at").fetchall()
        self.assertEqual(kinds, [("hande", "socket-write")] * 2)

    def test_turn_from_the_room_is_answered_with_an_empty_result(self) -> None:
        got: list[dict] = []

        def bot_side(bot: socket.socket) -> None:
            bot.sendall((json.dumps({"type": "turn", "turn_id": "t-9", "text": "누구 있어요?"}) + "\n").encode())
            got.extend(self._read_lines(bot, 1))
            bot.shutdown(socket.SHUT_WR)

        reason, _ = self._serve(bot_side)
        self.assertEqual(reason, "bot disconnected")
        self.assertEqual(got, [{"type": "result", "turn_id": "t-9", "status": "complete", "text": ""}])

    def test_pacer_holds_the_second_notice_inside_the_window(self) -> None:
        outbox.enqueue(self.queue, "one", tier="action")
        outbox.enqueue(self.queue, "two", tier="action")
        frames: list[dict] = []

        def bot_side(bot: socket.socket) -> None:
            frames.extend(self._read_lines(bot, 1))
            bot.settimeout(0.5)
            try:
                extra = bot.recv(65536)
            except socket.timeout:
                extra = b""
            frames.extend(json.loads(x) for x in extra.decode().split("\n") if x)
            bot.shutdown(socket.SHUT_WR)

        reason, _ = self._serve(bot_side, pacer=hande.Pacer(1), notice_ack=False)
        self.assertEqual(reason, "bot disconnected")
        self.assertEqual([f["text"] for f in frames], ["one"])
        self.assertEqual(hande.backlog(self.queue), 1)  # "two" waits for the next window, nothing lost

    def test_oversized_unterminated_frame_ends_the_connection(self) -> None:
        def bot_side(bot: socket.socket) -> None:
            bot.sendall(b"x" * (hande.MAX_FRAME_BYTES + 10))

        reason, _ = self._serve(bot_side)
        self.assertEqual(reason, "oversized frame")

    def test_blank_body_rows_are_skipped_not_sent(self) -> None:
        with outbox.database(self.queue) as conn:
            conn.execute("INSERT INTO hande_alerts(txn, body, created) VALUES ('fleet-x', '   ', ?)", (1e12,))
        outbox.enqueue(self.queue, "real", tier="action")
        frames: list[dict] = []

        def bot_side(bot: socket.socket) -> None:
            frames.extend(self._read_lines(bot, 1))

        reason, _ = self._serve(bot_side, max_notices=1, notice_ack=False)
        self.assertEqual([f["text"] for f in frames], ["real"])
        self.assertEqual(hande.backlog(self.queue), 0)


class _Clock:
    """Settable clock shared by the loop thread and the fake bot."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class _Bot:
    """Bot side of the socketpair: line-buffered reads (lines may arrive coalesced)."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""

    def line(self, timeout: float = 3.0) -> dict:
        self.sock.settimeout(timeout)
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise AssertionError("connection closed before a line")
            self.buf += chunk
        raw, self.buf = self.buf.split(b"\n", 1)
        return json.loads(raw)

    def silent(self, wait: float = 0.3) -> bool:
        """True when nothing arrives within ``wait`` seconds."""
        if self.buf:
            return False
        self.sock.settimeout(wait)
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return True
        self.buf += chunk
        return not chunk

    def send(self, *frames: dict) -> None:
        self.sock.sendall("".join(json.dumps(f) + "\n" for f in frames).encode())

    def ack(self, notice_id: str, seq: int = 1) -> None:
        self.send({"type": "notice_ack", "id": notice_id, "seq": seq})

    def nack(self, notice_id: str, reason: str) -> None:
        self.send({"type": "notice_nack", "id": notice_id, "reason": reason})


class NoticeAckLoopTest(_Base):
    """family-messenger#703: id on every notice, row kept until notice_ack, nack handling, resend on reconnect."""

    def setUp(self) -> None:
        super().setUp()
        self.clock = _Clock()

    def _serve(self, bot_side, **kw) -> str:
        ours, theirs = socket.socketpair()
        out: dict = {}

        def run() -> None:
            out["reason"] = hande.serve_connection(ours, self.queue, "fleet-alerts", kw.pop("pacer", hande.Pacer(9)),
                                                   poll=0.02, clock=self.clock, **kw)
            ours.close()

        th = threading.Thread(target=run, daemon=True)
        th.start()
        try:
            bot_side(_Bot(theirs))
        finally:
            th.join(timeout=5)
            theirs.close()
        self.assertFalse(th.is_alive(), "server loop did not finish")
        return out["reason"]

    def _receipts(self) -> list[tuple[str, str, str]]:
        with outbox.database(self.queue) as conn:
            return conn.execute("SELECT txn, kind, ref FROM receipts WHERE channel='hande' ORDER BY at").fetchall()

    def test_notice_carries_id_row_kept_until_ack_then_deleted_in_order(self) -> None:
        t1, _ = outbox.enqueue(self.queue, "first alert", tier="action")
        t2, _ = outbox.enqueue(self.queue, "second alert", tier="action")
        seen: list[dict] = []

        def bot_side(bot: _Bot) -> None:
            f1 = bot.line()
            seen.append(f1)
            self.assertEqual(f1["id"], t1)
            # Slow ack: well past the warn threshold the row stays and nothing is resent here.
            self.clock.now += hande.ACK_WARN_SECONDS + 1
            self.assertTrue(bot.silent(), "resent (or sent the next row) before the ack")
            self.assertEqual([t for t, _ in self._mirror_rows()], [t1, t2])
            bot.ack(t1, seq=21)
            f2 = bot.line()
            seen.append(f2)
            self.assertEqual([t for t, _ in self._mirror_rows()], [t2])
            bot.ack(t2, seq=22)

        with self.assertLogs(level="INFO") as logs:
            reason = self._serve(bot_side, max_notices=2)
        self.assertEqual(reason, "max notices reached")
        self.assertEqual([(f["id"], f["text"]) for f in seen], [(t1, "first alert"), (t2, "second alert")])
        self.assertEqual(hande.backlog(self.queue), 0)
        self.assertEqual(self._receipts(), [(t1, "hande-ack", "21"), (t2, "hande-ack", "22")])
        self.assertTrue(outbox.receipt_view(outbox.lookup(self.queue, t1))["notified"])  # hande-ack is strong
        joined = "\n".join(logs.output)
        self.assertIn("no notice_ack after", joined)
        self.assertNotIn("first alert", joined)
        self.assertNotIn("second alert", joined)

    def test_unacked_row_shows_a_weak_receipt_and_an_ack_for_another_id_is_ignored(self) -> None:
        txn, _ = outbox.enqueue(self.queue, "wait for me", tier="action")

        def bot_side(bot: _Bot) -> None:
            self.assertEqual(bot.line()["id"], txn)
            bot.ack("fleet-someone-else")
            bot.send({"type": "notice_ack", "id": "bad id"})
            self.assertTrue(bot.silent())
            bot.sock.shutdown(socket.SHUT_WR)

        self.assertEqual(self._serve(bot_side), "bot disconnected")
        self.assertEqual(hande.backlog(self.queue), 1)
        self.assertEqual(self._receipts(), [(txn, "socket-write", "")])

    def test_rate_nack_keeps_the_row_and_retries_after_a_minute(self) -> None:
        txn, _ = outbox.enqueue(self.queue, "rate limited", tier="action")
        ids: list[str] = []

        def bot_side(bot: _Bot) -> None:
            ids.append(bot.line()["id"])
            bot.nack(txn, "rate")
            self.assertTrue(bot.silent(), "resent inside the rate back-off")
            self.assertEqual(hande.backlog(self.queue), 1)
            self.clock.now += hande.RATE_RETRY_SECONDS - 5
            self.assertTrue(bot.silent(), "resent before ~60s")
            self.clock.now += 10
            ids.append(bot.line()["id"])
            bot.ack(txn)

        self.assertEqual(self._serve(bot_side, max_notices=1), "max notices reached")
        self.assertEqual(ids, [txn, txn])
        self.assertEqual(hande.backlog(self.queue), 0)

    def test_relay_conflict_and_client_id_reuse_nacks_keep_the_row_and_resend(self) -> None:
        for reason in ("relay", "conflict", "client_id_reuse"):
            with self.subTest(reason=reason):
                txn, _ = outbox.enqueue(self.queue, f"resend after {reason}", tier="action")
                texts: list[str] = []

                def bot_side(bot: _Bot) -> None:
                    texts.append(bot.line()["text"])
                    bot.nack(txn, reason)
                    self.assertTrue(bot.silent())
                    self.assertEqual(hande.backlog(self.queue), 1)
                    self.clock.now += hande.RESEND_DELAY_SECONDS + 1
                    frame = bot.line()
                    texts.append(frame["text"])
                    self.assertEqual(frame["id"], txn)
                    bot.ack(txn)

                self.assertEqual(self._serve(bot_side, max_notices=1), "max notices reached")
                self.assertEqual(texts, [f"resend after {reason}"] * 2)
                self.assertEqual(hande.backlog(self.queue), 0)

    def test_other_nack_is_logged_without_body_and_the_row_is_dropped(self) -> None:
        t1, _ = outbox.enqueue(self.queue, "secret-ish body", tier="action")
        t2, _ = outbox.enqueue(self.queue, "next one", tier="action")
        texts: list[str] = []

        def bot_side(bot: _Bot) -> None:
            texts.append(bot.line()["text"])
            bot.nack(t1, "not_joined_or_halted")
            texts.append(bot.line()["text"])
            bot.nack(t2, "Something Unexpected!")  # odd reason shape: still dropped, logged as "invalid"

        with self.assertLogs(level="INFO") as logs:
            reason = self._serve(bot_side, max_notices=2)
        self.assertEqual(reason, "max notices reached")
        self.assertEqual(texts, ["secret-ish body", "next one"])
        self.assertEqual(hande.backlog(self.queue), 0)
        self.assertEqual(self._receipts(), [(t1, "hande-dropped", "not_joined_or_halted"), (t2, "hande-dropped", "invalid")])
        joined = "\n".join(logs.output)
        self.assertIn("not_joined_or_halted", joined)
        self.assertNotIn("secret-ish body", joined)
        self.assertNotIn("Something Unexpected", joined)

    def test_reconnect_resends_the_unacked_row_first(self) -> None:
        t1, _ = outbox.enqueue(self.queue, "in flight", tier="action")
        t2, _ = outbox.enqueue(self.queue, "behind it", tier="action")
        pacer = hande.Pacer(9)

        def first(bot: _Bot) -> None:
            self.assertEqual(bot.line()["id"], t1)
            bot.sock.shutdown(socket.SHUT_WR)  # bot restarts before answering

        self.assertEqual(self._serve(first, pacer=pacer), "bot disconnected")
        self.assertEqual([t for t, _ in self._mirror_rows()], [t1, t2])
        seen: list[str] = []

        def second(bot: _Bot) -> None:
            for _ in range(2):
                frame = bot.line()
                seen.append(frame["text"])
                bot.ack(frame["id"])

        self.clock.now += 61  # fresh pacer window
        self.assertEqual(self._serve(second, pacer=pacer, max_notices=2), "max notices reached")
        self.assertEqual(seen, ["in flight", "behind it"])
        self.assertEqual(hande.backlog(self.queue), 0)

    def test_turn_frames_are_still_answered_while_a_notice_is_in_flight(self) -> None:
        txn, _ = outbox.enqueue(self.queue, "pending", tier="action")
        got: list[dict] = []

        def bot_side(bot: _Bot) -> None:
            self.assertEqual(bot.line()["id"], txn)
            bot.send({"type": "turn", "turn_id": "t-1", "text": "누구?"})
            got.append(bot.line())
            # ack and a turn coalesced in one chunk: both are handled
            bot.send({"type": "notice_ack", "id": txn, "seq": 5}, {"type": "turn", "turn_id": "t-2", "text": "?"})
            got.append(bot.line())
            bot.sock.shutdown(socket.SHUT_WR)

        self.assertEqual(self._serve(bot_side), "bot disconnected")
        self.assertEqual([g["turn_id"] for g in got], ["t-1", "t-2"])
        self.assertTrue(all(g == {"type": "result", "turn_id": g["turn_id"], "status": "complete", "text": ""} for g in got))
        self.assertEqual(hande.backlog(self.queue), 0)

    def test_blank_body_rows_are_skipped_in_ack_mode_too(self) -> None:
        with outbox.database(self.queue) as conn:
            conn.execute("INSERT INTO hande_alerts(txn, body, created) VALUES (NULL, '   ', ?)", (1e12,))
            conn.execute("INSERT INTO hande_alerts(txn, body, created) VALUES (NULL, 'no txn', ?)", (1e12,))
        ids: list[str] = []

        def bot_side(bot: _Bot) -> None:
            frame = bot.line()
            ids.append(frame["id"])
            self.assertEqual(frame["text"], "no txn")
            bot.ack(frame["id"])

        self.clock.now = 1e12
        self.assertEqual(self._serve(bot_side, max_notices=1), "max notices reached")
        self.assertTrue(ids[0].startswith("hande-"))  # no txn → row-id fallback
        self.assertEqual(hande.backlog(self.queue), 0)


class ReplyTurnTest(_Base):
    """#2413 stage 3: an owner device's ``ok/hold <id>`` turn becomes a reply receipt; others are ignored."""

    def _turn(self, prompt: str, sender: str = "@owner-web-87576b:hande", **extra) -> str:
        return json.dumps({"type": "turn", "turn_id": "t-fleet-alerts-1", "prompt": prompt, "sender": sender, "room": "fleet-alerts", **extra})

    def test_owner_turn_records_reply_and_queues_ack_mirrored_to_hande(self) -> None:
        txn, _ = outbox.enqueue(self.queue, "look", tier="action")
        aid = outbox.alert_id_of(self.queue, txn)
        out = hande.answer(self._turn(f"ok {aid}"), self.queue)
        self.assertEqual(json.loads(out)["text"], "")  # the bot still posts nothing itself
        info = outbox.lookup(self.queue, aid)
        self.assertEqual([r["ref"] for r in info["receipts"] if r["channel"] == "reply"], ["ok:hande:owner-web-87576b"])
        rows = self._mirror_rows()  # the ack row is mirrored, so the hande room shows it too
        self.assertEqual(len(rows), 2)
        self.assertIn(f"✅ #{aid} ok 접수", rows[1][1])

    def test_peer_turn_other_actor_and_plain_text_are_ignored(self) -> None:
        txn, _ = outbox.enqueue(self.queue, "look", tier="action")
        aid = outbox.alert_id_of(self.queue, txn)
        hande.answer(self._turn(f"ok {aid}", peer=True), self.queue)
        hande.answer(self._turn(f"ok {aid}", sender="@jungin-iphone-1b89e3:hande"), self.queue)
        hande.answer(self._turn(f"ok {aid}", sender="@owner:matrix.example"), self.queue)
        hande.answer(self._turn(f"please ok {aid}"), self.queue)
        hande.answer(self._turn(f"ok {aid}"), None)  # no queue → pure answer
        self.assertEqual([r for r in outbox.lookup(self.queue, aid)["receipts"] if r["channel"] == "reply"], [])
        self.assertEqual(hande.backlog(self.queue), 1)


class CliTest(_Base):
    def test_status_install_only_and_uninstall(self) -> None:
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(hande.main(["--queue", str(self.queue), "--status"]), 0)
        self.assertEqual(json.loads(buf.getvalue()), {"installed": True, "backlog": 0})
        self.assertEqual(hande.main(["--queue", str(self.queue), "--uninstall"]), 0)
        self.assertFalse(hande.installed(self.queue))
        self.assertEqual(hande.main(["--queue", str(self.queue), "--install-only"]), 0)
        self.assertTrue(hande.installed(self.queue))

    def test_notice_ack_is_on_by_default_and_off_with_no_notice_ack(self) -> None:
        from unittest import mock

        class _Stop(Exception):
            pass

        class _Server:
            def __init__(self) -> None:
                self.calls = 0

            def accept(self):
                self.calls += 1
                if self.calls > 1:
                    raise _Stop
                return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM), None

        for extra, expected in (([], True), (["--no-notice-ack"], False)):
            seen: dict = {}

            def fake_serve(conn, queue, room, pacer, **kw):
                seen.update(kw)
                return "done"

            argv = ["--queue", str(self.queue), "--socket", str(self.root / "s"), "--room", "fleet-alerts", *extra]
            with mock.patch.object(hande, "listen", return_value=_Server()), \
                    mock.patch.object(hande, "serve_connection", side_effect=fake_serve):
                with self.assertRaises(_Stop):
                    hande.main(argv)
            self.assertIs(seen["notice_ack"], expected, extra)

    def test_run_mode_requires_socket_and_valid_room_and_pace(self) -> None:
        for argv in (
            ["--queue", str(self.queue), "--socket", str(self.root / "s")],
            ["--queue", str(self.queue), "--socket", str(self.root / "s"), "--room", "bad room!"],
            ["--queue", str(self.queue), "--socket", str(self.root / "s"), "--room", "fleet-alerts", "--per-min", "10"],
        ):
            with self.assertRaises(SystemExit) as cm:
                hande.main(argv)
            self.assertEqual(cm.exception.code, 2, argv)


if __name__ == "__main__":
    unittest.main()
