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
    FAMILY_NOTICE,
    NOTICE_ACKED,
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
async def test_cancel_uncertainty_requires_explicit_same_scope_ack(tmp_path: Path) -> None:
    async with running(tmp_path, "cancel") as h:
        f = h.f
        await f.input(request(f))
        work = h.work()
        await h.until(lambda: bool(f.approvals))
        tid = turn_id("$request")
        await f.input(request(f, "$cancel", "/cancel " + tid))
        await h.until(lambda: bool(f.store.uncertain()))
        assert h.runner.cancels == ["$request"]
        assert f.store.session(request(f).scope) is None
        assert f.store.get_meta("last_turn")["outcome"] == "cancelled"
        await h.until(lambda: any("/ack " + tid in r for r in h.replies()))
        assert not work.done()  # a user cancel never stops the service
        assert f.store.claim() is None
        await f.input(request(f, "$ack", "/ack " + tid))
        assert not f.store.uncertain()
        assert NOTICE_ACKED in h.replies()
        assert f.store.claim() is None
        # Work resumes after the acknowledgement (once the scope's replies are out).
        h.runner.mode = "complete"
        h.drain()
        await f.input(request(f, "$next"))
        await h.until(lambda: f.store.session(request(f).scope) == "synthetic-session")
        assert [c["event_id"] for c in h.runner.calls] == ["$request", "$next"]


@pytest.mark.anyio
async def test_cancel_still_cancels_when_runner_cancel_raises(tmp_path: Path) -> None:
    async with running(tmp_path, "cancel-raises") as h:
        f = h.f
        await f.input(request(f))
        h.work()
        await h.until(lambda: bool(f.approvals))
        tid = turn_id("$request")
        await f.input(request(f, "$cancel", "/cancel " + tid))
        await h.until(lambda: bool(f.store.uncertain()))
        assert h.runner.cancels == ["$request"]
        assert NOTICE_CONTROL_FORWARDED in h.replies()


@pytest.mark.anyio
async def test_runner_failure_leaves_uncertain_and_pauses_all_work_until_ack(tmp_path: Path) -> None:
    async with running(tmp_path, "raise") as h:
        f = h.f
        await f.input(request(f))
        await f.input(request(f, "$second"))
        work = h.work()
        await h.until(lambda: bool(f.store.uncertain()))
        await asyncio.sleep(0.3)
        assert not work.done()
        assert [c["event_id"] for c in h.runner.calls] == ["$request"]  # nothing else claimed
        assert f.store.get_meta("last_turn")["outcome"] == "error:RuntimeError"
        assert "synthetic failure" not in json.dumps(f.store.get_meta("last_turn"))
        h.runner.mode = "complete"
        await f.input(request(f, "$ack", "/ack " + turn_id("$request")))
        await asyncio.sleep(0.3)
        assert len(h.runner.calls) == 1  # the acknowledged reply must be delivered before the scope continues
        h.drain()
        await h.until(lambda: len(h.runner.calls) == 2)
        assert h.runner.calls[1]["event_id"] == "$second"


@pytest.mark.anyio
async def test_uncertain_result_and_turn_timeout_never_publish(tmp_path: Path) -> None:
    async with running(tmp_path, "uncertain", turn_timeout=0.1) as h:
        f = h.f
        await f.input(request(f))
        h.work()
        await h.until(lambda: bool(f.store.uncertain()))
        assert f.store.get_meta("last_turn")["outcome"] == "uncertain"
        await f.input(request(f, "$ack", "/ack " + turn_id("$request")))
        h.drain()
        h.runner.mode = "slow"
        await f.input(request(f, "$slow"))
        await h.until(lambda: f.store.get_meta("last_turn")["outcome"] == "timeout")
        assert h.runner.interrupted == 1
        assert [j["event_id"] for j in f.store.uncertain()] == ["$slow"]
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
        await h.until(lambda: bool(f.store.uncertain()))
        assert h.runner.cancels == ["$request"]
        assert f.store.get_meta("last_turn")["outcome"] == "cancelled"
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
        assert f.store.uncertain()
        assert f.active is None and f.turn_task is None


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
        assert f.store.uncertain()


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
        f.store.stage_sync({**limited, "next_batch": "s2"})
        with pytest.raises(SafetyStop, match="timeline-gap-requires-backfill"):
            await f.process_pending()  # incremental sync with a gap
        assert f.store.token() == "s1"  # gap is never committed
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
        megolm = h.nio.MegolmEvent()
        megolm.sender, megolm.server_timestamp = DAD, h.now
        with pytest.raises(SafetyStop, match="undecrypted-event"):
            f.admit_event(FAMILY, megolm)
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
