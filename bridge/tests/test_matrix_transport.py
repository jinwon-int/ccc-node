"""Matrix transport (#1780 PR-2a): turn loop with an injected runner, controls, gates, delivery.

Ported from the family-messenger pilot's ``test_fleet_matrix`` (FrontendTests
and FamilyRoomTests). The pilot's fixture subprocess worker is replaced by
``FakeRunner``, an in-process ``TurnRunner``; ``nio``/``aiohttp`` are stubbed
because the transport imports them lazily and the test venv has neither.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import time
import types
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

import pytest

from telegram_bot.core.matrix import transport as t
from telegram_bot.core.matrix.state import MatrixStore, SafetyStop, turn_id
from telegram_bot.core.matrix.transport import (
    NOTICE_CANCELLED,
    NOTICE_RESTARTED,
    NOTICE_TIMEOUT,
    NOTICE_TURN_ERROR,
    NOTICE_UNDECRYPTABLE,
    NOTICE_UNTRUSTED_DEVICE,
    FAMILY_NOTICE,
    NOTICE_CONTROL_FORWARDED,
    NOTICE_INVALID_CONTROL,
    NOTICE_QUEUE_FULL,
    MatrixTransport,
    TurnResult,
    serve,
    stop_reason,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


FAMILY = "!family:test.invalid"
DAD = "@dad:test.invalid"
MOM = "@mom:test.invalid"
STRANGER = "@stranger:test.invalid"


def config(root: Path | str) -> dict[str, Any]:
    return dict(
        homeserver="http://127.0.0.1:18809",
        preview=True,
        account="@bot:test.invalid",
        owner="@owner:test.invalid",
        device_id="BOT",
        access_token="synthetic",
        pickle_key="x" * 32,
        rooms=["!room:test.invalid"],
        devices={"OWNER": {"ed25519": "a" * 43, "curve25519": "b" * 43}},
        state_directory=str(Path(root) / "state"),
        not_before_ms=0,
    )


def keyset(char: str) -> dict[str, str]:
    return {"ed25519": char * 43, "curve25519": char * 43}


def pinned_device(ed: str, curve: str | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(ed25519=ed * 43, curve25519=(curve or ed) * 43)


def family_config(root: Path | str) -> dict[str, Any]:
    c = config(root)
    return {
        **c,
        "rooms": [c["rooms"][0], FAMILY],
        "family_rooms": [FAMILY],
        "family_users": [DAD, MOM],
        "family_devices": {DAD: {"DAD1": keyset("c")}},
    }


def request(
    f: MatrixTransport,
    event: str = "$request",
    body: str = "synthetic prompt",
    room: str | None = None,
    sender: str | None = None,
) -> Any:
    now = int(time.time() * 1000)
    return f.policy.admit(
        room or f.c["rooms"][0],
        dict(
            type="m.room.message",
            event_id=event,
            sender=sender or f.c["owner"],
            origin_server_ts=now,
            content={"msgtype": "m.text", "body": body},
        ),
        decrypted=True,
        now_ms=now,
    )


def fake_nio() -> types.ModuleType:
    """Minimal stand-in for the nio module; the transport imports it lazily."""
    module = types.ModuleType("synthetic-nio")

    class MegolmEvent:
        pass

    class RoomMessageText:
        def __init__(
            self,
            sender: str = "",
            body: str = "",
            source: dict[str, Any] | None = None,
            verified: bool = True,
            decrypted: bool = True,
            sender_key: str = "",
            ts: int = 0,
        ) -> None:
            self.sender = sender
            self.body = body
            self.source = source or {}
            self.verified = verified
            self.decrypted = decrypted
            self.sender_key = sender_key
            self.server_timestamp = ts

    class KeysQueryResponse:
        @classmethod
        def from_dict(cls, raw: Any) -> KeysQueryResponse:
            return cls()

    class JoinedMembersResponse:
        @classmethod
        def from_dict(cls, raw: Any, room: str) -> JoinedMembersResponse:
            return cls()

    class SyncResponse:
        @classmethod
        def from_dict(cls, raw: Any) -> SyncResponse:
            response = cls()
            response.rooms = types.SimpleNamespace(join={})  # type: ignore[attr-defined]
            return response

    class AsyncClientConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class AsyncClient:
        instances: list[AsyncClient] = []

        def __init__(self, homeserver: str, account: str, **kwargs: Any) -> None:
            self.homeserver = homeserver
            self.account = account
            self.kwargs = kwargs
            self.olm = types.SimpleNamespace(account=types.SimpleNamespace(identity_keys={"ed25519": "agent-ed", "curve25519": "agent-cu"}))
            self.should_upload_keys = False
            self.keys_upload = AsyncMock(return_value=types.SimpleNamespace())
            self.receive_response = AsyncMock()
            self.close = AsyncMock()
            self.logins: list[tuple[str, str, str]] = []
            AsyncClient.instances.append(self)

        def restore_login(self, account: str, device: str, token: str) -> None:
            self.logins.append((account, device, token))

    for name, cls in [
        ("MegolmEvent", MegolmEvent),
        ("RoomMessageText", RoomMessageText),
        ("KeysQueryResponse", KeysQueryResponse),
        ("JoinedMembersResponse", JoinedMembersResponse),
        ("SyncResponse", SyncResponse),
        ("AsyncClientConfig", AsyncClientConfig),
        ("AsyncClient", AsyncClient),
    ]:
        setattr(module, name, cls)
    return module


def fake_aiohttp() -> types.ModuleType:
    module = types.ModuleType("synthetic-aiohttp")

    class ClientError(Exception):
        pass

    class ClientTimeout:
        def __init__(self, total: float) -> None:
            self.total = total

    class ClientSession:
        def __init__(self, headers: dict[str, str], timeout: Any) -> None:
            self.headers = headers
            self.timeout = timeout
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    module.ClientError = ClientError  # type: ignore[attr-defined]
    module.ClientTimeout = ClientTimeout  # type: ignore[attr-defined]
    module.ClientSession = ClientSession  # type: ignore[attr-defined]
    return module


class FakeRunner:
    """In-process TurnRunner with one behaviour per ``mode``."""

    def __init__(self, mode: str = "complete") -> None:
        self.mode = mode
        self.calls: list[dict[str, Any]] = []
        self.cancels: list[str] = []
        self.decisions: list[bool] = []
        self.interrupted = 0
        self.release = False
        self.started = asyncio.Event()

    async def run(self, job: Any, *, sink: Any, session_id: str | None, room_kind: str) -> TurnResult:
        self.calls.append(
            {"event_id": job["event_id"], "sender": job["sender"], "session_id": session_id, "room_kind": room_kind}
        )
        self.started.set()
        handler = getattr(self, "mode_" + self.mode.replace("-", "_"))
        result: TurnResult = await handler(sink)
        return result

    async def cancel(self, job: Any) -> bool:
        self.cancels.append(job["event_id"])
        if self.mode == "cancel-raises":
            raise RuntimeError("synthetic cancel failure")
        return True

    async def mode_complete(self, sink: Any) -> TurnResult:
        return TurnResult("synthetic answer", "synthetic-session")

    async def mode_empty(self, sink: Any) -> TurnResult:
        return TurnResult("", "synthetic-session", streamed=True)

    async def mode_uncertain(self, sink: Any) -> TurnResult:
        return TurnResult("", None, status="uncertain")

    async def mode_error(self, sink: Any) -> TurnResult:
        # MatrixBot reports ChatResponse(success=False) this way: the text is
        # the user-facing failure notice and nothing is still running.
        return TurnResult("❌ provider unavailable", "synthetic-session", status="error")

    async def mode_streamed(self, sink: Any) -> TurnResult:
        # The runner already delivered the answer through interim notices.
        await sink.interim("streamed answer")
        return TurnResult("streamed answer", "synthetic-session", streamed=True)

    async def mode_raise(self, sink: Any) -> TurnResult:
        raise RuntimeError("synthetic failure")

    async def mode_slow(self, sink: Any) -> TurnResult:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.interrupted += 1
            raise
        return TurnResult("late", None)

    async def mode_stubborn(self, sink: Any) -> TurnResult:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.interrupted += 1
            await asyncio.sleep(0.05)  # cleanup that may itself be cancelled
            raise
        return TurnResult("late", None)

    async def mode_immortal(self, sink: Any) -> TurnResult:
        while not self.release:
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                self.interrupted += 1
        return TurnResult("released", None)

    async def mode_interim(self, sink: Any) -> TurnResult:
        await sink.typing()
        await sink.interim("진행 중입니다")
        await sink.interim("")
        return TurnResult("synthetic answer", "synthetic-session")

    async def mode_approval(self, sink: Any) -> TurnResult:
        ok = await sink.approval("Synthetic action", {"value": 1})
        self.decisions.append(ok)
        return TurnResult("approved" if ok else "denied", "synthetic-session")

    async def mode_approval_big(self, sink: Any) -> TurnResult:
        ok = await sink.approval("x" * 13_000, None)
        self.decisions.append(ok)
        return TurnResult("denied", None)

    async def mode_approval_flood(self, sink: Any) -> TurnResult:
        results = await asyncio.gather(*[sink.approval("Synthetic action", i) for i in range(17)])
        self.decisions.extend(results)
        return TurnResult("flooded", None)

    async def mode_cancel(self, sink: Any) -> TurnResult:
        await sink.approval("Synthetic action", {"value": 1})
        await asyncio.sleep(3600)
        return TurnResult("never", None)

    mode_cancel_raises = mode_cancel


class Harness:
    def __init__(self, root: Path, mode: str, cfg: dict[str, Any] | None = None, **kwargs: Any) -> None:
        self.runner = FakeRunner(mode)
        self.f = MatrixTransport(cfg or config(root), self.runner, **kwargs)
        self.tasks: list[asyncio.Task[Any]] = []

    def start(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    def work(self) -> asyncio.Task[Any]:
        return self.start(self.f.work())

    async def until(self, predicate: Any) -> None:
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)

    def replies(self) -> list[str]:
        return [job["reply"] for job in self.f.store.outbox()]

    def drain(self) -> list[str]:
        """Mark every pending outbox row delivered (a scope claims nothing until its replies are out)."""
        delivered = []
        for job in self.f.store.outbox():
            self.f.store.delivered(job["event_id"])
            delivered.append(job["reply"])
        return delivered

    async def stop(self) -> None:
        self.runner.release = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.sleep(0.02)
        await self.f.close()


@asynccontextmanager
async def running(root: Path, mode: str = "complete", cfg: dict[str, Any] | None = None, **kwargs: Any) -> AsyncIterator[Harness]:
    h = Harness(root, mode, cfg, **kwargs)
    try:
        yield h
    finally:
        await h.stop()


# --------------------------------------------------------------------------- #
# Policy / construction
# --------------------------------------------------------------------------- #


class TestConstruction:
    def test_policy_changes_cannot_reroute_saved_jobs(self, tmp_path: Path) -> None:
        c = config(tmp_path)
        f = MatrixTransport(c, FakeRunner())
        f.store.close()
        with pytest.raises(SafetyStop, match="saved-policy-changed"):
            MatrixTransport({**c, "owner": "@changed:test.invalid"}, FakeRunner())
        f = MatrixTransport(c, FakeRunner())  # Failure released the process lock.
        f.store.close()

    def test_saved_policy_upgrade_drops_worker_keys_and_family_change_fails_closed(self, tmp_path: Path) -> None:
        c = config(tmp_path)
        f = MatrixTransport(c, FakeRunner())
        old = {k: c[k] for k in ("owner", "rooms", "devices", "not_before_ms")}
        old["worker_argv"] = ["/usr/bin/python3", "/opt/pilot/fleet_worker.py"]
        old["worker_argv_family"] = ["/opt/pilot/readonly"]
        old["remote_worker"] = True
        f.store.set_meta("policy", old)  # Pilot-era saved policy without family keys.
        f.store.close()
        f = MatrixTransport(c, FakeRunner())  # Upgrades silently: worker keys dropped, empty family.
        saved = f.store.get_meta("policy")
        assert saved["family_users"] == []
        assert "worker_argv" not in saved and "remote_worker" not in saved
        f.store.close()
        with pytest.raises(SafetyStop, match="saved-policy-changed"):
            MatrixTransport(
                {**c, "family_rooms": [c["rooms"][0]], "family_users": [DAD], "family_devices": {DAD: {"D1": keyset("d")}}},
                FakeRunner(),
            )
        f = MatrixTransport(c, FakeRunner())
        f.store.close()

    def test_turn_timeout_comes_from_config(self, tmp_path: Path) -> None:
        f = MatrixTransport(config(tmp_path), FakeRunner())
        assert f.turn_timeout == 21600.0  # fleet default: 6 h
        f.store.close()
        six_hours = MatrixTransport({**config(tmp_path), "turn_timeout_minutes": 360}, FakeRunner())
        assert six_hours.turn_timeout == 21600.0
        six_hours.store.close()
        with pytest.raises(SafetyStop, match="invalid-turn-timeout"):
            MatrixTransport({**config(tmp_path), "turn_timeout_minutes": 400}, FakeRunner())
        f = MatrixTransport(config(tmp_path), FakeRunner())  # refusal happened before state opened
        f.store.close()

    def test_family_config_is_rejected_before_state_opens(self, tmp_path: Path) -> None:
        base = family_config(tmp_path)
        with pytest.raises(SafetyStop, match="invalid-family-users"):
            MatrixTransport({**base, "family_users": [base["account"]]}, FakeRunner())
        assert not (tmp_path / "state").exists()
        f = MatrixTransport(base, FakeRunner())
        assert f.policy.rooms[FAMILY] == "mention"
        assert f.policy.rooms[base["rooms"][0]] == "direct"
        assert f.room_kind(FAMILY) == "family"
        assert f.room_kind(base["rooms"][0]) == "direct"
        f.store.close()

    def test_enqueue_self_job_runs_in_the_owner_scope_of_an_allowed_room(self, tmp_path: Path) -> None:
        f = MatrixTransport(config(tmp_path), FakeRunner())
        room = f.c["rooms"][0]
        with pytest.raises(ValueError, match="room-not-allowed"):
            f.enqueue_self_job("!other:test.invalid", '{"kind":"x"}', key="k")
        event = f.enqueue_self_job(room, '{"kind":"x"}', key="k")
        assert event.startswith("$self-")
        assert f.enqueue_self_job(room, '{"kind":"x"}', key="k") == event  # idempotent per key
        job = f.store.claim()
        assert job is not None and job["event_id"] == event
        assert job["sender"] == f.c["owner"] and job["room_id"] == room
        assert f.room_kind(room) == "direct"
        f.store.finish(event, "", None)
        f.store.close()

    def test_enqueue_notice_targets_allowed_rooms_with_unique_keys(self, tmp_path: Path) -> None:
        f = MatrixTransport(config(tmp_path), FakeRunner())
        room = f.c["rooms"][0]
        with pytest.raises(ValueError, match="room-not-allowed"):
            f.enqueue_notice("!other:test.invalid", "안내")
        with pytest.raises(ValueError):
            f.enqueue_notice(room, "")
        first = f.enqueue_notice(room, "비동기 작업이 끝났습니다")
        second = f.enqueue_notice(room, "비동기 작업이 끝났습니다")
        assert first != second
        outbox = f.store.outbox()
        assert [job["room_id"] for job in outbox] == [room, room]
        assert {job["sender"] for job in outbox} == {f.c["account"]}
        assert {job["reply"] for job in outbox} == {"비동기 작업이 끝났습니다"}
        assert f.store.claim() is None  # notices never become work
        f.store.close()


# --------------------------------------------------------------------------- #
# Turn loop, controls, uncertain work
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_rejected_queue_event_stays_rejected_after_replay(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        f = h.f
        f.store.total_cap = f.store.scope_cap = 1
        first = request(f)
        second = request(f, "$second")
        await f.input(first)
        await f.input(second)
        assert NOTICE_QUEUE_FULL in h.replies()
        job = f.store.claim()
        assert job is not None
        f.store.finish(job["event_id"], "first")
        f.store.delivered(job["event_id"])
        for notice in f.store.outbox():
            f.store.delivered(notice["event_id"])
        await f.input(second)
        assert f.store.claim() is None
        with pytest.raises(SafetyStop, match="control-identity-conflict"):
            await f.input(request(f, "$second", "changed"))


@pytest.mark.anyio
async def test_controls_not_blocked_by_ordinary_queue_and_nonce_is_single_use(tmp_path: Path) -> None:
    async with running(tmp_path, "approval") as h:
        f = h.f
        f.store.total_cap = f.store.scope_cap = 1
        await f.input(request(f))
        h.work()
        await h.until(lambda: bool(f.approvals))
        tid = turn_id("$request")
        nonce = next(iter(f.approvals))
        prompt = [r for r in h.replies() if "/approve " + tid + " " + nonce in r]
        assert len(prompt) == 1 and "/deny " + tid + " " + nonce in prompt[0] and '"value": 1' in prompt[0]
        # Wrong room/sender cannot enter through the admission boundary.
        assert request(f, "$evil", "/approve " + tid + " " + nonce, room="!other:test.invalid") is None
        assert request(f, "$evil", "/approve " + tid + " " + nonce, sender="@stranger:test.invalid") is None
        await f.input(request(f, "$invalid", "/approve " + tid + " " + "x" * 32))
        assert f.approvals
        await f.input(request(f, "$wrong-turn", "/approve " + "0" * 32 + " " + nonce))
        assert f.approvals
        approved = request(f, "$approved", "/approve " + tid + " " + nonce)
        await f.input(approved)
        await f.input(approved)  # replayed event: recorded once
        await h.until(lambda: f.store.session(request(f).scope) is not None)
        assert not f.approvals
        assert h.runner.decisions == [True]
        assert f.store.session(request(f).scope) == "synthetic-session"
        assert "approved" in h.replies()
        assert h.replies().count(NOTICE_CONTROL_FORWARDED) == 1
        assert h.replies().count(NOTICE_INVALID_CONTROL) == 2
        # The nonce is bound to that turn and single use: a fresh event replaying it is refused.
        await f.input(request(f, "$replay", "/approve " + tid + " " + nonce))
        assert h.replies().count(NOTICE_INVALID_CONTROL) == 3


@pytest.mark.anyio
async def test_deny_and_approval_timeout_resolve_false(tmp_path: Path) -> None:
    async with running(tmp_path, "approval", approval_timeout=0.1) as h:
        f = h.f
        await f.input(request(f))
        h.work()
        await h.until(lambda: bool(f.approvals))
        tid = turn_id("$request")
        nonce = next(iter(f.approvals))
        await f.input(request(f, "$deny", "/deny " + tid + " " + nonce))
        await h.until(lambda: "denied" in h.replies())  # finish() ran, not just the runner's return
        assert h.runner.decisions == [False]
        h.drain()
        await f.input(request(f, "$second"))
        await h.until(lambda: len(h.runner.decisions) == 2)  # nobody answered within 0.1s
        assert h.runner.decisions == [False, False]
        assert not f.approvals


@pytest.mark.anyio
async def test_oversized_or_flooded_approvals_are_refused_without_prompt(tmp_path: Path) -> None:
    # 16 prompts are durable commits; keep the timeout well above their fsync cost so none expires early.
    async with running(tmp_path, "approval-big", approval_timeout=1.5) as h:
        f = h.f
        await f.input(request(f))
        h.work()
        await h.until(lambda: f.store.get_meta("last_turn") is not None)  # turn fully recorded
        assert h.runner.decisions == [False]
        assert not any("/approve" in r for r in h.drain())
        h.runner.mode = "approval-flood"
        await f.input(request(f, "$flood"))
        await h.until(lambda: len(h.runner.decisions) == 18)
        assert h.runner.decisions[1:] == [False] * 17
        assert sum("/approve" in r for r in h.replies()) == 16


@pytest.mark.anyio
async def test_cancel_ends_with_a_notice_and_work_continues_without_ack(tmp_path: Path) -> None:
    async with running(tmp_path, "cancel") as h:
        f = h.f
        await f.input(request(f))
        work = h.work()
        await h.until(lambda: bool(f.approvals))
        tid = turn_id("$request")
        await f.input(request(f, "$cancel", "/cancel " + tid))
        await h.until(lambda: f.store.get_meta("last_turn") is not None and f.store.get_meta("last_turn")["outcome"] == "cancelled")
        assert h.runner.cancels == ["$request"]
        assert f.store.session(request(f).scope) is None
        assert not f.store.uncertain(), "a user cancel is closed immediately, no /ack gate"
        assert not work.done()  # a user cancel never stops the service
        assert NOTICE_CANCELLED in h.drain()
        assert not any("/ack" in r for r in h.replies())
        # The next message in the same scope is served right away.
        h.runner.mode = "complete"
        await f.input(request(f, "$next"))
        await h.until(lambda: f.store.session(request(f).scope) == "synthetic-session")
        assert [c["event_id"] for c in h.runner.calls] == ["$request", "$next"]
        # /ack is still accepted as a courtesy no-op.
        await f.input(request(f, "$ack", "/ack " + tid))
        assert not f.store.uncertain()


@pytest.mark.anyio
async def test_cancel_still_cancels_when_runner_cancel_raises(tmp_path: Path) -> None:
    async with running(tmp_path, "cancel-raises") as h:
        f = h.f
        await f.input(request(f))
        h.work()
        await h.until(lambda: bool(f.approvals))
        tid = turn_id("$request")
        await f.input(request(f, "$cancel", "/cancel " + tid))
        await h.until(lambda: f.store.get_meta("last_turn") is not None and f.store.get_meta("last_turn")["outcome"] == "cancelled")
        assert h.runner.cancels == ["$request"]
        assert NOTICE_CONTROL_FORWARDED in h.replies()
        assert not f.store.uncertain() and NOTICE_CANCELLED in h.replies()


@pytest.mark.anyio
async def test_runner_failure_posts_an_error_notice_and_the_next_job_runs(tmp_path: Path) -> None:
    async with running(tmp_path, "raise") as h:
        f = h.f
        await f.input(request(f))
        await f.input(request(f, "$second"))
        work = h.work()
        await h.until(lambda: f.store.get_meta("last_turn") is not None and f.store.get_meta("last_turn")["outcome"] == "error:RuntimeError")
        assert "synthetic failure" not in json.dumps(f.store.get_meta("last_turn"))
        assert not f.store.uncertain()
        assert not work.done()
        h.runner.mode = "complete"
        # The error notice must be delivered before the scope continues (outbox ordering).
        await asyncio.sleep(0.3)
        assert len(h.runner.calls) == 1
        assert NOTICE_TURN_ERROR in h.drain()
        await h.until(lambda: len(h.runner.calls) == 2)
        assert h.runner.calls[1]["event_id"] == "$second"


@pytest.mark.anyio
async def test_uncertain_result_and_turn_timeout_never_publish_the_answer(tmp_path: Path) -> None:
    async with running(tmp_path, "uncertain", turn_timeout=0.1) as h:
        f = h.f
        await f.input(request(f))
        h.work()
        await h.until(lambda: f.store.get_meta("last_turn") is not None and f.store.get_meta("last_turn")["outcome"] == "uncertain")
        assert not f.store.uncertain()
        assert NOTICE_TURN_ERROR in h.drain()
        h.runner.mode = "slow"
        await f.input(request(f, "$slow"))
        await h.until(lambda: f.store.get_meta("last_turn")["outcome"] == "timeout")
        assert h.runner.interrupted == 1
        await h.until(lambda: NOTICE_TIMEOUT.format(minutes=t._timeout_label(f.turn_timeout)) in h.replies())
        assert not f.store.uncertain()
        assert not any(r in ("late", "synthetic answer") for r in h.replies())


@pytest.mark.anyio
async def test_error_status_delivers_text_and_finishes(tmp_path: Path) -> None:
    async with running(tmp_path, "error") as h:
        f = h.f
        req = request(f)
        await f.input(req)
        h.work()
        await h.until(lambda: "❌ provider unavailable" in h.replies())
        assert f.store.get_meta("last_turn")["outcome"] == "error"
        assert not f.store.uncertain()  # no operator /ack needed for a reported failure
        assert f.store.session(req.scope) == "synthetic-session"


@pytest.mark.anyio
async def test_streamed_result_is_not_echoed_again(tmp_path: Path) -> None:
    async with running(tmp_path, "streamed") as h:
        f = h.f
        req = request(f)
        await f.input(req)
        h.work()
        await h.until(lambda: f.store.session(req.scope) == "synthetic-session")
        assert h.replies().count("streamed answer") == 1, "interim delivery must not be repeated as the final reply"
        assert f.store.get_meta("last_turn")["outcome"] == "complete"


@pytest.mark.anyio
async def test_stop_alias_cancels_the_senders_running_turn(tmp_path: Path) -> None:
    async with running(tmp_path, "cancel") as h:
        f = h.f
        await f.input(request(f))
        work = h.work()
        await h.until(lambda: bool(f.approvals))
        await f.input(request(f, "$stop", "/stop"))
        await h.until(lambda: f.store.get_meta("last_turn") is not None and f.store.get_meta("last_turn")["outcome"] == "cancelled")
        assert h.runner.cancels == ["$request"]
        assert not f.store.uncertain() and NOTICE_CANCELLED in h.replies()
        assert not work.done()
        # A stranger's room/scope cannot stop someone else's turn.
        h.runner.mode = "slow"
        assert request(f, "$evil", "/stop", sender="@stranger:test.invalid") is None


def test_message_content_adds_formatted_body_only_for_markup() -> None:
    from telegram_bot.core.matrix.transport import message_content

    plain = message_content("작업을 시작했습니다. 취소 명령:\n/cancel abc")
    assert plain == {"msgtype": "m.text", "body": "작업을 시작했습니다. 취소 명령:\n/cancel abc"}
    rich = message_content("**done** — see `ls -la`\n<script>alert(1)</script>")
    assert rich["format"] == "org.matrix.custom.html"
    assert "<strong>done</strong>" in rich["formatted_body"] and "<code>ls -la</code>" in rich["formatted_body"]
    assert "<script>" not in rich["formatted_body"]
    assert rich["body"].startswith("**done**")


@pytest.mark.anyio
async def test_second_message_while_busy_gets_a_queue_position_notice(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        f = h.f
        req = request(f)
        await f.input(req)
        assert h.replies() == [], "first message has nothing to queue behind"

        second = request(f, event="$second", body="두 번째 질문")
        await f.input(second)
        queued = [r for r in h.replies() if "대기 순번" in r]
        assert queued == ["⏳ 이 메시지는 대기 순번 2번에 저장되었으며 도착 순서대로 처리됩니다."]
        # A sync replay of the same event must not re-notice.
        await f.input(second)
        assert [r for r in h.replies() if "대기 순번" in r] == queued

        # Same scope is strictly FIFO: $second runs only after $request's
        # answer has been delivered. Interleave work and delivery like the
        # real send loop would.
        h.work()
        await h.until(lambda: f.store.db.execute("SELECT state FROM jobs WHERE event_id='$request'").fetchone()[0] == "ready")
        h.drain()
        await h.until(lambda: f.store.db.execute("SELECT state FROM jobs WHERE event_id='$second'").fetchone()[0] == "ready")
        h.drain()
        third = request(f, event="$third", body="세 번째 질문")
        await f.input(third)
        assert [r for r in h.replies() if "대기 순번" in r] == []


@pytest.mark.anyio
async def test_empty_result_completes_without_reply(tmp_path: Path) -> None:
    async with running(tmp_path, "empty") as h:
        f = h.f
        req = request(f)
        await f.input(req)
        h.work()
        await h.until(lambda: f.store.session(req.scope) == "synthetic-session")
        assert f.store.get_meta("last_turn")["outcome"] == "complete"
        assert not f.store.uncertain()
        assert h.replies() == [], "no started notice and no reply for an empty streamed result"
        row = f.store.db.execute("SELECT state FROM jobs WHERE event_id='$request'").fetchone()
        assert row["state"] == "done"


@pytest.mark.anyio
async def test_sink_typing_is_best_effort_and_interim_is_durable(tmp_path: Path) -> None:
    async with running(tmp_path, "interim") as h:
        f = h.f
        calls: list[tuple[str, str, Any]] = []

        async def raw(method: str, path: str, data: Any = None, params: Any = None) -> Any:
            calls.append((method, path, data))
            raise RuntimeError("no homeserver in tests")

        f.raw = raw
        req = request(f)
        await f.input(req)
        h.work()
        await h.until(lambda: f.store.session(req.scope) is not None)
        assert calls == [
            (
                "PUT",
                "/_matrix/client/v3/rooms/" + quote(req.room_id, safe="") + "/typing/" + quote(f.c["account"], safe=""),
                {"typing": True, "timeout": 8000},
            )
        ]
        assert "진행 중입니다" in h.replies()
        assert h.replies().count("진행 중입니다") == 1
        # An inert sink (turn finished) does nothing.
        sink = t._RoomSink(f, {"event_id": "$request", "room_id": req.room_id, "sender": req.sender, "body": "x", "scope": req.scope})
        await sink.typing()
        await sink.interim("늦은 안내")
        assert await sink.approval("late", None) is False
        assert len(calls) == 1 and "늦은 안내" not in h.replies()


@pytest.mark.anyio
async def test_status_bubble_edits_in_place_and_redacts(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        f = h.f
        room = f.c["rooms"][0]
        client_mock(f)
        f.client.olm.outbound_group_sessions = {room: types.SimpleNamespace(users_shared_with={(f.c["owner"], "OWNER")})}
        sends: list[str] = []
        redacts: list[str] = []
        calls: list[tuple[str, str]] = []

        async def raw(method: str, path: str, data: Any = None, params: Any = None) -> Any:
            calls.append((method, path))
            if "/send/m.room.encrypted/" in path:
                sends.append(path.rsplit("/", 1)[1])
                return {"event_id": f"$bubble{len(sends) - 1}"}
            assert "/redact/" in path
            redacts.append(path)
            return {}

        f.raw = raw
        job = {"event_id": "$request", "room_id": room, "sender": f.c["owner"], "body": "x", "scope": "scope"}
        sink = t._RoomSink(f, job)
        f.active = job
        from telegram_bot.core.matrix.transport import message_content

        await sink.status("⏳ Working — 1s")
        assert f.last_room_event[room] == "$bubble0"  # our own send counts as the newest event
        await sink.status("⏳ Working — 1m 23s")
        assert len(sends) == 2 and not redacts, "still newest: edit in place, no redact"
        first, edit = f.client.encrypt.call_args_list[0].args[2], f.client.encrypt.call_args_list[1].args[2]
        assert first == message_content("⏳ Working — 1s")
        assert edit["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$bubble0"}
        assert edit["m.new_content"]["body"] == "⏳ Working — 1m 23s"
        # A family member speaks after the bubble: it is buried now.
        f.last_room_event[room] = "$family-msg"
        await sink.status("⏳ Working — 2m")
        assert len(sends) == 3 and len(redacts) == 1, "buried bubble: redact + repost at the bottom"
        assert sink._bubble == "$bubble2"
        # The reposted bubble is newest again: back to edit-in-place.
        await sink.status("⏳ Working — 3m")
        assert len(sends) == 4 and len(redacts) == 1
        repost_edit = f.client.encrypt.call_args_list[-1].args[2]
        assert repost_edit["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$bubble2"}
        await sink.status(None)  # turn answered: the bubble is redacted
        assert len(redacts) == 2 and "/redact/" in calls[-1][1] and len(sends) == 4
        # An inactive turn stays inert.
        f.active = None
        await sink.status("⏳ inert")
        assert len(sends) == 4 and len(redacts) == 2


@pytest.mark.anyio
async def test_turn_carries_admitted_context_and_replays_execute_once(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        f = h.f
        req = request(f)
        await f.input(req)
        h.work()
        await h.until(lambda: f.store.session(req.scope) is not None)
        assert h.runner.calls == [
            {"event_id": "$request", "sender": f.c["owner"], "session_id": None, "room_kind": "direct"}
        ]
        for out in f.store.outbox():
            f.store.delivered(out["event_id"])
        await f.input(req)
        assert f.store.claim() is None
        await f.input(request(f, "$second"))
        await h.until(lambda: len(h.runner.calls) == 2)
        assert h.runner.calls[1]["session_id"] == "synthetic-session"


@pytest.mark.anyio
async def test_service_stop_during_turn_propagates_and_marks_uncertain(tmp_path: Path) -> None:
    async with running(tmp_path, "slow") as h:
        f = h.f
        await f.input(request(f))
        work = h.work()
        await h.until(h.runner.started.is_set)
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        assert work.cancelled()
        assert h.runner.interrupted == 1
        assert f.store.uncertain(), "a service stop leaves the turn for the next process"
        assert f.active is None and f.turn_task is None
        # The next process closes it with the restart notice and serves new work at once.
        await f.close()
        f2 = MatrixTransport(config(tmp_path), FakeRunner("complete"))
        try:
            task = asyncio.create_task(f2.work())
            async with asyncio.timeout(5):
                while NOTICE_RESTARTED not in [j["reply"] for j in f2.store.outbox()]:
                    await asyncio.sleep(0.01)
            assert not f2.store.uncertain()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            await f2.close()


@pytest.mark.anyio
async def test_repeated_cancellation_during_join_waits_for_runner(tmp_path: Path) -> None:
    async with running(tmp_path, "stubborn") as h:
        f = h.f
        await f.input(request(f))
        work = h.work()
        await h.until(h.runner.started.is_set)
        for _ in range(3):
            work.cancel()
            await asyncio.sleep(0.02)
        await asyncio.gather(work, return_exceptions=True)
        assert work.cancelled()
        assert h.runner.interrupted == 1
        assert f.store.uncertain()
        assert f.store.get_meta("turn_join_timeout") is None


@pytest.mark.anyio
async def test_join_gives_up_on_a_runner_that_ignores_cancellation(tmp_path: Path) -> None:
    async with running(tmp_path, "immortal", turn_timeout=0.05) as h:
        f = h.f
        await f.input(request(f))
        with patch.object(t, "TURN_JOIN_TIMEOUT_S", 0.05):
            h.work()
            await h.until(lambda: f.store.get_meta("turn_join_timeout") is not None)
        assert f.store.get_meta("last_turn")["outcome"] == "timeout"
        assert (
            not f.store.uncertain()
            and NOTICE_TIMEOUT.format(minutes=t._timeout_label(f.turn_timeout)) in h.replies()
        )


@pytest.mark.anyio
async def test_controls_without_an_active_turn_are_refused(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        f = h.f
        await f.input(request(f, "$c1", "/cancel " + "0" * 32))
        await f.input(request(f, "$c2", "/ack " + "0" * 32))
        await f.input(request(f, "$c3", "/approve " + "0" * 32 + " " + "n" * 32))
        await f.input(request(f, "$c4", "/deny"))
        assert h.replies().count(NOTICE_INVALID_CONTROL) == 4
        assert f.store.claim() is None
        await f._cancel_active()  # nothing active: no-op
        assert h.runner.cancels == []


# --------------------------------------------------------------------------- #
# HTTP, delivery, lifecycle
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, status: int, chunks: list[bytes]) -> None:
        self.status = status
        self.content = types.SimpleNamespace(iter_chunked=self._iter)
        self._chunks = chunks

    async def _iter(self, size: int) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.mark.anyio
async def test_raw_classifies_http_outcomes(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        f = h.f
        seen: list[tuple[str, str]] = []
        queue: list[FakeResponse] = []

        def http_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
            seen.append((method, url))
            return queue.pop(0)

        f.http = types.SimpleNamespace(request=http_request, close=AsyncMock())
        queue.append(FakeResponse(200, [b'{"ok":', b" true}"]))
        assert await f.raw("GET", "/x", params={"a": "b"}) == {"ok": True}
        assert seen == [("GET", "http://127.0.0.1:18809/x")]
        queue.append(FakeResponse(429, []))
        with pytest.raises(ConnectionError, match="matrix-temporary-error"):
            await f.raw("GET", "/x")
        queue.append(FakeResponse(404, []))
        with pytest.raises(SafetyStop, match="matrix-http-404"):
            await f.raw("GET", "/x")
        queue.append(FakeResponse(200, [b"x" * 4_194_305]))
        with pytest.raises(SafetyStop, match="matrix-response-too-large"):
            await f.raw("GET", "/x")


def client_mock(
    f: MatrixTransport,
    rooms: dict[str, set[str]] | None = None,
    devices: dict[str, dict[str, Any]] | None = None,
    identity: dict[str, str] | None = None,
) -> None:
    f.client = types.SimpleNamespace(
        rooms={room: types.SimpleNamespace(encrypted=True, users=set(users)) for room, users in (rooms or {}).items()},
        receive_response=AsyncMock(),
        device_store={user: dict(ds) for user, ds in (devices or {}).items()},
        verify_device=Mock(),
        blacklist_device=Mock(),
        share_group_session=AsyncMock(),
        invalidate_outbound_session=Mock(),
        encrypt=Mock(return_value=("m.room.encrypted", {})),
        keys_upload=AsyncMock(),
        should_upload_keys=False,
        next_batch=None,
        olm=types.SimpleNamespace(
            should_share_group_session=Mock(return_value=False),
            outbound_group_sessions={},
            account=types.SimpleNamespace(identity_keys=identity or {}),
        ),
        close=AsyncMock(),
    )


@pytest.mark.anyio
async def test_incomplete_key_sharing_never_sends_ciphertext(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        f = h.f
        room = f.c["rooms"][0]
        client_mock(f)
        f.client.olm.should_share_group_session = Mock(return_value=True)
        f.client.olm.outbound_group_sessions = {room: types.SimpleNamespace(users_shared_with=set())}
        f.raw = AsyncMock()
        with pytest.raises(ConnectionError, match="group-key-share-incomplete"):
            await f.encrypted_send(room, "synthetic", "txn")
        f.client.encrypt.assert_not_called()
        f.raw.assert_not_called()
        f.client.invalidate_outbound_session.assert_called_once_with(room)
        f.client.share_group_session.assert_awaited_once_with(room)
        # Plaintext output is refused even with a complete share.
        f.client.olm.outbound_group_sessions = {room: types.SimpleNamespace(users_shared_with={(f.c["owner"], "OWNER")})}
        f.client.encrypt = Mock(return_value=("m.room.message", {}))
        with pytest.raises(SafetyStop, match="plaintext-output-refused"):
            await f.encrypted_send(room, "synthetic", "txn")
        f.raw.assert_not_called()


@pytest.mark.anyio
async def test_outbox_chunks_are_delivered_idempotently_across_failures(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        f = h.f
        room = f.c["rooms"][0]
        client_mock(f)
        f.client.olm.outbound_group_sessions = {room: types.SimpleNamespace(users_shared_with={(f.c["owner"], "OWNER")})}
        f.pin_devices = AsyncMock()
        f.room_gate = AsyncMock(return_value=True)
        sent: list[str] = []
        failures = {"left": 1}

        async def raw(method: str, path: str, data: Any = None, params: Any = None) -> Any:
            assert method == "PUT" and "/send/m.room.encrypted/" in path
            if len(sent) == 1 and failures["left"]:
                failures["left"] -= 1
                raise ConnectionError("matrix-temporary-error")
            sent.append(path.rsplit("/", 1)[1])
            return {"event_id": "$sent"}

        f.raw = raw
        await f.input(request(f))
        job = f.store.claim()
        assert job is not None
        f.store.finish(job["event_id"], "a" * 12_000 + "b" * 100)
        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp()}):
            send = h.start(f.retry(f.send))
            await h.until(lambda: f.store.get_meta("health") == f.store.get_meta("health") and not f.store.outbox())
        assert not send.done()
        assert len(sent) == 2 and len(set(sent)) == 2  # chunk 0 once, chunk 1 once (after retry)
        assert f.store.delivered_parts(job["event_id"]) == 2
        assert f.store.get_meta("health")["state"] == "network-retry"
        # A muted room keeps its pending replies.
        f.blocked.add(room)
        f.enqueue_notice(room, "보류")
        await asyncio.sleep(0.3)
        assert [j["reply"] for j in f.store.outbox()] == ["보류"]
        f.blocked.discard(room)
        f.room_gate = AsyncMock(return_value=False)
        await asyncio.sleep(0.3)
        assert [j["reply"] for j in f.store.outbox()] == ["보류"]  # gate closed: still kept
        f.room_gate = AsyncMock(return_value=True)
        await h.until(lambda: not f.store.outbox())
        assert len(sent) == 3


@pytest.mark.anyio
async def test_receive_stages_sync_before_processing(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        f = h.f
        seen: list[dict[str, str]] = []

        async def raw(method: str, path: str, data: Any = None, params: Any = None) -> Any:
            assert (method, path) == ("GET", "/_matrix/client/v3/sync")
            seen.append(dict(params))
            return {"next_batch": "s" + str(len(seen))}

        f.raw = raw
        f.process_pending = AsyncMock(side_effect=[None, RuntimeError("stop")])
        with pytest.raises(RuntimeError, match="stop"):
            await f.receive()
        assert len(seen) == 1 and "since" not in seen[0] and seen[0]["timeout"] == "25000"
        assert json.loads(seen[0]["filter"])["room"]["rooms"] == f.c["rooms"]
        assert f.store.get_meta("pending_sync")["next_batch"] == "s1"  # still pending: raw not called again
        f.store.commit_sync("s1")
        f.process_pending = AsyncMock(side_effect=RuntimeError("stop"))
        with pytest.raises(RuntimeError):
            await f.receive()
        assert seen[1]["since"] == "s1"


@pytest.mark.anyio
async def test_run_and_serve_record_stop_reasons(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        f = h.f
        f.receive = AsyncMock(side_effect=SafetyStop("timeline-gap-requires-backfill"))

        async def idle() -> None:
            await asyncio.sleep(3600)

        f.send = idle
        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp()}):
            with pytest.raises(ExceptionGroup) as info:
                await f.run()
        assert stop_reason(info.value) == "timeline-gap-requires-backfill"
        assert stop_reason(ExceptionGroup("x", [RuntimeError()])) == "unexpected-failure"
        assert stop_reason(asyncio.CancelledError()) == "service-stopped"
        assert stop_reason(RuntimeError()) == "unexpected-failure"
        f.open = AsyncMock(side_effect=SafetyStop("credential-device-mismatch"))
        with pytest.raises(SafetyStop):
            await serve(f, initialize=True)
        with pytest.raises(ValueError, match="closed"):
            f.store.token()  # serve closed the transport
        with MatrixStore(f.c["state_directory"], f.c["account"]) as store:
            assert store.get_meta("health")["reason"] == "credential-device-mismatch"
        # Initialize-only runs open() and stops.
        h.f = MatrixTransport(config(tmp_path), h.runner)
        h.f.open = AsyncMock()
        h.f.run = AsyncMock(side_effect=AssertionError("must not run"))
        await serve(h.f, initialize=True)
        h.f = MatrixTransport(config(tmp_path), h.runner)


def crypto_dir(f: MatrixTransport) -> Path:
    crypto = Path(f.c["state_directory"]) / "crypto"
    crypto.mkdir(mode=0o700, exist_ok=True)
    return crypto


@pytest.mark.anyio
async def test_open_requires_explicit_initialization_and_pins_identity(tmp_path: Path) -> None:
    nio = fake_nio()
    async with running(tmp_path) as h:
        f = h.f
        crypto = crypto_dir(f)
        whoami = {"user_id": f.c["account"], "device_id": f.c["device_id"]}
        table: dict[tuple[str, str], Any] = {}

        async def raw(method: str, path: str, data: Any = None, params: Any = None) -> Any:
            if path.endswith("/whoami"):
                return whoami
            return table[(method, path)]

        f.raw = raw
        f.pin_devices = AsyncMock()
        f.room_gate = AsyncMock(return_value=True)
        for room in f.c["rooms"]:
            table[("GET", "/_matrix/client/v3/rooms/" + quote(room, safe="") + "/state")] = [{"type": "m.room.create"}]
        modules = {"nio": nio, "aiohttp": fake_aiohttp()}
        with patch.dict(sys.modules, modules):
            with pytest.raises(SafetyStop, match="explicit-new-device-initialization-required"):
                await f.open()
            leftover = crypto / "stale.db"
            leftover.write_text("x")
            leftover.chmod(0o600)
            with pytest.raises(SafetyStop, match="explicit-new-device-initialization-required"):
                await f.open(initialize=True)  # crypto files without an identity marker
            leftover.chmod(0o644)
            with pytest.raises(SafetyStop, match="unsafe-crypto-store"):
                await f.open(initialize=True)
            leftover.unlink()
            whoami["device_id"] = "OTHER"
            with pytest.raises(SafetyStop, match="credential-device-mismatch"):
                await f.open(initialize=True)
            whoami["device_id"] = f.c["device_id"]
            await f.open(initialize=True)
            client = nio.AsyncClient.instances[-1]
            assert client.kwargs["store_path"] == str(crypto)
            assert client.kwargs["config"].kwargs["pickle_key"] == f.c["pickle_key"]
            assert client.logins == [(f.c["account"], f.c["device_id"], f.c["access_token"])]
            primed = client.receive_response.await_args.args[0]
            assert isinstance(primed, nio.SyncResponse)
            assert f.http.headers == {"Authorization": "Bearer synthetic"}
            identity = f.store.get_meta("device_identity")
            assert identity["keys"] == {"ed25519": "agent-ed", "curve25519": "agent-cu"}
            assert "synthetic" not in json.dumps(identity)  # only the credential hash is stored
            assert f.store.get_meta("health")["state"] == "ready"
            f.pin_devices.assert_awaited_once()
            f.room_gate.assert_awaited_once_with(f.c["rooms"][0])
            # A different token on restart is identity drift, never a silent re-login.
            await f.close()
            h.f = f = MatrixTransport({**config(tmp_path), "access_token": "rotated"}, h.runner)
            f.raw = raw
            f.pin_devices = AsyncMock()
            with pytest.raises(SafetyStop, match="crypto-identity-or-token-drift"):
                await f.open()
            await f.close()
            h.f = f = MatrixTransport(config(tmp_path), h.runner)
            f.raw = raw
            f.pin_devices = AsyncMock()
            original_init = nio.AsyncClient.__init__

            def needs_upload(self: Any, *args: Any, **kwargs: Any) -> None:
                original_init(self, *args, **kwargs)
                self.should_upload_keys = True

            with patch.object(nio.AsyncClient, "__init__", needs_upload):
                with pytest.raises(SafetyStop, match="key-upload-failed"):
                    await f.open()


# --------------------------------------------------------------------------- #
# Family rooms (port of FamilyRoomTests)
# --------------------------------------------------------------------------- #


class FamilyHarness(Harness):
    def __init__(self, root: Path, **extra: Any) -> None:
        super().__init__(root, "complete", {**family_config(root), **extra})
        self.nio = fake_nio()
        self.owner = self.f.c["owner"]
        self.account = self.f.c["account"]
        self.now = int(time.time() * 1000)

    def route_raw(self, *routes: Any) -> None:
        table = {routes[i]: routes[i + 1] for i in range(0, len(routes), 2)}

        async def fake(method: str, path: str, data: Any = None, params: Any = None) -> Any:
            for (room, suffix), payload in table.items():
                if room == "keys":
                    if method == "POST" and path == "/_matrix/client/v3/keys/query":
                        return payload
                    continue
                if method == "GET" and quote(room, safe="") in path and path.endswith(suffix):
                    return payload
            raise AssertionError("unexpected raw call: " + method + " " + path)

        self.f.raw = fake

    def healthy_members(self) -> set[str]:
        return {self.owner, DAD, MOM, self.account}

    def gate_routes(self, joined: set[str], algorithm: str = "m.megolm.v1.aes-sha2") -> tuple[Any, ...]:
        return (
            (FAMILY, "/joined_members"),
            {"joined": {user: {} for user in joined}},
            (FAMILY, "/state/m.room.encryption"),
            {"algorithm": algorithm},
        )


@asynccontextmanager
async def family(root: Path, **extra: Any) -> AsyncIterator[FamilyHarness]:
    h = FamilyHarness(root, **extra)
    try:
        with patch.dict(sys.modules, {"nio": h.nio}):
            yield h
    finally:
        await h.stop()


@pytest.mark.anyio
async def test_family_gate_allows_family_blocks_stranger_and_recovers(tmp_path: Path) -> None:
    async with family(tmp_path) as h:
        f = h.f
        client_mock(f, rooms={FAMILY: h.healthy_members()})
        h.route_raw(*h.gate_routes(h.healthy_members()))
        assert await f.room_gate(FAMILY) is True
        # 가족방은 mention 없이는 입장되지 않는다(정책은 어댑터에서도 유지된다).
        probe = dict(
            type="m.room.message",
            event_id="$probe",
            sender=DAD,
            origin_server_ts=h.now,
            content={"msgtype": "m.text", "body": "멘션 없음"},
        )
        assert f.policy.admit(FAMILY, probe, decrypted=True, now_ms=h.now) is None
        notices = [job for job in f.store.outbox() if job["reply"] == FAMILY_NOTICE]
        assert len(notices) == 1
        assert notices[0]["room_id"] == FAMILY
        assert f.store.get_meta("family_room_notice") == {FAMILY: True}
        f.store.delivered(notices[0]["event_id"])
        joined = h.healthy_members() | {STRANGER}
        h.route_raw(*h.gate_routes(joined))
        assert await f.room_gate(FAMILY) is False
        assert FAMILY in f.blocked
        blocked = f.store.get_meta("room_gate_blocked")[FAMILY]
        assert blocked["members"] == sorted(joined)
        assert f.store.outbox() == []  # 중단된 방에는 안내도 답변도 없다.
        assert await f.room_gate(FAMILY) is False  # unchanged block is not rewritten
        h.route_raw(*h.gate_routes(h.healthy_members()))
        assert await f.room_gate(FAMILY) is True
        assert FAMILY not in f.blocked
        assert not f.store.get_meta("room_gate_blocked")
        assert f.store.outbox() == []  # 초대 안내는 한 번만 머문다.
        # SDK membership drift inside a family room also mutes it.
        f.client.rooms[FAMILY].users.add(STRANGER)
        assert await f.room_gate(FAMILY) is False
        # The disclosure notice is bound to the room: a different text later is an identity conflict, never a repeat.
        f.store.set_meta("family_room_notice", {})
        f.c["family_notice_text"] = "맞춤 안내"
        f.client.rooms[FAMILY].users.discard(STRANGER)
        with pytest.raises(SafetyStop, match="notice-identity-conflict"):
            await f.room_gate(FAMILY)


@pytest.mark.anyio
async def test_family_notice_text_override(tmp_path: Path) -> None:
    async with family(tmp_path, family_notice_text="맞춤 안내") as h:
        f = h.f
        client_mock(f, rooms={FAMILY: h.healthy_members()})
        h.route_raw(*h.gate_routes(h.healthy_members()))
        assert await f.room_gate(FAMILY) is True
        assert [job["reply"] for job in f.store.outbox()] == ["맞춤 안내"]


@pytest.mark.anyio
async def test_direct_gate_regression_and_family_encryption_required(tmp_path: Path) -> None:
    async with family(tmp_path) as h:
        f = h.f
        direct = f.c["rooms"][0]
        client_mock(f, rooms={FAMILY: h.healthy_members(), direct: {h.owner, h.account}})
        h.route_raw(
            (direct, "/joined_members"),
            {"joined": {h.owner: {}, STRANGER: {}, h.account: {}}},
            (direct, "/state/m.room.encryption"),
            {"algorithm": "m.megolm.v1.aes-sha2"},
        )
        with pytest.raises(SafetyStop, match="private-room-membership-changed"):
            await f.room_gate(direct)
        h.route_raw(*h.gate_routes(h.healthy_members(), algorithm="m.plain"))
        with pytest.raises(SafetyStop, match="encrypted-room-required"):
            await f.room_gate(FAMILY)
        assert FAMILY not in f.blocked
        h.route_raw(
            (direct, "/joined_members"),
            {"joined": {h.owner: {}, h.account: {}}},
            (direct, "/state/m.room.encryption"),
            {"algorithm": "m.megolm.v1.aes-sha2"},
        )
        assert await f.room_gate(direct) is True
        f.client.rooms[direct].users.add(STRANGER)
        with pytest.raises(SafetyStop, match="sdk-room-membership-changed"):
            await f.room_gate(direct)
        f.client.rooms[direct].encrypted = False
        with pytest.raises(SafetyStop, match="sdk-encryption-state-missing"):
            await f.room_gate(direct)


@pytest.mark.anyio
async def test_pin_devices_pins_each_family_user_and_detects_changes(tmp_path: Path) -> None:
    async with family(tmp_path) as h:
        f = h.f
        identity = {"ed25519": "agent-ed", "curve25519": "agent-cu"}
        devices = {
            h.owner: {"OWNER": pinned_device("a", "b")},
            DAD: {"DAD1": pinned_device("c"), "DAD2": pinned_device("e")},
            MOM: {"MOM1": pinned_device("f")},
        }
        bot_keys = {"keys": {"ed25519:BOT": "agent-ed", "curve25519:BOT": "agent-cu"}}
        everyone: dict[str, dict[str, Any]] = {h.owner: {"OWNER": {}}, DAD: {"DAD1": {}, "DAD2": {}}, MOM: {"MOM1": {}}}
        client_mock(f, devices=devices, identity=identity)
        h.route_raw(("keys", ""), {"device_keys": {h.account: {"BOT": bot_keys}, **everyone}})
        await f.pin_devices()
        verified = [call.args[0] for call in f.client.verify_device.call_args_list]
        assert len(verified) == 2  # OWNER+DAD1; unpinned DAD2/MOM1 stay unverified.
        assert devices[DAD]["DAD1"] in verified
        assert devices[h.owner]["OWNER"] in verified
        client_mock(f, devices={**devices, DAD: {"DAD1": pinned_device("z"), "DAD2": pinned_device("e")}}, identity=identity)
        with pytest.raises(SafetyStop, match="pinned-device-key-changed"):
            await f.pin_devices()
        client_mock(
            f,
            devices={h.owner: {"OWNER": pinned_device("a", "b")}, DAD: {"DAD2": pinned_device("e")}, MOM: {"MOM1": pinned_device("f")}},
            identity=identity,
        )  # Query rebuilt the store without pinned DAD1.
        h.route_raw(("keys", ""), {"device_keys": {h.account: {"BOT": bot_keys}, **everyone, DAD: {"DAD2": {}}}})
        with pytest.raises(SafetyStop, match="pinned-device-missing"):
            await f.pin_devices()
        client_mock(f, devices=devices, identity=identity)
        h.route_raw(("keys", ""), {"device_keys": {h.account: {"BOT": bot_keys}, **everyone, h.owner: {"OWNER": {}, "OWN2": {}}}})
        with pytest.raises(SafetyStop, match="owner-device-set-changed"):
            await f.pin_devices()
        h.route_raw(("keys", ""), {"device_keys": {h.account: {"BOT": bot_keys, "BOT2": {}}, **everyone}})
        with pytest.raises(SafetyStop, match="unexpected-agent-device"):
            await f.pin_devices()
        h.route_raw(("keys", ""), {"device_keys": {h.account: {"BOT": {"keys": {"ed25519:BOT": "other"}}}, **everyone}})
        with pytest.raises(SafetyStop, match="published-agent-key-changed"):
            await f.pin_devices()
        client_mock(f, devices={**devices, h.owner: {"OWNER": pinned_device("a", "z")}}, identity=identity)
        h.route_raw(("keys", ""), {"device_keys": {h.account: {"BOT": bot_keys}, **everyone}})
        with pytest.raises(SafetyStop, match="owner-device-key-changed"):
            await f.pin_devices()
        setattr(h.nio, "KeysQueryResponse", type("ErrorResponse", (), {"from_dict": classmethod(lambda cls, raw: cls())}))
        with pytest.raises(SafetyStop, match="device-query-failed"):
            await f.pin_devices()


@pytest.mark.anyio
async def test_family_send_excludes_unpinned_and_requires_pinned_delivery(tmp_path: Path) -> None:
    async with family(tmp_path) as h:
        f = h.f
        f.room_members[FAMILY] = set(h.healthy_members())
        devices = {
            h.owner: {"OWNER": pinned_device("a", "b")},
            DAD: {"DAD1": pinned_device("c"), "DAD2": pinned_device("e")},
            MOM: {"MOM1": pinned_device("f")},
        }
        client_mock(f, devices=devices)
        f.client.olm.should_share_group_session = Mock(return_value=True)
        session = types.SimpleNamespace(users_shared_with={(h.owner, "OWNER"), (DAD, "DAD1")})
        f.client.olm.outbound_group_sessions = {FAMILY: session}
        sent = []

        async def fake(method: str, path: str, data: Any = None, params: Any = None) -> Any:
            if method == "PUT" and path.endswith("/send/m.room.encrypted/txn1"):
                sent.append(path)
                return {"event_id": "$sent"}
            raise AssertionError("unexpected raw call: " + method + " " + path)

        f.raw = fake
        await f.encrypted_send(FAMILY, "가족 답변", "txn1")
        blacklisted = [call.args[0] for call in f.client.blacklist_device.call_args_list]
        assert devices[DAD]["DAD2"] in blacklisted
        assert devices[MOM]["MOM1"] in blacklisted
        assert devices[DAD]["DAD1"] not in blacklisted
        assert len(sent) == 1
        assert f.store.get_meta("family_room_devices")[FAMILY] == {DAD: ["DAD2"], MOM: ["MOM1"]}
        # 모든 고정 기기에 세션이 배포되기 전에는 송신하지 않는다.
        session.users_shared_with = {(DAD, "DAD1")}
        with pytest.raises(ConnectionError, match="group-key-share-incomplete"):
            await f.encrypted_send(FAMILY, "가족 답변", "txn1")
        f.client.invalidate_outbound_session.assert_called_with(FAMILY)
        f.client.encrypt.assert_called_once()  # Missing pinned delivery refused to encrypt.
        # 미고정 기기가 세션을 받았다면 송신을 거부한다.
        session.users_shared_with = {(h.owner, "OWNER"), (DAD, "DAD1"), (DAD, "DAD2"), (MOM, "MOM1")}
        with pytest.raises(ConnectionError, match="group-key-share-incomplete"):
            await f.encrypted_send(FAMILY, "가족 답변", "txn1")
        f.client.encrypt.assert_called_once()
        assert f.store.get_meta("family_room_devices")[FAMILY] == {DAD: ["DAD2"], MOM: ["MOM1"]}  # unchanged, not rewritten


@pytest.mark.anyio
async def test_initial_snapshot_sync_is_not_a_timeline_gap(tmp_path: Path) -> None:
    # Servers flag timeline.limited=true for every room on the first sync
    # (no `since`). Only an incremental sync with limited=true is a gap.
    async with family(tmp_path) as h:
        f = h.f
        direct = f.c["rooms"][0]
        identity = {"ed25519": "agent-ed", "curve25519": "agent-cu"}
        client_mock(
            f,
            rooms={FAMILY: h.healthy_members(), direct: {h.owner, h.account}},
            devices={h.owner: {"OWNER": pinned_device("a", "b")}, DAD: {"DAD1": pinned_device("c")}},
            identity=identity,
        )
        bot_keys = {"keys": {"ed25519:BOT": "agent-ed", "curve25519:BOT": "agent-cu"}}
        h.route_raw(
            ("keys", ""),
            {"device_keys": {h.account: {"BOT": bot_keys}, h.owner: {"OWNER": {}}, DAD: {"DAD1": {}}}},
            *h.gate_routes(h.healthy_members()),
            (direct, "/joined_members"),
            {"joined": {h.owner: {}, h.account: {}}},
            (direct, "/state/m.room.encryption"),
            {"algorithm": "m.megolm.v1.aes-sha2"},
        )
        f.c["not_before_ms"] = h.now - 1000
        mention = {"msgtype": "m.text", "body": "호출", "m.mentions": {"user_ids": [h.account]}}
        source = {"type": "m.room.message", "event_id": "$family", "sender": DAD, "origin_server_ts": h.now, "content": mention}
        admitted = h.nio.RoomMessageText(sender=DAD, source=source, sender_key="c" * 43, ts=h.now)
        timeline = {FAMILY: [admitted], "!unknown:test.invalid": [admitted]}

        class SyncResponse:
            @classmethod
            def from_dict(cls, raw: Any) -> Any:
                response = cls()
                response.rooms = types.SimpleNamespace(  # type: ignore[attr-defined]
                    join={room: types.SimpleNamespace(timeline=types.SimpleNamespace(events=events)) for room, events in timeline.items()}
                )
                return response

        setattr(h.nio, "SyncResponse", SyncResponse)
        limited = {"next_batch": "s1", "rooms": {"join": {FAMILY: {"timeline": {"events": [], "limited": True}}}}}
        assert f.store.token() is None
        await f.process_pending()  # nothing pending: no-op
        f.store.stage_sync(limited)
        await f.process_pending()  # snapshot sync: must not stop
        assert f.store.token() == "s1"
        assert f.store.get_meta("health")["state"] == "ready"
        assert f.client.next_batch is None
        job = f.store.claim()
        assert job is not None and (job["event_id"], job["room_id"]) == ("$family", FAMILY)
        # limited=true with a short batch is Tuwunel noise (display-name change,
        # 2026-09-18): every event since the token is present, so carry on.
        soft = {"next_batch": "s1b", "rooms": {"join": {FAMILY: {"timeline": {"events": [{}], "limited": True}}}}}
        f.store.stage_sync(soft)
        await f.process_pending()
        assert f.store.token() == "s1b"
        assert f.store.get_meta("sync_limited_soft")["events"] == 1
        # A batch filled to the requested limit is a real gap: fail closed.
        gap = {"next_batch": "s2", "rooms": {"join": {FAMILY: {"timeline": {"events": [{}] * 100, "limited": True}}}}}
        f.store.stage_sync(gap)
        with pytest.raises(SafetyStop, match="timeline-gap-requires-backfill"):
            await f.process_pending()  # incremental sync with a gap
        assert f.store.token() == "s1b"  # gap is never committed
        f.store.set_meta("pending_sync", {"next_batch": "s2", "rooms": {"join": {FAMILY: {"timeline": {"events": [], "limited": False}}}}})
        setattr(h.nio, "SyncResponse", type("ErrorResponse", (), {"from_dict": classmethod(lambda cls, raw: cls())}))
        with pytest.raises(SafetyStop, match="invalid-sync-response"):
            await f.process_pending()


@pytest.mark.anyio
async def test_family_admission_pinned_verified_mentioned_humans_only(tmp_path: Path) -> None:
    async with family(tmp_path) as h:
        f = h.f
        mention = {"msgtype": "m.text", "body": "호출", "m.mentions": {"user_ids": [h.account]}}
        f.c["not_before_ms"] = h.now - 1000

        def event(sender: str = DAD, verified: bool = True, key: str = "c" * 43, content: Any = None, ts: int | None = None) -> Any:
            stamp = h.now if ts is None else ts
            source = {"type": "m.room.message", "event_id": "$family", "sender": sender, "origin_server_ts": stamp, "content": content or dict(mention)}
            return h.nio.RoomMessageText(sender=sender, source=source, verified=verified, sender_key=key, ts=stamp)

        assert f.admit_event(FAMILY, event()) is not None
        assert f.admit_event(FAMILY, event(content={"msgtype": "m.text", "body": "멘션 없음"})) is None
        assert f.admit_event(FAMILY, event(content={"msgtype": "m.text", "body": h.account + " 이름 호출"})) is None
        assert f.admit_event(FAMILY, event(sender=h.account)) is None  # 봇 발신 무시.
        assert f.admit_event(FAMILY, event(sender=STRANGER)) is None
        assert f.admit_event(FAMILY, event(ts=h.now - 5000)) is None  # before not_before_ms
        with pytest.raises(SafetyStop, match="unverified-family-event"):
            f.admit_event(FAMILY, event(verified=False))
        with pytest.raises(SafetyStop, match="unverified-family-event"):
            f.admit_event(FAMILY, event(key="z" * 43))
        with pytest.raises(SafetyStop, match="unverified-family-event"):
            f.admit_event(FAMILY, event(sender=MOM))  # allowlisted but without any pinned device
        # An undecryptable event never stops the service: it is recorded, a key
        # request is queued and the room is told once (jingun 2026-09-18).
        megolm = h.nio.MegolmEvent()
        megolm.sender, megolm.server_timestamp, megolm.event_id = DAD, h.now, "$undecryptable"
        assert f.admit_event(FAMILY, megolm) is None
        assert f.admit_event(FAMILY, megolm) is None  # duplicate: still one record, one notice
        assert [e["event_id"] for e in f.store.get_meta("undecryptable_events")] == ["$undecryptable"]
        assert f.key_requests == [megolm, megolm]
        assert sum(NOTICE_UNDECRYPTABLE == r for r in h.replies()) == 1
        f.client = types.SimpleNamespace(request_room_key=AsyncMock(side_effect=[None, RuntimeError("no session")]), close=AsyncMock())
        await f._request_room_keys()
        assert f.client.request_room_key.await_count == 2 and f.key_requests == []
        assert f.admit_event(FAMILY, types.SimpleNamespace(sender=DAD, server_timestamp=h.now)) is None  # not text
        undecrypted = event()
        undecrypted.decrypted = False
        assert f.admit_event(FAMILY, undecrypted) is None
        f.blocked.add(FAMILY)
        assert f.admit_event(FAMILY, event()) is None  # 중단된 방은 무시한다.
        f.blocked.discard(FAMILY)
        direct = f.c["rooms"][0]
        owner_source = {"type": "m.room.message", "event_id": "$direct", "sender": h.owner, "origin_server_ts": h.now, "content": {"msgtype": "m.text", "body": "개인방"}}
        owner_event = h.nio.RoomMessageText(sender=h.owner, source=owner_source, verified=True, sender_key="b" * 43, ts=h.now)
        assert f.admit_event(direct, owner_event) is not None
        with pytest.raises(SafetyStop, match="unverified-owner-event"):
            f.admit_event(direct, h.nio.RoomMessageText(sender=h.owner, source=owner_source, verified=False, sender_key="b" * 43, ts=h.now))
        # Family turns carry the family room kind to the runner.
        assert f.room_kind(FAMILY) == "family"
        assert f.expected_recipients(direct) == {(h.owner, "OWNER")}
        f.room_members[FAMILY] = h.healthy_members()
        assert f.expected_recipients(FAMILY) == {(h.owner, "OWNER"), (DAD, "DAD1")}


def _cross_signing_raw(account: str, owner: str, master: str, ssk: str, devices: dict[str, tuple[str, str, bool]]) -> dict[str, Any]:
    """keys/query payload: devices -> (ed25519, curve25519, signed_by_ssk)."""
    bot_keys = {"keys": {"ed25519:BOT": "agent-ed", "curve25519:BOT": "agent-cu"}}
    device_keys = {
        d: {"keys": {"ed25519:" + d: ed, "curve25519:" + d: cu},
            "signatures": {owner: {"ed25519:" + ssk: "sig-ok" if signed else "sig-bad"}}}
        for d, (ed, cu, signed) in devices.items()
    }
    return {
        "device_keys": {account: {"BOT": bot_keys}, owner: device_keys},
        "master_keys": {owner: {"keys": {"ed25519:" + master: master}}},
        "self_signing_keys": {owner: {"keys": {"ed25519:" + ssk: ssk}, "signatures": {owner: {"ed25519:" + master: "sig-ok"}}}},
    }


@pytest.fixture(autouse=True)
def _drop_fake_nio_module() -> Any:
    had = sys.modules.get("nio")
    yield
    if had is None:
        sys.modules.pop("nio", None)
    else:
        sys.modules["nio"] = had


def _fake_verify_json(json: Any, user_key: str, user_id: str, device_id: str) -> bool:
    return (json.get("signatures") or {}).get(user_id, {}).get("ed25519:" + device_id) == "sig-ok"


@pytest.mark.anyio
async def test_cross_signed_identity_trusts_signed_devices_without_pins(tmp_path: Path) -> None:
    master, ssk = "M" * 43, "S" * 43
    base = config(tmp_path)
    cfg = {**base, "devices": {}, "identities": {base["owner"]: {"master": master}}}
    async with running(tmp_path, cfg=cfg) as h:
        f = h.f
        owner = f.c["owner"]
        assert f.pins == {} and f.trusted == {}
        sys.modules["nio"] = fake_nio()
        identity = {"ed25519": "agent-ed", "curve25519": "agent-cu"}
        store = {"NEW": pinned_device("a", "b"), "OLD": pinned_device("c", "d")}
        client_mock(f, devices={owner: store}, identity=identity)

        class NioLikeDeviceStore:
            """nio.crypto.DeviceStore: __getitem__(user) -> dict, __iter__ over devices (never user ids)."""

            def __init__(self, users: dict[str, dict[str, Any]]) -> None:
                self._users = users

            def __getitem__(self, user: str) -> dict[str, Any]:
                return self._users.setdefault(user, {})

            def __iter__(self) -> Any:
                return iter(d for devices in self._users.values() for d in devices.values())

        f.client.device_store = NioLikeDeviceStore({owner: dict(store)})
        f.client.olm.verify_json = Mock(side_effect=_fake_verify_json)
        raw = _cross_signing_raw(f.c["account"], owner, master, ssk, {"NEW": ("a" * 43, "b" * 43, True), "OLD": ("c" * 43, "d" * 43, False)})
        f.raw = AsyncMock(return_value=raw)
        await f.pin_devices()
        assert f.trusted[owner] == {"NEW": "b" * 43}
        assert [c.args[0] for c in f.client.verify_device.call_args_list] == [store["NEW"]]
        assert [c.args[0] for c in f.client.blacklist_device.call_args_list] == [store["OLD"]]
        assert f.store.get_meta("trusted_devices")[owner]["devices"] == ["NEW"]
        # A login/logout only changes the trusted set; it never stops the service.
        raw2 = _cross_signing_raw(f.c["account"], owner, master, ssk, {"NEW": ("a" * 43, "b" * 43, True), "N2": ("e" * 43, "f" * 43, True)})
        client_mock(f, devices={owner: {"NEW": store["NEW"], "N2": pinned_device("e", "f")}}, identity=identity)
        f.client.olm.verify_json = Mock(side_effect=_fake_verify_json)
        f.raw = AsyncMock(return_value=raw2)
        await f.pin_devices()
        assert set(f.trusted[owner]) == {"NEW", "N2"}
        assert f.expected_recipients(f.c["rooms"][0]) == {(owner, "NEW"), (owner, "N2")}
        # The published ed25519 must match the stored device key even when signed.
        raw3 = _cross_signing_raw(f.c["account"], owner, master, ssk, {"NEW": ("x" * 43, "b" * 43, True)})
        client_mock(f, devices={owner: {"NEW": store["NEW"]}}, identity=identity)
        f.client.olm.verify_json = Mock(side_effect=_fake_verify_json)
        f.raw = AsyncMock(return_value=raw3)
        await f.pin_devices()
        assert f.trusted[owner] == {}
        # Only an identity change (account reset) or a broken chain stops the service.
        for payload, reason in (
            ({**raw, "master_keys": {owner: {"keys": {"ed25519:" + "Z" * 43: "Z" * 43}}}}, "owner-identity-changed"),
            ({**raw, "master_keys": {}}, "cross-signing-missing"),
            ({**raw, "self_signing_keys": {owner: {"keys": {"ed25519:" + ssk: ssk}, "signatures": {owner: {"ed25519:" + master: "sig-bad"}}}}}, "cross-signing-invalid"),
        ):
            f.raw = AsyncMock(return_value=payload)
            with pytest.raises(SafetyStop, match=reason):
                await f.pin_devices()


@pytest.mark.anyio
async def test_cross_signed_mode_ignores_untrusted_sender_devices_with_one_notice(tmp_path: Path) -> None:
    master = "M" * 43
    base = config(tmp_path)
    cfg = {**base, "devices": {}, "identities": {base["owner"]: {"master": master}}}
    nio = fake_nio()
    async with running(tmp_path, cfg=cfg) as h:
        f = h.f
        owner, room = f.c["owner"], f.c["rooms"][0]
        sys.modules["nio"] = nio
        f.trusted[owner] = {"NEW": "b" * 43}
        f.c["not_before_ms"] = h_now = int(time.time() * 1000) - 1000

        def message(event_id: str, key: str, verified: bool = True) -> Any:
            source = {"type": "m.room.message", "event_id": event_id, "sender": owner, "origin_server_ts": h_now + 500,
                      "content": {"msgtype": "m.text", "body": "hi"}}
            return nio.RoomMessageText(sender=owner, source=source, verified=verified, sender_key=key, ts=h_now + 500)

        assert f.admit_event(room, message("$ok", "b" * 43)) is not None
        assert f.admit_event(room, message("$bad1", "d" * 43)) is None
        assert f.admit_event(room, message("$bad2", "d" * 43)) is None  # same device: no second notice
        assert f.admit_event(room, message("$bad3", "b" * 43, verified=False)) is None
        assert sum(NOTICE_UNTRUSTED_DEVICE == r for r in h.replies()) == 2  # one per untrusted device key
        assert set(f.store.get_meta("untrusted_senders")) == {f"{owner}:{'d' * 43}", f"{owner}:{'b' * 43}"}


# --------------------------------------------------------------------------- #
# Reply context (#1943)
# --------------------------------------------------------------------------- #

from telegram_bot.core.matrix.state import (  # noqa: E402
    MAX_TEXT_BYTES,
    REPLY_CONTEXT_MAX_CHARS,
    reply_context_body,
    reply_target,
    strip_reply_fallback,
)


def reply_event(
    event: str = "$reply",
    body: str = "이거 다시 설명해줘",
    parent: str = "$parent",
    sender: str | None = None,
    now: int | None = None,
    **content: Any,
) -> dict[str, Any]:
    stamp = now if now is not None else int(time.time() * 1000)
    return dict(
        type="m.room.message",
        event_id=event,
        sender=sender or "@owner:test.invalid",
        origin_server_ts=stamp,
        content={"msgtype": "m.text", "body": body, "m.relates_to": {"m.in_reply_to": {"event_id": parent}}, **content},
    )


def test_reply_target_only_for_plain_replies() -> None:
    assert reply_target({"m.relates_to": {"m.in_reply_to": {"event_id": "$p"}}}) == "$p"
    assert reply_target({"m.relates_to": {"rel_type": "m.thread", "m.in_reply_to": {"event_id": "$p"}}}) is None
    assert reply_target({"m.relates_to": {"m.in_reply_to": {"event_id": "not-an-id"}}}) is None
    assert reply_target({"m.relates_to": {"m.in_reply_to": "$p"}}) is None
    assert reply_target({"body": "x"}) is None


def test_strip_reply_fallback() -> None:
    legacy = "> <@bot:test.invalid> 첫 줄\n> 둘째 줄\n\n진짜 답글"
    assert strip_reply_fallback(legacy) == "진짜 답글"
    assert strip_reply_fallback("> 인용만 한 평범한 글") == "> 인용만 한 평범한 글"  # not a fallback
    assert strip_reply_fallback("> <@bot:test.invalid> only quote") == "> <@bot:test.invalid> only quote"
    assert strip_reply_fallback("평범한 답글") == "평범한 답글"


def test_reply_context_body_labels_truncates_and_fits() -> None:
    kw = dict(account="@bot:test.invalid", sender="@owner:test.invalid", limit_bytes=MAX_TEXT_BYTES)
    mine = reply_context_body("왜?", parent_sender="@bot:test.invalid", parent_body="줄1\n줄2", **kw)
    assert mine == "[Reply context: the user is replying to your (the assistant's) earlier message]\n> 줄1\n> 줄2\n\n왜?"
    own = reply_context_body("왜?", parent_sender="@owner:test.invalid", parent_body="x", **kw)
    assert "their own earlier message" in own
    other = reply_context_body("왜?", parent_sender=DAD, parent_body="x", **kw)
    assert "an earlier message from " + DAD in other
    long = reply_context_body("왜?", parent_sender=DAD, parent_body="가" * 5000, **kw)
    assert long.count("가") == REPLY_CONTEXT_MAX_CHARS and "…(truncated)" in long
    assert len(long.encode()) <= MAX_TEXT_BYTES
    tight = reply_context_body("왜?", parent_sender=DAD, parent_body="가" * 5000, **{**kw, "limit_bytes": 400})
    assert len(tight.encode()) <= 400 and tight.endswith("\n\n왜?")
    body = "나" * 5000
    assert reply_context_body(body, parent_sender=DAD, parent_body="x", **{**kw, "limit_bytes": 15_000}) == body


def test_policy_admit_records_parent_and_strips_fallback(tmp_path: Path) -> None:
    f = MatrixTransport(family_config(tmp_path), FakeRunner("complete"))
    try:
        now = int(time.time() * 1000)
        req = f.policy.admit(f.c["rooms"][0], reply_event(now=now), decrypted=True, now_ms=now)
        assert req is not None and req.reply_to == "$parent" and req.body == "이거 다시 설명해줘"
        legacy = reply_event(now=now, body="> <@bot:test.invalid> 예전 답\n\n그래서?")
        req = f.policy.admit(f.c["rooms"][0], legacy, decrypted=True, now_ms=now)
        assert req is not None and req.body == "그래서?"
        # Family admission is unchanged: a reply still needs a mention (the
        # fallback's full "@bot:server" mxid never counted as a typed handle).
        fallback = "> <@bot:test.invalid> 예전 답\n\n그래서?"
        family = reply_event(sender=DAD, now=now, body=fallback)
        assert f.policy.admit(FAMILY, family, decrypted=True, now_ms=now) is None
        mentioned = reply_event(sender=DAD, now=now, body=fallback, **{"m.mentions": {"user_ids": ["@bot:test.invalid"]}})
        req = f.policy.admit(FAMILY, mentioned, decrypted=True, now_ms=now)
        assert req is not None and req.reply_to == "$parent" and req.body == "그래서?"
    finally:
        f.store.close()


def _admit_reply(f: MatrixTransport, **kw: Any) -> Any:
    now = int(time.time() * 1000)
    return f.policy.admit(f.c["rooms"][0], reply_event(now=now, **kw), decrypted=True, now_ms=now)


@pytest.mark.anyio
async def test_reply_to_cached_bot_message_carries_parent_text(tmp_path: Path) -> None:
    async with family(tmp_path) as h:
        f = h.f
        room = f.c["rooms"][0]
        f._remember_text("$parent", room, h.account, "지난번 답변 내용")
        await f.input(_admit_reply(f))
        job = f.store.claim()
        assert job is not None and job["event_id"] == "$reply"
        assert job["body"] == (
            "[Reply context: the user is replying to your (the assistant's) earlier message]\n"
            "> 지난번 답변 내용\n\n이거 다시 설명해줘"
        )


@pytest.mark.anyio
async def test_reply_parent_is_fetched_and_decrypted_on_cache_miss(tmp_path: Path) -> None:
    async with family(tmp_path) as h:
        f = h.f
        room = f.c["rooms"][0]
        client_mock(f)

        class Event:
            @staticmethod
            def parse_encrypted_event(raw: Any) -> Any:
                megolm = h.nio.MegolmEvent()
                megolm.raw = raw  # type: ignore[attr-defined]
                return megolm

        setattr(h.nio, "Event", Event)
        decrypted = h.nio.RoomMessageText(sender=h.owner, body="어제 보낸 질문", sender_key="b" * 43)
        f.client.decrypt_event = Mock(return_value=decrypted)
        calls: list[str] = []

        async def raw(method: str, path: str, data: Any = None, params: Any = None) -> Any:
            calls.append(path)
            return {"event_id": "$parent", "type": "m.room.encrypted", "content": {}}

        f.raw = raw
        await f.input(_admit_reply(f))
        assert calls == ["/_matrix/client/v3/rooms/" + quote(room, safe="") + "/event/" + quote("$parent", safe="")]
        assert f.client.decrypt_event.call_args.args[0].room_id == room
        job = f.store.claim()
        assert job is not None and job["body"].startswith("[Reply context: the user is replying to their own earlier message]\n> 어제 보낸 질문")
        assert f.recent_text["$parent"] == (room, h.owner, "어제 보낸 질문")


@pytest.mark.anyio
async def test_reply_context_fails_open_and_rejects_untrusted_or_plaintext_parents(tmp_path: Path) -> None:
    async with family(tmp_path) as h:
        f = h.f
        client_mock(f)

        async def missing(method: str, path: str, data: Any = None, params: Any = None) -> Any:
            raise SafetyStop("matrix-http-404")

        f.raw = missing
        await f.input(_admit_reply(f, event="$r1"))
        job = f.store.claim()
        assert job is not None and job["body"] == "이거 다시 설명해줘"
        assert f.store.get_meta("reply_context_miss")["room"] == f.c["rooms"][0]
        f.store.finish(job["event_id"], "ok")
        h.drain()

        async def plaintext(method: str, path: str, data: Any = None, params: Any = None) -> Any:
            return {"event_id": "$parent", "type": "m.room.message", "content": {"msgtype": "m.text", "body": "평문"}}

        f.raw = plaintext
        await f.input(_admit_reply(f, event="$r2"))
        job = f.store.claim()
        assert job is not None and job["body"] == "이거 다시 설명해줘"
        f.store.finish(job["event_id"], "ok")
        h.drain()

        # A parent from an unpinned device (or a stranger) is never quoted.
        setattr(h.nio, "Event", types.SimpleNamespace(parse_encrypted_event=lambda raw: h.nio.MegolmEvent()))
        f.client.decrypt_event = Mock(return_value=h.nio.RoomMessageText(sender=DAD, body="위조", sender_key="z" * 43))

        async def encrypted(method: str, path: str, data: Any = None, params: Any = None) -> Any:
            return {"event_id": "$parent", "type": "m.room.encrypted", "content": {}}

        f.raw = encrypted
        await f.input(_admit_reply(f, event="$r3"))
        job = f.store.claim()
        assert job is not None and job["body"] == "이거 다시 설명해줘"
        assert "$parent" not in f.recent_text


@pytest.mark.anyio
async def test_reply_replay_and_command_replies_stay_stable(tmp_path: Path) -> None:
    async with family(tmp_path) as h:
        f = h.f
        room = f.c["rooms"][0]
        f._remember_text("$parent", room, h.account, "원문")
        req = _admit_reply(f)
        await f.input(req)
        # Replayed sync after the cache changed: no re-enrichment, no identity conflict.
        f.recent_text.clear()
        f.raw = AsyncMock(side_effect=AssertionError("replay must not fetch"))
        await f.input(req)
        job = f.store.claim()
        assert job is not None and "> 원문" in job["body"]
        f.store.finish(job["event_id"], "ok")
        h.drain()
        # A /command sent as a reply stays verbatim so the bot still parses it.
        f._remember_text("$parent", room, h.account, "원문")
        await f.input(_admit_reply(f, event="$cmd", body="/new"))
        job = f.store.claim()
        assert job is not None and job["body"] == "/new"
        f.store.finish(job["event_id"], "ok")
        h.drain()
        # So does a bare number answering a numbered menu (Danso recovery, /resume).
        await f.input(_admit_reply(f, event="$pick", body=" 2 "))
        job = f.store.claim()
        assert job is not None and job["body"] == " 2 "


@pytest.mark.anyio
async def test_sent_chunks_and_trusted_sync_texts_are_remembered(tmp_path: Path) -> None:
    async with family(tmp_path) as h:
        f = h.f
        room = f.c["rooms"][0]
        # Sync side: only trusted decrypted texts enter the cache.
        good = h.nio.RoomMessageText(sender=DAD, body="가족 메시지", sender_key="c" * 43)
        bad = h.nio.RoomMessageText(sender=DAD, body="미검증", sender_key="z" * 43)
        mine = h.nio.RoomMessageText(sender=h.account, body="봇 메시지")
        assert f._trusted_text(good) and not f._trusted_text(bad) and f._trusted_text(mine)
        assert not f._trusted_text(h.nio.RoomMessageText(sender=DAD, body="x", sender_key="c" * 43, verified=False))
        # Send side: every delivered chunk is remembered under its event id.
        client_mock(f)
        f.client.olm.outbound_group_sessions = {room: types.SimpleNamespace(users_shared_with={(h.owner, "OWNER")})}
        f.pin_devices = AsyncMock()
        f.room_gate = AsyncMock(return_value=True)

        async def raw(method: str, path: str, data: Any = None, params: Any = None) -> Any:
            return {"event_id": "$sent-" + path.rsplit("/", 1)[1][:8]}

        f.raw = raw
        f.enqueue_notice(room, "보낸 답변")
        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp()}):
            h.start(f.retry(f.send))
            await h.until(lambda: not f.store.outbox())
        assert [v for v in f.recent_text.values()] == [(room, h.account, "보낸 답변")]
        for i in range(t.RECENT_TEXT_CAP + 5):
            f._remember_text(f"$e{i}", room, DAD, "x")
        assert len(f.recent_text) == t.RECENT_TEXT_CAP


# --------------------------------------------------------------------------- #
# Trust-store churn (nio file KeyStore reports a change on every call)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_pin_devices_leaves_devices_already_in_the_wanted_trust_state(tmp_path: Path) -> None:
    async with family(tmp_path) as h:
        f = h.f
        identity = {"ed25519": "agent-ed", "curve25519": "agent-cu"}
        owner = pinned_device("a", "b")
        owner.verified = True  # restored from the trust file: nothing to change
        dad = pinned_device("c")
        dad.verified = False
        devices = {h.owner: {"OWNER": owner}, DAD: {"DAD1": dad, "DAD2": pinned_device("e")}, MOM: {"MOM1": pinned_device("f")}}
        bot_keys = {"keys": {"ed25519:BOT": "agent-ed", "curve25519:BOT": "agent-cu"}}
        everyone: dict[str, dict[str, Any]] = {h.owner: {"OWNER": {}}, DAD: {"DAD1": {}, "DAD2": {}}, MOM: {"MOM1": {}}}
        client_mock(f, devices=devices, identity=identity)
        h.route_raw(("keys", ""), {"device_keys": {h.account: {"BOT": bot_keys}, **everyone}})
        await f.pin_devices()
        assert [call.args[0] for call in f.client.verify_device.call_args_list] == [dad]

        blocked = pinned_device("x")
        blocked.blacklisted = True
        fresh = pinned_device("y")
        fresh.blacklisted = False
        f._blacklist(blocked)
        f._blacklist(fresh)
        assert [call.args[0] for call in f.client.blacklist_device.call_args_list] == [fresh]


def test_compact_trust_files_keeps_first_copies_only(tmp_path: Path) -> None:
    crypto = tmp_path / "crypto"
    crypto.mkdir(mode=0o700)
    a = "@o:test.invalid A matrix-ed25519 keyA"
    b = "@o:test.invalid B matrix-ed25519 keyB"
    trusted = crypto / "@bot:test.invalid_BOT.trusted_devices"
    trusted.write_text("\n".join([a, b, a, a, b, a]) + "\n")
    blacklisted = crypto / "@bot:test.invalid_BOT.blacklisted_devices"
    blacklisted.write_text(b + "\n")
    database = crypto / "@bot:test.invalid_BOT.db"
    database.write_bytes(b"\x00dup\n\x00dup\n")
    for path in (trusted, blacklisted, database):
        path.chmod(0o600)
    (crypto / ("." + trusted.name + ".compact")).write_text("stale")

    removed = t.compact_trust_files(crypto)

    assert removed == {trusted.name: 4}
    assert trusted.read_text() == a + "\n" + b + "\n"
    assert oct(trusted.stat().st_mode & 0o777) == oct(0o600)
    assert blacklisted.read_text() == b + "\n"
    assert database.read_bytes() == b"\x00dup\n\x00dup\n", "only trust files are touched"
    assert sorted(p.name for p in crypto.iterdir()) == sorted([trusted.name, blacklisted.name, database.name])
    assert t.compact_trust_files(crypto) == {}
