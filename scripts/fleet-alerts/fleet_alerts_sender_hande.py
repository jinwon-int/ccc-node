"""Send-only 「한데」 lane for the fleet alert bot (#2413 / family-messenger#460). No AI runtime, no inbound commands.

Second delivery channel next to ``fleet_alerts_sender.py`` (Matrix). The owner is
moving the fleet alert room from Matrix to 「한데」; during the transition both
senders run and every alert is posted to both rooms.

How it attaches to the existing queue
-------------------------------------
The Matrix sender wipes an alert's body once it is delivered, so a second
reader of ``alerts`` would race it. Instead this lane keeps its own mirror:
an ``AFTER INSERT`` trigger on ``alerts`` copies ``(txn, body, created)`` into
``hande_alerts`` at enqueue time (every tier except ``info`` — info alerts
reach both rooms through the daily digest row, #2413 stage 2), and this
process drains that table. The
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

Delivery (family-messenger#703, notice ack): every notice frame carries
``"id"`` (the row's txn, or ``hande-<row id>``) and the row stays in the mirror
until the bot answers on the same connection — ``notice_ack`` (posted, relay
seq known → row deleted, strong ``hande-ack`` receipt) or ``notice_nack``.
Only one notice is in flight at a time, so order is preserved. A nack
``rate`` keeps the row and retries after ~60s; ``relay`` / ``conflict`` /
``client_id_reuse`` keep it and resend on the next round; any other reason is
logged (reason only) and the row is dropped. A slow ack is never answered by
a resend on the same connection (the bot is still retrying its relay POST);
if the connection ends first, the unacked row is resent on the next one
(at-least-once: a bot crash after the POST but before the ack can post it
twice). ``--no-notice-ack`` restores the old behaviour for a bot without
acks: no id, the row is deleted once its frame is written (a bot crash
between the write and its relay POST can then lose that one 「한데」 copy;
Matrix is unaffected). Mirror rows older than 24h are dropped, so a stopped
lane never turns the mirror into an indefinite alert-text archive. Bodies,
tokens and frames are never logged.

Retiring the Matrix sender (``--close-source``, opt-in, ack mode only)
-----------------------------------------------------------------------
Without the flag this lane changes nothing in ``alerts``: only the Matrix
sender closes a row (``delivered`` + body wiped, ``outbox.delivered``), and only
its loop runs the tier policy (``fleet_alerts_policy.tick``: 09:00 KST info
digest, action/critical re-notify). Stopping it alone would leave every body in
the queue and stop the digest and re-notify. With ``--close-source`` this lane
takes both jobs over (same design as the bunjang 「한데」 sender):

* ``notice_ack`` → the source row is closed: ``delivered`` = now, ``event_id``
  ``hande:<notice id>``, body wiped (the ``hande-ack`` receipt is unchanged and
  is what re-notify keys on);
* a dropping ``notice_nack`` → closed as ``hande-dropped:<reason>`` (no strong
  receipt, so never re-notified — like a Matrix send that never succeeded);
* a non-``info`` row older than 24h whose mirror row is gone (expired, or never
  mirrored) → closed as ``hande-expired``;
* a row the Matrix sender already closed (``$event``) is left as it is;
* ``info`` rows are never closed here: as on the Matrix side they are held,
  not mirrored, and closed by the 09:00 digest (``digested`` receipt);
* the lane calls ``policy.tick`` once per poll while a bot is connected, so the
  digest and re-notify rows are created here — and, being non-``info``
  inserts, reach 「한데」 through the mirror like any alert. ``tick`` holds a
  non-blocking queue-directory lock, so while both senders still run (the
  cut-over window) the two ticks never interleave; whichever lane gets the lock
  runs that round, the other skips it.

Turn it on together with retiring the Matrix sender: while both run, the first
lane to close a row keeps the other from sending it (the Matrix sender only
sends rows with ``delivered IS NULL``). Rollback: drop the flag and restart this
lane; re-enabling the Matrix sender resumes Matrix delivery of still-open rows.

Rollback (lane): stop the service, then ``--uninstall`` (drops trigger + mirror table).
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
import fleet_alerts_policy as policy  # noqa: E402
import fleet_alerts_reply as reply  # noqa: E402

ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TURN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_AGE_SECONDS = 24 * 3600
MAX_FRAME_BYTES = 512 * 1024
MAX_TEXT_BYTES = 8000  # bot reply_max_bytes is 8 KiB; stay below so nothing is cut
MAX_PER_MIN = 9  # the bot drops notices above 10/min per room
TRUNCATION_MARKER = "…(길어서 줄임)"
# family-messenger#703: notice ids and the bot's answers.
NOTICE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
NACK_REASON_RE = re.compile(r"^[a-z_]{1,32}$")
NACK_RETRY_LATER = frozenset({"rate"})  # bot's per-room minute cap — back off, then resend
NACK_RESEND = frozenset({"relay", "conflict", "client_id_reuse"})  # not stored — resend next round
RATE_RETRY_SECONDS = 60.0
RESEND_DELAY_SECONDS = 5.0
ACK_WARN_SECONDS = 300.0  # an in-flight notice this old gets one warning (old bot without acks?)
# --close-source: ``alerts.event_id`` markers for rows this lane closes (a Matrix close is ``$event``).
CLOSED_ACK_PREFIX = "hande:"
CLOSED_DROPPED_PREFIX = "hande-dropped:"
CLOSED_EXPIRED = "hande-expired"

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS hande_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, txn TEXT, body TEXT NOT NULL, created REAL NOT NULL)""",
    # Stage 2 (#2413): ``info`` rows are held for the daily digest on the Matrix
    # side; the digest itself is a derived ``digest`` row, so mirroring only
    # non-info inserts keeps both rooms identical. The trigger is dropped and
    # recreated on every install so an older WHEN-less trigger is replaced.
    f"""CREATE TRIGGER hande_mirror AFTER INSERT ON alerts WHEN NEW.tier <> 'info' BEGIN
        DELETE FROM hande_alerts WHERE created < NEW.created - {MAX_AGE_SECONDS};
        INSERT INTO hande_alerts(txn, body, created) VALUES (NEW.txn, NEW.body, NEW.created);
    END""",
)


