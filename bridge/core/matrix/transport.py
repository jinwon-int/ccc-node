"""Persistent encrypted Matrix transport: owner direct rooms plus mention-gated family rooms.

Port of the ``family-messenger`` pilot's ``fleet_matrix.Frontend``. The
``/sync`` loop, device pinning, room gates, admission, chunked encrypted
delivery, control commands and the uncertain-work rule are unchanged. What
changed (#1780 PR-2a): the pilot ran every turn in a subprocess worker that
spoke JSON frames over stdin/stdout; here a turn is executed in-process by an
injected :class:`TurnRunner`, which receives a :class:`TurnSink` for typing
indicators, interim notices and approval prompts.

Turn outcomes:

* ``TurnResult(status="complete")`` — :meth:`MatrixStore.finish` records the
  reply (an empty reply completes the job without an outbox delivery).
* anything else — a ``TurnResult(status="uncertain")``, a runner exception,
  the configured turn timeout, or a ``/cancel`` — ends the job with a short
  notice ("중단했습니다 / 시간 제한 / 오류 … 다시 보내 주세요") and the loop
  keeps serving, exactly like the Telegram bridge. Nothing is re-run.
* a turn interrupted by a service stop/restart is left *uncertain* by the
  dying process and resolved on the next start with a "재시작으로 끊겼습니다,
  다시 보내 주세요" notice (Telegram's RESTART_INTERRUPT_NOTICE) — no
  ``/ack`` gate any more (owner 2026-09-18: the pilot's acknowledgement step
  was unusable in practice). ``/ack`` stays accepted as a no-op courtesy and
  :meth:`MatrixStore.unblock` remains for operators.

``nio`` and ``aiohttp`` are imported lazily inside the methods that use them.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import copy
from dataclasses import dataclass, replace
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Any, Mapping, Protocol
from urllib.parse import quote

from telegram_bot.core.matrix.state import (
    MAX_REPLY_BYTES,
    MAX_TEXT_BYTES,
    MatrixStore,
    Policy,
    QueueFull,
    Request,
    SafetyStop,
    bounded_text,
    family_config,
    identities,
    mention_aliases,
    private_directory,
    reply_context_body,
    saved_policy,
    scope_of,
    turn_id,
    turn_timeout_minutes,
    upgrade_saved_policy,
    wake_words,
)

logger = logging.getLogger(__name__)

FAMILY_NOTICE = "이 AI는 이 방을 읽을 수 있으며 답변에 필요한 내용이 제공업체에 전달될 수 있습니다."
NOTICE_QUEUE_FULL = "대기 중인 요청이 많습니다. 잠시 후 다시 요청해 주세요."
NOTICE_QUEUED = "⏳ 이 메시지는 대기 순번 {position}번에 저장되었으며 도착 순서대로 처리됩니다."
NOTICE_ACKED = "이전 작업의 결과 확인을 완료한 것으로 기록했습니다. 자동 재실행은 하지 않습니다."
NOTICE_CONTROL_FORWARDED = "요청을 전달했습니다. 실제 처리 결과는 이어지는 안내를 확인해 주세요."
NOTICE_INVALID_CONTROL = "현재 이 대화방에서 처리할 수 있는 제어 요청이 아닙니다. 작업 번호와 승인 번호를 확인해 주세요."
NOTICE_UNCERTAIN = (
    "작업이 중단되어 결과 확인이 필요합니다. 자동으로 다시 실행하지 않습니다.\n"
    "결과를 확인한 뒤 다음 명령으로 대기를 해제할 수 있습니다:\n/ack "
)  # legacy text kept for the operator unblock audit; no longer posted to rooms
NOTICE_RESTARTED = "⏳ 답변 중에 서비스가 재시작되어 마지막 답변이 끊겼습니다. 메시지를 다시 보내 주세요."
NOTICE_CANCELLED = "⏹ 요청대로 작업을 중단했습니다."
NOTICE_TIMEOUT = "⏳ 시간 제한({minutes}분)을 넘겨 작업을 중단했습니다. 요청을 나눠서 다시 보내 주세요."


def _timeout_label(turn_timeout: float) -> str:
    """Render the ceiling for the interrupted-turn notice (6시간, not 360분)."""
    minutes = round(turn_timeout / 60)
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60}시간"
    return f"{minutes}분"
NOTICE_TURN_ERROR = "❌ 처리 중 오류가 나서 답변을 만들지 못했습니다. 잠시 후 다시 보내 주세요."
# The pilot posted "작업을 시작했습니다. 취소 명령: /cancel <turn>" at every turn
# start because it had no typing indicator. This frontend shows typing plus
# throttled progress notices and accepts a bare "/stop", so the notice was
# dropped (owner request 2026-09-18). "/cancel <turn id>" still works; the
# turn id is visible in the uncertain/ack notice when it matters.

NOTICE_UNDECRYPTABLE = (
    "이 메시지의 암호 키를 받지 못해 읽을 수 없었습니다. 다시 보내 주세요. "
    "(봇 기기가 만들어지기 전에 보낸 메시지는 복구할 수 없습니다.)"
)

NOTICE_UNTRUSTED_DEVICE = (
    "검증되지 않은 기기에서 보낸 메시지는 처리하지 않습니다. "
    "그 기기에서 기기 검증(이모지 비교)을 마친 뒤 다시 보내 주세요."
)

NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,64}")
APPROVAL_TIMEOUT_S = 120.0
TURN_JOIN_TIMEOUT_S = 30.0
MAX_APPROVAL_TEXT_BYTES = 12_000
MAX_PENDING_APPROVALS = 16
MEGOLM = "m.megolm.v1.aes-sha2"
SYNC_TIMELINE_LIMIT = 100  # /sync filter timeline.limit; a batch this full is a real gap
CONTROL_PREFIXES = ("/approve", "/deny", "/cancel", "/ack", "/stop")
RECENT_TEXT_CAP = 512  # recent trusted room texts kept to resolve reply parents (#1943)
# work()/send() wake on an event as soon as a job or reply is stored; this
# poll is only the fallback for writers that do not signal (other processes).
IDLE_POLL_S = 0.25
TURN_TIMING_CAP = 64  # in-flight per-turn latency records kept in memory
TURN_TIMINGS_KEPT = 50  # finished records kept in meta.turn_timings
# TurnResult.status values that end a job with its text delivered. "error" is
# what MatrixBot reports for a ChatResponse(success=False): the text is the
# user-facing failure notice and nothing is left running, so it is not
# "uncertain" (which needs an operator /ack).
FINAL_STATUSES = frozenset({"complete", "error"})


def message_content(text: str) -> dict[str, str]:
    """``m.text`` content with a Matrix-HTML ``formatted_body`` when the text has markup.

    Rendering happens at send time so every outgoing message (replies and
    notices alike) goes through the same escaping renderer; plain text stays
    a bare ``body``.
    """

    from telegram_bot.core.matrix.render import render_matrix_message

    body, formatted = render_matrix_message(text)
    content = {"msgtype": "m.text", "body": body}
    if formatted:
        content["format"] = "org.matrix.custom.html"
        content["formatted_body"] = formatted
    return content


# --------------------------------------------------------------------------- #
# Runner contract
# --------------------------------------------------------------------------- #


class TurnSink(Protocol):
    """What a running turn may do to its room while it runs."""

    async def typing(self) -> None:
        """Best-effort typing indicator (PUT /typing, 8s); errors are swallowed."""
        ...

    async def interim(self, text: str) -> None:
        """Durable progress notice, delivered by the outbox like any reply."""
        ...

    async def status(self, text: str | None) -> None:
        """Progress bubble: created once per turn, edited in place, redacted when done."""
        ...

    async def approval(self, description: str, arguments: Any) -> bool:
        """Ask the room; True only when ``/approve <turn> <nonce>`` arrives in time."""
        ...


@dataclass(frozen=True)
class TurnResult:
    text: str
    session_id: str | None
    streamed: bool = False
    status: str = "complete"  # or "uncertain"


class TurnRunner(Protocol):
    async def run(
        self,
        job: Mapping[str, Any],
        *,
        sink: TurnSink,
        session_id: str | None,
        room_kind: str,
    ) -> TurnResult: ...

    async def cancel(self, job: Mapping[str, Any]) -> bool: ...


def unique_key(prefix: str) -> str:
    return f"{prefix}-{time.time_ns()}-{secrets.token_hex(8)}"


class _RoomSink:
    """TurnSink bound to one claimed job; inert once that turn is no longer active."""

    def __init__(self, transport: MatrixTransport, job: Mapping[str, Any]) -> None:
        self.transport = transport
        self.job = job
        self.request = transport.as_request(job)
        self.tid = turn_id(job["event_id"])
        self._bubble: str | None = None  # this turn's progress message event id
        self._bubble_tail: str | None = None  # latest event representing the bubble (bubble or its last edit)

    def _active(self) -> bool:
        return self.transport.active is self.job

    async def typing(self) -> None:
        if not self._active():
            return
        try:
            await self.transport.send_typing(self.request.room_id)
        except Exception:
            return
        self.transport.mark(self.job["event_id"], "typing")

    async def interim(self, text: str) -> None:
        if not self._active() or not isinstance(text, str) or not text.strip():
            return
        self.transport.store.notice(self.request, unique_key("interim"), text)
        self.transport.wake()

    async def status(self, text: str | None) -> None:
        """One progress bubble per turn: created, then refreshed at the room's bottom.

        Telegram edits its status bubble; Matrix edits too (m.replace), but an
        edit keeps the original timeline position — once any other event lands
        after the bubble it would stay buried. So: while the bubble is still
        the newest event in the room, refresh via edit; when it has been
        buried, redact and repost at the bottom (owner request 2026-09-18).
        ``None`` redacts the bubble when the answer replaces it. Cosmetic
        only: like typing, this is a direct send outside the durable outbox,
        so a crash may leave a stale bubble behind.
        """
        if not self._active():
            return
        transport = self.transport
        room = self.request.room_id
        if text is None:
            bubble, self._bubble = self._bubble, None
            self._bubble_tail = None
            if bubble is None:
                return
            try:
                await transport.redact(room, bubble, "status-" + self.tid)
            except Exception:
                pass  # cosmetic cleanup; the answer itself went through the outbox
            return
        if not isinstance(text, str) or not text.strip():
            return
        bounded_text(text, MAX_REPLY_BYTES)
        async with transport.matrix_lock:
            txn = hashlib.sha256(("status-" + self.tid + ":" + str(time.time_ns())).encode()).hexdigest()
            latest = transport.last_room_event.get(room)
            if self._bubble is None:
                self._bubble = await transport.encrypted_send(room, text, txn)
                self._bubble_tail = self._bubble
                transport.last_room_event[room] = self._bubble
            elif latest in (self._bubble, self._bubble_tail):
                # Still the newest event: an edit is enough (no new event id churn).
                self._bubble_tail = await transport.encrypted_edit(room, text, self._bubble, txn)
                transport.last_room_event[room] = self._bubble_tail
            else:
                # Buried by later events: redact and repost at the bottom.
                try:
                    await transport.redact(room, self._bubble, "status-" + self.tid + "-move")
                except Exception:
                    pass  # the repost below still lands; a redact failure is cosmetic
                self._bubble = await transport.encrypted_send(room, text, txn)
                self._bubble_tail = self._bubble
                transport.last_room_event[room] = self._bubble

    async def approval(self, description: str, arguments: Any) -> bool:
        transport = self.transport
        if not self._active():
            return False
        text = str(description) + "\n" + json.dumps(arguments, ensure_ascii=False, default=str)
        if len(text.encode()) > MAX_APPROVAL_TEXT_BYTES or len(transport.approvals) >= MAX_PENDING_APPROVALS:
            return False
        nonce = secrets.token_urlsafe(24)
        if not NONCE_PATTERN.fullmatch(nonce) or nonce in transport.approvals:
            return False
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        transport.approvals[nonce] = future
        try:
            transport.store.notice(
                self.request,
                "approval-" + nonce,
                text + "\n승인: /approve " + self.tid + " " + nonce + "\n거절: /deny " + self.tid + " " + nonce,
            )
            async with asyncio.timeout(transport.approval_timeout):
                return await future
        except TimeoutError:
            return False
        finally:
            transport.approvals.pop(nonce, None)


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


class MatrixTransport:
    def __init__(
        self,
        config: dict[str, Any],
        runner: TurnRunner,
        *,
        approval_timeout: float = APPROVAL_TIMEOUT_S,
        turn_timeout: float | None = None,
    ) -> None:
        self.c = config
        self.runner = runner
        self.approval_timeout = approval_timeout
        # Config ceiling (default 20 min, up to 6 h); explicit tests still win.
        self.turn_timeout = (
            turn_timeout if turn_timeout is not None else turn_timeout_minutes(config) * 60.0
        )
        # Family settings are a trust boundary; reject them before opening state.
        self.family_rooms, self.family_users, self.family_devices = family_config(config)
        # Trust model per user (#149): a user with a pinned cross-signing
        # identity trusts every self-signed device; anyone else keeps the
        # pinned device set. ``trusted`` (user -> device -> curve25519) is the
        # single table every send/admit decision reads; pin_devices fills it.
        self.identities = identities(config)
        self.pins: dict[str, dict[str, dict[str, str]]] = {
            user: pins
            for user, pins in {config["owner"]: config["devices"], **self.family_devices}.items()
            if user not in self.identities and pins
        }
        self.trusted: dict[str, dict[str, str]] = {user: {d: k["curve25519"] for d, k in pins.items()} for user, pins in self.pins.items()}
        self.last_room_event: dict[str, str] = {}  # newest event id seen per room (drives status-bubble placement)
        # event id -> (room, sender, text) of recent trusted decrypted texts,
        # our own sent chunks included; resolves reply parents without a fetch.
        self.recent_text: OrderedDict[str, tuple[str, str, str]] = OrderedDict()
        self.senders = frozenset([config["owner"]]) | self.family_users
        self.family_allowed = self.senders | frozenset([config["account"]])
        self.store = MatrixStore(config["state_directory"], config["account"])
        # A saved inbox must never be silently rerouted by editing configuration.
        policy = saved_policy(config)
        old = self.store.get_meta("policy")
        if old is not None and upgrade_saved_policy(old) != policy:
            self.store.close()
            raise SafetyStop("saved-policy-changed")
        self.store.set_meta("policy", policy)
        # Family rooms reuse mention admission; direct rooms are unchanged.
        self.policy = Policy(
            config["account"],
            self.senders,
            frozenset([config["account"]]),
            {r: "mention" if r in self.family_rooms else "direct" for r in config["rooms"]},
            config["not_before_ms"],
            aliases=mention_aliases(config),
            wake_words=wake_words(config),
        )
        self.blocked: set[str] = set(self.store.get_meta("room_gate_blocked") or ())
        self.room_members: dict[str, set[str]] = {}
        self.client: Any = None
        self.http: Any = None
        self.active: Mapping[str, Any] | None = None
        self.turn_task: asyncio.Task[TurnResult] | None = None
        self.approvals: dict[str, asyncio.Future[bool]] = {}
        self.key_requests: list[Any] = []
        self.cancel_requested = False
        self.matrix_lock = asyncio.Lock()
        self.stopping = False
        self.work_wake = asyncio.Event()
        self.send_wake = asyncio.Event()
        self.background: set[asyncio.Task[Any]] = set()
        # Per-turn latency stages (wall-clock seconds), keyed by event id;
        # body-free. ``_batch`` carries the sync batch being processed.
        self.turn_timing: OrderedDict[str, dict[str, float]] = OrderedDict()
        self._batch: dict[str, float] = {}
        self._origin_ms: int | None = None

    # -- HTTP -----------------------------------------------------------------

    async def raw(
        self,
        method: str,
        path: str,
        data: Any = None,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        url = self.c["homeserver"].rstrip("/") + path
        async with self.http.request(method, url, json=data, params=params, allow_redirects=False) as response:
            if response.status in (429, 500, 502, 503, 504):
                raise ConnectionError("matrix-temporary-error")
            if response.status != 200:
                raise SafetyStop("matrix-http-" + str(response.status))
            body = bytearray()
            async for chunk in response.content.iter_chunked(65_536):
                body.extend(chunk)
                if len(body) > 4_194_304:
                    raise SafetyStop("matrix-response-too-large")
            return json.loads(body)

    async def send_typing(self, room: str) -> None:
        """PUT the bot's typing indicator (8 s) in ``room``; raises on failure."""
        path = "/_matrix/client/v3/rooms/" + quote(room, safe="") + "/typing/" + quote(self.c["account"], safe="")
        await self.raw("PUT", path, {"typing": True, "timeout": 8000})

    # -- wakeups and latency record ---------------------------------------------

    def wake(self) -> None:
        """A job or an outbox row was stored: let work()/send() look now."""
        self.work_wake.set()
        self.send_wake.set()

    async def _idle(self, event: asyncio.Event) -> None:
        # Cleared only after the wait: a set() that races the caller's store
        # read makes this return at once, and the caller reads the store again.
        # asyncio.timeout, not wait_for: 3.11's wait_for can swallow a
        # cancellation that races the inner wait, hanging service shutdown.
        try:
            async with asyncio.timeout(IDLE_POLL_S):
                await event.wait()
        except TimeoutError:
            pass
        event.clear()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    async def _early_typing(self, room: str, event_id: str) -> None:
        """Typing right after admission, like Telegram's send_action on receipt."""
        try:
            await self.send_typing(room)
        except Exception:
            return  # cosmetic; the turn's own typing loop follows
        self.mark(event_id, "typing")

    def mark(self, event_id: str, stage: str) -> None:
        """Record the first time ``stage`` happened for a tracked turn."""
        record = self.turn_timing.get(event_id)
        if record is not None:
            record.setdefault(stage, time.time())

    def _track(self, event_id: str) -> None:
        now = time.time()
        record: dict[str, float] = {"admitted": now}
        if self._origin_ms is not None:
            record["origin"] = self._origin_ms / 1000.0
        if self._batch:
            record.update(self._batch)
        self.turn_timing[event_id] = record
        while len(self.turn_timing) > TURN_TIMING_CAP:
            self.turn_timing.popitem(last=False)

    def _finish_timing(self, event_id: str) -> None:
        """Log and keep one body-free latency summary for a finished turn."""
        record = self.turn_timing.pop(event_id, None)
        if record is None:
            return

        def span(start: str, end: str) -> float | None:
            if start in record and end in record:
                return round(record[end] - record[start], 3)
            return None

        end = "delivered" if "delivered" in record else "done"
        summary: dict[str, Any] = {
            "turn": turn_id(event_id),
            "at": round(record.get(end, time.time()), 3),
            # origin is the homeserver clock; the other stages are ours.
            "server_to_received_s": span("origin", "received"),
            "gate_s": record.get("gate_s"),
            "received_to_admitted_s": span("received", "admitted"),
            "admitted_to_claimed_s": span("admitted", "claimed"),
            "admitted_to_typing_s": span("admitted", "typing"),
            "admitted_to_first_sent_s": span("admitted", "first_sent"),
            "claimed_to_done_s": span("claimed", "done"),
            "done_to_delivered_s": span("done", "delivered"),
            "server_to_" + end + "_s": span("origin", end),
        }
        summary = {k: v for k, v in summary.items() if v is not None}
        logger.info("Matrix turn timing %s", json.dumps(summary, sort_keys=True))
        kept = self.store.get_meta("turn_timings") or []
        self.store.set_meta("turn_timings", (kept + [summary])[-TURN_TIMINGS_KEPT:])

    # -- startup --------------------------------------------------------------

    def _check_crypto_store(self, crypto: Path, initialize: bool) -> Any:
        fd = private_directory(crypto)
        try:
            for name in os.listdir(fd):
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(st.st_mode)
                    or st.st_nlink != 1
                    or st.st_uid != os.getuid()
                    or stat.S_IMODE(st.st_mode) != 0o600
                ):
                    raise SafetyStop("unsafe-crypto-store")
            old = self.store.get_meta("device_identity")
            if old is None and (not initialize or os.listdir(fd)):
                raise SafetyStop("explicit-new-device-initialization-required")
            return old
        finally:
            os.close(fd)

    async def _upload_keys_if_needed(self) -> None:
        if self.client.should_upload_keys:
            if type(await self.client.keys_upload()).__name__ != "KeysUploadResponse":
                raise SafetyStop("key-upload-failed")

    async def open(self, initialize: bool = False) -> None:
        import aiohttp
        from nio import AsyncClient, AsyncClientConfig, SyncResponse

        root = Path(self.c["state_directory"])
        self.store.storage_gate()
        crypto = root / "crypto"
        old = self._check_crypto_store(crypto, initialize)
        self.http = aiohttp.ClientSession(
            headers={"Authorization": "Bearer " + self.c["access_token"]},
            timeout=aiohttp.ClientTimeout(total=40),
        )
        who = await self.raw("GET", "/_matrix/client/v3/account/whoami")
        if who.get("user_id") != self.c["account"] or who.get("device_id") != self.c["device_id"]:
            raise SafetyStop("credential-device-mismatch")
        self.client = AsyncClient(
            self.c["homeserver"],
            self.c["account"],
            device_id=self.c["device_id"],
            store_path=str(crypto),
            config=AsyncClientConfig(
                pickle_key=self.c["pickle_key"],
                store_sync_tokens=False,
                max_timeouts=0,
                max_limit_exceeded=0,
                request_timeout=35,
            ),
        )
        self.client.restore_login(self.c["account"], self.c["device_id"], self.c["access_token"])
        identity = {
            "account": self.c["account"],
            "device": self.c["device_id"],
            "keys": self.client.olm.account.identity_keys,
            "credential_hash": hashlib.sha256(self.c["access_token"].encode()).hexdigest(),
            "homeserver": self.c["homeserver"],
        }
        if old is not None and old != identity:
            raise SafetyStop("crypto-identity-or-token-drift")
        self.store.set_meta("device_identity", identity)
        await self._upload_keys_if_needed()
        # Rebuild volatile room state before replaying an incremental saved batch.
        join = {}
        for room in self.c["rooms"]:
            events = await self.raw("GET", "/_matrix/client/v3/rooms/" + quote(room, safe="") + "/state")
            join[room] = {
                "state": {"events": events},
                "timeline": {"events": [], "limited": False},
                "ephemeral": {"events": []},
                "account_data": {"events": []},
                "unread_notifications": {},
            }
        await self.client.receive_response(
            SyncResponse.from_dict(
                {
                    "next_batch": "prime-" + str(time.time_ns()),
                    "rooms": {"join": join},
                    "to_device": {"events": []},
                    "device_one_time_keys_count": {},
                }
            )
        )
        await self.pin_devices()
        for room in self.c["rooms"]:
            await self.room_gate(room)
        self.store.set_meta("health", {"state": "ready", "updated": time.time()})

    # -- trust ----------------------------------------------------------------

    async def pin_devices(self) -> None:
        from nio import KeysQueryResponse

        query: dict[str, list[str]] = {self.c["account"]: [], self.c["owner"]: [], **{u: [] for u in self.family_users}}
        raw = await self.raw("POST", "/_matrix/client/v3/keys/query", {"device_keys": query})
        response = KeysQueryResponse.from_dict(raw)
        if type(response).__name__ != "KeysQueryResponse":
            raise SafetyStop("device-query-failed")
        await self.client.receive_response(response)
        devices = raw.get("device_keys", {}).get(self.c["owner"], {})
        if self.c["owner"] in self.pins and set(devices) != set(self.c["devices"]):
            raise SafetyStop("owner-device-set-changed")
        own = raw.get("device_keys", {}).get(self.c["account"], {})
        if set(own) != {self.c["device_id"]}:
            raise SafetyStop("unexpected-agent-device")
        for kind, key in self.client.olm.account.identity_keys.items():
            if own[self.c["device_id"]].get("keys", {}).get(kind + ":" + self.c["device_id"]) != key:
                raise SafetyStop("published-agent-key-changed")
        for device, pin in self.pins.get(self.c["owner"], {}).items():
            stored = self.client.device_store[self.c["owner"]][device]
            if stored.ed25519 != pin["ed25519"] or stored.curve25519 != pin["curve25519"]:
                raise SafetyStop("owner-device-key-changed")
            self.client.verify_device(stored)
        for user in sorted(self.identities):
            self._trust_cross_signed(user, raw)
        # Family devices are pinned per user. Each user keeps the strict pin
        # check, while extra unpinned family devices stay merely untrusted and
        # are handled by exclude_unpinned_devices at session-share time.
        for user, pins in sorted(self.family_devices.items()):
            if user in self.identities:
                continue
            for device, pin in pins.items():
                stored = self.client.device_store[user].get(device)
                if stored is None:
                    raise SafetyStop("pinned-device-missing")
                if stored.ed25519 != pin["ed25519"] or stored.curve25519 != pin["curve25519"]:
                    raise SafetyStop("pinned-device-key-changed")
                self.client.verify_device(stored)

    def _trust_cross_signed(self, user: str, raw: Mapping[str, Any]) -> None:
        """Trust exactly the devices ``user``'s self-signing key has signed (#149).

        The pinned value is the master key. The self-signing key must be
        signed by it and every device by the self-signing key (ed25519 over
        canonical JSON, via nio's ``verify_json``). Unsigned devices are
        blacklisted for key sharing but never stop the service; a changed
        master key (account reset) does.
        """
        master_obj = raw.get("master_keys", {}).get(user)
        ssk_obj = raw.get("self_signing_keys", {}).get(user)
        if not isinstance(master_obj, dict) or not isinstance(ssk_obj, dict):
            raise SafetyStop("cross-signing-missing")
        master = next(iter((master_obj.get("keys") or {}).values()), None)
        if master != self.identities[user]:
            raise SafetyStop("owner-identity-changed" if user == self.c["owner"] else "family-identity-changed")
        if not self.client.olm.verify_json(copy.deepcopy(ssk_obj), master, user, master):
            raise SafetyStop("cross-signing-invalid")
        ssk = next(iter((ssk_obj.get("keys") or {}).values()), None)
        if not isinstance(ssk, str):
            raise SafetyStop("cross-signing-invalid")
        trusted: dict[str, str] = {}
        # nio's DeviceStore iterates devices, not user ids, so `user in store`
        # is always False; __getitem__ returns the (possibly empty) per-user map.
        try:
            store = self.client.device_store[user]
        except KeyError:
            store = {}
        for device, obj in (raw.get("device_keys", {}).get(user) or {}).items():
            stored = store.get(device)
            if stored is None or not isinstance(obj, dict):
                continue
            published = (obj.get("keys") or {}).get("ed25519:" + device)
            if published == stored.ed25519 and self.client.olm.verify_json(copy.deepcopy(obj), ssk, user, ssk):
                self.client.verify_device(stored)
                trusted[device] = stored.curve25519
            else:
                self.client.blacklist_device(stored)
        previous = self.trusted.get(user)
        self.trusted[user] = trusted
        if previous is None or set(previous) != set(trusted):
            record = self.store.get_meta("trusted_devices") or {}
            record[user] = {"devices": sorted(trusted), "updated": time.time()}
            self.store.set_meta("trusted_devices", record)

    async def room_gate(self, room: str) -> bool:
        from nio import JoinedMembersResponse

        path = "/_matrix/client/v3/rooms/" + quote(room, safe="")
        members = await self.raw("GET", path + "/joined_members")
        joined = set(members.get("joined", {}))
        if room not in self.family_rooms and joined != {self.c["owner"], self.c["account"]}:
            raise SafetyStop("private-room-membership-changed")
        await self.client.receive_response(JoinedMembersResponse.from_dict(members, room))
        encryption = await self.raw("GET", path + "/state/m.room.encryption")
        if encryption.get("algorithm") != MEGOLM:
            raise SafetyStop("encrypted-room-required")
        if room not in self.client.rooms or not self.client.rooms[room].encrypted:
            raise SafetyStop("sdk-encryption-state-missing")
        users = set(self.client.rooms[room].users)
        if room in self.family_rooms:
            # A family room fails closed per room: exactly one bot plus the
            # allowlisted family members. An unauthorized member mutes only
            # that room and is recorded in state; no plaintext fallback exists.
            if (
                self.c["account"] not in joined
                or not joined <= self.family_allowed
                or self.c["account"] not in users
                or not users <= self.family_allowed
            ):
                self.block_room(room, joined)
                return False
            self.unblock_room(room)
            self.room_members[room] = joined
            await self.family_notice(room)
            return True
        if users != {self.c["owner"], self.c["account"]}:
            raise SafetyStop("sdk-room-membership-changed")
        self.room_members[room] = joined
        return True

    def block_room(self, room: str, members: set[str]) -> None:
        state = self.store.get_meta("room_gate_blocked") or {}
        record = {"reason": "unauthorized-room-member", "members": sorted(members)}
        if state.get(room) != record:
            state[room] = record
            self.store.set_meta("room_gate_blocked", state)
        self.blocked.add(room)

    def unblock_room(self, room: str) -> None:
        if room not in self.blocked:
            return
        state = self.store.get_meta("room_gate_blocked") or {}
        state.pop(room, None)
        self.store.set_meta("room_gate_blocked", state)
        self.blocked.discard(room)

    async def family_notice(self, room: str) -> None:
        # First healthy join posts the disclosure notice once per room. The
        # saved marker plus the notice dedup keep it from ever repeating.
        sent = self.store.get_meta("family_room_notice") or {}
        if room in sent:
            return
        req = Request(
            "$family-join-" + hashlib.sha256(room.encode()).hexdigest()[:32],
            room,
            self.c["account"],
            "family-join",
            hashlib.sha256(json.dumps([self.c["account"], room, "family-notice"]).encode()).hexdigest(),
        )
        self.store.notice(req, "family-join", self.c.get("family_notice_text") or FAMILY_NOTICE)
        sent[room] = True
        self.store.set_meta("family_room_notice", sent)

    # -- routing helpers ------------------------------------------------------

    def as_request(self, job: Mapping[str, Any]) -> Request:
        return Request(job["event_id"], job["room_id"], job["sender"], job["body"], job["scope"])

    def room_kind(self, room_id: str) -> str:
        return "family" if room_id in self.family_rooms else "direct"

    def enqueue_notice(self, room_id: str, text: str, *, key: str | None = None) -> str:
        """Queue unsolicited output (async completion, reminders) for an allowed room.

        Delivered by :meth:`send` with the same chunking, pinning and room
        gate as a reply; a muted room keeps it until the gate reopens. A
        caller-supplied ``key`` makes the notice idempotent (same key + same
        text → queued once), e.g. the startup banner across restarts.
        """
        if room_id not in self.c["rooms"]:
            raise ValueError("room-not-allowed")
        bounded_text(text, MAX_REPLY_BYTES)
        req = Request(
            "$unsolicited-" + hashlib.sha256(room_id.encode()).hexdigest()[:32],
            room_id,
            self.c["account"],
            "notice",
            hashlib.sha256(json.dumps([self.c["account"], room_id, "unsolicited-notice"]).encode()).hexdigest(),
        )
        event_id = self.store.notice(req, key or unique_key("unsolicited"), text)
        self.wake()
        return event_id

    def enqueue_self_job(self, room_id: str, body: str, *, key: str) -> str:
        """Queue a turn the frontend runs for the owner in ``room_id`` (#1895 PR-A2).

        Only the configured owner's scope and an allowed room: the job then
        goes through the normal claim → runner.run(sink) → finish path, so the
        work happens inside the transport's single-turn discipline instead of
        beside it. Idempotent per ``key``.
        """
        if room_id not in self.c["rooms"]:
            raise ValueError("room-not-allowed")
        event_id = self.store.self_job(room_id, self.c["owner"], body, key=key)
        self.wake()
        return event_id

    # -- input ----------------------------------------------------------------

    async def input(self, req: Request) -> None:
        if self.store.seen_control(req, record=False):
            return
        if req.body.startswith(CONTROL_PREFIXES):
            await self.control(req)
            return
        if req.reply_to is not None:
            if self.store.job_exists(req.event_id):
                # A replayed sync: the job was stored with its reply context
                # already; re-resolving could change the body (identity conflict).
                return
            req = await self.with_reply_context(req)
        fresh = not self.store.job_exists(req.event_id)
        try:
            self.store.accept_batch([req], None)
        except QueueFull:
            # Reject visibly and durably, so a full ordinary queue cannot stop
            # later syncs from carrying cancellation/approval controls.
            if not self.store.seen_control(req):
                self.store.notice(req, "queue-full", NOTICE_QUEUE_FULL)
            self.wake()
            return
        if fresh:
            self._track(req.event_id)
            self.wake()  # start the turn now, not after the rest of the batch
            self._spawn(self._early_typing(req.room_id, req.event_id))
            # Telegram tells a sender whose turn is still running where their
            # message landed in the queue; family rooms get the same notice.
            ahead = self.store.pending_before(req.event_id)
            if ahead:
                self.store.notice(
                    req,
                    "queued-" + req.event_id,
                    NOTICE_QUEUED.format(position=ahead + 1),
                )

    def _turn_running(self) -> bool:
        return self.active is not None and self.turn_task is not None and not self.turn_task.done()

    async def control(self, req: Request) -> None:
        if self.store.seen_control(req):
            return
        fields = req.body.split()
        if len(fields) == 2 and fields[0] == "/ack":
            job = next(
                (
                    j
                    for j in self.store.uncertain()
                    if turn_id(j["event_id"]) == fields[1] and j["scope"] == req.scope
                ),
                None,
            )
            if job:
                self.store.resolve_uncertain(job["event_id"], NOTICE_ACKED)
                return
        allowed = False
        if self.active is not None and self._turn_running() and self.active["scope"] == req.scope:
            tid = turn_id(self.active["event_id"])
            # "/stop" is the Telegram-parity alias: no turn id needed when the
            # sender's own scope is the one running.
            if fields == ["/cancel", tid] or fields == ["/stop"]:
                allowed = True
                await self._cancel_active()
            elif (
                len(fields) == 3
                and fields[0] in ("/approve", "/deny")
                and fields[1] == tid
                and fields[2] in self.approvals
            ):
                allowed = True
                future = self.approvals.pop(fields[2])  # single use
                if not future.done():
                    future.set_result(fields[0] == "/approve")
            if allowed:
                self.store.notice(req, "control", NOTICE_CONTROL_FORWARDED)
                self.wake()
                return
        self.store.notice(req, "invalid-control", NOTICE_INVALID_CONTROL)
        self.wake()

    async def _cancel_active(self) -> None:
        job, task = self.active, self.turn_task
        if job is None or task is None:
            return
        self.cancel_requested = True
        try:
            await self.runner.cancel(job)
        except Exception:
            pass  # The task cancellation below is authoritative; the turn stays uncertain.
        task.cancel()

    # -- sync -----------------------------------------------------------------

    async def receive(self) -> None:
        while True:
            self.store.storage_gate()
            raw = self.store.get_meta("pending_sync")
            if raw is None:
                params = {
                    "timeout": "25000",
                    "filter": json.dumps(
                        {
                            "room": {
                                "rooms": self.c["rooms"],
                                "timeline": {"limit": 100},
                                "ephemeral": {"types": []},
                            },
                            "presence": {"types": []},
                        }
                    ),
                }
                token = self.store.token()
                if token:
                    params["since"] = token
                raw = await self.raw("GET", "/_matrix/client/v3/sync", params=params)
                self._batch = {"received": time.time()}
                self.store.stage_sync(raw)
            await self.process_pending()

    async def process_pending(self) -> None:
        from nio import SyncResponse

        raw = self.store.get_meta("pending_sync")
        if raw is None:
            return
        async with self.matrix_lock:
            # `limited` marks a real gap only for an incremental sync. The first
            # sync (no saved token) is a snapshot: servers set limited=true for
            # every room joined since "never", and open() already primed state.
            if self.store.token() is not None:
                for room, info in raw.get("rooms", {}).get("join", {}).items():
                    timeline = info.get("timeline", {}) if room in self.c["rooms"] else {}
                    if not timeline.get("limited"):
                        continue
                    events = timeline.get("events") or []
                    if len(events) >= SYNC_TIMELINE_LIMIT:
                        raise SafetyStop("timeline-gap-requires-backfill")
                    # Tuwunel marks `limited` on a batch that is nowhere near the
                    # requested limit (seen 2026-09-18 04:42 KST: one m.room.member
                    # event per room after a display-name change) — every event
                    # since the saved token is present, so nothing was skipped.
                    # A real gap fills the timeline up to the limit (114 events on
                    # 2026-09-17). Record it and carry on instead of fail-closing.
                    self.store.set_meta(
                        "sync_limited_soft",
                        {"room": room, "events": len(events), "updated": time.time()},
                    )
            gate_started = time.monotonic()
            await self.pin_devices()
            response = SyncResponse.from_dict(raw)
            if type(response).__name__ != "SyncResponse":
                raise SafetyStop("invalid-sync-response")
            self.client.next_batch = None  # Replay pending raw after a failed receive.
            await self.client.receive_response(response)
            for room in self.c["rooms"]:
                await self.room_gate(room)
            self._batch = {**self._batch, "gate_s": round(time.monotonic() - gate_started, 3)}
            for room, info in response.rooms.join.items():
                if room not in self.c["rooms"]:
                    continue
                for event in info.timeline.events:
                    event_id = getattr(event, "event_id", None)
                    if isinstance(event_id, str) and event_id:
                        self.last_room_event[room] = event_id
                        if self._trusted_text(event):
                            self._remember_text(event_id, room, event.sender, event.body)
                    req = self.admit_event(room, event)
                    if req:
                        self._origin_ms = getattr(event, "server_timestamp", None)
                        try:
                            await self.input(req)
                        finally:
                            self._origin_ms = None
            self._batch = {}
            self.wake()  # notices queued while admitting the batch
            await self._request_room_keys()
            self.store.commit_sync(raw["next_batch"])
            await self._upload_keys_if_needed()
            self.store.set_meta("health", {"state": "ready", "updated": time.time()})

    def admit_event(self, room: str, event: Any) -> Request | None:
        """Admit only decrypted text from allowed senders' verified pinned devices."""
        from nio import MegolmEvent, RoomMessageText

        if event.sender not in self.senders or room in self.blocked:
            return None
        if event.server_timestamp < self.c["not_before_ms"]:
            return None
        if isinstance(event, MegolmEvent):
            # The pilot fail-closed here, which poisons the service forever
            # when a message was encrypted before this device existed (jingun
            # 2026-09-18: the owner wrote seconds after accepting the invite,
            # before the bot device was initialised — that megolm session can
            # never reach us). Skip it, ask for the key, tell the room once.
            self._undecryptable(room, event)
            return None
        if not isinstance(event, RoomMessageText):
            return None
        if not event.decrypted:
            return None  # No plaintext task execution.
        trusted = self.trusted.get(event.sender, {})
        if not event.verified or event.sender_key not in set(trusted.values()):
            if event.sender in self.identities:
                # Cross-signing mode: an unverified device is the owner's own
                # problem to fix (verify it in the app); never stop the service.
                self._untrusted_sender(room, event)
                return None
            raise SafetyStop("unverified-owner-event" if event.sender == self.c["owner"] else "unverified-family-event")
        return self.policy.admit(room, event.source, decrypted=event.decrypted, now_ms=int(time.time() * 1000))

    def _untrusted_sender(self, room: str, event: Any) -> None:
        """Ignore a message from an unsigned device; tell the room once per device."""
        key = str(getattr(event, "sender_key", "") or "")
        seen = self.store.get_meta("untrusted_senders") or {}
        marker = f"{event.sender}:{key}"
        if marker in seen:
            return
        seen[marker] = {"room": room, "updated": time.time()}
        self.store.set_meta("untrusted_senders", dict(list(seen.items())[-50:]))
        req = Request(str(getattr(event, "event_id", "") or "$untrusted-" + hashlib.sha256(marker.encode()).hexdigest()[:24]),
                      room, event.sender, "notice", scope_of(self.c["account"], room, event.sender))
        self.store.notice(req, "untrusted-device", NOTICE_UNTRUSTED_DEVICE)

    def _undecryptable(self, room: str, event: Any) -> None:
        """Record an undecryptable event, queue a key request and a one-time room notice."""
        event_id = str(getattr(event, "event_id", "") or "")
        seen = self.store.get_meta("undecryptable_events") or []
        if event_id and event_id not in [e.get("event_id") for e in seen]:
            seen.append({"event_id": event_id, "room": room, "ts": getattr(event, "server_timestamp", None),
                         "updated": time.time()})
            self.store.set_meta("undecryptable_events", seen[-50:])
        self.key_requests.append(event)
        if event_id:
            req = Request(event_id, room, str(getattr(event, "sender", "")), "notice",
                          scope_of(self.c["account"], room, str(getattr(event, "sender", ""))))
            self.store.notice(req, "undecryptable", NOTICE_UNDECRYPTABLE)

    # -- reply context (#1943) ------------------------------------------------

    def _trusted_text(self, event: Any) -> bool:
        """A decrypted text from our own device or an allowed sender's trusted device."""
        from nio import RoomMessageText

        if not isinstance(event, RoomMessageText) or not event.decrypted or not event.verified:
            return False
        if not isinstance(getattr(event, "body", None), str):
            return False
        if event.sender == self.c["account"]:
            return True
        return event.sender in self.senders and event.sender_key in set(self.trusted.get(event.sender, {}).values())

    def _remember_text(self, event_id: Any, room: str, sender: str, body: str) -> None:
        if not isinstance(event_id, str) or not event_id or not body.strip():
            return
        self.recent_text[event_id] = (room, sender, body)
        self.recent_text.move_to_end(event_id)
        while len(self.recent_text) > RECENT_TEXT_CAP:
            self.recent_text.popitem(last=False)

    async def _fetch_parent(self, room: str, event_id: str) -> tuple[str, str] | None:
        """Fetch and decrypt a reply parent; ``None`` on any failure (best-effort)."""
        try:
            from nio import Event, MegolmEvent

            raw = await self.raw(
                "GET",
                "/_matrix/client/v3/rooms/" + quote(room, safe="") + "/event/" + quote(event_id, safe=""),
            )
            if not isinstance(raw, dict) or raw.get("event_id") != event_id or raw.get("type") != "m.room.encrypted":
                return None  # plaintext parents are never trusted as context
            encrypted = Event.parse_encrypted_event(raw)
            if not isinstance(encrypted, MegolmEvent):
                return None
            encrypted.room_id = room
            event = self.client.decrypt_event(encrypted)
            if not self._trusted_text(event):
                return None
            self._remember_text(event_id, room, event.sender, event.body)
            return event.sender, event.body
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - context is optional; never stop the service for it
            self.store.set_meta("reply_context_miss", {"room": room, "updated": time.time()})
            return None

    async def with_reply_context(self, req: Request) -> Request:
        """Prefix a reply's body with the message it answers; unchanged on any miss.

        ``/command`` and bare-number replies (``2`` answering a numbered menu:
        Danso recovery, ``/resume`` pick) stay verbatim so the bot still parses them.
        """
        text = req.body.strip()
        if req.reply_to is None or text.startswith("/") or text.isdigit():
            return req
        cached = self.recent_text.get(req.reply_to)
        if cached is not None and cached[0] == req.room_id:
            parent: tuple[str, str] | None = (cached[1], cached[2])
        else:
            parent = await self._fetch_parent(req.room_id, req.reply_to)
        if parent is None:
            return req
        body = reply_context_body(
            req.body,
            parent_sender=parent[0],
            parent_body=parent[1],
            account=self.c["account"],
            sender=req.sender,
            limit_bytes=MAX_TEXT_BYTES,
        )
        return replace(req, body=body)

    async def _request_room_keys(self) -> None:
        """Best-effort m.room_key_request for events we could not decrypt."""
        pending, self.key_requests = self.key_requests, []
        for event in pending:
            try:
                await self.client.request_room_key(event)
            except Exception:
                pass  # the sender may simply not have the session for us

    # -- output ---------------------------------------------------------------

    async def send(self) -> None:
        while True:
            for job in self.store.outbox():
                if job["room_id"] in self.blocked:
                    continue  # Muted room keeps its pending replies.
                async with self.matrix_lock:
                    await self.pin_devices()
                    if not await self.room_gate(job["room_id"]):
                        continue
                    # Fence-aware chunking (never splits a ``` block) at the pilot's 12 KB size.
                    from telegram_bot.core.matrix.render import chunk_text

                    chunks = chunk_text(job["reply"])
                    for i in range(self.store.delivered_parts(job["event_id"]), len(chunks)):
                        tx = hashlib.sha256((job["txn_id"] + ":" + str(i)).encode()).hexdigest()
                        sent = await self.encrypted_send(job["room_id"], chunks[i], tx)
                        self._remember_text(sent, job["room_id"], self.c["account"], chunks[i])
                        self.store.mark_part(job["event_id"], i + 1)
                    self.store.delivered(job["event_id"])
                    if "done" in self.turn_timing.get(job["event_id"], {}):
                        self.mark(job["event_id"], "delivered")
                        self._finish_timing(job["event_id"])
            await self._idle(self.send_wake)

    async def _encrypted_raw(self, room: str, kind: str, content: Mapping[str, Any], txn: str) -> str:
        # nio 0.25.2 room_send ignores incomplete key sharing. Confirm every
        # pinned recipient before encrypting, and avoid its implicit queries.
        if self.client.olm.should_share_group_session(room):
            if room in self.family_rooms:
                self.exclude_unpinned_devices(room)
            await self.client.share_group_session(room)
        session = self.client.olm.outbound_group_sessions.get(room)
        expected = self.expected_recipients(room)
        if session is None or session.users_shared_with != expected:
            self.client.invalidate_outbound_session(room)
            raise ConnectionError("group-key-share-incomplete")
        out_kind, encrypted = self.client.encrypt(room, kind, content)
        if out_kind != "m.room.encrypted":
            raise SafetyStop("plaintext-output-refused")
        result = await self.raw(
            "PUT",
            "/_matrix/client/v3/rooms/" + quote(room, safe="") + "/send/m.room.encrypted/" + txn,
            data=encrypted,
        )
        event_id = result.get("event_id")
        bounded_text(event_id, 255)
        active = self.active
        if active is not None and active.get("room_id") == room:
            self.mark(active["event_id"], "first_sent")
        return event_id

    async def encrypted_send(self, room: str, text: str, txn: str) -> str:
        return await self._encrypted_raw(room, "m.room.message", message_content(text), txn)

    async def encrypted_edit(self, room: str, text: str, replaces: str, txn: str) -> str:
        """m.replace edit of ``replaces``; the bubble keeps its original event id."""
        new_content = message_content(text)
        content: dict[str, Any] = dict(new_content)
        content["body"] = "* " + text
        content["m.new_content"] = new_content
        content["m.relates_to"] = {"rel_type": "m.replace", "event_id": replaces}
        return await self._encrypted_raw(room, "m.room.message", content, txn)

    async def redact(self, room: str, event_id: str, tag: str) -> None:
        txn = hashlib.sha256(("redact-" + tag).encode()).hexdigest()
        await self.raw(
            "PUT",
            "/_matrix/client/v3/rooms/" + quote(room, safe="") + "/redact/" + quote(event_id, safe="") + "/" + txn,
            {},
        )

    def expected_recipients(self, room: str) -> set[tuple[str, str]]:
        """Pinned devices of every room member; direct rooms keep owner-only pins."""
        members = self.room_members.get(room, {self.c["owner"], self.c["account"]})
        return {
            (user, device)
            for user in members
            if user != self.c["account"]
            for device in self.trusted.get(user, {})
        }

    def exclude_unpinned_devices(self, room: str) -> None:
        # Unpinned family devices never receive megolm sessions; the warning is
        # recorded in state, while sending still waits for every pinned device.
        unpinned: dict[str, list[str]] = {}
        for user in self.room_members.get(room, ()):
            if user == self.c["account"]:
                continue
            missing = sorted(d for d in self.client.device_store[user] if d not in self.trusted.get(user, {}))
            if missing:
                unpinned[user] = missing
        if unpinned:
            warnings = self.store.get_meta("family_room_devices") or {}
            if warnings.get(room) != unpinned:
                warnings[room] = unpinned
                self.store.set_meta("family_room_devices", warnings)
        for user, devices in unpinned.items():
            for device in devices:
                self.client.blacklist_device(self.client.device_store[user][device])

    # -- turns ----------------------------------------------------------------

    async def work(self) -> None:
        while True:
            # Work left uncertain by a previous process (store open converts
            # crashed 'running' rows) was interrupted by a stop/restart: tell
            # the room once and move on, like the Telegram bridge does.
            for job in self.store.uncertain():
                self.store.resolve_uncertain(job["event_id"], NOTICE_RESTARTED)
            job = self.store.claim()
            if not job:
                await self._idle(self.work_wake)
                continue
            self.mark(job["event_id"], "claimed")
            await self.run_turn(job)

    async def run_turn(self, job: Mapping[str, Any]) -> None:
        """Execute one claimed job; anything short of a confirmed result leaves it uncertain."""
        self.active = job
        self.approvals = {}
        self.cancel_requested = False
        outcome = "uncertain"
        # Routing context is admitted data (sender/room passed the gate), not user text.
        turn = asyncio.create_task(
            self.runner.run(
                job,
                sink=_RoomSink(self, job),
                session_id=self.store.session(job["scope"]),
                room_kind=self.room_kind(job["room_id"]),
            )
        )
        self.turn_task = turn
        shutting_down = False
        try:
            # asyncio.wait (not `await turn`) keeps timeout/shutdown cancellation
            # with this loop: a runner that swallows CancelledError cannot absorb it.
            async with asyncio.timeout(self.turn_timeout):
                await asyncio.wait({turn})
            result = turn.result()
            if isinstance(result, TurnResult) and result.status in FINAL_STATUSES:
                # ``streamed`` means the runner already delivered the text
                # (interim notices); finishing with an empty reply keeps the
                # session id without echoing the answer a second time.
                self.store.finish(job["event_id"], "" if result.streamed else result.text, result.session_id)
                outcome = result.status
        except asyncio.CancelledError:
            outcome = "cancelled"
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                shutting_down = True
                raise  # The service is stopping; the join below still runs.
            # Only the runner task was cancelled (/cancel or /stop): keep serving.
        except Exception as exc:
            outcome = "timeout" if isinstance(exc, TimeoutError) else "error:" + type(exc).__name__
        finally:
            try:
                await self._join(turn)
            finally:
                # Runs even when the join itself is cancelled again.
                self.store.uncertain_job(job["event_id"])  # no-op once finished
                if not shutting_down:
                    # Telegram parity: an interrupted turn ends with a short
                    # notice and the next message is served normally. A stop/
                    # restart leaves it uncertain for the next process to notice.
                    self._close_interrupted(job, outcome)
                for future in self.approvals.values():
                    if not future.done():
                        future.set_result(False)
                self.approvals = {}
                self.active = None
                self.turn_task = None
                self.store.set_meta(
                    "last_turn", {"event_id": job["event_id"], "outcome": outcome, "updated": time.time()}
                )
                self.mark(job["event_id"], "done")
                if not any(row["event_id"] == job["event_id"] for row in self.store.outbox()):
                    self._finish_timing(job["event_id"])  # else when send() delivers the reply
                self.wake()  # the reply or closing notice is in the outbox now

    def _close_interrupted(self, job: Mapping[str, Any], outcome: str) -> None:
        if not any(j["event_id"] == job["event_id"] for j in self.store.uncertain()):
            return  # finished normally
        if outcome == "cancelled":
            text = NOTICE_CANCELLED
        elif outcome == "timeout":
            text = NOTICE_TIMEOUT.format(minutes=_timeout_label(self.turn_timeout))
        else:  # runner exception or an explicit uncertain result
            text = NOTICE_TURN_ERROR
        self.store.resolve_uncertain(job["event_id"], text)

    async def _join(self, turn: asyncio.Task[TurnResult]) -> None:
        """Stop the runner task and wait for it, surviving repeated cancels like the pilot's cleanup join."""
        interrupted = False
        if not turn.done():
            turn.cancel()
        deadline = time.monotonic() + TURN_JOIN_TIMEOUT_S
        while not turn.done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.store.set_meta("turn_join_timeout", {"updated": time.time()})
                break
            try:
                await asyncio.wait({turn}, timeout=remaining)
            except asyncio.CancelledError:
                interrupted = True
                turn.cancel()
        if interrupted:
            raise asyncio.CancelledError

    # -- lifecycle ------------------------------------------------------------

    async def retry(self, operation: Any) -> None:
        import aiohttp

        delay = 1.0
        while True:
            try:
                await operation()
                return
            except (ConnectionError, aiohttp.ClientError, TimeoutError):
                self.store.set_meta("health", {"state": "network-retry", "updated": time.time()})
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self.retry(self.receive))
            group.create_task(self.retry(self.send))
            group.create_task(self.work())

    async def close(self) -> None:
        for task in list(self.background):
            task.cancel()
        if self.background:
            await asyncio.gather(*self.background, return_exceptions=True)
        if self.client:
            await self.client.close()
        if self.http:
            await self.http.close()
        self.store.close()


def stop_reason(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return next(
            (stop_reason(e) for e in exc.exceptions if stop_reason(e) != "unexpected-failure"),
            "unexpected-failure",
        )
    if isinstance(exc, SafetyStop):
        return str(exc)
    if isinstance(exc, asyncio.CancelledError):
        return "service-stopped"
    return "unexpected-failure"


async def serve(transport: MatrixTransport, *, initialize: bool = False) -> None:
    """Open, run until stopped, and record the stop reason (without bodies) in ``meta.health``."""
    try:
        await transport.open(initialize=initialize)
        if not initialize:
            await transport.run()
    except BaseException as exc:
        transport.store.set_meta("health", {"state": "stopped", "reason": stop_reason(exc), "updated": time.time()})
        raise
    finally:
        await transport.close()
