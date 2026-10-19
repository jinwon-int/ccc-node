"""「한데」 transport: the bridge as the native bot's responder (#2380 PR-2).

Speaks the FLEET-WORKER port (family-messenger ``docs/FLEET-WORKER.md``) as a
unix-socket **server**. The native bot (``native-mls-bot``) connects once per
process, sends one ``turn`` at a time and reads JSON lines back; a closed
stream is ``worker_gone`` on its side and systemd restarts it, after which it
reconnects here. So this transport accepts one client at a time and keeps
listening across client lifetimes.

Frames in (from the bot)::

    {"type":"turn","turn_id":"t-12","prompt":"…","session_id":null,
     "sender":"@owner-pc:hande","room_kind":"family"[,"room":"agent"]}
    {"type":"approve"|"deny","turn_id":"t-12","approval_id":"<nonce>"}
    {"type":"cancel","turn_id":"t-12"}
    {"type":"tool-result", …}            # steward tools: not served here

Frames out (to the bot)::

    {"type":"session","turn_id":…,"session_id":…}
    {"type":"approval","turn_id":…,"approval_id":…,"action":"approval",
     "description":<redacted snapshot>,"arguments":{}}
    {"type":"approval-resolved","turn_id":…,"approval_id":…,"allowed":bool}
    {"type":"control","turn_id":…,"accepted":bool}
    {"type":"rejected","turn_id":…,"reason":…}
    {"type":"result","turn_id":…,"status":"complete"|"uncertain",
     "text":…,"session_id":…[,"reason":…]}

Limits follow the port: 64 KiB per inbound line, 512 KiB per outbound line,
turn ids ``[A-Za-z0-9_-]{1,128}``. Anything malformed ends the connection
(the port says so; the bot restarts and reconnects). The bot's own inbox is
the duplicate guard, not this port — a turn id seen twice while a turn is
active is ``busy``; after a bridge restart the bot re-offers the same seq and
the bridge runs it (the bot marks it ``uncertain`` first if it had started).

Implements the surface ``MatrixBot`` drives: ``open``/``run``/``close``,
``enqueue_notice``, ``room_kind``, ``policy.rooms``, ``health_signals``,
``active``. ``TurnSink``/``TurnRunner``/``TurnResult`` are the
``telegram_bot.contracts.transport`` shapes (PR-1).
"""

from __future__ import annotations

import asyncio
from collections import deque
import grp
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import time
from types import SimpleNamespace
from typing import Any, Mapping

from telegram_bot.contracts.transport import TurnResult, TurnRunner

from .state import DEFAULT_SOCKET_MODE, ROOM_PATTERN, sender_identity

logger = logging.getLogger(__name__)

MAX_IN_FRAME_BYTES = 64 * 1024
MAX_OUT_FRAME_BYTES = 512 * 1024
TURN_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
# The bot bounds replies itself (reply_max_bytes, 8 KiB by default); this is the
# port ceiling so a huge reply never ends the connection.
MAX_RESULT_TEXT_BYTES = 256 * 1024
NOTICE_DROP_LOG = 32
PROTOCOL = "fleet-worker/1"


