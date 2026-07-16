"""Wiring tests: the usage meter observes real bridge spend sites (#388)."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from telegram_bot.core.codex_app_server import CodexNotification
from telegram_bot.core.codex_runtime import CodexRuntime
from telegram_bot.core.project_chat import ProjectChatHandler
from telegram_bot.core.usage import UsageSnapshot


def _settings(project_root: Path, provider: str = "claude") -> SimpleNamespace:
    return SimpleNamespace(
        agent_provider=provider,
        project_root=project_root,
        execution_profile="strict-project",
        bash_policy="disabled",
        allowed_user_ids=[7],
        require_allowlist=True,
        claude_cli_path=None,
        enable_streaming=False,
        enable_partial_streaming=False,
        usage_meter_enabled=True,
        usage_budget_tokens_claude=0,
        usage_budget_tokens_codex=0,
        usage_budget_warn_percent=80,
    )


class _RecorderRuntime:
    """Minimal agent runtime double exposing the usage-recorder seam."""

    def __init__(self) -> None:
        self.recorder = None

    def set_usage_recorder(self, recorder) -> None:
        self.recorder = recorder


def _meter_state(project_root: Path) -> dict:
    path = project_root / ".telegram_bot" / "usage-meter.json"
    return json.loads(path.read_text(encoding="utf-8"))


class ProjectChatMeterWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    async def test_claude_result_messages_meter_interactive_tokens(self) -> None:
        handler = ProjectChatHandler(settings=_settings(self.root))
        self.assertIsNotNone(handler.usage_meter)
        request = SimpleNamespace(user_id=7, chat_id=9)
        message = SimpleNamespace(
            session_id="session-1",
            usage={"input_tokens": 120, "output_tokens": 30},
            model_usage={},
            total_cost_usd=None,
        )
        handler._record_claude_usage(request, message)

        day_buckets = next(iter(_meter_state(self.root)["days"].values()))
        self.assertEqual(
            day_buckets["claude"]["interactive"],
            {"input_tokens": 120, "output_tokens": 30, "requests": 1},
        )

    async def test_metering_failure_never_breaks_the_result_path(self) -> None:
        handler = ProjectChatHandler(settings=_settings(self.root))
        meter = handler.usage_meter
        self.assertIsNotNone(meter)

        def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("meter offline")

        meter.record = explode  # type: ignore[method-assign]
        request = SimpleNamespace(user_id=7, chat_id=9)
        message = SimpleNamespace(
            session_id="session-1",
            usage={"input_tokens": 1},
            model_usage={},
            total_cost_usd=None,
        )
        with self.assertLogs("telegram_bot.core.project_chat", level="ERROR"):
            handler._record_claude_usage(request, message)

    async def test_meter_can_be_disabled(self) -> None:
        settings = _settings(self.root)
        settings.usage_meter_enabled = False
        handler = ProjectChatHandler(settings=settings)
        self.assertIsNone(handler.usage_meter)
        handler.record_agent_turn_request()
        self.assertFalse((self.root / ".telegram_bot" / "usage-meter.json").exists())

    async def test_agent_runtime_recorder_is_wired_and_meters_codex_deltas(self) -> None:
        runtime = _RecorderRuntime()
        handler = ProjectChatHandler(
            settings=_settings(self.root, provider="codex"), agent_runtime=runtime
        )
        meter = handler.usage_meter
        assert meter is not None
        self.assertEqual(runtime.recorder, meter.record_codex_thread_usage)
        assert runtime.recorder is not None

        runtime.recorder(
            "thread-1",
            None,
            UsageSnapshot(provider="codex", input_tokens=500, output_tokens=100),
        )
        runtime.recorder(
            "thread-1",
            UsageSnapshot(provider="codex", input_tokens=500, output_tokens=100),
            UsageSnapshot(provider="codex", input_tokens=800, output_tokens=150),
        )
        handler.record_agent_turn_request()

        day_buckets = next(iter(_meter_state(self.root)["days"].values()))
        self.assertEqual(
            day_buckets["codex"]["interactive"],
            {"input_tokens": 300, "output_tokens": 50, "requests": 1},
        )


class CodexRuntimeRecorderTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _runtime() -> CodexRuntime:
        return CodexRuntime(client_factory=lambda handler: SimpleNamespace())

    @staticmethod
    def _usage_notification(total_input: int, total_output: int) -> CodexNotification:
        return CodexNotification(
            "thread/tokenUsage/updated",
            {
                "threadId": "thread-1",
                "tokenUsage": {
                    "last": {"totalTokens": 10},
                    "total": {
                        "inputTokens": total_input,
                        "outputTokens": total_output,
                        "totalTokens": total_input + total_output,
                    },
                },
            },
        )

    async def test_recorder_sees_previous_and_current_snapshots(self) -> None:
        runtime = self._runtime()
        observed: list[tuple[str, int | None, int]] = []

        def recorder(
            thread_id: str,
            previous: UsageSnapshot | None,
            current: UsageSnapshot,
        ) -> None:
            observed.append(
                (
                    thread_id,
                    previous.input_tokens if previous is not None else None,
                    current.input_tokens or 0,
                )
            )

        runtime.set_usage_recorder(recorder)
        runtime._route_notification(self._usage_notification(500, 100))
        runtime._route_notification(self._usage_notification(800, 150))
        self.assertEqual(observed, [("thread-1", None, 500), ("thread-1", 500, 800)])

    async def test_recorder_failure_never_breaks_notification_dispatch(self) -> None:
        runtime = self._runtime()

        def broken(*_args: object) -> None:
            raise RuntimeError("meter offline")

        runtime.set_usage_recorder(broken)
        with self.assertLogs("telegram_bot.core.codex_runtime", level="ERROR"):
            runtime._route_notification(self._usage_notification(500, 100))
        self.assertEqual(runtime._thread_usage["thread-1"].input_tokens, 500)

    async def test_without_a_recorder_dispatch_is_unchanged(self) -> None:
        runtime = self._runtime()
        runtime._route_notification(self._usage_notification(500, 100))
        self.assertEqual(runtime._thread_usage["thread-1"].output_tokens, 100)


if __name__ == "__main__":
    unittest.main()
