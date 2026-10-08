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
import traceback
from typing import Any, Mapping, Protocol
from urllib.parse import quote

from telegram_bot.core.matrix.state import (
    FILE_JOB_BODY,
    MAX_REPLY_BYTES,
    MAX_TEXT_BYTES,
    MEDIA_MSGTYPES,
    MatrixStore,
    Policy,
    QueueFull,
    REJECT_EDIT,
    REJECT_TEXT_TOO_LARGE,
    Request,
    SafetyStop,
    bounded_text,
    family_config,
    identities,
    mention_aliases,
    private_directory,
    reply_context_body,
    saved_policy,
    identifier,
    outbox_msgtype,
    scope_of,
    turn_id,
    turn_timeout_minutes,
    upgrade_saved_policy,
    wake_words,
)
from telegram_bot.core.matrix.attachments import decode_attachment, encode_attachment, media_attachment, media_caption
from telegram_bot.utils.redaction import redact_credentials

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplyParent:
    sender: str
    body: str
    attachment: str | None = None


def _raise_site(exc: BaseException) -> str:
    """``file:line in function`` of the innermost frame, or ``unknown``."""

    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    if not frames:
        return "unknown"
    frame = frames[-1]
    return f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"

FAMILY_NOTICE = "이 AI는 이 방을 읽을 수 있으며 답변에 필요한 내용(부른 메시지의 사진·파일 포함)이 제공업체에 전달될 수 있습니다."
NOTICE_QUEUE_FULL = "대기 중인 요청이 많습니다. 잠시 후 다시 요청해 주세요."
NOTICE_QUEUED = "⏳ 이 메시지는 대기 순번 {position}번에 저장되었으며 도착 순서대로 처리됩니다."
NOTICE_ACKED = "이전 작업의 결과 확인을 완료한 것으로 기록했습니다. 자동 재실행은 하지 않습니다."
NOTICE_CONTROL_FORWARDED = "요청을 전달했습니다. 실제 처리 결과는 이어지는 안내를 확인해 주세요."
NOTICE_CONTINUATIONS_CANCELLED = "⏹️ 대기 중이던 자동 이어하기를 취소했습니다."
NOTICE_INVALID_CONTROL = "현재 이 대화방에서 처리할 수 있는 제어 요청이 아닙니다. 작업 번호와 승인 번호를 확인해 주세요."
NOTICE_UNCERTAIN = (
    "작업이 중단되어 결과 확인이 필요합니다. 자동으로 다시 실행하지 않습니다.\n"
    "결과를 확인한 뒤 다음 명령으로 대기를 해제할 수 있습니다:\n/ack "
)  # legacy text kept for the operator unblock audit; no longer posted to rooms
NOTICE_RESTARTED = "⏳ 답변 중에 서비스가 재시작되어 마지막 답변이 끊겼습니다. 메시지를 다시 보내 주세요."
NOTICE_CANCELLED = "⏹ 요청대로 작업을 중단했습니다."
# #2001: an agent deliverable that could not be sent (gone, too large, refused).
NOTICE_FILE_UNSENT = "📎 파일을 보내지 못했습니다: {name}"
# Same cap as the Telegram bridge (bot_delivery.MAX_SEND_FILE_BYTES); the
# homeserver's m.upload.size lowers it further.
MAX_OUTBOUND_FILE_BYTES = 50 * 1024 * 1024
# A file whose upload keeps failing temporarily is given up after this many
# tries (one notice) instead of holding the ordered outbox forever.
MAX_OUTBOUND_FILE_ATTEMPTS = 3
# #2002: messages that used to vanish without a word.
NOTICE_TEXT_TOO_LARGE = "⚠️ 메시지가 너무 길어 읽지 않았습니다({size} KiB, 한도 {limit} KiB). 내용을 파일로 첨부해 보내 주세요."
NOTICE_EDIT_IGNORED = "✏️ 수정한 메시지는 다시 읽지 않습니다. 고친 내용을 새 메시지로 보내 주세요."
NOTICE_THREAD_IGNORED = "🧵 스레드 안의 답글은 읽지 않습니다. 방에 바로 보내 주세요."
NOTICE_UNSUPPORTED_KIND = "스티커·이모트·알림 형식 메시지는 읽지 않습니다. 글로 보내 주세요."
# #2159: a trusted sender's attachment that is never run — plaintext ``url``
# media (#1795 policy) or a media event nio could not validate (BadEvent).
NOTICE_ATTACHMENT_REFUSED = (
    "📎 첨부를 읽지 못했습니다. 암호화되지 않은 첨부이거나 형식이 맞지 않습니다. "
    "내용을 글로 붙여 넣거나, 첨부를 암호화해 보내는 앱(Element 등)에서 다시 보내 주세요."
)
NOTICE_TIMEOUT = "⏳ 시간 제한({minutes}분)을 넘겨 작업을 중단했습니다. 요청을 나눠서 다시 보내 주세요."


# nio's DefaultStore keeps device trust in three plaintext files next to the
# crypto database, one key per line.
TRUST_FILE_SUFFIXES = (".trusted_devices", ".blacklisted_devices", ".ignored_devices")


def compact_trust_files(crypto: Path) -> dict[str, int]:
    """Drop duplicate lines from nio's trust files; return removed counts per file.

    nio 0.25's file ``KeyStore.add`` appends without a duplicate check, reports
    a change every time and rewrites the whole file, while ``remove`` deletes
    only the first copy. ``pin_devices`` used to verify every trusted device on
    every sync batch and every send, so the files grew without bound (sogyo
    2026-09-24: 199,250 lines for 7 devices, 0.2 s of blocking I/O per call)
    and every call invalidated the room's megolm session. The first copy of
    each line is kept, which preserves what nio loads: membership is the same
    set and ``get_key`` already returned the first match.
    """
    removed: dict[str, int] = {}
    for path in sorted(crypto.iterdir()):
        if not path.name.endswith(TRUST_FILE_SUFFIXES) or path.is_symlink() or not path.is_file():
            continue
        lines = path.read_text().splitlines()
        seen: set[str] = set()
        kept: list[str] = []
        for line in lines:
            key = line.strip()
            if key and not key.startswith("#"):
                if key in seen:
                    continue
                seen.add(key)
            kept.append(line)
        if len(kept) == len(lines):
            continue
        tmp = path.with_name("." + path.name + ".compact")
        tmp.unlink(missing_ok=True)  # leftover of a crash mid-compaction
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as out:
                out.write("".join(line + "\n" for line in kept))
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        removed[path.name] = len(lines) - len(kept)
    return removed


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

# Pin mode (#1958): a family member without a cross-signing identity is
# trusted per pinned device, so in-app verification cannot help; only an
# operator re-pin (``python -m telegram_bot.core.matrix.repin``) can.
NOTICE_UNPINNED_DEVICE = (
    "등록되지 않은 기기에서 보낸 메시지는 처리하지 않습니다. "
    "이 기기를 쓰려면 운영자에게 기기 등록을 요청해 주세요. 등록된 기기에서는 계속 이용할 수 있습니다."
)

NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,64}")
APPROVAL_TIMEOUT_S = 120.0
# #1959: /stop runs inside process_pending while matrix_lock is held; the
# runner's graceful stop is bounded so a hung provider cannot freeze sync and
# sending. The task cancellation that follows stays authoritative.
RUNNER_CANCEL_TIMEOUT_S = 10.0
# ``_RoomSink.approval_outcome`` results (#1959).
APPROVAL_ALLOW = "allow"
APPROVAL_DENY = "deny"
APPROVAL_TIMEOUT = "timeout"
APPROVAL_UNAVAILABLE = "unavailable"
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


NOTICE_PARTS_UNDELIVERED = (
    "⚠️ 직전 답변 {total}개 조각 중 {failed}개를 Matrix 서버가 거부해 전달하지 못했습니다. "
    "필요하면 나눠서 다시 요청해 주세요."
)
# Responses that say "try again", not "this request is wrong": they take the
# ConnectionError path (network-retry with backoff), never quarantine or stop.
# 408 Request Timeout and 425 Too Early are transient by definition (RFC 9110
# / RFC 8470); 429 and the 5xx gateway family were already retried. Every
# other 4xx describes the request itself, so repeating it cannot help.
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
# Bounded, body-free errcode taken from a homeserver error response.
_ERRCODE_RE = re.compile(r"M_[A-Z0-9_]{1,60}")
_ERROR_BODY_CAP = 4_096
# #1959: a server-requested wait (429 ``retry_after_ms`` / ``Retry-After``) is
# honoured but bounded, and a leg that ran this long before failing is treated
# as healthy again, so its backoff restarts at 1 s instead of staying at 30 s.
_RETRY_AFTER_CAP_S = 300.0
_HEALTHY_RUN_S = 60.0


class MatrixTemporaryError(ConnectionError):
    """A retryable homeserver status; ``str()`` stays ``matrix-temporary-error``.

    ``retry_after`` (seconds, bounded) carries the server's requested wait when
    it sent one (``Retry-After`` header or 429 body ``retry_after_ms``).
    """

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("matrix-temporary-error")
        self.retry_after = retry_after


class MatrixHTTPError(SafetyStop):
    """A non-retryable homeserver response; ``str()`` stays ``matrix-http-<status>``.

    Still a :class:`SafetyStop`, so every caller that does not handle it keeps
    fail-closing exactly as before (and ``meta.health.reason`` is unchanged).
    Only the outbox (:meth:`MatrixTransport.send`) looks inside: it downgrades
    a too-large part to plain text and quarantines other rejected parts.
    """

    def __init__(self, status: int, errcode: str = "") -> None:
        super().__init__("matrix-http-" + str(status))
        self.status = status
        self.errcode = errcode if _ERRCODE_RE.fullmatch(errcode or "") else ""

    @property
    def too_large(self) -> bool:
        return self.status == 413 or self.errcode == "M_TOO_LARGE"

    @property
    def part_rejected(self) -> bool:
        """A 4xx that condemns this one event rather than the service.

        401 (token revoked/expired) and 403 (bot no longer allowed in the room)
        stay fatal: skipping on them would silently discard every later reply
        while the real fault — credentials or membership, both trust
        boundaries — goes unnoticed. The service stops and reports
        ``matrix-http-401/403`` so an operator reconciles it.
        """
        return 400 <= self.status < 500 and self.status not in (401, 403) and self.status not in RETRYABLE_STATUSES


