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
        t1, _ = outbox.enqueue(self.queue, "first", node="node-a", event="AgentCronRun")
        t2, _ = outbox.enqueue(self.queue, "second", node="node-b", event="SelfUpdate")
        self.assertEqual(self._mirror_rows(), [(t1, "first"), (t2, "second")])
        self.assertEqual(hande.backlog(self.queue), 2)

    def test_dedup_duplicate_is_not_mirrored_twice(self) -> None:
        fp = outbox.fingerprint("node-a", "r1.json")
        outbox.enqueue(self.queue, "a", fingerprint=fp)
        outbox.enqueue(self.queue, "a again", fingerprint=fp)
        self.assertEqual(hande.backlog(self.queue), 1)

    def test_matrix_delivery_wipes_outbox_body_but_mirror_keeps_it(self) -> None:
        txn, _ = outbox.enqueue(self.queue, "keep for hande")
        fake = _FakeMatrix()
        self.assertTrue(asyncio.run(tx.deliver_one(self.queue, fake, "!room:hs")))
        self.assertEqual(fake.sent, [txn])
        with outbox.database(self.queue) as conn:
            self.assertEqual(conn.execute("SELECT body FROM alerts WHERE txn=?", (txn,)).fetchone()[0], "")
        self.assertEqual(hande.next_alert(self.queue)[1:], (txn, "keep for hande"))

    def test_next_alert_is_oldest_forget_removes_and_expired_rows_are_dropped(self) -> None:
        t1, _ = outbox.enqueue(self.queue, "old")
        t2, _ = outbox.enqueue(self.queue, "new")
        with outbox.database(self.queue) as conn:
            conn.execute("UPDATE hande_alerts SET created=created-? WHERE txn=?", (hande.MAX_AGE_SECONDS + 5, t1))
        item = hande.next_alert(self.queue)
        self.assertEqual(item[1:], (t2, "new"))
        self.assertEqual(hande.backlog(self.queue), 1)
        hande.forget(self.queue, item[0])
        self.assertIsNone(hande.next_alert(self.queue))

    def test_trigger_prunes_mirror_rows_older_than_24h_on_insert(self) -> None:
        t1, _ = outbox.enqueue(self.queue, "stale")
        with outbox.database(self.queue) as conn:
            conn.execute("UPDATE hande_alerts SET created=created-? WHERE txn=?", (hande.MAX_AGE_SECONDS + 5, t1))
        outbox.enqueue(self.queue, "fresh")
        self.assertEqual([b for _, b in self._mirror_rows()], ["fresh"])

    def test_uninstall_drops_trigger_and_table_and_matrix_sender_is_unaffected(self) -> None:
        hande.uninstall(self.queue)
        self.assertFalse(hande.installed(self.queue))
        txn, _ = outbox.enqueue(self.queue, "matrix only")
        with outbox.database(self.queue) as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        self.assertNotIn("hande_alerts", names)
        self.assertNotIn("hande_mirror", names)
        fake = _FakeMatrix()
        self.assertTrue(asyncio.run(tx.deliver_one(self.queue, fake, "!room:hs")))
        self.assertEqual(fake.sent, [txn])

    def test_outbox_prune_of_delivered_rows_leaves_mirror_alone(self) -> None:
        txn, _ = outbox.enqueue(self.queue, "x")
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

    def test_mirrored_alerts_arrive_as_notice_frames_in_order_and_are_forgotten(self) -> None:
        outbox.enqueue(self.queue, "first alert")
        outbox.enqueue(self.queue, "second alert")
        frames: list[dict] = []

        def bot_side(bot: socket.socket) -> None:
            frames.extend(self._read_lines(bot, 2))

        reason, _ = self._serve(bot_side, max_notices=2)
        self.assertEqual(reason, "max notices reached")
        self.assertEqual([f["type"] for f in frames], ["notice", "notice"])
        self.assertEqual([f["text"] for f in frames], ["first alert", "second alert"])
        self.assertEqual({f["room"] for f in frames}, {"fleet-alerts"})
        self.assertEqual(hande.backlog(self.queue), 0)

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
        outbox.enqueue(self.queue, "one")
        outbox.enqueue(self.queue, "two")
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

        reason, _ = self._serve(bot_side, pacer=hande.Pacer(1))
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
        outbox.enqueue(self.queue, "real")
        frames: list[dict] = []

        def bot_side(bot: socket.socket) -> None:
            frames.extend(self._read_lines(bot, 1))

        reason, _ = self._serve(bot_side, max_notices=1)
        self.assertEqual([f["text"] for f in frames], ["real"])
        self.assertEqual(hande.backlog(self.queue), 0)


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
