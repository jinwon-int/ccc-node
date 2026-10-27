"""Send-only 「한데」 lane for the fleet alert bot (#2413 / family-messenger#460). No AI runtime, no inbound commands.

Second delivery channel next to ``fleet_alerts_sender.py`` (Matrix). The owner is
moving the fleet alert room from Matrix to 「한데」; during the transition both
senders run and every alert is posted to both rooms.

How it attaches to the existing queue
-------------------------------------
The Matrix sender wipes an alert's body once it is delivered, so a second
reader of ``alerts`` would race it. Instead this lane keeps its own mirror:
an ``AFTER INSERT`` trigger on ``alerts`` copies ``(txn, body, created)`` into
``hande_alerts`` at enqueue time, and this process drains that table. The
receiver, the outbox module and the Matrix sender are not modified; one
channel failing never blocks the other. Same pattern as the card-alert
feeder that has posted to 「한데」 from the relay host since 2026-10-26.

How it reaches 「한데」
--------------------
A dedicated relay bot (``native-mls-bot <relay> steward <device>
--responder-unix <sock>``, family-messenger) connects to the Unix socket this
process listens on. We speak the FLEET-WORKER JSON-lines port from the worker
side:

* every mirrored alert becomes one ``{"type":"notice","room":…,"text":…}``
  frame, which the bot encrypts (MLS) and posts into its room — paced at most
  ``--per-min`` (default 8; the bot drops notices above 10/min per room);
* every ``turn`` frame (someone typed in the room) is answered with an empty
  complete ``result``, so the bot posts nothing and the room stays read-only
  in practice; approve/deny/cancel/tool frames are ignored.

Delivery scope (known limit, #2413 receipts): a mirror row is deleted once its
notice frame is written to the bot's socket. The bot has no acknowledgement
frame for notices, so a bot crash between the write and its relay POST can
lose that one alert in 「한데」 (Matrix is unaffected). Mirror rows older than
24h are dropped, so a stopped lane never turns the mirror into an indefinite
alert-text archive. Bodies, tokens and frames are never logged.

Rollback: stop the service, then ``--uninstall`` (drops trigger + mirror table).
stdlib only.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import select
import socket
import stat
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_alerts_outbox as outbox  # noqa: E402

ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TURN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_AGE_SECONDS = 24 * 3600
MAX_FRAME_BYTES = 512 * 1024
MAX_TEXT_BYTES = 8000  # bot reply_max_bytes is 8 KiB; stay below so nothing is cut
MAX_PER_MIN = 9  # the bot drops notices above 10/min per room
TRUNCATION_MARKER = "…(길어서 줄임)"

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS hande_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, txn TEXT, body TEXT NOT NULL, created REAL NOT NULL)""",
    f"""CREATE TRIGGER IF NOT EXISTS hande_mirror AFTER INSERT ON alerts BEGIN
        DELETE FROM hande_alerts WHERE created < NEW.created - {MAX_AGE_SECONDS};
        INSERT INTO hande_alerts(txn, body, created) VALUES (NEW.txn, NEW.body, NEW.created);
    END""",
)


# ---- mirror table ----------------------------------------------------------

def install(queue: os.PathLike | str) -> None:
    """Create the mirror table + trigger (idempotent, under the outbox lock)."""
    with outbox.database(queue) as conn:
        for statement in SCHEMA:
            conn.execute(statement)


def uninstall(queue: os.PathLike | str) -> None:
    with outbox.database(queue) as conn:
        conn.execute("DROP TRIGGER IF EXISTS hande_mirror")
        conn.execute("DROP TABLE IF EXISTS hande_alerts")


def installed(queue: os.PathLike | str) -> bool:
    with outbox.database(queue) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE (type='trigger' AND name='hande_mirror')"
            " OR (type='table' AND name='hande_alerts')"
        ).fetchone()
    return int(row[0]) == 2


def next_alert(queue: os.PathLike | str, now: Optional[float] = None) -> Optional[tuple[int, str, str]]:
    """Oldest mirrored alert younger than 24h as ``(id, txn, body)``; expired rows are dropped."""
    now = time.time() if now is None else now
    with outbox.database(queue) as conn:
        expired = conn.execute("DELETE FROM hande_alerts WHERE created < ?", (now - MAX_AGE_SECONDS,)).rowcount
        if expired:
            logging.warning("expired %d undelivered 한데 fleet alerts older than 24h", expired)
        return conn.execute("SELECT id, txn, body FROM hande_alerts ORDER BY id LIMIT 1").fetchone()


def forget(queue: os.PathLike | str, row_id: int) -> None:
    with outbox.database(queue) as conn:
        conn.execute("DELETE FROM hande_alerts WHERE id=?", (row_id,))


def backlog(queue: os.PathLike | str) -> int:
    with outbox.database(queue) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM hande_alerts").fetchone()[0])


# ---- FLEET-WORKER frames ---------------------------------------------------

def bounded(text: str, limit: int = MAX_TEXT_BYTES) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    cut = data[: limit - len(TRUNCATION_MARKER.encode("utf-8"))].decode("utf-8", "ignore")
    return cut + TRUNCATION_MARKER


def notice_frame(room: str, body: str) -> str:
    return json.dumps({"type": "notice", "room": room, "text": bounded(body.strip())}, ensure_ascii=False) + "\n"