# ---- mirror table ----------------------------------------------------------

def install(queue: os.PathLike | str) -> None:
    """Create the mirror table and (re)create the trigger (idempotent, under the outbox lock)."""
    with outbox.database(queue) as conn:
        conn.execute("DROP TRIGGER IF EXISTS hande_mirror")
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


def next_alert(
    queue: os.PathLike | str, now: Optional[float] = None, *, close_source: bool = False,
) -> Optional[tuple[int, str, str]]:
    """Oldest mirrored alert younger than 24h as ``(id, txn, body)``; expired rows are dropped.

    With ``close_source`` the non-``info`` outbox rows that lost their mirror row
    this way (or never had one) are closed too (``hande-expired``), so no alert
    text outlives 24h once the Matrix sender is retired. Held ``info`` rows wait
    for the digest, which closes them.
    """
    now = time.time() if now is None else now
    with outbox.database(queue) as conn:
        expired = conn.execute("DELETE FROM hande_alerts WHERE created < ?", (now - MAX_AGE_SECONDS,)).rowcount
        if expired:
            logging.warning("expired %d undelivered 한데 fleet alerts older than 24h", expired)
        if close_source:
            closed = conn.execute(
                "UPDATE alerts SET delivered=?, event_id=?, body='' WHERE delivered IS NULL AND tier<>'info'"
                " AND created < ? AND txn NOT IN (SELECT txn FROM hande_alerts WHERE txn IS NOT NULL)",
                (now, CLOSED_EXPIRED, now - MAX_AGE_SECONDS),
            ).rowcount
            if closed:
                logging.warning("closed %d fleet alerts older than 24h never posted to 한데", closed)
        return conn.execute("SELECT id, txn, body FROM hande_alerts ORDER BY id LIMIT 1").fetchone()


def close_source_row(queue: os.PathLike | str, txn: Optional[str], marker: str,
                     now: Optional[float] = None) -> int:
    """Close the ``alerts`` row a mirror row came from: delivered, ``event_id`` = marker, body wiped.

    The Matrix sender's ``outbox.delivered`` does the same for a Matrix event; a
    row it (or the digest) already closed is left untouched. Returns rows closed.
    """
    if not txn:
        return 0
    now = time.time() if now is None else now
    with outbox.database(queue) as conn:
        return int(conn.execute(
            "UPDATE alerts SET delivered=?, event_id=?, body='' WHERE txn=? AND delivered IS NULL",
            (now, marker[:256], txn),
        ).rowcount or 0)


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


