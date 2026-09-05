"""Tests for the standalone Codex app-server transport."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import pytest
import unittest

if TYPE_CHECKING:
    from core.codex_app_server import (
        STDOUT_BUFFER_LIMIT,
        CodexAppServerClient,
        CodexConnectionClosedError,
        CodexNotification,
        CodexProtocolError,
        CodexServerRequest,
    )
else:
    from telegram_bot.core.codex_app_server import (
        STDOUT_BUFFER_LIMIT,
        CodexAppServerClient,
        CodexConnectionClosedError,
        CodexNotification,
        CodexProtocolError,
        CodexServerRequest,
    )


class FakeWriter:
    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.reader = reader
        self.messages: list[dict[str, Any]] = []
        self.close_calls = 0
        self.active_drains = 0
        self.max_active_drains = 0
        self.fail_drain = False

    def write(self, data: bytes) -> None:
        message = json.loads(data.decode())
        self.messages.append(message)
        if message.get("method") == "initialize" and "id" in message:
            self.feed({"id": message["id"], "result": {"userAgent": "fake"}})

    async def drain(self) -> None:
        if self.fail_drain:
            raise OSError("writer unavailable")
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


class _SilentWriter(FakeWriter):
    """A ``FakeWriter`` that never auto-answers "initialize".

    ``FakeWriter.write`` immediately feeds a fake response the moment it sees
    an outgoing "initialize" request, which is convenient for the happy-path
    tests above but makes it impossible to control exactly when (or whether)
    a response arrives. Tests that need to simulate a cancelled/timed-out
    handshake use this variant and feed responses manually.
    """

    def write(self, data: bytes) -> None:
        self.messages.append(json.loads(data.decode()))


class FakeProcess:
    def __init__(self, *, ignore_terminate: bool = False) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = FakeWriter(self.stdout)
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.ignore_terminate = ignore_terminate
        self._exited = asyncio.Event()

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.ignore_terminate:
            return
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.exit(0)

    def kill(self) -> None:
        self.kill_calls += 1
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.exit(-9)

    def exit(self, returncode: int) -> None:
        self.returncode = returncode
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode



class CodexAppServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_performs_initialize_handshake(self) -> None:
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


    async def test_start_is_idempotent_and_keeps_one_reader(self) -> None:
        reader = asyncio.StreamReader()
        writer = FakeWriter(reader)
        client = CodexAppServerClient(reader=reader, writer=writer)

        first, second = await asyncio.gather(client.start(), client.start())

        assert first == second == {"userAgent": "fake"}
        assert [message.get("method") for message in writer.messages] == ["initialize", "initialized"]
        await client.close()

    async def test_start_discards_transport_after_a_cancelled_initialize(self) -> None:
        """Regression for the "Already initialized" -32600 failure.

        If start() is cancelled while awaiting the "initialize" response
        (e.g. an outer asyncio.wait_for(..., timeout=7.0) during a slow cold
        start), the write may already have reached the app-server even
        though the client never saw the response. Leaving the old
        process/reader/writer installed would make the next start() call
        skip spawning a fresh process and resend "initialize" on that
        already-initialized connection, which the real app-server rejects
        with {"code": -32600, "message": "Already initialized"}. start() must
        discard the half-initialized transport so a retry always begins from
        a brand new process.
        """

        processes: list[FakeProcess] = []

        async def factory() -> FakeProcess:
            process = FakeProcess()
            process.stdin = _SilentWriter(process.stdout)
            processes.append(process)
            return process

        client = CodexAppServerClient(process_factory=factory)

        first_attempt = asyncio.create_task(client.start())
        for _ in range(2000):
            if processes and processes[0].stdin.messages:
                break
            await asyncio.sleep(0)
        else:
            self.fail("first start() never wrote an initialize request")
        assert [m.get("method") for m in processes[0].stdin.messages] == ["initialize"]

        first_attempt.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(first_attempt), timeout=5)

        # The failed attempt must not leave a half-initialized transport
        # behind: no reader/writer/process installed, and the abandoned
        # process's stdin was actually closed (not merely forgotten).
        assert client._reader is None
        assert client._writer is None
        assert client._process is None
        assert client._started is False
        assert processes[0].stdin.close_calls == 1
        assert processes[0].terminate_calls == 1

        second_attempt = asyncio.create_task(client.start())
        for _ in range(2000):
            if len(processes) >= 2 and processes[1].stdin.messages:
                break
            await asyncio.sleep(0)
        else:
            detail = f", second start() failed with {second_attempt.exception()!r}" if second_attempt.done() else ""
            self.fail(f"retry never spawned a fresh process{detail}")
        first_message = processes[1].stdin.messages[0]
        assert first_message["method"] == "initialize"
        processes[1].stdin.feed({"id": first_message["id"], "result": {"userAgent": "fake"}})

        second_result = await asyncio.wait_for(second_attempt, timeout=5)

        # The retry must spawn a genuinely new process rather than resending
        # "initialize" on the abandoned one.
        assert len(processes) == 2
        assert second_result == {"userAgent": "fake"}
        assert [m.get("method") for m in processes[1].stdin.messages] == ["initialize", "initialized"]

        await client.close()

    async def test_concurrent_requests_correlate_out_of_order_responses_and_serialize_writes(self) -> None:
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

    async def test_write_failure_closes_connection_without_leaking_pending_future(self) -> None:
        reader = asyncio.StreamReader()
        writer = FakeWriter(reader)
        client = CodexAppServerClient(reader=reader, writer=writer)
        await client.start()
        writer.fail_drain = True

        with self.assertRaisesRegex(CodexConnectionClosedError, "write failed"):
            await client.request("model/list", {})
        with self.assertRaisesRegex(CodexConnectionClosedError, "write failed"):
            await client.notify("after/write-failure")

        await client.close()

    async def test_notifications_and_server_requests_are_surfaced_separately(self) -> None:
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

    async def test_custom_server_handler_does_not_block_stdout_reader(self) -> None:
        release = asyncio.Event()
        handled: list[CodexServerRequest] = []

        async def handler(request: CodexServerRequest) -> Mapping[str, Any]:
            handled.append(request)
            await release.wait()
            return {"result": {"decision": "accept"}}

        reader = asyncio.StreamReader()
        writer = FakeWriter(reader)
        client = CodexAppServerClient(
            reader=reader,
            writer=writer,
            server_request_handler=handler,
        )
        await client.start()
        pending = asyncio.create_task(client.request("model/list", {}))
        while len(writer.messages) < 3:
            await asyncio.sleep(0)
        outgoing_id = writer.messages[-1]["id"]

        writer.feed(
            {
                "id": "approval-1",
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1"},
            }
        )
        writer.feed({"id": outgoing_id, "result": {"data": []}})

        assert await asyncio.wait_for(pending, timeout=0.2) == {"data": []}
        assert handled and handled[0].id == "approval-1"
        assert not any(message.get("id") == "approval-1" for message in writer.messages)
        release.set()
        while not any(message.get("id") == "approval-1" for message in writer.messages):
            await asyncio.sleep(0)
        assert writer.messages[-1] == {
            "id": "approval-1",
            "result": {"decision": "accept"},
        }
        await client.close()

    async def test_server_handler_timeout_falls_back_to_fail_closed_response(self) -> None:
        never = asyncio.Event()

        async def handler(_request: CodexServerRequest) -> Mapping[str, Any]:
            await never.wait()
            return {"result": {"decision": "accept"}}

        reader = asyncio.StreamReader()
        writer = FakeWriter(reader)
        client = CodexAppServerClient(
            reader=reader,
            writer=writer,
            server_request_handler=handler,
            server_request_timeout=0.01,
        )
        await client.start()
        writer.feed(
            {
                "id": 42,
                "method": "item/fileChange/requestApproval",
                "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1"},
            }
        )

        while not any(message.get("id") == 42 for message in writer.messages):
            await asyncio.sleep(0)
        assert writer.messages[-1] == {"id": 42, "result": {"decision": "decline"}}
        await client.close()

    async def test_approval_request_is_denied_by_default(self) -> None:
        cases: tuple[tuple[str, Mapping[str, Any]], ...] = (
            ("item/commandExecution/requestApproval", {"decision": "decline"}),
            ("item/fileChange/requestApproval", {"decision": "decline"}),
            ("item/permissions/requestApproval", {"permissions": {}, "scope": "turn"}),
            (
                "mcpServer/elicitation/request",
                {"action": "decline", "content": None, "_meta": None},
            ),
        )
        for method, expected_result in cases:
            with self.subTest(method=method):
                reader = asyncio.StreamReader()
                writer = FakeWriter(reader)
                client = CodexAppServerClient(reader=reader, writer=writer)
                await client.start()

                writer.feed(
                    {
                        "id": 41,
                        "method": method,
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "itemId": "item-1",
                        },
                    }
                )

                request = await client.next_server_request()
                assert request.method == method
                while len(writer.messages) < 3:
                    await asyncio.sleep(0)
                assert writer.messages[-1] == {"id": 41, "result": expected_result}
                await client.close()


    async def test_eof_fails_pending_requests(self) -> None:
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


    async def test_injected_process_factory_and_close_are_idempotent(self) -> None:
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

    async def test_close_kills_process_that_ignores_terminate(self) -> None:
        process = FakeProcess(ignore_terminate=True)

        async def factory() -> FakeProcess:
            return process

        client = CodexAppServerClient(
            process_factory=factory,
            process_shutdown_timeout=0.01,
        )
        await client.start()

        await asyncio.wait_for(client.close(), timeout=0.2)

        assert process.terminate_calls == 1
        assert process.kill_calls == 1
        assert process.returncode == -9

    async def test_default_factory_launches_codex_app_server_stdio(self) -> None:
        process = FakeProcess()
        spawn_args: tuple[str, ...] = ()
        spawn_kwargs: dict[str, Any] = {}

        async def fake_spawn(*args: str, **kwargs: Any) -> FakeProcess:
            nonlocal spawn_args, spawn_kwargs
            spawn_args = args
            spawn_kwargs = kwargs
            return process

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
            client = CodexAppServerClient()

            await client.start()

            assert spawn_args == ("codex", "app-server", "--stdio")
            assert spawn_kwargs == {
                "stdin": asyncio.subprocess.PIPE,
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
                "limit": STDOUT_BUFFER_LIMIT,
            }
            # Regression: the stdout buffer must exceed asyncio's 64 KiB default
            # so oversized JSONL frames are read whole rather than crashing the
            # reader loop.
            assert spawn_kwargs["limit"] > 64 * 1024
            await client.close()

    async def test_default_factory_binds_an_explicit_process_environment(self) -> None:
        process = FakeProcess()
        spawn_kwargs: dict[str, Any] = {}

        async def fake_spawn(*_args: str, **kwargs: Any) -> FakeProcess:
            nonlocal spawn_kwargs
            spawn_kwargs = kwargs
            return process

        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "CODEX_HOME": "/private/audience/codex",
            "CODEX_SQLITE_HOME": "/private/audience/codex",
        }
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
            client = CodexAppServerClient(process_environment=environment)

            await client.start()

            assert spawn_kwargs["env"] == environment
            await client.close()

    async def test_audience_keyring_mode_is_bound_as_a_codex_config_override(self) -> None:
        process = FakeProcess()
        spawn_args: tuple[str, ...] = ()

        async def fake_spawn(*args: str, **_kwargs: Any) -> FakeProcess:
            nonlocal spawn_args
            spawn_args = args
            return process

        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "CODEX_HOME": "/private/audience/codex",
            "CODEX_SQLITE_HOME": "/private/audience/codex",
            "CCC_CODEX_AUDIENCE_AUTH_MODE": "keyring",
        }
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
            client = CodexAppServerClient(process_environment=environment)

            await client.start()

            assert spawn_args == (
                "codex",
                "--config",
                'cli_auth_credentials_store="keyring"',
                "app-server",
                "--stdio",
            )
            await client.close()

        with pytest.raises(ValueError, match="audience auth mode is invalid"):
            CodexAppServerClient(
                process_environment={"CCC_CODEX_AUDIENCE_AUTH_MODE": "file"}
            )

    async def test_reader_accepts_lines_larger_than_default_stream_limit(self) -> None:
        # A default StreamReader (64 KiB limit) raises ValueError on this line and
        # tears the connection down; the raised STDOUT_BUFFER_LIMIT reads it whole.
        reader = asyncio.StreamReader(limit=STDOUT_BUFFER_LIMIT)
        writer = FakeWriter(reader)
        client = CodexAppServerClient(reader=reader, writer=writer)
        await client.start()

        request_task = asyncio.create_task(client.request("model/list", {}))
        while writer.messages[-1].get("method") != "model/list":
            await asyncio.sleep(0)
        request = writer.messages[-1]

        big_value = "x" * (200 * 1024)  # 200 KiB — well past the 64 KiB default
        writer.feed({"id": request["id"], "result": {"blob": big_value}})

        result = await asyncio.wait_for(request_task, timeout=5)
        assert result == {"blob": big_value}
        await client.close()


    async def test_protocol_helpers_use_known_method_names_and_parameters(self) -> None:
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
            client.thread_rollback("thread-1", num_turns=1),
            "thread/rollback",
            {"threadId": "thread-1", "numTurns": 1},
        )
        await assert_call(
            client.turn_start(
                "thread-1",
                [{"type": "text", "text": "hello"}],
                model="o3",
                effort="high",
                approval_policy="on-request",
                approvals_reviewer="auto_review",
                sandbox_policy={"type": "workspaceWrite", "networkAccess": False},
            ),
            "turn/start",
            {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "hello"}],
                "model": "o3",
                "effort": "high",
                "approvalPolicy": "on-request",
                "approvalsReviewer": "auto_review",
                "sandboxPolicy": {"type": "workspaceWrite", "networkAccess": False},
            },
        )
        await assert_call(
            client.turn_start(
                "thread-1",
                [{"type": "text", "text": "full access"}],
                approval_policy="never",
                sandbox_policy={"type": "dangerFullAccess"},
            ),
            "turn/start",
            {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "full access"}],
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "dangerFullAccess"},
            },
        )
        await assert_call(
            client.turn_start(
                "thread-1", [{"type": "text", "text": "use defaults"}]
            ),
            "turn/start",
            {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "use defaults"}],
            },
        )
        await assert_call(
            client.turn_interrupt("thread-1", "turn-1"),
            "turn/interrupt",
            {"threadId": "thread-1", "turnId": "turn-1"},
        )
        await assert_call(client.list_models(include_hidden=True), "model/list", {"includeHidden": True})
        for call, method in (
            (client.account_rate_limits(), "account/rateLimits/read"),
            (client.account_usage(), "account/usage/read"),
        ):
            before = len(writer.messages)
            task = asyncio.create_task(call)
            while len(writer.messages) == before:
                await asyncio.sleep(0)
            request = writer.messages[-1]
            assert request["method"] == method
            assert "params" not in request
            writer.feed({"id": request["id"], "result": {"ok": True}})
            assert await task == {"ok": True}
        await client.close()


    async def test_thread_browsing_helpers_use_v2_shapes_and_parse_defensively(self) -> None:
        reader = asyncio.StreamReader()
        writer = FakeWriter(reader)
        client = CodexAppServerClient(reader=reader, writer=writer)
        await client.start()

        list_task = asyncio.create_task(client.thread_list(limit=7, cursor="next-1"))
        while writer.messages[-1].get("method") != "thread/list":
            await asyncio.sleep(0)
        request = writer.messages[-1]
        assert request["params"] == {
            "limit": 7,
            "cursor": "next-1",
            "sortKey": "updated_at",
        }
        writer.feed({"id": request["id"], "result": {
            "data": [
                {"id": "thread-1", "title": "Title", "preview": "Preview", "updatedAt": 42,
                 "cwd": "/workspace", "model": "o3"},
                {"id": "", "preview": "invalid"},
                "invalid",
            ],
            "nextCursor": "next-2",
        }})
        page = await list_task
        assert len(page.data) == 1
        assert page.data[0].id == "thread-1"
        assert page.next_cursor == "next-2"

        read_task = asyncio.create_task(client.thread_read("thread-1", include_turns=True))
        while writer.messages[-1].get("method") != "thread/read":
            await asyncio.sleep(0)
        request = writer.messages[-1]
        assert request["params"] == {"threadId": "thread-1", "includeTurns": True}
        writer.feed({"id": request["id"], "result": {"thread": {
            "id": "thread-1",
            "turns": [{"id": "turn-1", "items": [{"type": "userMessage", "content": [
                {"type": "text", "text": "hello"}
            ]}]}],
        }}})
        thread = await read_task
        assert thread is not None
        assert thread.id == "thread-1"
        assert len(thread.turns) == 1

        malformed_task = asyncio.create_task(client.thread_list())
        while writer.messages[-1].get("method") != "thread/list":
            await asyncio.sleep(0)
        request = writer.messages[-1]
        writer.feed({"id": request["id"], "result": {
            "data": "private raw payload", "nextCursor": 7
        }})
        malformed = await malformed_task
        assert malformed.data == ()
        assert malformed.next_cursor is None

        error_task = asyncio.create_task(client.thread_read("thread-1"))
        while writer.messages[-1].get("method") != "thread/read":
            await asyncio.sleep(0)
        request = writer.messages[-1]
        writer.feed({
            "id": request["id"],
            "error": {"code": -1, "message": "private raw payload"},
        })
        with pytest.raises(CodexProtocolError) as caught:
            await error_task
        assert str(caught.value) == "thread/read request failed"
        assert "private raw payload" not in str(caught.value)
        await client.close()

    async def test_malformed_messages_do_not_deadlock_pending_requests(self) -> None:
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
        writer.feed({"id": request_id, "result": {"data": []}})

        assert await asyncio.wait_for(pending, timeout=0.2) == {"data": []}
        await client.close()

    async def test_malformed_request_with_id_gets_invalid_request_response(self) -> None:
        handled: list[CodexServerRequest] = []

        async def handler(request: CodexServerRequest) -> Mapping[str, Any]:
            handled.append(request)
            return {"result": {}}

        reader = asyncio.StreamReader()
        writer = FakeWriter(reader)
        client = CodexAppServerClient(
            reader=reader, writer=writer, server_request_handler=handler
        )
        await client.start()
        baseline = len(writer.messages)

        writer.feed({"id": "srv-1", "method": 42, "params": {}})
        while len(writer.messages) == baseline:
            await asyncio.sleep(0)
        assert writer.messages[-1] == {
            "id": "srv-1",
            "error": {"code": -32600, "message": "Invalid Request"},
        }
        assert handled == []

        # An in-flight client request is unaffected by the rejection.
        pending = asyncio.create_task(client.request("model/list", {}))
        while writer.messages[-1].get("method") != "model/list":
            await asyncio.sleep(0)
        request_id = writer.messages[-1]["id"]
        writer.feed({"id": request_id, "result": {"data": []}})
        assert await asyncio.wait_for(pending, timeout=0.2) == {"data": []}
        await client.close()

    async def test_malformed_request_with_null_params_gets_invalid_request_response(
        self,
    ) -> None:
        # Concrete trigger shape (#1132): serde emits "params": null for an
        # Option<T> without skip_serializing_if, and dict.get("params", {})
        # returns None — not {} — when the key is present with a null value.
        reader = asyncio.StreamReader()
        writer = FakeWriter(reader)
        client = CodexAppServerClient(reader=reader, writer=writer)
        await client.start()
        baseline = len(writer.messages)

        writer.feed({"id": 7, "method": "thread/list", "params": None})
        while len(writer.messages) == baseline:
            await asyncio.sleep(0)
        assert writer.messages[-1] == {
            "id": 7,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
        await client.close()

    async def test_malformed_notification_is_logged_but_never_answered(self) -> None:
        reader = asyncio.StreamReader()
        writer = FakeWriter(reader)
        client = CodexAppServerClient(reader=reader, writer=writer)
        await client.start()
        baseline = len(writer.messages)

        with self.assertLogs(
            "telegram_bot.core.codex_app_server", level="WARNING"
        ) as captured:
            writer.feed({"method": 42, "params": {}})
            # Synchronize on the trailing valid notification: the single
            # reader loop processes frames in order, so once it is dequeued
            # the malformed frame has been fully handled.
            writer.feed({"method": "thread/started", "params": {"threadId": "t-1"}})
            notification = await asyncio.wait_for(client.next_notification(), timeout=0.2)
        assert notification.method == "thread/started"
        assert len(writer.messages) == baseline
        assert any(
            "malformed" in line and "notification" in line for line in captured.output
        )
        await client.close()

    async def test_well_formed_request_with_non_rpc_id_is_dropped_with_log(self) -> None:
        handled: list[CodexServerRequest] = []

        async def handler(request: CodexServerRequest) -> Mapping[str, Any]:
            handled.append(request)
            return {"result": {}}

        reader = asyncio.StreamReader()
        writer = FakeWriter(reader)
        client = CodexAppServerClient(
            reader=reader, writer=writer, server_request_handler=handler
        )
        await client.start()
        baseline = len(writer.messages)

        with self.assertLogs(
            "telegram_bot.core.codex_app_server", level="WARNING"
        ) as captured:
            writer.feed({"id": 1.5, "method": "thread/list", "params": {}})
            writer.feed({"id": True, "method": "thread/list", "params": {}})
            writer.feed({"method": "thread/started", "params": {}})
            await asyncio.wait_for(client.next_notification(), timeout=0.2)
        assert handled == []
        assert len(writer.messages) == baseline
        assert sum("unaddressable id type" in line for line in captured.output) == 2
        await client.close()

    async def test_malformed_correlated_response_fails_promptly(self) -> None:
        reader = asyncio.StreamReader()
        writer = FakeWriter(reader)
        client = CodexAppServerClient(reader=reader, writer=writer)
        await client.start()
        pending = asyncio.create_task(client.request("model/list", {}))
        while len(writer.messages) < 3:
            await asyncio.sleep(0)
        request_id = writer.messages[-1]["id"]

        writer.feed({"id": request_id})

        with self.assertRaisesRegex(CodexProtocolError, "exactly one"):
            await asyncio.wait_for(pending, timeout=0.2)
        await client.close()


    async def test_process_exit_fails_pending_requests(self) -> None:
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



class CodexAppServerStderrTests(unittest.IsolatedAsyncioTestCase):
    """stderr drain: journal forwarding + orphan-loop counting (#1112)."""

    async def test_stderr_lines_feed_the_orphan_loop_tracker(self) -> None:
        from telegram_bot.core import codex_app_server, turn_stall
        from telegram_bot.core.turn_stall import OrphanLoopTracker

        # Swap in an isolated tracker so the process-wide one stays untouched.
        tracker = OrphanLoopTracker()
        original = turn_stall.orphan_tool_loop_tracker
        turn_stall.orphan_tool_loop_tracker = tracker
        # The client module imported the tracker by value; rebind there too.
        codex_app_server.orphan_tool_loop_tracker = tracker
        try:
            process = FakeProcess()

            async def factory() -> FakeProcess:
                return process

            client = CodexAppServerClient(process_factory=factory)
            await client.start()

            process.stderr.feed_data(
                b"ERROR Custom tool call output is missing for call abc\n"
            )
            process.stderr.feed_data(b"ordinary warning line\n")
            for _ in range(20):
                await asyncio.sleep(0.01)
                if tracker.recent_count() >= 1:
                    break
            assert tracker.recent_count() == 1

            await client.close()
        finally:
            turn_stall.orphan_tool_loop_tracker = original
            codex_app_server.orphan_tool_loop_tracker = original

    async def test_process_exited_reports_confirmed_death_only(self) -> None:
        process = FakeProcess()

        async def factory() -> FakeProcess:
            return process

        client = CodexAppServerClient(process_factory=factory)
        assert client.process_exited() is None  # no process yet: ambiguous

        await client.start()
        assert client.process_exited() is False  # running

        process.exit(0)
        assert client.process_exited() is True  # confirmed dead
        await client.close()
