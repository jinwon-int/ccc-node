"""Contract tests for the unrestricted Piri runtime adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import TYPE_CHECKING, Any
import unittest
from unittest.mock import patch

if TYPE_CHECKING:
    from core.agent_runtime import AgentEvent
    from core.piri_runtime import PiriLaunchConfig
else:
    from telegram_bot.core.agent_runtime import (
        ApprovalRequestEvent,
        CompletionEvent,
        ErrorEvent,
        MessageCompletedEvent,
        ReasoningDeltaEvent,
        ResultEvent,
        SessionRequest,
        TextDeltaEvent,
        ToolCompletedEvent,
        ToolStartedEvent,
    )
    from telegram_bot.core.piri_rpc import PiriRpcProcessClient
    from telegram_bot.core.piri_runtime import PiriLaunchConfig, PiriRuntime
    from telegram_bot.memory.distill_types import TranscriptBounds


class FakePiriClient:
    def __init__(self, config: PiriLaunchConfig, *, session_id: str) -> None:
        self.config = config
        self.state: Mapping[str, Any] = {"sessionId": session_id, "model": None}
        self.models: Sequence[Mapping[str, Any]] = ()
        self.events: asyncio.Queue[Mapping[str, Any] | BaseException] = asyncio.Queue()
        self.start_calls = 0
        self.close_calls = 0
        self.abort_calls = 0
        self.prompt_calls: list[str] = []
        self.prompted = asyncio.Event()

    async def start(self) -> None:
        self.start_calls += 1

    async def prompt(self, message: str) -> None:
        self.prompt_calls.append(message)
        self.prompted.set()

    async def abort(self) -> None:
        self.abort_calls += 1
        await self.events.put(
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "aborted"},
            }
        )
        await self.events.put({"type": "agent_settled"})

    async def get_state(self) -> Mapping[str, Any]:
        return self.state

    async def get_available_models(self) -> Sequence[Mapping[str, Any]]:
        return self.models

    async def next_event(self) -> Mapping[str, Any]:
        event = await self.events.get()
        if isinstance(event, BaseException):
            raise event
        return event

    async def close(self) -> None:
        self.close_calls += 1


class FakePiriFactory:
    def __init__(self) -> None:
        self.clients: list[FakePiriClient] = []
        self.next_session_id = "piri-new"

    def __call__(self, config: PiriLaunchConfig) -> FakePiriClient:
        session_id = self.next_session_id
        if "--session-id" in config.command:
            index = config.command.index("--session-id")
            session_id = config.command[index + 1]
        client = FakePiriClient(config, session_id=session_id)
        self.clients.append(client)
        return client


async def collect(stream: Any) -> list[AgentEvent]:
    return [event async for event in stream]


class PiriRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.factory = FakePiriFactory()
        self.runtime = PiriRuntime(
            executable="/opt/piri/bin/piri",
            client_factory=self.factory,
            process_environment={"BASE": "one"},
        )

    async def asyncTearDown(self) -> None:
        await self.runtime.close()

    async def test_new_session_uses_unrestricted_process_contract(self) -> None:
        session = await self.runtime.start_or_resume(
            SessionRequest(
                working_directory="/workspace/project",
                model="openai-codex/gpt-5.5",
                effort="high",
                approval_policy="never",
                sandbox_policy={"type": "dangerFullAccess"},
                memory_environment={"MEMORY": "two"},
            )
        )

        self.assertEqual(session.session_id, "piri-new")
        config = self.factory.clients[0].config
        self.assertEqual(config.working_directory, "/workspace/project")
        self.assertEqual(
            config.command,
            (
                "/opt/piri/bin/piri",
                "--mode",
                "rpc",
                "--approve",
                "--model",
                "openai-codex/gpt-5.5",
                "--thinking",
                "high",
            ),
        )
        self.assertNotIn("--no-tools", config.command)
        self.assertNotIn("--tools", config.command)
        self.assertEqual(config.environment["BASE"], "one")
        self.assertEqual(config.environment["MEMORY"], "two")
        self.assertTrue(config.auto_confirm_extensions)

    async def test_audience_memory_is_materialized_and_isolated_from_context_discovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_dir = root / "sessions"
            bootstrap_home = root / "bootstrap"
            context_file = bootstrap_home / "AGENTS.md"
            memory_environment = {
                "PIRI_CODING_AGENT_SESSION_DIR": str(session_dir),
                "CCC_PIRI_BOOTSTRAP_HOME": str(bootstrap_home),
                "CCC_PIRI_BOOTSTRAP_CONTEXT_FILE": str(context_file),
            }

            async def materialize(
                _path: str, *, timeout_seconds: float, environment: Mapping[str, str]
            ) -> None:
                self.assertEqual(timeout_seconds, 3.0)
                self.assertEqual(environment["CODEX_HOME"], str(bootstrap_home))
                self.assertEqual(environment["CCC_MEMORY_MATERIALIZER_PROVIDER"], "piri")
                context_file.write_text("scoped memory", encoding="utf-8")
                context_file.chmod(0o600)

            runtime = PiriRuntime(
                executable="/opt/piri/bin/piri",
                client_factory=self.factory,
                process_environment={"BASE": "one"},
                memory_materializer_path="/materializer",
                memory_bootstrap_timeout_seconds=3.0,
                memory_environment_validator=lambda value: self.assertEqual(
                    dict(value), memory_environment
                ),
            )
            with patch(
                "telegram_bot.core.piri_runtime._run_codex_memory_bootstrap",
                side_effect=materialize,
            ):
                session = await runtime.start_or_resume(
                    SessionRequest(
                        working_directory="/workspace/project",
                        memory_environment=memory_environment,
                    )
                )

            config = self.factory.clients[-1].config
            self.assertIn("--no-context-files", config.command)
            self.assertEqual(
                config.command[-2:],
                ("--append-system-prompt", str(context_file)),
            )
            self.assertEqual(
                config.environment["PIRI_CODING_AGENT_SESSION_DIR"],
                str(session_dir),
            )
            self.assertEqual(runtime._session_directories[session.session_id], session_dir)
            await runtime.close()

    async def test_unscoped_snapshot_falls_back_to_default_sessions_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent_dir = root / "agent"
            agent_dir.mkdir(mode=0o700)
            sessions_root = agent_dir / "sessions"
            sessions_root.mkdir(mode=0o700)
            session_dir = sessions_root / "--root--"
            session_dir.mkdir(mode=0o700)
            session_id = "019fd178-8590-7a99-92ed-962d0982495f"
            timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            payload = [
                {
                    "type": "session",
                    "version": 3,
                    "id": session_id,
                    "timestamp": timestamp,
                    "cwd": "/root",
                },
                {
                    "type": "message",
                    "id": "m1",
                    "timestamp": timestamp,
                    "message": {"role": "user", "content": "fallback works"},
                },
            ]
            path = session_dir / f"2026-08-05T00-00-00-000Z_{session_id}.jsonl"
            path.write_text(
                "".join(json.dumps(value) + "\n" for value in payload),
                encoding="utf-8",
            )
            path.chmod(0o600)

            runtime = PiriRuntime(
                executable="/opt/piri/bin/piri",
                client_factory=self.factory,
                process_environment={"PIRI_CODING_AGENT_DIR": str(agent_dir)},
            )
            snapshot = await runtime.read_session_snapshot(
                session_id,
                bounds=TranscriptBounds(),
            )
            self.assertEqual(
                [message.text for message in snapshot.messages],
                ["fallback works"],
            )
            await runtime.close()

    async def test_unscoped_snapshot_without_a_transcript_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = PiriRuntime(
                executable="/opt/piri/bin/piri",
                client_factory=self.factory,
                process_environment={"PIRI_CODING_AGENT_DIR": directory},
            )
            with self.assertRaisesRegex(ValueError, "Piri snapshot session route is unavailable"):
                await runtime.read_session_snapshot(
                    "019fd490-a780-7b0d-bf4e-4baf0e6a1762",
                    bounds=TranscriptBounds(),
                )
            await runtime.close()

    async def test_audience_memory_rejects_a_tampered_route_before_launch(self) -> None:
        runtime = PiriRuntime(
            client_factory=self.factory,
            process_environment={"BASE": "one"},
            memory_materializer_path="/materializer",
            memory_environment_validator=lambda _value: (_ for _ in ()).throw(
                ValueError("invalid route")
            ),
        )
        with self.assertRaisesRegex(ValueError, "invalid route"):
            await runtime.start_or_resume(
                SessionRequest(
                    working_directory="/workspace",
                    memory_environment={"UNTRUSTED": "path"},
                )
            )
        self.assertEqual(self.factory.clients, [])

    async def test_memory_materializer_without_route_validator_fails_closed(self) -> None:
        runtime = PiriRuntime(
            client_factory=self.factory,
            process_environment={"BASE": "one"},
            memory_materializer_path="/materializer",
        )
        with self.assertRaisesRegex(ValueError, "route validator"):
            await runtime.start_or_resume(
                SessionRequest(
                    working_directory="/workspace",
                    memory_environment={"UNTRUSTED": "path"},
                )
            )
        self.assertEqual(self.factory.clients, [])

    async def test_resume_uses_and_verifies_exact_session_id(self) -> None:
        session = await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace", session_id="piri-existing")
        )

        self.assertEqual(session.session_id, "piri-existing")
        self.assertIn("--session-id", self.factory.clients[0].config.command)
        self.assertIn("piri-existing", self.factory.clients[0].config.command)

    async def test_resume_rejects_a_different_returned_session(self) -> None:
        class WrongSessionFactory(FakePiriFactory):
            def __call__(self, config: PiriLaunchConfig) -> FakePiriClient:
                client = FakePiriClient(config, session_id="wrong-session")
                self.clients.append(client)
                return client

        factory = WrongSessionFactory()
        runtime = PiriRuntime(client_factory=factory, process_environment={"A": "b"})
        with self.assertRaisesRegex(RuntimeError, "different session"):
            await runtime.start_or_resume(
                SessionRequest(working_directory="/workspace", session_id="expected")
            )
        self.assertEqual(factory.clients[0].close_calls, 1)

    async def test_startup_transport_details_are_not_exposed(self) -> None:
        class FailingClient(FakePiriClient):
            async def get_state(self) -> Mapping[str, Any]:
                raise RuntimeError("Authorization: Bearer raw-secret")

        class FailingFactory(FakePiriFactory):
            def __call__(self, config: PiriLaunchConfig) -> FakePiriClient:
                client = FailingClient(config, session_id="unused")
                self.clients.append(client)
                return client

        factory = FailingFactory()
        runtime = PiriRuntime(client_factory=factory, process_environment={"A": "b"})

        with self.assertRaisesRegex(
            RuntimeError,
            "^Piri runtime failed to start$",
        ) as caught:
            await runtime.start_or_resume(SessionRequest(working_directory="/workspace"))

        self.assertNotIn("raw-secret", str(caught.exception))
        self.assertEqual(factory.clients[0].close_calls, 1)

    async def test_session_close_unregisters_it_from_runtime(self) -> None:
        session = await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace")
        )
        client = self.factory.clients[0]

        self.assertIn(session, self.runtime._sessions)
        await session.close()
        await session.close()

        self.assertNotIn(session, self.runtime._sessions)
        self.assertEqual(client.close_calls, 1)

    async def test_restrictive_policies_are_rejected_instead_of_ignored(self) -> None:
        requests = (
            SessionRequest(working_directory="/workspace", approval_policy="on-request"),
            SessionRequest(working_directory="/workspace", approvals_reviewer="user"),
            SessionRequest(
                working_directory="/workspace",
                sandbox_policy={"type": "workspaceWrite"},
            ),
        )
        for request in requests:
            with self.subTest(request=request), self.assertRaises(ValueError):
                await self.runtime.start_or_resume(request)
        self.assertEqual(self.factory.clients, [])

    async def test_cli_selectors_are_bounded_and_cannot_become_flags(self) -> None:
        requests = (
            SessionRequest(working_directory="/workspace", session_id="../session"),
            SessionRequest(working_directory="/workspace", model="--no-tools"),
            SessionRequest(working_directory="/workspace", effort="turbo"),
        )
        for request in requests:
            with self.subTest(request=request), self.assertRaises(ValueError):
                await self.runtime.start_or_resume(request)
        self.assertEqual(self.factory.clients, [])

    async def test_streams_text_reasoning_tools_and_terminal_pair(self) -> None:
        session = await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace")
        )
        client = self.factory.clients[0]
        events = (
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "thinking_delta", "delta": "plan"},
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "hello"},
            },
            {
                "type": "tool_execution_start",
                "toolCallId": "call-1",
                "toolName": "bash",
                "args": {"command": "pwd"},
            },
            {
                "type": "tool_execution_end",
                "toolCallId": "call-1",
                "toolName": "bash",
                "result": {"content": [{"type": "text", "text": "/workspace"}]},
                "isError": False,
            },
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "stop"},
            },
            {"type": "agent_settled"},
        )
        for event in events:
            await client.events.put(event)

        approval_called = False

        async def approval_handler(_request: ApprovalRequestEvent) -> Any:
            nonlocal approval_called
            approval_called = True
            raise AssertionError("Piri must not request command approval")

        result = await collect(session.send_turn("work", approval_handler=approval_handler))

        self.assertEqual(client.prompt_calls, ["work"])
        self.assertFalse(approval_called)
        self.assertIsInstance(result[0], ReasoningDeltaEvent)
        self.assertEqual(result[0].text, "plan")
        self.assertIsInstance(result[1], TextDeltaEvent)
        self.assertEqual(result[1].text, "hello")
        self.assertIsInstance(result[2], ToolStartedEvent)
        self.assertEqual(result[2].arguments["command"], "pwd")
        self.assertIsInstance(result[3], ToolCompletedEvent)
        self.assertTrue(result[3].success)
        self.assertIsInstance(result[4], MessageCompletedEvent)
        self.assertIsInstance(result[-2], ResultEvent)
        self.assertIsInstance(result[-1], CompletionEvent)
        self.assertFalse(any(isinstance(event, ApprovalRequestEvent) for event in result))

    async def test_failed_turn_has_one_terminal_error_and_no_result(self) -> None:
        session = await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace")
        )
        client = self.factory.clients[0]
        await client.events.put(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "provider failed",
                },
            }
        )
        await client.events.put({"type": "agent_settled"})

        result = await collect(session.send_turn("work"))

        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], ErrorEvent)
        self.assertEqual(result[0].code, "piri_turn_failed")
        self.assertEqual(result[0].message, "Piri provider turn failed")
        self.assertFalse(any(isinstance(event, ResultEvent) for event in result))

    async def test_successful_auto_retry_clears_an_earlier_provider_error(self) -> None:
        session = await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace")
        )
        client = self.factory.clients[0]
        for event in (
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "error"},
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "recovered"},
            },
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "stop"},
            },
            {"type": "agent_settled"},
        ):
            await client.events.put(event)

        result = await collect(session.send_turn("retry"))

        self.assertIsInstance(result[-2], ResultEvent)
        self.assertIsInstance(result[-1], CompletionEvent)
        self.assertFalse(any(isinstance(event, ErrorEvent) for event in result))

    async def test_interrupt_aborts_only_an_active_turn(self) -> None:
        session = await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace")
        )
        client = self.factory.clients[0]
        await session.interrupt()
        self.assertEqual(client.abort_calls, 0)

        turn = asyncio.create_task(collect(session.send_turn("long task")))
        await client.prompted.wait()
        await session.interrupt()
        result = await turn

        self.assertEqual(client.abort_calls, 1)
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], ErrorEvent)
        self.assertEqual(result[0].code, "interrupted")

    async def test_early_stream_close_aborts_and_drains_before_reuse(self) -> None:
        session = await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace")
        )
        client = self.factory.clients[0]
        stream = session.send_turn("first")
        await client.events.put(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "partial"},
            }
        )
        first_event = await anext(stream)
        self.assertIsInstance(first_event, TextDeltaEvent)
        await stream.aclose()
        self.assertEqual(client.abort_calls, 1)

        await client.events.put({"type": "agent_settled"})
        result = await collect(session.send_turn("second"))

        self.assertEqual(client.prompt_calls, ["first", "second"])
        self.assertIsInstance(result[-2], ResultEvent)
        self.assertIsInstance(result[-1], CompletionEvent)

    async def test_turns_are_serialized_per_session(self) -> None:
        session = await self.runtime.start_or_resume(
            SessionRequest(working_directory="/workspace")
        )
        client = self.factory.clients[0]
        first = asyncio.create_task(collect(session.send_turn("first")))
        await client.prompted.wait()
        client.prompted.clear()
        second = asyncio.create_task(collect(session.send_turn("second")))
        await asyncio.sleep(0)
        self.assertEqual(client.prompt_calls, ["first"])

        await client.events.put({"type": "agent_settled"})
        await first
        await client.prompted.wait()
        self.assertEqual(client.prompt_calls, ["first", "second"])
        await client.events.put({"type": "agent_settled"})
        await second

    async def test_model_discovery_uses_provider_qualified_ids(self) -> None:
        def factory(config: PiriLaunchConfig) -> FakePiriClient:
            client = FakePiriClient(config, session_id="catalog")
            client.state = {
                "sessionId": "catalog",
                "model": {"provider": "openai-codex", "id": "gpt-5.5"},
            }
            client.models = (
                {
                    "provider": "openai-codex",
                    "id": "gpt-5.5",
                    "name": "GPT 5.5",
                    "reasoning": True,
                    "thinkingLevelMap": {"minimal": None, "max": "xhigh"},
                },
                {
                    "provider": "kimi-coding",
                    "id": "k3",
                    "name": "Kimi K3",
                    "reasoning": False,
                },
            )
            self.catalog_client = client
            return client

        runtime = PiriRuntime(client_factory=factory, process_environment={"A": "b"})
        models = await runtime.list_models()

        self.assertEqual([model.id for model in models], ["openai-codex/gpt-5.5", "kimi-coding/k3"])
        self.assertTrue(models[0].is_default)
        self.assertNotIn("minimal", models[0].supported_reasoning_efforts)
        self.assertEqual(models[1].supported_reasoning_efforts, ())
        self.assertEqual(self.catalog_client.close_calls, 1)


class PiriRpcProcessClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_confirms_extension_yes_no_dialog(self) -> None:
        child = """