def notice_frame(room: str, body: str, notice_id: Optional[str] = None) -> str:
    frame = {"type": "notice", "room": room, "text": bounded(body.strip())}
    if notice_id is not None:
        if not NOTICE_ID_RE.match(notice_id):
            raise ValueError("invalid notice id")
        frame["id"] = notice_id
    return json.dumps(frame, ensure_ascii=False) + "\n"


def notice_id_of(row_id: int, txn: Optional[str]) -> str:
    """The ``id`` a row's notice carries (#703): its txn when that fits the bot's rule, else ``hande-<row id>``."""
    if isinstance(txn, str) and NOTICE_ID_RE.match(txn):
        return txn
    return f"hande-{int(row_id)}"


def notice_answer(line: str) -> Optional[tuple[str, str, str]]:
    """Parse a ``notice_ack`` / ``notice_nack`` line (#703) as ``(kind, id, detail)``, else None.

    ``detail`` is the relay seq for an ack and the reason for a nack (an
    unexpected reason shape becomes ``"invalid"`` so nothing odd is logged).
    """
    try:
        frame = json.loads(line)
    except ValueError:
        return None
    if not isinstance(frame, dict) or frame.get("type") not in ("notice_ack", "notice_nack"):
        return None
    notice_id = frame.get("id")
    if not isinstance(notice_id, str) or not NOTICE_ID_RE.match(notice_id):
        return None
    if frame["type"] == "notice_ack":
        seq = frame.get("seq")
        return "ack", notice_id, str(seq) if isinstance(seq, int) and not isinstance(seq, bool) else ""
    reason = frame.get("reason")
    return "nack", notice_id, reason if isinstance(reason, str) and NACK_REASON_RE.match(reason) else "invalid"