def answer(line: str) -> Optional[str]:
    """Reply for one inbound frame from the bot, or None.

    Only ``turn`` frames get a reply — an empty complete result, so the bot posts
    nothing. Everything else (approve/deny/cancel/tool-result, malformed JSON) is
    ignored: this lane never executes anything on the owner's behalf.
    """
    try:
        frame = json.loads(line)
    except ValueError:
        return None
    if not isinstance(frame, dict) or frame.get("type") != "turn":
        return None
    turn_id = frame.get("turn_id")
    if not isinstance(turn_id, str) or not TURN_ID_RE.match(turn_id):
        return None
    return json.dumps({"type": "result", "turn_id": turn_id, "status": "complete", "text": ""}) + "\n"


class Pacer:
    def __init__(self, per_min: int) -> None:
        self.per_min = per_min
        self.sent: list[float] = []

    def ready(self, now: float) -> bool:
        self.sent = [t for t in self.sent if now - t < 60]
        return len(self.sent) < self.per_min

    def mark(self, now: float) -> None:
        self.sent.append(now)


def serve_connection(
    conn: socket.socket,
    queue: os.PathLike | str,
    room: str,
    pacer: Pacer,
    *,
    poll: float = 1.0,
    max_notices: Optional[int] = None,
    clock=time.time,
) -> str:
    """Serve one bot connection until it ends; returns the reason.

    ``max_notices`` stops after that many notices were handed over (tests only).
    """
    conn.setblocking(False)
    inbound = b""
    next_poll = 0.0
    handed = 0
    while True:
        readable, _, _ = select.select([conn], [], [], poll)
        if readable:
            chunk = conn.recv(65536)
            if not chunk:
                return "bot disconnected"
            inbound += chunk
            if len(inbound) > MAX_FRAME_BYTES and b"\n" not in inbound:
                return "oversized frame"
            while b"\n" in inbound:
                line, inbound = inbound.split(b"\n", 1)
                reply = answer(line.decode("utf-8", "replace"))
                if reply:
                    conn.setblocking(True)
                    conn.sendall(reply.encode("utf-8"))
                    conn.setblocking(False)
                    logging.info("answered a fleet-alert-room turn with an empty result")
        now = clock()
        if now < next_poll or not pacer.ready(now):
            continue
        next_poll = now + poll
        item = next_alert(queue, now)
        if item is None:
            if max_notices is not None and handed >= max_notices:
                return "max notices reached"
            continue
        row_id, txn, body = item
        if not body.strip():
            forget(queue, row_id)
            continue
        conn.setblocking(True)
        conn.sendall(notice_frame(room, body).encode("utf-8"))
        conn.setblocking(False)
        pacer.mark(now)
        forget(queue, row_id)
        # Weak receipt (#2413): the frame left for the bot; there is no notice ack.
        if txn:
            try:
                outbox.receipt(queue, txn, "hande", "socket-write", "")
            except Exception:
                logging.warning("hande receipt not recorded txn=%s", txn)
        handed += 1
        logging.info("한데 fleet alert handed to the bot txn=%s", txn)
        if max_notices is not None and handed >= max_notices:
            return "max notices reached"


def listen(path: str, owner_uid: Optional[int]) -> socket.socket:
    try:
        info = os.lstat(path)
        if not stat.S_ISSOCK(info.st_mode):
            raise ValueError("responder path exists and is not a socket")
        os.unlink(path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        server.bind(path)
    finally:
        os.umask(old)
    if owner_uid is not None:
        os.chown(path, owner_uid, -1)
    os.chmod(path, 0o600)
    server.listen(1)
    return server


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--queue", required=True, type=Path, help="owner-only queue directory shared with the receiver")
    parser.add_argument("--socket", help="Unix socket the relay bot connects to (--responder-unix)")
    parser.add_argument("--room", help="relay room id of the 「한데」 fleet alert room ([A-Za-z0-9_-]{1,64})")
    parser.add_argument("--socket-owner", help="user name that may connect (the bot user)")
    parser.add_argument("--per-min", type=int, default=8, help=f"notice pace per minute, 1..{MAX_PER_MIN}")
    parser.add_argument("--install-only", action="store_true", help="create mirror table + trigger and exit")
    parser.add_argument("--uninstall", action="store_true", help="drop mirror table + trigger and exit")
    parser.add_argument("--status", action="store_true", help="print mirror backlog and exit")
    args = parser.parse_args(argv)
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.uninstall:
        uninstall(args.queue)
        return 0
    if args.status:
        print(json.dumps({"installed": installed(args.queue), "backlog": backlog(args.queue)}))
        return 0
    install(args.queue)
    if args.install_only:
        return 0
    if not args.socket or not args.room or not ROOM_RE.match(args.room):
        parser.error("--socket and a relay room id are required")
    if not 1 <= args.per_min <= MAX_PER_MIN:
        parser.error(f"--per-min must be 1..{MAX_PER_MIN} (the bot drops notices above 10/min)")
    owner_uid = None
    if args.socket_owner:
        import pwd

        owner_uid = pwd.getpwnam(args.socket_owner).pw_uid
    server = listen(args.socket, owner_uid)
    pacer = Pacer(args.per_min)
    while True:
        conn, _ = server.accept()
        logging.info("fleet alert bot connected")
        try:
            reason = serve_connection(conn, args.queue, args.room, pacer)
        except OSError as exc:
            reason = type(exc).__name__
        finally:
            conn.close()
        logging.warning("fleet alert bot connection ended: %s", reason)


if __name__ == "__main__":
    raise SystemExit(main())
