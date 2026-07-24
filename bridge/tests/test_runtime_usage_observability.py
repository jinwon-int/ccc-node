"""End-to-end /usage observability on the ClaudeRuntime adapter path.

#584 C-1 follow-up: with the ClaudeRuntime adapter default-on, the legacy
reader loop that fed the /usage recorders never runs, so ``/usage`` rendered
Plan/Rate limits/Context/Session tokens/Session cost as "unavailable" while
turns kept working.  This module drives the real ``ClaudeRuntime`` plus the
real ``ProjectChatHandler`` adapter path over scripted SDK frames carrying a
usage block, a cost, and a rate-limit window, then asserts ``get_usage`` —
the exact accessor the /usage command reads — reports them.

Note on the module name: like ``test_runtime_conformance`` and
``test_runtime_unsolicited`` this module drives real ``claude_agent_sdk``
frame types, so it must collect AFTER the project_chat modules that inject
spec-less SDK stubs.  It additionally re-imports the project_chat module
chain so the isinstance routing in ``_register_agent_frame_observer`` binds
the same import generation as the frames the scripted client emits.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import unittest
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace


def _purge_injected_sdk_stubs() -> None:
    """Drop spec-less ``claude_agent_sdk`` stubs left by earlier test modules."""

    for name in [
        module_name
        for module_name in sys.modules
        if module_name == "claude_agent_sdk" or module_name.startswith("claude_agent_sdk.")
    ]:
        if getattr(sys.modules[name], "__spec__", None) is None:
            del sys.modules[name]


_purge_injected_sdk_stubs()

# Re-import the project_chat chain (and the Claude runtime adapter) against
# the real SDK so every module-level frame-class binding in this test shares
# one import generation — the stub-injecting modules leave stub-bound
# generations behind in sys.modules.
for _name in [
    "telegram_bot.core.claude_runtime",
    "telegram_bot.core.project_chat",
    "telegram_bot.core.project_chat_history",
    "telegram_bot.core.project_chat_process",
    "telegram_bot.core.project_chat_state",
]:
    sys.modules.pop(_name, None)

from claude_agent_sdk import (  # noqa: E402 -- must follow the stub purge above
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from telegram_bot.core.usage import render_usage  # noqa: E402

_claude_runtime = importlib.import_module("telegram_bot.core.claude_runtime")
_project_chat = importlib.import_module("telegram_bot.core.project_chat")
ClaudeRuntime = _claude_runtime.ClaudeRuntime
ProjectChatHandler = _project_chat.ProjectChatHandler

TURN_USAGE = {
    "input_tokens": 1_000,
    "cache_creation_input_tokens": 200,
    "cache_read_input_tokens": 300,
    "output_tokens": 50,
}
TURN_MODEL_USAGE = {
    "claude-fable-5": {
        "inputTokens": 900,
        "cacheCreationInputTokens": 200,
        "cacheReadInputTokens": 300,
        "outputTokens": 40,
        "costUSD": 0.2,
    },
    "claude-haiku-4-5": {
        "inputTokens": 100,
        "outputTokens": 10,
        "costUSD": 0.05,
    },
}
TURN_COST_USD = 0.25


class ScriptedUsageSdkClient:
    """Fake SDK client: each turn answers with usage, rate-limit, and cost."""

    # Optional CLI-internal per-window map passed through RateLimitInfo.raw
    # (window name -> {utilization, resets_at}); empty by default.
    rate_limit_raw: dict = {}

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.options = options
        self.session_id = options.resume or options.session_id or "claude-usage-e2e"
        self._initialized = False
        self._messages: asyncio.Queue[Message | None] = asyncio.Queue()

    def _emit(self, message: Message) -> None:
        self._messages.put_nowait(message)

    # -- SdkClient protocol ------------------------------------------------

    async def connect(self) -> None:
        pass

    async def query(self, prompt: str) -> None:
        if not self._initialized:
            self._initialized = True
            self._emit(
                SystemMessage(subtype="init", data={"session_id": self.session_id})
            )
        self._emit(
            AssistantMessage(
                content=[TextBlock(text="the answer")],
                model="claude-test-model",
                session_id=self.session_id,
            )
        )
        self._emit(
            RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="allowed_warning",
                    resets_at=1_900_000_000,
                    rate_limit_type="five_hour",
                    utilization=0.5,
                    overage_status="allowed",
                    overage_resets_at=1_900_100_000,
                    raw={key: dict(value) for key, value in self.rate_limit_raw.items()},
                ),
                uuid=str(uuid.uuid4()),
                session_id=self.session_id,
            )
        )
        self._emit(
            ResultMessage(
                subtype="success",
                duration_ms=5,
                duration_api_ms=3,
                is_error=False,
                num_turns=1,
                session_id=self.session_id,
                total_cost_usd=TURN_COST_USD,
                usage=dict(TURN_USAGE),
                model_usage={model: dict(raw) for model, raw in TURN_MODEL_USAGE.items()},
                result="the answer",
            )
        )

    async def receive_messages(self) -> AsyncIterator[Message]:
        while True:
            message = await self._messages.get()
            if message is None:
                return
            yield message

    async def interrupt(self) -> None:  # pragma: no cover - not exercised
        pass

    async def disconnect(self) -> None:
        self._messages.put_nowait(None)


def _settings(project_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        agent_provider="claude",
        project_root=project_root,
        execution_profile="strict-project",
        bash_policy="disabled",
        allowed_user_ids=[7],
        require_allowlist=True,
        claude_cli_path=None,
        enable_streaming=False,
        enable_partial_streaming=False,
        bot_data_dir=None,
        task_ledger_path=None,
        usage_meter_enabled=False,
        claude_settings_path=project_root / "claude" / "settings.json",
    )


class AdapterUsageObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_turn_feeds_the_usage_command_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ClaudeRuntime(sdk_client_factory=ScriptedUsageSdkClient)
            self.addAsyncCleanup(runtime.close)
            handler = ProjectChatHandler(
                settings=_settings(Path(tmp)), agent_runtime=runtime
            )
            handler._task_ledger_cache = False

            response = await handler.process_message(
                "how much did that cost?", user_id=7, chat_id=70
            )
            self.assertTrue(response.success)
            assert response.session_id is not None
            self.assertEqual(uuid.UUID(response.session_id).version, 4)

            # The exact accessor the /usage command reads (bot_commands).
            snapshot = await handler.get_usage(7, 70, response.session_id)

            self.assertEqual(snapshot.input_tokens, 1_000)
            self.assertEqual(snapshot.output_tokens, 50)
            # raw + cache creation + cache read input tokens
            self.assertEqual(snapshot.context_used, 1_500)
            self.assertEqual(snapshot.total_tokens, 1_550)
            self.assertEqual(snapshot.total_cost_usd, TURN_COST_USD)
            self.assertEqual(
                [window.label for window in snapshot.windows], ["five hour"]
            )
            self.assertEqual(snapshot.windows[0].used_percent, 50.0)

            self.assertEqual(
                [entry.model for entry in snapshot.models],
                ["claude-fable-5", "claude-haiku-4-5"],
            )

            rendered = render_usage(snapshot)
            self.assertIn(
                "Session tokens: input 1,000 · output 50 · total 1,550", rendered
            )
            self.assertIn("Models:", rendered)
            self.assertIn("  claude-fable-5 · in 1,400 · out 40 · $0.2000", rendered)
            self.assertIn("  claude-haiku-4-5 · in 100 · out 10 · $0.0500", rendered)
            self.assertIn("Session cost: $0.2500", rendered)
            self.assertIn("five hour: 50% used", rendered)
            self.assertIn("Overage: allowed · ", rendered)
            self.assertNotIn("Rate limits: unavailable", rendered)
            self.assertNotIn("Session cost: unavailable", rendered)

            # Session-scoped like the direct path: a different conversation
            # sees no tokens while the account-global windows still surface.
            other = await handler.get_usage(7, 71, response.session_id)
            self.assertIsNone(other.input_tokens)
            self.assertEqual(
                [window.label for window in other.windows], ["five hour"]
            )

    async def test_raw_window_map_surfaces_model_class_rate_limit_line(self) -> None:
        """When the CLI passes its per-window map through RateLimitInfo.raw,
        model-class buckets (e.g. seven_day_opus) render as their own /usage
        lines even though the event's rate_limit_type only names five_hour."""

        class RawWindowMapSdkClient(ScriptedUsageSdkClient):
            rate_limit_raw = {
                "windows": {
                    "five_hour": {"utilization": 0.5, "resetsAt": 1_900_000_000},
                    "seven_day_opus": {
                        "utilization": 0.12,
                        "resets_at": 1_900_200_000,
                    },
                }
            }

        with tempfile.TemporaryDirectory() as tmp:
            runtime = ClaudeRuntime(sdk_client_factory=RawWindowMapSdkClient)
            self.addAsyncCleanup(runtime.close)
            handler = ProjectChatHandler(
                settings=_settings(Path(tmp)), agent_runtime=runtime
            )
            handler._task_ledger_cache = False

            response = await handler.process_message(
                "how are the limits?", user_id=7, chat_id=70
            )
            self.assertTrue(response.success)

            snapshot = await handler.get_usage(7, 70, response.session_id)
            self.assertEqual(
                [window.label for window in snapshot.windows],
                ["five hour", "seven day opus"],
            )
            self.assertAlmostEqual(snapshot.windows[1].used_percent, 12.0)

            rendered = render_usage(snapshot)
            self.assertIn("five hour: 50% used", rendered)
            self.assertIn("seven day opus: 12% used", rendered)


if __name__ == "__main__":
    unittest.main()