import json
import sys
from pathlib import Path
Path("piri-child-state").write_text("private")
command = json.loads(sys.stdin.readline())
request = {
    "type": "extension_ui_request",
    "id": "confirm-1",
    "method": "confirm",
    "title": "Run",
    "message": "Proceed?",
}
print(json.dumps(request), flush=True)
answer = json.loads(sys.stdin.readline())
if answer != {"type":"extension_ui_response","id":"confirm-1","confirmed":True}:
    raise SystemExit(2)
response = {
    "id": command["id"],
    "type": "response",
    "command": "get_state",
    "success": True,
    "data": {"sessionId": "rpc-session"},
}
print(json.dumps(response), flush=True)
"""
        with tempfile.TemporaryDirectory() as directory:
            client = PiriRpcProcessClient(
                (sys.executable, "-u", "-c", child),
                working_directory=str(Path(directory)),
                auto_confirm=True,
            )
            try:
                await client.start()
                state = await client.get_state()
                self.assertEqual(state["sessionId"], "rpc-session")
                if os.name == "posix":
                    self.assertEqual(
                        stat.S_IMODE(
                            (Path(directory) / "piri-child-state").stat().st_mode
                        ),
                        0o600,
                    )
            finally:
                await client.close()


if __name__ == "__main__":
    unittest.main()