def _frame_bytes(frame: Mapping[str, Any]) -> bytes:
    return (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _bounded_text(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    cut = data[:limit].decode("utf-8", errors="ignore")
    return cut + "\n…(truncated)"


class _Client:
    """One connected bot process."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.connected_at = time.monotonic()
        self.last_frame_at = self.connected_at
        self.send_lock = asyncio.Lock()
        self.closed = False

    async def send(self, frame: Mapping[str, Any]) -> bool:
        data = _frame_bytes(frame)
        if len(data) > MAX_OUT_FRAME_BYTES:
            logger.error("hande frame too large to send kind=%s", frame.get("type"))
            return False
        async with self.send_lock:
            if self.closed:
                return False
            try:
                self.writer.write(data)
                await asyncio.wait_for(self.writer.drain(), 5)
            except (OSError, asyncio.TimeoutError, ConnectionError):
                self.closed = True
                return False
        return True

    async def close(self) -> None:
        self.closed = True
        try:
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), 2)
        except Exception:
            pass


class _ActiveTurn:
    def __init__(self, turn_id: str, job: Mapping[str, Any]) -> None:
        self.turn_id = turn_id
        self.job = job
        self.task: asyncio.Task[Any] | None = None
        self.approvals: dict[str, asyncio.Future[bool]] = {}
        self.cancelled = False


class _HandeSink:
    """``TurnSink`` bound to one turn on one client; inert once that turn is over."""

    def __init__(self, transport: "HandeTransport", client: _Client, turn: _ActiveTurn) -> None:
        self._transport = transport
        self._client = client
        self._turn = turn

    def _live(self) -> bool:
        return self._transport._active is self._turn and not self._client.closed

    async def typing(self) -> None:
        return None

    async def status(self, text: str | None) -> None:
        del text
        return None

    async def interim(self, text: str) -> None:
        if not self._live():
            return
        if not self._transport.interim_frames:
            self._transport._dropped("interim")
            return
        await self._client.send(
            {"type": "interim", "turn_id": self._turn.turn_id, "text": _bounded_text(text, 8192)}
        )

    async def approval_outcome(self, text: str) -> str:
        """``allow``/``deny``/``timeout``/``unavailable`` (what ``MatrixBot._ask_approval`` prefers)."""

        if not self._live():
            return "unavailable"
        approval_id = secrets.token_urlsafe(18)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._turn.approvals[approval_id] = future
        frame = {
            "type": "approval",
            "turn_id": self._turn.turn_id,
            "approval_id": approval_id,
            "action": "approval",
            "description": text,
            "arguments": {},
        }
        if len(_frame_bytes(frame)) > 60_000:
            # Never truncate what the owner is asked to approve.
            self._turn.approvals.pop(approval_id, None)
            await self._client.send(
                {
                    "type": "approval-denied",
                    "turn_id": self._turn.turn_id,
                    "reason": "approval-too-large",
                }
            )
            return "deny"
        if not await self._client.send(frame):
            self._turn.approvals.pop(approval_id, None)
            return "unavailable"
        try:
            allowed = await asyncio.wait_for(future, self._transport.approval_timeout_s)
        except asyncio.TimeoutError:
            await self._client.send(
                {
                    "type": "approval-resolved",
                    "turn_id": self._turn.turn_id,
                    "approval_id": approval_id,
                    "allowed": False,
                }
            )
            return "timeout"
        except asyncio.CancelledError:
            raise
        finally:
            self._turn.approvals.pop(approval_id, None)
        return "allow" if allowed else "deny"

    async def approval(self, description: str, arguments: Any) -> bool:
        del arguments  # MatrixBot passes the redacted snapshot as the description
        return await self.approval_outcome(description) == "allow"


class HandeTransport:
    """FLEET-WORKER port server; see the module docstring for the frame mapping."""

    def __init__(
        self,
        config: Mapping[str, Any],
        runner: TurnRunner,
        *,
        socket_path: str | os.PathLike[str] | None = None,
        listen_fd: int | None = None,
    ) -> None:
        self.c = dict(config)
        self.runner = runner
        self.rooms: list[str] = list(self.c["rooms"])
        self.family_rooms = frozenset(self.c.get("family_rooms") or ())
        self.allowed_users = frozenset([self.c["owner"], *(self.c.get("family_users") or ())])
        self.account = str(self.c["account"])
        self.approval_timeout_s = int(self.c.get("approval_timeout_s") or 120)
        self.interim_frames = bool(self.c.get("interim_frames"))
        self.notice_frames = bool(self.c.get("notice_frames"))
        self.socket_path = Path(socket_path or self.c.get("socket_path") or "").expanduser()
        self.listen_fd = listen_fd if listen_fd is not None else _systemd_listen_fd()
        self.socket_mode = int(self.c.get("socket_mode") or DEFAULT_SOCKET_MODE)
        self.socket_group = self.c.get("socket_group")
        # What MatrixBot reads off ``transport.policy``: the room → mode map.
        self.policy = SimpleNamespace(
            rooms={
                room: ("mention" if room in self.family_rooms else "direct") for room in self.rooms
            }
        )
        self._server: asyncio.base_events.Server | None = None
        self._client: _Client | None = None
        self._active: _ActiveTurn | None = None
        self._stopping = asyncio.Event()
        self._notice_keys: dict[str, str] = {}
        self._notices_dropped = 0
        self._recent_drops: deque[str] = deque(maxlen=NOTICE_DROP_LOG)
        self._turns_served = 0
        self._turns_uncertain = 0
        self._clients_seen = 0

    # -- lifecycle -----------------------------------------------------------

    async def open(self, initialize: bool = False) -> None:
        del initialize
        if self._server is not None:
            return
        if self.listen_fd is not None:
            sock = socket.socket(fileno=self.listen_fd)
            if sock.family != socket.AF_UNIX:
                raise RuntimeError("hande: LISTEN_FDS socket is not AF_UNIX")
            self._server = await asyncio.start_unix_server(
                self._serve_client, sock=sock, limit=MAX_IN_FRAME_BYTES
            )
            return
        if not str(self.socket_path):
            raise RuntimeError("hande: socket_path (or LISTEN_FDS) is required")
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            if not stat.S_ISSOCK(self.socket_path.lstat().st_mode):
                raise RuntimeError(f"hande: {self.socket_path} exists and is not a socket")
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._serve_client, path=str(self.socket_path), limit=MAX_IN_FRAME_BYTES
        )
        os.chmod(self.socket_path, self.socket_mode)
        if self.socket_group:
            os.chown(self.socket_path, -1, grp.getgrnam(str(self.socket_group)).gr_gid)

    async def run(self) -> None:
        if self._server is None:
            await self.open()
        assert self._server is not None
        async with self._server:
            await self._stopping.wait()

    async def close(self) -> None:
        self._stopping.set()
        if self._active is not None and self._active.task is not None:
            self._active.task.cancel()
        if self._client is not None:
            await self._client.close()
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 5)
            except Exception:
                pass
            self._server = None
        if self.listen_fd is None and self.socket_path and self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

    # -- surface MatrixBot drives ---------------------------------------------

    @property
    def active(self) -> bool:
        return self._active is not None

    def room_kind(self, room_id: str) -> str:
        return "family" if room_id in self.family_rooms else "direct"

    def enqueue_notice(
        self, room_id: str, text: str, *, key: str | None = None, msgtype: str = "m.text"
    ) -> str:
        """Unsolicited output. Delivered only when the bot speaks the ``notice`` frame.

        Returns an opaque id either way; a dropped notice is counted in
        ``health_signals`` (``notices_dropped``) so nothing is claimed sent.
        """

        del msgtype
        if room_id not in self.rooms:
            raise ValueError("room-not-allowed")
        if key is not None:
            if self._notice_keys.get(key) == text:
                return key
            self._notice_keys[key] = text
        client = self._client
        if self.notice_frames and client is not None and not client.closed:
            frame = {"type": "notice", "room": room_id, "text": _bounded_text(text, 8192)}
            asyncio.get_running_loop().create_task(client.send(frame))
            return key or f"notice-{time.time_ns()}"
        self._dropped("notice")
        return key or f"dropped-{time.time_ns()}"

    def health_signals(self, now: float | None = None) -> dict[str, Any]:
        del now
        now_mono = time.monotonic()
        client = self._client
        sync_age: float | None = None
        client_age: float | None = None
        connected = False
        if client is not None and not client.closed:
            connected = True
            sync_age = now_mono - client.last_frame_at
            client_age = now_mono - client.connected_at
        return {
            "protocol": PROTOCOL,
            "connected": connected,
            "sync_age_s": sync_age,
            "client_age_s": client_age,
            "clients_seen": self._clients_seen,
            "active_turn": self._active.turn_id if self._active else None,
            "turns_served": self._turns_served,
            "turns_uncertain": self._turns_uncertain,
            "notices_dropped": self._notices_dropped,
            "notice_frames": self.notice_frames,
            "receive_failures": 0,
            "send_failures": 0,
        }

    # -- client handling -------------------------------------------------------

    def _dropped(self, kind: str) -> None:
        self._notices_dropped += 1
        self._recent_drops.append(kind)
        logger.info("hande %s dropped: bot does not speak that frame yet", kind)

    async def _serve_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._client is not None and not self._client.closed:
            # The port is one bot per responder. A second connection while the
            # first is alive is refused; the first keeps its turn.
            logger.warning("hande: second client refused while one is connected")
            writer.close()
            return
        client = _Client(reader, writer)
        self._client = client
        self._clients_seen += 1
        logger.info("hande: bot connected")
        try:
            while not self._stopping.is_set():
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.LimitOverrunError:
                    logger.error("hande: inbound frame over %d bytes; closing", MAX_IN_FRAME_BYTES)
                    break
                except (asyncio.IncompleteReadError, ConnectionError, OSError):
                    break
                if len(line) > MAX_IN_FRAME_BYTES:
                    logger.error("hande: inbound frame over %d bytes; closing", MAX_IN_FRAME_BYTES)
                    break
                client.last_frame_at = time.monotonic()
                try:
                    frame = json.loads(line.decode("utf-8"))
                except ValueError:
                    logger.error("hande: invalid JSON frame; closing")
                    break
                if not isinstance(frame, dict):
                    logger.error("hande: non-object frame; closing")
                    break
                if not await self._dispatch(client, frame):
                    break
        finally:
            logger.info("hande: bot disconnected")
            client.closed = True
            await self._abandon_turn("worker_gone")
            if self._client is client:
                self._client = None
            await client.close()

    async def _dispatch(self, client: _Client, frame: Mapping[str, Any]) -> bool:
        kind = frame.get("type")
        turn_id = frame.get("turn_id")
        if turn_id is not None and (
            not isinstance(turn_id, str) or not TURN_ID_PATTERN.fullmatch(turn_id)
        ):
            await client.send({"type": "rejected", "reason": "invalid-turn-id"})
            return True
        if kind == "turn":
            return await self._on_turn(client, frame)
        if kind in ("approve", "deny"):
            return await self._on_decision(client, frame, kind == "approve")
        if kind == "cancel":
            return await self._on_cancel(client, frame)
        if kind == "tool-result":
            await client.send({"type": "control", "turn_id": turn_id or "", "accepted": False})
            return True
        await client.send(
            {"type": "rejected", "turn_id": turn_id or "", "reason": "unknown-command"}
        )
        return True

    def _job_for(self, frame: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
        turn_id = frame.get("turn_id")
        prompt = frame.get("prompt")
        if not isinstance(turn_id, str) or not isinstance(prompt, str):
            return None, "invalid-turn"
        sender = sender_identity(str(frame.get("sender") or ""))
        if sender is None:
            return None, "invalid-turn"
        if sender not in self.allowed_users:
            return None, "sender-not-allowed"
        room = frame.get("room")
        if room is None:
            room = self.rooms[0]
        if not isinstance(room, str) or not ROOM_PATTERN.fullmatch(room) or room not in self.rooms:
            return None, "room-not-allowed"
        device = str(frame.get("sender") or "")[1:].split(":", 1)[0]
        job = {
            "event_id": turn_id,
            "room_id": room,
            "sender": sender,
            "device": device,
            "body": prompt,
            "scope": "hande",
        }
        return job, ""

    async def _on_turn(self, client: _Client, frame: Mapping[str, Any]) -> bool:
        turn_id = str(frame.get("turn_id") or "")
        if self._active is not None:
            await client.send({"type": "rejected", "turn_id": turn_id, "reason": "busy"})
            return True
        job, reason = self._job_for(frame)
        if job is None:
            await client.send({"type": "rejected", "turn_id": turn_id, "reason": reason})
            return True
        turn = _ActiveTurn(turn_id, job)
        self._active = turn
        turn.task = asyncio.get_running_loop().create_task(self._run_turn(client, turn))
        return True

    async def _run_turn(self, client: _Client, turn: _ActiveTurn) -> None:
        sink = _HandeSink(self, client, turn)
        room_kind = self.room_kind(str(turn.job["room_id"]))
        result: TurnResult | None = None
        reason = ""
        try:
            result = await self.runner.run(
                turn.job, sink=sink, session_id=None, room_kind=room_kind
            )
        except asyncio.CancelledError:
            reason = "cancelled" if turn.cancelled else "worker_gone"
        except Exception as error:
            logger.error("hande turn failed: %s", type(error).__name__)
            reason = "error"
        finally:
            for future in list(turn.approvals.values()):
                if not future.done():
                    future.set_result(False)
            if self._active is turn:
                self._active = None
        self._turns_served += 1
        if result is not None and getattr(result, "status", "complete") == "complete":
            text = _bounded_text(str(getattr(result, "text", "") or ""), MAX_RESULT_TEXT_BYTES)
            session_id = getattr(result, "session_id", None)
            if session_id:
                await client.send(
                    {"type": "session", "turn_id": turn.turn_id, "session_id": session_id}
                )
            await client.send(
                {
                    "type": "result",
                    "turn_id": turn.turn_id,
                    "status": "complete",
                    "text": text,
                    "session_id": session_id,
                }
            )
        else:
            self._turns_uncertain += 1
            if result is not None:
                reason = reason or str(getattr(result, "status", "uncertain"))
            await client.send(
                {
                    "type": "result",
                    "turn_id": turn.turn_id,
                    "status": "uncertain",
                    "reason": reason or "uncertain",
                    "session_id": getattr(result, "session_id", None)
                    if result is not None
                    else None,
                }
            )
        self._hook("turn_closed", turn.job)
        self._hook("delivered", turn.job)

    def _hook(self, name: str, job: Mapping[str, Any]) -> None:
        hook = getattr(self.runner, name, None)
        if not callable(hook):
            return

        async def _call() -> None:
            try:
                await hook(job)
            except Exception:
                logger.debug("hande %s hook failed", name, exc_info=True)

        asyncio.get_running_loop().create_task(_call())

    async def _on_decision(self, client: _Client, frame: Mapping[str, Any], allow: bool) -> bool:
        turn_id = str(frame.get("turn_id") or "")
        approval_id = frame.get("approval_id")
        turn = self._active
        found = (
            turn is not None
            and turn.turn_id == turn_id
            and isinstance(approval_id, str)
            and approval_id in turn.approvals
            and not turn.approvals[approval_id].done()
        )
        await client.send({"type": "control", "turn_id": turn_id, "accepted": bool(found)})
        if found:
            assert turn is not None and isinstance(approval_id, str)
            turn.approvals[approval_id].set_result(allow)
            await client.send(
                {
                    "type": "approval-resolved",
                    "turn_id": turn_id,
                    "approval_id": approval_id,
                    "allowed": allow,
                }
            )
        return True

    async def _on_cancel(self, client: _Client, frame: Mapping[str, Any]) -> bool:
        turn_id = str(frame.get("turn_id") or "")
        turn = self._active
        found = turn is not None and turn.turn_id == turn_id
        await client.send({"type": "control", "turn_id": turn_id, "accepted": bool(found)})
        if found:
            assert turn is not None
            turn.cancelled = True
            for future in turn.approvals.values():
                if not future.done():
                    future.set_result(False)
            try:
                await self.runner.cancel(turn.job)
            except Exception:
                logger.debug("hande runner.cancel failed", exc_info=True)
            if turn.task is not None and not turn.task.done():
                turn.task.cancel()
        return True

    async def _abandon_turn(self, reason: str) -> None:
        turn = self._active
        if turn is None:
            return
        logger.warning("hande: client gone during turn %s (%s)", turn.turn_id, reason)
        for future in turn.approvals.values():
            if not future.done():
                future.set_result(False)
        try:
            await self.runner.cancel(turn.job)
        except Exception:
            logger.debug("hande runner.cancel failed", exc_info=True)
        if turn.task is not None and not turn.task.done():
            turn.task.cancel()


def _systemd_listen_fd() -> int | None:
    """fd 3 when systemd handed exactly one socket to this process (``LISTEN_FDS``)."""

    try:
        if int(os.environ.get("LISTEN_PID", "0")) != os.getpid():
            return None
        if int(os.environ.get("LISTEN_FDS", "0")) != 1:
            return None
    except ValueError:
        return None
    return 3