def answer(line: str, queue: Optional[os.PathLike | str] = None, *, owner_actor: str = "owner") -> Optional[str]:
    """Reply for one inbound frame from the bot, or None.

    Only ``turn`` frames get a reply — an empty complete result, so the bot posts
    nothing. Everything else (approve/deny/cancel/tool-result, malformed JSON) is
    ignored: this lane never executes anything on the owner's behalf.

    Stage 3 (#2413): when ``queue`` is given and the turn is an exact
    ``ok <id>`` / ``hold <id>`` line from a device of ``owner_actor`` (the
    sender is MLS-authenticated by the bot; peer-bot turns carry ``peer`` and
    are ignored), the reply receipt is recorded and an ``ack`` row is queued
    for both rooms. The result stays empty either way.
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
    if queue is not None and not frame.get("peer"):
        device = reply.hande_sender_device(frame.get("sender"))
        if device and reply.actor_of(device) == owner_actor:
            try:
                out = reply.handle_text(queue, frame.get("prompt"), channel="hande", who=device)
            except Exception:
                out = None
                logging.warning("hande reply not recorded (queue error)")
            if out:
                logging.info("hande reply %s alert=%s from=%s", out["outcome"], out["alert_id"], device)
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


class _InFlight:
    """The one notice handed to the bot and not yet answered (#703)."""

    __slots__ = ("row_id", "txn", "notice_id", "sent_at", "warned")

    def __init__(self, row_id: int, txn: Optional[str], notice_id: str, sent_at: float) -> None:
        self.row_id = row_id
        self.txn = txn
        self.notice_id = notice_id
        self.sent_at = sent_at
        self.warned = False


def _receipt(queue: os.PathLike | str, txn: Optional[str], kind: str, ref: str) -> None:
    if not txn:
        return
    try:
        outbox.receipt(queue, txn, "hande", kind, ref)
    except Exception:
        logging.warning("hande receipt not recorded txn=%s", txn)


def serve_connection(
    conn: socket.socket,
    queue: os.PathLike | str,
    room: str,
    pacer: Pacer,
    *,
    poll: float = 1.0,
    max_notices: Optional[int] = None,
    clock=time.time,
    owner_actor: str = "owner",
    notice_ack: bool = True,
    close_source: bool = False,
) -> str:
    """Serve one bot connection until it ends; returns the reason.

    ``notice_ack`` (default, #703): send each notice with an ``id`` and keep its
    row until the bot's ``notice_ack`` / ``notice_nack`` for that id arrives on
    this connection; one notice in flight at a time. In-flight state lives only
    in this call — the row itself stays the oldest in the mirror, so the next
    connection resends it. ``notice_ack=False`` is the old write-then-delete path.

    ``close_source`` (ack mode only; ignored with ``notice_ack=False``): this
    lane owns the queue — it closes the outbox row of every acked, dropped or
    expired notice and runs ``policy.tick`` once per ``poll`` (see the module
    docstring, "Retiring the Matrix sender").

    ``max_notices`` stops after that many notices were settled — written
    (legacy) or acked/dropped (ack mode) — tests only.
    """
    return _Connection(conn, queue, room, pacer, poll=poll, max_notices=max_notices, clock=clock,
                       owner_actor=owner_actor, notice_ack=notice_ack,
                       close_source=close_source and notice_ack).run()


class _Connection:
    """One bot connection: the read loop (turns, acks) and the paced notice hand-off."""

    def __init__(self, conn: socket.socket, queue: os.PathLike | str, room: str, pacer: Pacer, *,
                 poll: float, max_notices: Optional[int], clock, owner_actor: str, notice_ack: bool,
                 close_source: bool = False) -> None:
        self.conn = conn
        self.queue = queue
        self.room = room
        self.pacer = pacer
        self.poll = poll
        self.max_notices = max_notices
        self.clock = clock
        self.owner_actor = owner_actor
        self.notice_ack = notice_ack
        self.close_source = close_source
        self.inbound = b""
        self.next_poll = 0.0
        self.next_policy = 0.0
        self.retry_at = 0.0
        self.handed = 0
        self.inflight: Optional[_InFlight] = None

    def _done(self) -> bool:
        return self.max_notices is not None and self.handed >= self.max_notices and self.inflight is None

    def _send(self, frame: str) -> None:
        self.conn.setblocking(True)
        self.conn.sendall(frame.encode("utf-8"))
        self.conn.setblocking(False)

    def run(self) -> str:
        self.conn.setblocking(False)
        while True:
            readable, _, _ = select.select([self.conn], [], [], self.poll)
            if readable:
                ended = self._read()
                if ended:
                    return ended
            if self._done():
                return "max notices reached"
            self._tick(self.clock())
            if self._done():
                return "max notices reached"

    def _read(self) -> Optional[str]:
        chunk = self.conn.recv(65536)
        if not chunk:
            return "bot disconnected"
        self.inbound += chunk
        if len(self.inbound) > MAX_FRAME_BYTES and b"\n" not in self.inbound:
            return "oversized frame"
        while b"\n" in self.inbound:
            line, self.inbound = self.inbound.split(b"\n", 1)
            text = line.decode("utf-8", "replace")
            settled = notice_answer(text)
            if settled is not None:
                self._settle(*settled)
                continue
            out_frame = answer(text, self.queue, owner_actor=self.owner_actor)
            if out_frame:
                self._send(out_frame)
                logging.info("answered a fleet-alert-room turn with an empty result")
        return None

    def _settle(self, kind: str, notice_id: str, detail: str) -> None:
        current = self.inflight
        if current is None or notice_id != current.notice_id:
            logging.info("ignored a notice_%s for a notice not in flight", kind)
            return
        self.inflight = None
        if kind == "ack":
            self._finish(current, "hande-ack", detail, CLOSED_ACK_PREFIX + current.notice_id)
            logging.info("한데 fleet alert posted txn=%s seq=%s", current.txn, detail or "?")
        elif detail in NACK_RETRY_LATER:
            self.retry_at = self.clock() + RATE_RETRY_SECONDS
            logging.warning("한데 fleet alert deferred by the bot (%s) txn=%s; retry in %ds",
                            detail, current.txn, int(RATE_RETRY_SECONDS))
        elif detail in NACK_RESEND:
            self.retry_at = self.clock() + RESEND_DELAY_SECONDS
            logging.warning("한데 fleet alert not stored by the bot (%s) txn=%s; will resend", detail, current.txn)
        else:
            self._finish(current, "hande-dropped", detail, CLOSED_DROPPED_PREFIX + detail)
            logging.warning("한데 fleet alert dropped by the bot (%s) txn=%s", detail, current.txn)

    def _finish(self, current: _InFlight, receipt_kind: str, ref: str, close_marker: str) -> None:
        forget(self.queue, current.row_id)
        _receipt(self.queue, current.txn, receipt_kind, ref)
        if self.close_source:
            try:
                close_source_row(self.queue, current.txn, close_marker, self.clock())
            except Exception:  # the 24h sweep closes it later; never lose the connection over it
                logging.warning("fleet alert source row not closed txn=%s", current.txn)
        self.handed += 1
        self.next_poll = 0.0  # settled: the next row may go out at once (pacer permitting)

    def _tick(self, now: float) -> None:
        if self.close_source and now >= self.next_policy:
            # Matrix sender retired: the tier policy (digest, re-notify) runs here.
            # Its rows are non-info inserts, so the mirror hands them to this loop.
            self.next_policy = now + self.poll
            policy.tick(self.queue, now)
        if self.inflight is not None:
            # Never resend on this connection while the ack is pending: the bot
            # keeps retrying its relay POST and a resend would post twice.
            if not self.inflight.warned and now - self.inflight.sent_at >= ACK_WARN_SECONDS:
                self.inflight.warned = True
                logging.warning("no notice_ack after %ds txn=%s (bot without acks? see --no-notice-ack)",
                                int(now - self.inflight.sent_at), self.inflight.txn)
            return
        if self._done() or now < self.next_poll or now < self.retry_at or not self.pacer.ready(now):
            return
        self.next_poll = now + self.poll
        item = next_alert(self.queue, now, close_source=self.close_source)
        if item is None:
            return
        row_id, txn, body = item
        if not body.strip():
            forget(self.queue, row_id)
            self.next_poll = 0.0  # look at the next row right away
            return
        if not self.notice_ack:
            self._send(notice_frame(self.room, body))
            self.pacer.mark(now)
            forget(self.queue, row_id)
            # Weak receipt (#2413): the frame left for the bot; no ack in this mode.
            _receipt(self.queue, txn, "socket-write", "")
            self.handed += 1
            logging.info("한데 fleet alert handed to the bot txn=%s", txn)
            return
        notice_id = notice_id_of(row_id, txn)
        self._send(notice_frame(self.room, body, notice_id))
        self.pacer.mark(now)
        self.inflight = _InFlight(row_id, txn, notice_id, now)
        # Weak until the bot's notice_ack replaces it with ``hande-ack`` (strong).
        _receipt(self.queue, txn, "socket-write", "")
        logging.info("한데 fleet alert handed to the bot txn=%s (awaiting ack)", txn)


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
    parser.add_argument("--owner-actor", default="owner", help="relay actor whose devices may reply ok/hold <id>")
    parser.add_argument("--no-notice-ack", action="store_true",
                        help="old bot without notice_ack (family-messenger#703): send without id, delete on write")
    parser.add_argument("--close-source", action="store_true",
                        help="Matrix sender retired: close acked/dropped/expired outbox rows here and run the"
                             " tier policy (09:00 digest, re-notify) in this lane; ack mode only, default off")
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
    if args.close_source and args.no_notice_ack:
        parser.error("--close-source needs ack mode (drop --no-notice-ack)")
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
    logging.info("notice ack mode: %s; outbox rows closed and tier policy run by %s",
                 "off (--no-notice-ack)" if args.no_notice_ack else "on",
                 "this lane (--close-source)" if args.close_source else "the Matrix sender")
    while True:
        conn, _ = server.accept()
        logging.info("fleet alert bot connected")
        try:
            reason = serve_connection(conn, args.queue, args.room, pacer, owner_actor=args.owner_actor,
                                      notice_ack=not args.no_notice_ack, close_source=args.close_source)
        except OSError as exc:
            reason = type(exc).__name__
        finally:
            conn.close()
        logging.warning("fleet alert bot connection ended: %s", reason)


if __name__ == "__main__":
    raise SystemExit(main())
