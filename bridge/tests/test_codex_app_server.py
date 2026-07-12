"""Tests for the standalone Codex app-server transport."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest

from telegram_bot.core.codex_app_server import (
    CodexAppServerClient,
    CodexConnectionClosedError,
    CodexNotification,
    CodexServerRequest,
)


class FakeWriter:
    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.reader = reader
        self.messages: list[dict[str, Any]] = []
        self.close_calls = 0
        self.active_drains = 0
        self.max_active_drains = 0

    def write(self, data: bytes) -> None:
        message = json.loads(data.decode())
        self.messages.append(message)
        if message.get("method") == "initialize" and "id" in message:
            self.feed({"id": message["id"], "result": {"userAgent": "fake"}})

    async def drain(self) -> None:
        self.active_drains += 1
        self.max_active_drains = max(self.max_active_drains, self.active_drains)
        await asyncio.sleep(0)
        self.active_drains -= 1

    def close(self) -> None:
        self.close_calls += 1

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)

    def feed(self, message: Mapping[str, Any]) -> None:
        self.reader.feed_data(json.dumps(message).encode() + b"\n")

    def feed_raw(self, data: bytes) -> None:
        self.reader.feed_data(data + b"\n")


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdin = FakeWriter(self.stdout)
        self.returncode: int | None = None
        self.terminate_calls = 0
        self._exited = asyncio.Event()

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.stdout.feed_eof()
        self.exit(0)

    def exit(self, returncode: int) -> None:
        self.returncode = returncode
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode


@pytest.mark.asyncio
async def test_start_performs_initialize_handshake() -> None:
    reader = asyncio.StreamReader()
    writer = FakeWriter(reader)
    client = CodexAppServerClient(reader=reader, writer=writer)

    initialize_result = await client.start()

    assert initialize_result == {"userAgent": "fake"}
    assert writer.messages == [
        {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "ccc_node",
                    "title": "CCC Node",
                    "version": "0.1.0",
                }
            },
        },
        {"method": "initialized"},
    ]
    await client.close()


@pytest.mark.asyncio
async def test_start_is_idempotent_and_keeps_one_reader() -> None:
    reader = asyncio.StreamReader()
    writer = FakeWriter(reader)
    client = CodexAppServerClient(reader=reader, writer=writer)

    first, second = await asyncio.gather(client.start(), client.start())

    assert first == second == {"userAgent": "fake"}
    assert [message.get("method") for message in writer.messages] == ["initialize", "initialized"]
    await client.close()


@pytest.mark.asyncio
async def test_concurrent_requests_correlate_out_of_order_responses_and_serialize_writes() -> None:
    reader = asyncio.StreamReader()
    writer = FakeWriter(reader)
    client = CodexAppServerClient(reader=reader, writer=writer)
    await client.start()

    first = asyncio.create_task(client.request("first", {"value": 1}))
    second = asyncio.create_task(client.request("second", {"value": 2}))
    while len(writer.messages) < 4:
        await asyncio.sleep(0)
    first_message, second_message = writer.messages[-2:]

    writer.feed({"id": second_message["id"], "result": "second-result"})
    writer.feed({"id": first_message["id"], "result": "first-result"})

    assert list(await asyncio.gather(first, second)) == ["first-result", "second-result"]
    assert writer.max_active_drains == 1
    await client.close()


@pytest.mark.asyncio
async def test_notifications_and_server_requests_are_surfaced_separately() -> None:
    reader = asyncio.StreamReader()
    writer = FakeWriter(reader)
    client = CodexAppServerClient(reader=reader, writer=writer)
    await client.start()

    writer.feed({"method": "turn/started", "params": {"turn": {"id": "turn-1"}}})
    writer.feed({"id": "server-1", "method": "unrecognized/request", "params": {"value": 1}})

    assert await client.next_notification() == CodexNotification(
        method="turn/started", params={"turn": {"id": "turn-1"}}
    )
    assert await client.next_server_request() == CodexServerRequest(
        id="server-1", method="unrecognized/request", params={"value": 1}
    )
    while len(writer.messages) < 3:
        await asyncio.sleep(0)
    assert writer.messages[-1] == {
        "id": "server-1",
        "error": {"code": -32601, "message": "Client does not support server request"},
    }
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "expected_result"),
    (
        ("item/commandExecution/requestApproval", {"decision": "decline"}),
        ("item/fileChange/requestApproval", {"decision": "decline"}),
        ("item/permissions/requestApproval", {"permissions": {}}),
        ("mcpServer/elicitation/request", {"action": "decline", "content": None}),
    ),
)
async def test_approval_request_is_denied_by_default(
    method: str,
    expected_result: Mapping[str, Any],
) -> None:
    reader = asyncio.StreamReader()
    writer = FakeWriter(reader)
    client = CodexAppServerClient(reader=reader, writer=writer)
    await client.start()

    writer.feed(
        {
            "id": 41,
            "method": method,
            "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1"},
        }
    )

    request = await client.next_server_request()
    assert request.method == method
    while len(writer.messages) < 3:
        await asyncio.sleep(0)
    assert writer.messages[-1] == {"id": 41, "result": expected_result}
    await client.close()


@pytest.mark.asyncio
async def test_eof_fails_pending_requests() -> None:
    reader = asyncio.StreamReader()
    writer = FakeWriter(reader)
    client = CodexAppServerClient(reader=reader, writer=writer)
    await client.start()
    pending = asyncio.create_task(client.request("model/list", {}))
    while len(writer.messages) < 3:
        await asyncio.sleep(0)

    reader.feed_eof()

    with pytest.raises(CodexConnectionClosedError, match="EOF"):
        await asyncio.wait_for(pending, timeout=0.2)
    with pytest.raises(CodexConnectionClosedError, match="EOF"):
        await client.start()
    with pytest.raises(CodexConnectionClosedError, match="EOF"):
        await client.notify("after/eof")
    await client.close()


@pytest.mark.asyncio
async def test_injected_process_factory_and_close_are_idempotent() -> None:
    process = FakeProcess()
    factory_calls = 0

    async def factory() -> FakeProcess:
        nonlocal factory_calls
        factory_calls += 1
        return process

    client = CodexAppServerClient(process_factory=factory)
    await client.start()

    await client.close()
    await client.close()

    assert factory_calls == 1
    assert process.terminate_calls == 1
    assert process.stdin.close_calls == 1


@pytest.mark.asyncio
async def test_default_factory_launches_codex_app_server_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess()
    spawn_args: tuple[str, ...] = ()
    spawn_kwargs: dict[str, Any] = {}

    async def fake_spawn(*args: str, **kwargs: Any) -> FakeProcess:
        nonlocal spawn_args, spawn_kwargs
        spawn_args = args
        spawn_kwargs = kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    client = CodexAppServerClient()

    await client.start()

    assert spawn_args == ("codex", "app-server", "--stdio")
    assert spawn_kwargs == {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
    }
    await client.close()


@pytest.mark.asyncio
async def test_protocol_helpers_use_known_method_names_and_parameters() -> None:
    reader = asyncio.StreamReader()
    writer = FakeWriter(reader)
    client = CodexAppServerClient(reader=reader, writer=writer)
    await client.start()

    async def assert_call(coro: Any, method: str, params: Mapping[str, Any]) -> None:
        before = len(writer.messages)
        task = asyncio.create_task(coro)
        while len(writer.messages) == before:
            await asyncio.sleep(0)
        message = writer.messages[-1]
        assert message["method"] == method
        assert message["params"] == params
        writer.feed({"id": message["id"], "result": {"ok": True}})
        assert await task == {"ok": True}

    await assert_call(client.thread_start(cwd="/workspace", model="o3"), "thread/start", {
        "cwd": "/workspace",
        "model": "o3",
    })
    await assert_call(client.thread_resume("thread-1"), "thread/resume", {"threadId": "thread-1"})
    await assert_call(
        client.turn_start("thread-1", [{"type": "text", "text": "hello"}]),
        "turn/start",
        {"threadId": "thread-1", "input": [{"type": "text", "text": "hello"}]},
    )
    await assert_call(
        client.turn_interrupt("thread-1", "turn-1"),
        "turn/interrupt",
        {"threadId": "thread-1", "turnId": "turn-1"},
    )
    await assert_call(client.list_models(include_hidden=True), "model/list", {"includeHidden": True})
    await client.close()


@pytest.mark.asyncio
async def test_malformed_messages_do_not_deadlock_pending_requests() -> None:
    reader = asyncio.StreamReader()
    writer = FakeWriter(reader)
    client = CodexAppServerClient(reader=reader, writer=writer)
    await client.start()
    pending = asyncio.create_task(client.request("model/list", {}))
    while len(writer.messages) < 3:
        await asyncio.sleep(0)
    request_id = writer.messages[-1]["id"]

    writer.feed_raw(b"not-json")
    writer.feed({"id": request_id, "method": 42, "params": {}})
    writer.feed({"id": request_id})
    writer.feed({"id": request_id, "result": {"data": []}})

    assert await asyncio.wait_for(pending, timeout=0.2) == {"data": []}
    await client.close()


@pytest.mark.asyncio
async def test_process_exit_fails_pending_requests() -> None:
    process = FakeProcess()

    async def factory() -> FakeProcess:
        return process

    client = CodexAppServerClient(process_factory=factory)
    await client.start()
    pending = asyncio.create_task(client.request("model/list", {}))
    while len(process.stdin.messages) < 3:
        await asyncio.sleep(0)

    process.exit(7)

    with pytest.raises(CodexConnectionClosedError, match="status 7"):
        await asyncio.wait_for(pending, timeout=0.2)
    await client.close()