def message_content(text: str, *, msgtype: str = "m.text") -> dict[str, Any]:
    """``m.text`` content with a Matrix-HTML ``formatted_body`` when the text has markup.

    Rendering happens at send time so every outgoing message (replies and
    notices alike) goes through the same escaping renderer; plain text stays
    a bare ``body``. Kept as the transport's name for
    :func:`~telegram_bot.core.matrix.render.event_content`. ``msgtype``
    ``m.notice`` is only for quiet bot status lines (#2088).
    """

    from telegram_bot.core.matrix.render import event_content

    return event_content(text, msgtype=msgtype)


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
    """``run``/``cancel`` are required. Optional hooks, looked up with ``getattr``:
    ``stop_idle(job)``, and (#2088) ``turn_closed(job)`` once a turn's reply
    row is queued and ``delivered(job)`` after an outbox row went out — both
    run in the background and fail open.
    """

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
        from telegram_bot.core.matrix.render import trim_to_event

        # One event, no chunking: sized for the edit form (text carried twice)
        # so the same bubble text fits whether it is posted or edited (#1956).
        text = trim_to_event(text, edit=True)
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
        """Legacy bool form; ``arguments=None`` posts ``description`` alone."""

        text = str(description)
        if arguments is not None:
            text += "\n" + json.dumps(arguments, ensure_ascii=False, default=str)
        return await self.approval_outcome(text) == APPROVAL_ALLOW

    async def approval_outcome(self, text: str) -> str:
        """Post an already-redacted approval prompt and return how it ended.

        One of ``allow`` / ``deny`` (the sender's ``/approve`` / ``/deny``),
        ``timeout``, or ``unavailable`` (turn inactive, over the size or
        pending cap -- nothing was posted). The caller owns redaction (#1959):
        this layer never serializes provider arguments itself.
        """

        transport = self.transport
        if not self._active():
            return APPROVAL_UNAVAILABLE
        text = str(text)
        if len(text.encode()) > MAX_APPROVAL_TEXT_BYTES or len(transport.approvals) >= MAX_PENDING_APPROVALS:
            return APPROVAL_UNAVAILABLE
        nonce = secrets.token_urlsafe(24)
        if not NONCE_PATTERN.fullmatch(nonce) or nonce in transport.approvals:
            return APPROVAL_UNAVAILABLE
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        transport.approvals[nonce] = future
        try:
            transport.store.notice(
                self.request,
                "approval-" + nonce,
                text + "\n승인: /approve " + self.tid + " " + nonce + "\n거절: /deny " + self.tid + " " + nonce,
            )
            async with asyncio.timeout(transport.approval_timeout):
                return APPROVAL_ALLOW if await future else APPROVAL_DENY
        except TimeoutError:
            return APPROVAL_TIMEOUT
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
        runner_cancel_timeout: float = RUNNER_CANCEL_TIMEOUT_S,
    ) -> None:
        self.c = config
        self.runner = runner
        self.approval_timeout = approval_timeout
        self.runner_cancel_timeout = runner_cancel_timeout
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
        # Liveness facts for the frontend's health.json (#1820), in memory and
        # body-free: last committed sync (wall clock), per-leg retry counts and
        # a safe label of the last retried error, and the outbox head as first
        # observed by health_signals() (the jobs table carries no timestamps).
        self.last_sync_at: float | None = None
        self.leg_failures: dict[str, int] = {"receive": 0, "send": 0}
        self.leg_error: dict[str, str] = {"receive": "", "send": ""}
        self._outbox_head: tuple[str, float, int] | None = None  # (event id, first seen, send-failure baseline)
        # Set when the active turn hit ``turn_timeout``, before the runner is
        # cancelled, so the runner can tell a timeout from /stop or shutdown.
        self.turn_timed_out = False
        # Consecutive outbox parts quarantined on a 4xx; any successfully sent
        # part resets it. Health reads it (getattr, #1963): a streak means
        # replies are being dropped even though the service keeps running.
        self.delivery_rejections_streak = 0

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
            if response.status in RETRYABLE_STATUSES:
                raise MatrixTemporaryError(await self._retry_after(response))
            if response.status != 200:
                raise MatrixHTTPError(response.status, await self._errcode(response))
            body = bytearray()
            async for chunk in response.content.iter_chunked(65_536):
                body.extend(chunk)
                if len(body) > 4_194_304:
                    raise SafetyStop("matrix-response-too-large")
            return json.loads(body)

    @staticmethod
    async def _retry_after(response: Any) -> float | None:
        """Server-requested wait in seconds, bounded; ``None`` when absent or unreadable."""

        value: float | None = None
        try:
            header = (getattr(response, "headers", None) or {}).get("Retry-After")
            if header is not None and str(header).strip().isdigit():
                value = float(str(header).strip())
            elif response.status == 429:
                body = bytearray()
                async for chunk in response.content.iter_chunked(_ERROR_BODY_CAP):
                    body.extend(chunk)
                    if len(body) >= _ERROR_BODY_CAP:
                        break
                raw = json.loads(bytes(body[:_ERROR_BODY_CAP])).get("retry_after_ms")
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    value = float(raw) / 1000.0
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the status alone still makes it retryable
            return None
        if value is None or value != value or value < 0:
            return None
        return min(value, _RETRY_AFTER_CAP_S)

    @staticmethod
    async def _errcode(response: Any) -> str:
        """Best-effort ``errcode`` of an error response (read at most 4 KiB, never logged)."""
        body = bytearray()
        try:
            async for chunk in response.content.iter_chunked(_ERROR_BODY_CAP):
                body.extend(chunk)
                if len(body) >= _ERROR_BODY_CAP:
                    break
            errcode = json.loads(bytes(body[:_ERROR_BODY_CAP])).get("errcode")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the status alone still classifies the error
            return ""
        return errcode if isinstance(errcode, str) and _ERRCODE_RE.fullmatch(errcode) else ""

    async def download_media(self, mxc: str, *, max_bytes: int) -> bytes:
        """Authenticated media download (#1795); errors never stop the service."""
        from telegram_bot.core.matrix.media import download_ciphertext

        return await download_ciphertext(self.http, self.c["homeserver"], mxc, max_bytes=max_bytes)

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
        # Before nio loads the store: shrink trust files bloated by the old
        # verify-on-every-sync loop (see compact_trust_files).
        compacted = compact_trust_files(crypto)
        if compacted:
            self.store.set_meta("trust_store_compacted", {"removed": compacted, "updated": time.time()})
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
            self._verify(stored)
        for user in sorted(self.identities):
            self._trust_cross_signed(user, raw)
        self._trust_pinned_family()

    def _trust_pinned_family(self) -> None:
        """Trust exactly the pinned family devices currently present with their pinned keys.

        Family devices are pinned per user. Extra unpinned family devices
        stay merely untrusted and are handled by exclude_unpinned_devices at
        session-share time. A pinned family device that is gone (signed out,
        deleted) is contained the same way (#1958): it leaves the trusted
        table until it reappears with its pinned keys, so its events get the
        unpinned-device notice and sends stop expecting it, while the owner
        room and the rest of the family keep working.

        A pinned device id presenting *different* keys stays fatal: normal
        clients never re-key a device id (a new login is a new device), so
        that is homeserver-level key injection — by the same homeserver that
        serves the owner's device list — and needs an operator.
        """
        missing: dict[str, list[str]] = {}
        for user, pins in sorted(self.family_devices.items()):
            if user in self.identities:
                continue
            present: dict[str, str] = {}
            for device, pin in sorted(pins.items()):
                stored = self.client.device_store[user].get(device)
                if stored is None:
                    missing.setdefault(user, []).append(device)
                    continue
                if stored.ed25519 != pin["ed25519"] or stored.curve25519 != pin["curve25519"]:
                    raise SafetyStop("pinned-device-key-changed")
                self._verify(stored)
                present[device] = pin["curve25519"]
            self.trusted[user] = present
        if (self.store.get_meta("family_pins_missing") or {}) != missing:
            if missing:
                logger.warning(
                    "matrix pinned family devices missing (contained): %s",
                    ", ".join(f"{user}:{'/'.join(devices)}" for user, devices in missing.items()),
                )
            self.store.set_meta("family_pins_missing", missing)

    def _verify(self, device: Any) -> None:
        """Mark ``device`` verified only when it is not already.

        nio's ``verify_device``/``blacklist_device`` report a change on every
        call with the default file trust store, and a reported change drops
        the outbound megolm session of every room the user is in — so an
        unconditional call on each sync forced a fresh room-key share before
        every message and grew the trust files (see compact_trust_files).
        """
        if not getattr(device, "verified", False):
            self.client.verify_device(device)

    def _blacklist(self, device: Any) -> None:
        """Blacklist ``device`` only when it is not already (see :meth:`_verify`)."""
        if not getattr(device, "blacklisted", False):
            self.client.blacklist_device(device)

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
                self._verify(stored)
                trusted[device] = stored.curve25519
            else:
                self._blacklist(stored)
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
        try:
            encryption = await self.raw("GET", path + "/state/m.room.encryption")
        except MatrixHTTPError as exc:
            # #1959: an unencrypted room has no m.room.encryption state event,
            # which the homeserver answers with 404 — the same verdict as a
            # non-Megolm algorithm, not an opaque matrix-http-404 stop.
            if exc.status == 404:
                raise SafetyStop("encrypted-room-required") from None
            raise
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

    def enqueue_notice(
        self, room_id: str, text: str, *, key: str | None = None, msgtype: str = "m.text"
    ) -> str:
        """Queue unsolicited output (async completion, reminders) for an allowed room.

        Delivered by :meth:`send` with the same chunking, pinning and room
        gate as a reply; a muted room keeps it until the gate reopens. A
        caller-supplied ``key`` makes the notice idempotent (same key + same
        text → queued once), e.g. the startup banner across restarts.
        ``msgtype="m.notice"`` delivers it as a notice clients do not ping on
        (the external-wait status line, #2088); the default stays ``m.text``.
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
        event_id = self.store.notice(req, key or unique_key("unsolicited"), text, msgtype=msgtype)
        self.wake()
        return event_id

    def enqueue_file(
        self, room_id: str, path: str, *, key: str, after: str | None = None,
        root: str | None = None, voice: bool = False,
    ) -> str:
        """Queue one agent deliverable for a *direct* room (#2001); idempotent per ``key``.

        Family rooms are refused: a file an owner-host agent names must not
        land in a room other people read. The file is read, encrypted and
        uploaded only when :meth:`send` reaches the row, after the answer.
        """
        if room_id not in self.c["rooms"]:
            raise ValueError("room-not-allowed")
        if self.room_kind(room_id) != "direct":
            raise ValueError("file-room-not-direct")
        file_path = Path(path)
        if not file_path.is_absolute():
            raise ValueError("file-path-not-absolute")
        record: dict[str, Any] = {"v": 1, "path": str(file_path), "name": file_path.name}
        if voice:
            record["voice"] = True
        if root:
            record["root"] = str(root)
        payload = json.dumps(record, ensure_ascii=False)
        req = Request(
            "$outbound-file-" + hashlib.sha256(room_id.encode()).hexdigest()[:32],
            room_id,
            self.c["account"],
            "file",
            hashlib.sha256(json.dumps([self.c["account"], room_id, "outbound-file"]).encode()).hexdigest(),
        )
        if after:
            # Held until ``after``'s answer is recorded, then queued behind it;
            # dropped if that turn is cancelled, times out or is interrupted.
            self.store.hold_file(after, req, key, payload)
            return ""
        event_id = self.store.file_job(req, key, payload)
        self.wake()
        return event_id

    def enqueue_voice(self, room_id: str, data: bytes, *, key: str, after: str) -> None:
        """Persist generated speech privately until its durable outbox row is settled."""
        from telegram_bot.core.matrix import media, voice

        if room_id not in self.c["rooms"] or self.room_kind(room_id) != "direct":
            raise ValueError("voice-room-not-direct")
        if not data or len(data) > MAX_OUTBOUND_FILE_BYTES:
            raise ValueError("voice-size-invalid")
        directory = Path(self.c["state_directory"]) / voice.REPLY_DIRNAME
        path = media.store(directory, data, {"name": "voice.ogg", "mimetype": "audio/ogg"})
        try:
            self.enqueue_file(room_id, str(path.resolve()), key=key, after=after, root=str(directory.resolve()), voice=True)
            self.store._cleanup_voice_replies()  # duplicate keys must not leave a second file
        except BaseException:
            media.remove(path)
            raise

    async def _upload_cap(self) -> int:
        """Bridge cap (50 MB, as Telegram) or the homeserver's ``m.upload.size`` if lower."""
        cached = getattr(self, "_upload_cap_cache", None)
        if cached is None:
            from telegram_bot.core.matrix.outbound_media import upload_limit

            server = await upload_limit(self.http, self.c["homeserver"])
            if not server:
                return MAX_OUTBOUND_FILE_BYTES  # unknown: ask again next time
            cached = min(MAX_OUTBOUND_FILE_BYTES, server)
            self._upload_cap_cache = cached
        return int(cached)

    async def _deliver_file(self, job: Mapping[str, Any]) -> bool:
        """Encrypt, upload and send one queued file row (#2001), then mark it delivered.

        A file that is gone, unreadable, too large or refused by the homeserver
        gets one fixed notice naming it and the row completes — it never
        blocks the rows behind it. A retryable transport error propagates to
        the send leg's backoff and the row is retried from the start (the
        upload is repeated; the room event keeps one transaction id).
        """
        from telegram_bot.core.matrix.outbound_media import (
            OutboundMediaError,
            encrypt,
            file_content,
            mimetype_of,
            read_deliverable,
            upload,
        )

        event_id, room = job["event_id"], job["room_id"]
        try:
            payload = json.loads(job["reply"])
            path = Path(str(payload["path"]))
            name = str(payload.get("name") or path.name)[:255]
            root = Path(str(payload["root"])) if payload.get("root") else None
        except (ValueError, KeyError, TypeError):
            self.store.delivered(event_id)
            return True
        if room in self.blocked:
            return False  # a muted room keeps the row; nothing is uploaded meanwhile
        try:
            cap = await self._upload_cap()
            plaintext = await asyncio.to_thread(read_deliverable, path, max_bytes=cap, root=root)
            ciphertext, file_info = await asyncio.to_thread(encrypt, plaintext)
            mxc = await upload(self.http, self.c["homeserver"], ciphertext)
            content = file_content(name, mimetype_of(path), len(plaintext), mxc, file_info, voice=payload.get("voice") is True)
            async with self.matrix_lock:
                if room in self.blocked:
                    return False
                txn = hashlib.sha256((job["txn_id"] + ":file").encode()).hexdigest()
                await self._encrypted_raw(room, "m.room.message", content, txn)
        except OutboundMediaError as exc:
            logger.info("matrix outbound file not sent reason=%s room=%s", exc.reason, room)
            self._count_file_failure(exc.reason)
            self._file_unsent_notice(job, name)
        except ConnectionError:
            # Retryable, but bounded: the outbox is one ordered queue, so a
            # file whose upload keeps failing would hold every later reply in
            # every room behind it (review of #2010).
            if self._file_attempt_exhausted(event_id):
                logger.info("matrix outbound file gave up after retries room=%s", room)
                self._count_file_failure("retries-exhausted")
                self._file_unsent_notice(job, name)
                self.store.delivered(event_id)
                return True
            raise
        except MatrixHTTPError as exc:
            if not exc.part_rejected:
                raise
            logger.info("matrix outbound file event rejected status=%s room=%s", exc.status, room)
            self._count_file_failure("event-rejected")
            self._file_unsent_notice(job, name)
        else:
            logger.info("matrix outbound file sent room=%s", room)
        self._forget_file_attempts(event_id)
        self.store.delivered(event_id)
        return True

    def _file_attempt_exhausted(self, event_id: str) -> bool:
        attempts = self.store.get_meta("outbound_file_attempts") or {}
        count = int(attempts.get(event_id) or 0) + 1
        if count >= MAX_OUTBOUND_FILE_ATTEMPTS:
            attempts.pop(event_id, None)
            self.store.set_meta("outbound_file_attempts", attempts)
            return True
        attempts[event_id] = count
        self.store.set_meta("outbound_file_attempts", dict(list(attempts.items())[-50:]))
        return False

    def _forget_file_attempts(self, event_id: str) -> None:
        attempts = self.store.get_meta("outbound_file_attempts") or {}
        if event_id in attempts:
            attempts.pop(event_id, None)
            self.store.set_meta("outbound_file_attempts", attempts)

    def _file_unsent_notice(self, job: Mapping[str, Any], name: str) -> None:
        """One notice per failed file row; the key carries the text digest.

        Notice rows are permanent and a same-key/different-text notice stops
        the service, so a reworded text in a later release must be a new key.
        """
        text = NOTICE_FILE_UNSENT.format(name=name)
        key = "file-unsent-" + hashlib.sha256(text.encode()).hexdigest()[:12]
        self.store.notice(self.as_request(job), key, text)

    def _count_file_failure(self, reason: str) -> None:
        counts = self.store.get_meta("outbound_file_failures") or {}
        counts[reason] = int(counts.get(reason) or 0) + 1
        counts["updated"] = time.time()
        self.store.set_meta("outbound_file_failures", counts)

    def enqueue_self_job(self, room_id: str, body: str, *, key: str, sender: str | None = None) -> str:
        """Queue a turn the frontend runs for ``sender`` in ``room_id`` (#1895 PR-A2).

        ``sender`` defaults to the configured owner. A non-owner sender (#1955)
        must be a ``family_users`` member and the room a family room — the
        same pairing inbound admission allows. Always an allowed room: the job
        then goes through the normal claim → runner.run(sink) → finish path, so
        the work happens inside the transport's single-turn discipline instead
        of beside it. Idempotent per ``key``.
        """
        if room_id not in self.c["rooms"]:
            raise ValueError("room-not-allowed")
        if sender is None:
            sender = self.c["owner"]
        elif sender != self.c["owner"] and (
            sender not in self.family_users or room_id not in self.family_rooms
        ):
            raise ValueError("sender-not-allowed")
        event_id = self.store.self_job(room_id, sender, body, key=key)
        self.wake()
        return event_id

    # -- input ----------------------------------------------------------------

    async def input(self, req: Request) -> None:
        if self.store.seen_control(req, record=False):
            return
        if req.attachment is None and req.body.startswith(CONTROL_PREFIXES):
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
        elif fields == ["/stop"] and await self._stop_idle(req):
            # #1825: with no turn running in the sender's scope, a bare /stop
            # still cancels that conversation's queued auto-continuations.
            self.store.notice(req, "control", NOTICE_CONTINUATIONS_CANCELLED)
            self.wake()
            return
        self.store.notice(req, "invalid-control", NOTICE_INVALID_CONTROL)
        self.wake()

    async def _stop_idle(self, req: Request) -> bool:
        """Optional runner seam: cancel queued background work for an idle scope."""

        stop_idle = getattr(self.runner, "stop_idle", None)
        if not callable(stop_idle):
            return False
        try:
            return bool(await stop_idle({"room_id": req.room_id, "sender": req.sender}))
        except Exception:
            logger.warning("Matrix idle /stop hook failed", exc_info=True)
            return False

    async def _cancel_active(self) -> None:
        job, task = self.active, self.turn_task
        if job is None or task is None:
            return
        self.cancel_requested = True
        try:
            await asyncio.wait_for(self.runner.cancel(job), timeout=self.runner_cancel_timeout)
        except TimeoutError:
            logger.warning("matrix runner cancel exceeded %.0fs; cancelling the turn task", self.runner_cancel_timeout)
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
            self.last_sync_at = time.time()
            self.leg_failures["receive"] = 0
            self.store.set_meta("health", {"state": "ready", "updated": self.last_sync_at})

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
        if self._is_media(event):
            return self._admit_media(room, event)
        if not isinstance(event, RoomMessageText):
            self._unsupported_kind(room, event)
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
            if event.sender != self.c["owner"]:
                # Pin mode family member (#1958): pin_devices tolerates extra
                # unpinned family devices, so a message from one is expected,
                # not an anomaly. Contain it to this event: never processed,
                # one notice per device, and the sync batch still commits so
                # the owner's room and every other room keep working.
                self._untrusted_sender(room, event, pinned=True)
                return None
            # The owner's pinned device set and keys were just re-checked by
            # pin_devices (a set/key change already stops there), so an owner
            # event outside that set is anomalous key material on the
            # operator's own channel: stay fail-closed.
            raise SafetyStop("unverified-owner-event")
        now_ms = int(time.time() * 1000)
        req = self.policy.admit(room, event.source, decrypted=event.decrypted, now_ms=now_ms)
        if req is None:
            reason = self.policy.rejection(room, event.source, decrypted=event.decrypted, now_ms=now_ms)
            if reason is not None:
                self._rejected(room, event, reason)
        return req

    def _rejected(self, room: str, event: Any, reason: str) -> None:
        """Tell the sender once why a message was not read (#2002); body-free record.

        An edit is answered once per *edited* message, so fixing a typo twice
        does not produce two notices. The notice key carries the text digest:
        notice rows are permanent and ``store.notice`` stops the service on a
        same-key/different-text conflict, so rewording a notice in a later
        release must yield a new key, never a conflict on an old message.
        """
        source = getattr(event, "source", None)
        content = source.get("content") if isinstance(source, dict) else None
        raw_id = source.get("event_id") if isinstance(source, dict) else None
        event_id = str(getattr(event, "event_id", "") or raw_id or "")
        if reason == REJECT_EDIT and isinstance(content, dict):
            relation = content.get("m.relates_to")
            target = relation.get("event_id") if isinstance(relation, dict) else None
            if identifier(target, "$"):
                event_id = str(target)
        if not identifier(event_id, "$"):
            return
        if reason == REJECT_TEXT_TOO_LARGE:
            body = content.get("body") if isinstance(content, dict) else ""
            size_kib = -(-len(str(body).encode("utf-8", "replace")) // 1024)
            text = NOTICE_TEXT_TOO_LARGE.format(size=size_kib, limit=MAX_TEXT_BYTES // 1024)
        elif reason == REJECT_EDIT:
            text = NOTICE_EDIT_IGNORED
        else:
            text = NOTICE_THREAD_IGNORED
        sender = str(getattr(event, "sender", ""))
        req = Request(event_id, room, sender, "notice", scope_of(self.c["account"], room, sender))
        if self._notice_once(req, f"rejected-{reason}", text):
            self._count_ignored(room, reason)
            logger.info("matrix message not read reason=%s room=%s", reason, room)

    def _notice_once(self, req: Request, key: str, text: str) -> bool:
        """Queue a versioned idempotent notice; ``True`` only the first time (#2002)."""
        versioned = f"{key}-{hashlib.sha256(text.encode()).hexdigest()[:12]}"
        if self.store.has_notice(req.event_id, versioned):
            return False
        self.store.notice(req, versioned, text)
        return True

    def _unsupported_kind(self, room: str, event: Any) -> None:
        """Stickers, emotes and m.notice from a trusted sender: one notice per direct room (#2002).

        Membership, reactions and other state events also land here and stay
        silent; only message kinds a person sends expecting a reply count.
        Events older than the 24 h admission window are ignored, as in
        ``Policy.admit``.
        """
        import nio

        kinds = tuple(
            cls
            for cls in (
                getattr(nio, "StickerEvent", None),
                getattr(nio, "RoomMessageEmote", None),
                getattr(nio, "RoomMessageNotice", None),
            )
            if isinstance(cls, type)
        )
        if not kinds or not isinstance(event, kinds):
            return
        if not getattr(event, "decrypted", False) or not getattr(event, "verified", False):
            return
        stamp = getattr(event, "server_timestamp", None)
        if not isinstance(stamp, int) or stamp < int(time.time() * 1000) - 86_400_000:
            return
        sender = str(getattr(event, "sender", "") or "")
        trusted_keys = set(self.trusted.get(sender, {}).values())
        if sender not in self.policy.users or getattr(event, "sender_key", None) not in trusted_keys:
            return
        event_id = str(getattr(event, "event_id", "") or "")
        if not identifier(event_id, "$"):
            return
        kind = type(event).__name__
        seen = self.store.get_meta("unsupported_kind_counted") or []
        if event_id in seen:
            return  # a replayed sync batch
        self.store.set_meta("unsupported_kind_counted", (seen + [event_id])[-50:])
        self._count_ignored(room, kind)
        if self.policy.rooms.get(room) != "direct":
            return
        notified = self.store.get_meta("unsupported_kind_notified") or {}
        if room in notified:
            return
        req = Request(event_id, room, sender, "notice", scope_of(self.c["account"], room, sender))
        # Queue first, then remember: a crash in between re-queues the same
        # (idempotent) notice instead of losing it.
        self._notice_once(req, "unsupported-kind", NOTICE_UNSUPPORTED_KIND)
        notified[room] = time.time()
        self.store.set_meta("unsupported_kind_notified", notified)

    def _count_ignored(self, room: str, reason: str) -> None:
        """Body-free counter of messages not read, surfaced in the inbox meta."""
        counts = self.store.get_meta("ignored_messages") or {}
        counts[reason] = int(counts.get(reason) or 0) + 1
        counts["updated"] = time.time()
        counts["room"] = room
        self.store.set_meta("ignored_messages", counts)

    @staticmethod
    def _is_media(event: Any) -> bool:
        """An encrypted or plaintext nio media event (image/file/video/audio)."""
        import nio

        kinds = tuple(
            cls
            for cls in (getattr(nio, "RoomEncryptedMedia", None), getattr(nio, "RoomMessageMedia", None))
            if isinstance(cls, type)
        )
        if kinds and isinstance(event, kinds):
            return True
        return MatrixTransport._is_malformed_media(event)

    @staticmethod
    def _is_malformed_media(event: Any) -> bool:
        """A decrypted media message nio could not validate (#2159).

        nio parses every decrypted ``m.image``/``m.file``/... as
        ``RoomEncrypted*``, whose schema requires ``content.file``; a client
        that sends plaintext ``url`` media inside an encrypted room therefore
        yields a ``BadEvent``, which used to fall through without a trace.
        """
        import nio

        bad = getattr(nio, "BadEvent", None)
        if not isinstance(bad, type) or not isinstance(event, bad) or not getattr(event, "decrypted", False):
            return False
        source = getattr(event, "source", None)
        content = source.get("content") if isinstance(source, dict) else None
        return isinstance(content, dict) and content.get("msgtype") in MEDIA_MSGTYPES

    def _admit_media(self, room: str, event: Any) -> Request | None:
        """Admit a photo/file under the text rules (#1795); never a new stop path.

        Media used to be dropped silently, so an unverified device is ignored
        (with the cross-signing notice) rather than raising ``SafetyStop``, and
        plaintext ``url`` media is refused and recorded, never executed. A
        trusted sender is told once per refused event (#2159).
        """
        source = getattr(event, "source", None)
        content = source.get("content") if isinstance(source, dict) else None
        msgtype = content.get("msgtype") if isinstance(content, dict) else None
        kind = MEDIA_MSGTYPES.get(msgtype, "unknown") if isinstance(msgtype, str) else "unknown"
        if not event.decrypted or not isinstance(content, dict) or not isinstance(content.get("file"), dict):
            self._media_ignored(room, "plaintext-attachment-refused", kind)
            self._attachment_refused(room, event)
            return None
        trusted = self.trusted.get(event.sender, {})
        if not event.verified or event.sender_key not in set(trusted.values()):
            if event.sender in self.identities:
                self._untrusted_sender(room, event)
            elif event.sender != self.c["owner"]:
                self._untrusted_sender(room, event, pinned=True)
            self._media_ignored(room, "untrusted-device", kind)
            return None
        if self._is_malformed_media(event):
            self._media_ignored(room, "malformed-attachment", kind)
            self._attachment_refused(room, event)
            return None
        req = self.policy.admit(room, source, decrypted=True, now_ms=int(time.time() * 1000))
        if req is None or req.attachment is None:
            self._media_ignored(room, "not-admitted", kind)
            return None
        logger.info("matrix media admitted kind=%s room=%s", kind, room)
        return req

    def _media_ignored(self, room: str, reason: str, kind: str) -> None:
        """Body-free record of a dropped attachment (no file name, URL or key)."""
        logger.info("matrix media ignored reason=%s kind=%s room=%s", reason, kind, room)
        self.store.set_meta("media_ignored", {"room": room, "reason": reason, "kind": kind, "updated": time.time()})

    def _attachment_refused(self, room: str, event: Any) -> None:
        """Tell a trusted sender once per event that an attachment was not read (#2159).

        Only a decrypted event from a verified, trusted device of an allowed
        sender gets a reply, within the 24 h admission window, and only where
        the bot would have read it: a direct room, or a family room that
        addresses the bot. Anything else stays a body-free record.
        """
        if not getattr(event, "decrypted", False) or not getattr(event, "verified", False):
            return
        sender = str(getattr(event, "sender", "") or "")
        if sender not in self.policy.users:
            return
        if getattr(event, "sender_key", None) not in set(self.trusted.get(sender, {}).values()):
            return
        stamp = getattr(event, "server_timestamp", None)
        if not isinstance(stamp, int) or stamp < int(time.time() * 1000) - 86_400_000:
            return
        source = getattr(event, "source", None)
        content = source.get("content") if isinstance(source, dict) else None
        if not isinstance(content, dict):
            return
        if self.policy.rooms.get(room) != "direct" and not self.policy.addressed(content, media_caption(content)):
            return
        raw_id = source.get("event_id") if isinstance(source, dict) else None
        event_id = str(getattr(event, "event_id", "") or raw_id or "")
        if not identifier(event_id, "$"):
            return
        req = Request(event_id, room, sender, "notice", scope_of(self.c["account"], room, sender))
        if self._notice_once(req, "attachment-refused", NOTICE_ATTACHMENT_REFUSED):
            self._count_ignored(room, "attachment-refused")

    def _untrusted_sender(self, room: str, event: Any, *, pinned: bool = False) -> None:
        """Ignore a message from an unsigned/unpinned device; tell the room once per device.

        ``pinned`` selects the pin-mode text: the fix there is an operator
        re-pin, not in-app verification.
        """
        key = str(getattr(event, "sender_key", "") or "")
        seen = self.store.get_meta("untrusted_senders") or {}
        marker = f"{event.sender}:{key}"
        if marker in seen:
            return
        seen[marker] = {"room": room, "updated": time.time()}
        self.store.set_meta("untrusted_senders", dict(list(seen.items())[-50:]))
        req = Request(str(getattr(event, "event_id", "") or "$untrusted-" + hashlib.sha256(marker.encode()).hexdigest()[:24]),
                      room, event.sender, "notice", scope_of(self.c["account"], room, event.sender))
        if pinned:
            self.store.notice(req, "unpinned-device", NOTICE_UNPINNED_DEVICE)
        else:
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

    def _trusted_parent_sender(self, event: Any) -> bool:
        """Only decrypted/verified events from our device or a pinned allowed sender."""
        if not getattr(event, "decrypted", False) or not getattr(event, "verified", False):
            return False
        if event.sender == self.c["account"]:
            return True
        return event.sender in self.senders and event.sender_key in set(self.trusted.get(event.sender, {}).values())

    def _trusted_text(self, event: Any) -> bool:
        """A trusted text suitable for the bounded recent-text cache.

        Our own ``m.notice`` events (the external-wait status, #2088) count as
        text too, so a reply to one is quoted like a reply to any bot message.
        Other senders' notices stay out: those are never admitted (#2002).
        """
        import nio

        kinds: tuple[type, ...] = (nio.RoomMessageText,)
        notice = getattr(nio, "RoomMessageNotice", None)
        if isinstance(notice, type) and getattr(event, "sender", None) == self.c["account"]:
            kinds += (notice,)
        return (isinstance(event, kinds)
                and isinstance(getattr(event, "body", None), str)
                and self._trusted_parent_sender(event))

    def _remember_text(self, event_id: Any, room: str, sender: str, body: str) -> None:
        if not isinstance(event_id, str) or not event_id or not body.strip():
            return
        self.recent_text[event_id] = (room, sender, body)
        self.recent_text.move_to_end(event_id)
        while len(self.recent_text) > RECENT_TEXT_CAP:
            self.recent_text.popitem(last=False)

    async def _fetch_parent(self, room: str, event_id: str) -> ReplyParent | None:
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
            if not self._trusted_parent_sender(event):
                return None
            if self._trusted_text(event):
                self._remember_text(event_id, room, event.sender, event.body)
                return ReplyParent(event.sender, event.body)
            if self._is_media(event):
                source = getattr(event, "source", {})
                content = source.get("content") if isinstance(source, dict) else None
                attachment = media_attachment(content) if isinstance(content, dict) else None
                if attachment is not None:
                    description = media_caption(content) or f"Attached {attachment['kind']}: {attachment['name']}"
                    return ReplyParent(event.sender, description, encode_attachment(attachment))
            return None
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
        if req.reply_to is None or (req.attachment is None and (text.startswith("/") or text.isdigit())):
            return req
        cached = self.recent_text.get(req.reply_to)
        if cached is not None and cached[0] == req.room_id:
            parent: ReplyParent | None = ReplyParent(cached[1], cached[2])
        else:
            parent = await self._fetch_parent(req.room_id, req.reply_to)
        if parent is None:
            return req
        body = reply_context_body(
            req.body,
            parent_sender=parent.sender,
            parent_body=parent.body,
            account=self.c["account"],
            sender=req.sender,
            limit_bytes=MAX_TEXT_BYTES,
        )
        attachment = req.attachment
        if attachment is not None and body != req.body:
            current = decode_attachment(attachment)
            if current is not None:
                # The trusted parent prefix is meaningful input even when the
                # newly attached media originally had no caption.
                attachment = encode_attachment({**current, "captioned": True})
        return replace(req, body=body, attachment=attachment, reply_attachment=parent.attachment)

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
                if not await self._deliver(job):
                    continue  # room muted mid-row: the rest waits, like a muted room's replies
                # Bot-authored notices the runner tracks (the external-wait
                # status, #2088) learn their event id only now.
                self._runner_hook("delivered", job)
                if "done" in self.turn_timing.get(job["event_id"], {}):
                    self.mark(job["event_id"], "delivered")
                    self._finish_timing(job["event_id"])
            await self._idle(self.send_wake)

    async def _deliver(self, job: Mapping[str, Any]) -> bool:
        """Send every undelivered part of one outbox row, then mark it delivered.

        Parts are fence-aware 12 KB pieces re-split until each *encrypted
        event* fits the PDU limit (``event_chunks``, #1956). A part the
        homeserver rejects with a non-retryable 4xx (other than 401/403, see
        :attr:`MatrixHTTPError.part_rejected`) is recorded in
        ``delivery_failures`` and skipped *durably* — its part index advances
        in the same transaction — so a restart never replays it and the rows
        behind it keep flowing. Once the row is done, the room gets one
        fixed-text notice naming how many parts were lost; a notice about a
        failed notice is never queued, so this cannot loop.

        ``matrix_lock`` is taken per part, not per row: a 1 MiB reply is ~90
        events, and holding the lock across all of them would stall sync
        processing (and with it /cancel, /stop and approvals) for minutes.
        Between parts a sync batch re-pins devices and re-gates every room,
        so each part re-checks the room's mute and ``_encrypted_raw`` re-checks
        the recipients against the fresh trust state. The part index is only
        advanced after its send, and each part keeps its txn id, so ordering
        and idempotency are the same as before. Returns ``False`` when the
        room was muted mid-row (the row stays ready and resumes later).
        """
        from telegram_bot.core.matrix.render import event_chunks

        if job.get("body") == FILE_JOB_BODY and str(job["event_id"]).startswith("$file-"):
            return await self._deliver_file(job)
        event_id, room = job["event_id"], job["room_id"]
        msgtype = outbox_msgtype(job)
        chunks = event_chunks(job["reply"])
        for i in range(self.store.delivered_parts(event_id), len(chunks)):
            async with self.matrix_lock:
                if room in self.blocked:
                    return False
                tx = hashlib.sha256((job["txn_id"] + ":" + str(i)).encode()).hexdigest()
                try:
                    sent = await self._send_part(room, chunks[i], tx, msgtype=msgtype)
                except MatrixHTTPError as exc:
                    if not exc.part_rejected:
                        raise
                    self.delivery_rejections_streak += 1
                    logger.warning(
                        "Matrix outbox part rejected status=%s errcode=%s part=%d/%d turn=%s streak=%d",
                        exc.status, exc.errcode or "-", i + 1, len(chunks), turn_id(event_id),
                        self.delivery_rejections_streak,
                    )
                    self.store.skip_part(event_id, i, exc.status, exc.errcode)
                    continue
                self.delivery_rejections_streak = 0
                self._remember_text(sent, room, self.c["account"], chunks[i])
                self.store.mark_part(event_id, i + 1, sent)
        failed = self.store.failed_parts(event_id)
        if failed and not self.store.is_failure_notice(event_id):
            # Queued before the row is marked delivered: a crash in between
            # re-queues the same (idempotent) notice instead of losing it.
            self.store.failure_notice(
                self.as_request(job),
                NOTICE_PARTS_UNDELIVERED.format(failed=len(failed), total=len(chunks)),
            )
        self.store.delivered(event_id)
        return True

    async def _send_part(self, room: str, text: str, txn: str, *, msgtype: str = "m.text") -> str:
        """One outbox part; a too-large rejection is retried once as plain text.

        The size model should prevent 413s, but homeservers differ (some also
        answer ``400 M_TOO_LARGE``). Dropping ``formatted_body`` halves the
        event. The retry uses its own transaction id: the first attempt was
        rejected, so no event exists under ``txn`` to deduplicate against.
        """
        try:
            return await self.encrypted_send(room, text, txn, msgtype=msgtype)
        except MatrixHTTPError as exc:
            if not exc.too_large:
                raise
        from telegram_bot.core.matrix.render import event_content

        plain_txn = hashlib.sha256((txn + ":plain").encode()).hexdigest()
        return await self._encrypted_raw(
            room, "m.room.message", event_content(text, plain=True, msgtype=msgtype), plain_txn
        )

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

    async def encrypted_send(self, room: str, text: str, txn: str, *, msgtype: str = "m.text") -> str:
        return await self._encrypted_raw(room, "m.room.message", message_content(text, msgtype=msgtype), txn)

    async def encrypted_edit(
        self, room: str, text: str, replaces: str, txn: str, *, msgtype: str = "m.text"
    ) -> str:
        """m.replace edit of ``replaces``; the bubble keeps its original event id."""
        from telegram_bot.core.matrix.render import edit_content

        return await self._encrypted_raw(
            room, "m.room.message", edit_content(text, replaces, msgtype=msgtype), txn
        )

    async def redact(self, room: str, event_id: str, tag: str) -> None:
        txn = hashlib.sha256(("redact-" + tag).encode()).hexdigest()
        await self.raw(
            "PUT",
            "/_matrix/client/v3/rooms/" + quote(room, safe="") + "/redact/" + quote(event_id, safe="") + "/" + txn,
            {},
        )

    # -- bot-authored notice upkeep (#2088) ------------------------------------

    def notice_event(self, row_id: str) -> tuple[str, str | None]:
        """Where one queued notice stands: ``pending``, ``sent`` (+ event id) or ``gone``.

        ``gone`` covers an unknown row and a row that completed without
        producing an event (every part rejected, or the id predates #2088).
        """
        state, event = self.store.sent_event(row_id)
        if state == "ready":
            return "pending", None
        if state == "done" and event:
            return "sent", event
        return "gone", None

    async def edit_notice(self, room: str, event_id: str, text: str, *, msgtype: str = "m.text") -> bool:
        """Replace a bot-authored event's text in place (``m.replace``), outside the outbox.

        Like the progress bubble this is a direct, single-event send: the
        caller keeps its own record of what the event shows and retries on
        its next pass. ``False`` means the homeserver refused the edit for
        good (a 4xx other than 401/403); a transient failure or a muted room
        raises. ``msgtype`` must match the edited event's (``m.notice`` for
        the external-wait status, #2088); it is set on the fallback and on
        ``m.new_content``. A successful edit also refreshes the reply-context
        cache, so a reply to the event quotes what it shows now.
        """
        from telegram_bot.core.matrix.render import trim_to_event

        bounded_text(text, MAX_REPLY_BYTES)
        text = trim_to_event(text, edit=True)
        async with self.matrix_lock:
            if room in self.blocked:
                raise ConnectionError("room-muted")
            # A fresh txn per attempt: re-sending an earlier text (A -> B -> A)
            # under a reused txn id would be deduplicated into the old edit.
            txn = hashlib.sha256(("notice-edit-" + event_id + ":" + str(time.time_ns())).encode()).hexdigest()
            try:
                await self.encrypted_edit(room, text, event_id, txn, msgtype=msgtype)
            except MatrixHTTPError as exc:
                if exc.part_rejected:
                    return False
                raise
            self._remember_text(event_id, room, self.c["account"], text)
        return True

    async def redact_notice(self, room: str, event_id: str) -> bool:
        """Redact a bot-authored event; ``False`` when it is already gone or not redactable.

        A redaction carries no content, so a muted room does not hold it
        back. Retries reuse one transaction id per event (idempotent).
        """
        async with self.matrix_lock:
            try:
                await self.redact(room, event_id, "notice-" + event_id)
            except MatrixHTTPError as exc:
                if exc.part_rejected:
                    return False
                raise
        return True

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
                self._blacklist(self.client.device_store[user][device])

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
        self.turn_timed_out = False
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
            self.turn_timed_out = isinstance(exc, TimeoutError)
            if not self.turn_timed_out:
                # #1819: the room only gets the generic NOTICE_TURN_ERROR and
                # the outcome keeps just the type name, so without this line a
                # runner exception left no trace anywhere. Body-free: redacted,
                # bounded message plus the raise site, no chained traceback.
                logger.error(
                    "Matrix turn failed: error=%s: %s at=%s",
                    type(exc).__name__,
                    redact_credentials(" ".join(str(exc).split()))[:300],
                    _raise_site(exc),
                )
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
                if not shutting_down:
                    # After the reply row is ready: anything the runner queues
                    # from here sorts behind it in the outbox (#2088).
                    self._runner_hook("turn_closed", job)

    def _runner_hook(self, name: str, job: Mapping[str, Any]) -> None:
        """Run the runner's optional ``name(job)`` hook in the background, fail-open.

        Background so neither the work loop nor the send loop waits on the
        hook's own homeserver calls; the task is cancelled on :meth:`close`.
        """
        hook = getattr(self.runner, name, None)
        if not callable(hook):
            return

        async def call() -> None:
            try:
                await hook(job)
            except Exception as exc:
                logger.debug("Matrix runner hook %s failed: %s", name, type(exc).__name__)

        self._spawn(call())

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

    def health_signals(self, now: float | None = None) -> dict[str, Any]:
        """Body-free liveness snapshot for the frontend's health.json (#1820).

        Read-only apart from remembering when the current outbox head was
        first seen: ``send_failures`` counts send-leg retries since the head
        last changed, so one delivered reply clears an earlier failure.
        """

        now = time.time() if now is None else now
        pending = [job for job in self.store.outbox() if job["room_id"] not in self.blocked]
        head_age: float | None = None
        send_failures = 0
        if pending:
            head_id = str(pending[0]["event_id"])
            if self._outbox_head is None or self._outbox_head[0] != head_id:
                self._outbox_head = (head_id, now, self.leg_failures["send"])
            head_age = max(0.0, now - self._outbox_head[1])
            send_failures = self.leg_failures["send"] - self._outbox_head[2]
        else:
            self._outbox_head = None
        return {
            "last_sync_at": self.last_sync_at,
            "sync_age_s": None if self.last_sync_at is None else max(0.0, now - self.last_sync_at),
            "receive_failures": self.leg_failures["receive"],
            "receive_error": self.leg_error["receive"],
            "send_failures": send_failures,
            "send_error": self.leg_error["send"] if send_failures else "",
            "outbox_pending": len(pending),
            "outbox_head_age_s": head_age,
        }

    async def retry(self, operation: Any, leg: str = "") -> None:
        import aiohttp

        loop = asyncio.get_running_loop()
        delay = 1.0
        while True:
            started = loop.time()
            try:
                await operation()
                return
            except (ConnectionError, aiohttp.ClientError, TimeoutError) as exc:
                if leg:
                    self.leg_failures[leg] = self.leg_failures.get(leg, 0) + 1
                    self.leg_error[leg] = retry_label(exc)
                self.store.set_meta("health", {"state": "network-retry", "updated": time.time()})
                if loop.time() - started >= _HEALTHY_RUN_S:
                    delay = 1.0  # #1959: a long healthy run resets the backoff
                wait = delay
                retry_after = getattr(exc, "retry_after", None)
                if isinstance(retry_after, (int, float)) and retry_after > wait:
                    wait = float(retry_after)
                await asyncio.sleep(wait)
                delay = min(delay * 2, 30)

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self.retry(self.receive, leg="receive"))
            group.create_task(self.retry(self.send, leg="send"))
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


# Static ConnectionError tokens raised by this module; anything else (aiohttp
# errors can embed hosts or URLs) is reported by exception type name only.
_SAFE_RETRY_LABELS = frozenset({"matrix-temporary-error", "group-key-share-incomplete"})


def retry_label(exc: BaseException) -> str:
    text = str(exc)
    return text if text in _SAFE_RETRY_LABELS else type(exc).__name__


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
