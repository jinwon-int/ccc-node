"""Wiring tests: the usage meter observes real bridge spend sites (#388)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from telegram_bot.core.agent_runtime import (
    CompletionEvent,
    ErrorEvent,
    ResultEvent,
    SessionRequest,
    TextDeltaEvent,
)
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
        push_enabled=False,
        push_spool_dir=str(project_root / "spool"),
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
        handler.record_claude_attempt(request)
        handler._record_claude_usage(request, message)

        day_buckets = next(iter(_meter_state(self.root)["days"].values()))
        self.assertEqual(
            day_buckets["claude"]["interactive"],
            {"input_tokens": 120, "output_tokens": 30, "requests": 1},
        )

    async def test_claude_cache_tokens_count_toward_the_metered_input(self) -> None:
        handler = ProjectChatHandler(settings=_settings(self.root))
        self.assertIsNotNone(handler.usage_meter)
        request = SimpleNamespace(user_id=7, chat_id=9)
        message = SimpleNamespace(
            session_id="session-1",
            usage={
                "input_tokens": 10,
                "cache_creation_input_tokens": 2000,
                "cache_read_input_tokens": 3000,
                "output_tokens": 5,
            },
            model_usage={},
            total_cost_usd=None,
        )
        handler.record_claude_attempt(request)
        handler._record_claude_usage(request, message)

        day_buckets = next(iter(_meter_state(self.root)["days"].values()))
        # The complete validated input total (raw + cache creation + cache
        # read) is metered, not just the 10 raw input tokens.
        self.assertEqual(
            day_buckets["claude"]["interactive"],
            {"input_tokens": 5010, "output_tokens": 5, "requests": 1},
        )
        meter = handler.usage_meter
        assert meter is not None
        self.assertEqual(meter.used_tokens("claude"), 5015)

    async def test_claude_attempt_is_metered_once_even_without_a_result(self) -> None:
        # Reviewer probe: an accepted query that emits output and then loses
        # its reader must still count one request; repeated events and a
        # normal ResultMessage completion never double-charge it.
        handler = ProjectChatHandler(settings=_settings(self.root))
        request = SimpleNamespace(user_id=7, chat_id=9)
        handler.record_claude_attempt(request)
        handler.record_claude_attempt(request)

        day_buckets = next(iter(_meter_state(self.root)["days"].values()))
        self.assertEqual(day_buckets["claude"]["interactive"]["requests"], 1)

        message = SimpleNamespace(
            session_id="session-1",
            usage={"input_tokens": 100, "output_tokens": 20},
            model_usage={},
            total_cost_usd=None,
        )
        handler._record_claude_usage(request, message)
        day_buckets = next(iter(_meter_state(self.root)["days"].values()))
        self.assertEqual(
            day_buckets["claude"]["interactive"],
            {"input_tokens": 100, "output_tokens": 20, "requests": 1},
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


class _ScriptedTurnSession:
    def __init__(self, runtime: "_ScriptedTurnRuntime", session_id: str) -> None:
        self._runtime = runtime
        self.session_id = session_id

    def send_turn(self, message: str, *, approval_handler=None):
        events = self._runtime.scripts.pop(0)
        if self._runtime.turn_attempt_recorder is not None:
            # Mirror the real runtime contract: the spend boundary fires when
            # the provider accepts turn/start, before any event is consumed.
            self._runtime.turn_attempt_recorder()

        async def stream():
            for event in events:
                yield event

        return stream()

    async def interrupt(self) -> None:
        return None


class _ScriptedTurnRuntime:
    """Agent runtime double whose session replays scripted event turns."""

    def __init__(self, scripts: list) -> None:
        self.scripts = list(scripts)
        self.recorder = None
        self.turn_attempt_recorder = None

    def set_usage_recorder(self, recorder) -> None:
        self.recorder = recorder

    def set_turn_attempt_recorder(self, recorder) -> None:
        self.turn_attempt_recorder = recorder

    async def start_or_resume(self, request: SessionRequest) -> _ScriptedTurnSession:
        return _ScriptedTurnSession(self, request.session_id or "thread-scripted")


class _RecordingJournal:
    """Minimal journal double: enough surface for the deferred-job path."""

    def __init__(self) -> None:
        self.claim_calls = 0
        self.sentinel = object()

    def get(self, job_id: str) -> object:
        return self.sentinel

    def claim_extraction(self, *args: object, **kwargs: object) -> None:
        self.claim_calls += 1
        return None


class _CountingBackend:
    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, extraction_input: object) -> object:
        self.calls += 1
        return object()


class ProductionCompositionTests(unittest.IsolatedAsyncioTestCase):
    """The composition root wires the shared gate and the owner alert sink."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    async def test_budget_alerts_reach_the_owner_push_spool_when_enabled(self) -> None:
        settings = _settings(self.root)
        settings.push_enabled = True
        settings.usage_budget_tokens_codex = 1000
        handler = ProjectChatHandler(settings=settings)
        meter = handler.usage_meter
        assert meter is not None

        meter.record("codex", "interactive", input_tokens=1200)

        spooled = sorted((self.root / "spool").glob("*.json"))
        self.assertEqual(len(spooled), 2, "warn and enforce alerts must both spool")
        events = []
        for path in spooled:
            payload = json.loads(path.read_text(encoding="utf-8"))
            events.append(payload["event"])
            self.assertEqual(payload["event"], "usage-budget")
            self.assertRegex(
                payload["text"],
                r"^(⚠️ warn|🛑 enforce): codex used \d+ of \d+ daily budget tokens",
            )
            self.assertTrue(payload["dedup"].startswith("usage-budget:"))
        self.assertEqual(events, ["usage-budget", "usage-budget"])

    async def test_budget_alerts_stay_log_only_when_push_is_disabled(self) -> None:
        settings = _settings(self.root)
        settings.usage_budget_tokens_codex = 1000
        handler = ProjectChatHandler(settings=settings)
        meter = handler.usage_meter
        assert meter is not None

        meter.record("codex", "interactive", input_tokens=1200)

        self.assertFalse((self.root / "spool").exists())
        self.assertEqual(meter.used_tokens("codex"), 1200)

    async def test_built_distill_worker_is_gated_by_the_shared_meter(self) -> None:
        settings = _settings(self.root, provider="codex")
        settings.usage_budget_tokens_codex = 1000
        handler = ProjectChatHandler(
            settings=settings, agent_runtime=_RecorderRuntime()
        )
        meter = handler.usage_meter
        assert meter is not None
        meter.record("codex", "interactive", input_tokens=1000)

        journal = _RecordingJournal()
        backend = _CountingBackend()
        worker = handler.build_distill_extraction_worker(
            journal, backend, owner_token="composition-test"
        )
        result = await worker.extract_once(job_id="job-1")

        # The shared meter is over budget, so the production-built worker
        # defers the job without claiming it or calling the provider.
        self.assertIs(result, journal.sentinel)
        self.assertEqual(journal.claim_calls, 0)
        self.assertEqual(backend.calls, 0)

    async def test_failed_and_successful_attempts_each_meter_one_request(self) -> None:
        # Reviewer probe: an attempt that reaches the provider must charge
        # exactly one request even when the turn ends in an error terminal.
        runtime = _ScriptedTurnRuntime(
            [
                [
                    TextDeltaEvent("partial answer"),
                    ErrorEvent(code="codex_turn_failed", message="scripted failure"),
                ],
                [
                    TextDeltaEvent("final answer"),
                    ResultEvent(result={"status": "completed"}),
                    CompletionEvent(stop_reason="end_turn"),
                ],
            ]
        )
        handler = ProjectChatHandler(
            settings=_settings(self.root, provider="codex"), agent_runtime=runtime
        )
        handler._task_ledger_cache = False

        await handler.process_message("first", 7, 9)
        day_buckets = next(iter(_meter_state(self.root)["days"].values()))
        self.assertEqual(day_buckets["codex"]["interactive"]["requests"], 1)

        await handler.process_message("second", 7, 9)
        day_buckets = next(iter(_meter_state(self.root)["days"].values()))
        self.assertEqual(day_buckets["codex"]["interactive"]["requests"], 2)

    async def test_built_distill_worker_rejects_external_meters(self) -> None:
        handler = ProjectChatHandler(
            settings=_settings(self.root, provider="codex"),
            agent_runtime=_RecorderRuntime(),
        )
        with self.assertRaises(ValueError):
            handler.build_distill_extraction_worker(
                _RecordingJournal(), _CountingBackend(), usage_meter=None
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

    async def test_created_threads_meter_their_first_turn_and_resumed_do_not(
        self,
    ) -> None:
        class _UsageClientStub:
            async def start(self) -> dict:
                return {}

            async def thread_start(self, *, cwd: str, model: str | None = None) -> dict:
                return {"thread": {"id": "thread-created"}}

            async def thread_resume(
                self,
                thread_id: str,
                *,
                cwd: str | None = None,
                model: str | None = None,
            ) -> dict:
                return {"thread": {"id": thread_id}}

            async def next_notification(self) -> CodexNotification:
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def close(self) -> None:
                return None

        runtime = CodexRuntime(client_factory=lambda handler: _UsageClientStub())
        observed: list[tuple[str, tuple[int, int] | None, tuple[int, int]]] = []

        def recorder(
            thread_id: str,
            previous: UsageSnapshot | None,
            current: UsageSnapshot,
        ) -> None:
            observed.append(
                (
                    thread_id,
                    None
                    if previous is None
                    else (previous.input_tokens or 0, previous.output_tokens or 0),
                    (current.input_tokens or 0, current.output_tokens or 0),
                )
            )

        runtime.set_usage_recorder(recorder)
        try:
            created = await runtime.start_or_resume(
                SessionRequest(working_directory="/workspace")
            )
            self.assertEqual(created.session_id, "thread-created")
            runtime._route_notification(
                self._notification_for("thread-created", 500, 100)
            )
            resumed = await runtime.start_or_resume(
                SessionRequest(
                    working_directory="/workspace", session_id="thread-resumed"
                )
            )
            self.assertEqual(resumed.session_id, "thread-resumed")
            runtime._route_notification(
                self._notification_for("thread-resumed", 7000, 900)
            )
        finally:
            await runtime.close()

        # A created thread's first observation is real new spend (zero
        # baseline); a resumed thread's first observation is history and
        # only establishes the baseline.
        self.assertEqual(
            observed,
            [
                ("thread-created", (0, 0), (500, 100)),
                ("thread-resumed", None, (7000, 900)),
            ],
        )

    @staticmethod
    def _notification_for(
        thread_id: str,
        total_input: int,
        total_output: int,
        *,
        turn_id: str | None = None,
        last: dict | None = None,
    ) -> CodexNotification:
        params: dict = {
            "threadId": thread_id,
            "tokenUsage": {
                "total": {
                    "inputTokens": total_input,
                    "outputTokens": total_output,
                    "totalTokens": total_input + total_output,
                }
            },
        }
        if turn_id is not None:
            params["turnId"] = turn_id
        if last is not None:
            params["tokenUsage"]["last"] = last
        return CodexNotification("thread/tokenUsage/updated", params)

    async def test_resumed_threads_first_paid_turn_is_metered_via_last(self) -> None:
        # Reviewer probe: a resumed thread's first notification carries
        # history AND the new turn. When it belongs to a turn this process
        # started, the turn-scoped `last` block sizes the new spend and the
        # implied pre-turn baseline (total - last) excludes the history.
        runtime = self._runtime()
        observed: list[tuple[int, int] | None] = []
        runtime.set_usage_recorder(
            lambda _tid, prev, _cur: observed.append(
                None
                if prev is None
                else (prev.input_tokens or 0, prev.output_tokens or 0)
            )
        )
        runtime._started_turn_ids["turn-ours"] = None
        runtime._route_notification(
            self._notification_for(
                "thread-resumed",
                7000,
                900,
                turn_id="turn-ours",
                last={"inputTokens": 400, "outputTokens": 100},
            )
        )
        self.assertEqual(observed, [(6600, 800)])

    async def test_resumed_threads_first_turn_falls_back_to_last_total(self) -> None:
        runtime = self._runtime()
        observed: list[tuple[int, int] | None] = []
        runtime.set_usage_recorder(
            lambda _tid, prev, _cur: observed.append(
                None
                if prev is None
                else (prev.input_tokens or 0, prev.output_tokens or 0)
            )
        )
        runtime._started_turn_ids["turn-ours"] = None
        runtime._route_notification(
            self._notification_for(
                "thread-resumed",
                7000,
                900,
                turn_id="turn-ours",
                last={"totalTokens": 500},
            )
        )
        # Only the turn total is exposed: it is attributed to input so the
        # turn is still metered (6500 implied input baseline, output intact).
        self.assertEqual(observed, [(6500, 900)])

    async def test_turn_cancelled_before_first_event_still_counts_one_request(
        self,
    ) -> None:
        # Reviewer probe: the spend boundary is the accepted turn/start, so a
        # turn cancelled while waiting for its first event is still charged.
        class _AcceptingClientStub:
            async def start(self) -> dict:
                return {}

            async def thread_start(self, *, cwd: str, model: str | None = None) -> dict:
                return {"thread": {"id": "thread-cancel"}}

            async def turn_start(self, thread_id: str, input_items, **kwargs) -> dict:
                return {"turn": {"id": "turn-1"}}

            async def next_notification(self) -> CodexNotification:
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def close(self) -> None:
                return None

        runtime = CodexRuntime(client_factory=lambda handler: _AcceptingClientStub())
        attempts: list[int] = []
        runtime.set_turn_attempt_recorder(lambda: attempts.append(1))
        try:
            session = await runtime.start_or_resume(
                SessionRequest(working_directory="/workspace")
            )

            async def drain() -> None:
                async for _event in session.send_turn("hello"):
                    pass

            collector = asyncio.ensure_future(drain())
            while not attempts:
                await asyncio.sleep(0.005)
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
        finally:
            await runtime.close()
        self.assertEqual(attempts, [1])

    async def test_output_heavy_resumed_turn_keeps_its_full_total(self) -> None:
        # Reviewer probe: cumulative 100/500 with last.totalTokens=600 must
        # imply a (0, 0) baseline so the whole 600-token turn is metered,
        # not just the 100 input tokens.
        runtime = self._runtime()
        observed: list[tuple[int, int] | None] = []
        runtime.set_usage_recorder(
            lambda _tid, prev, _cur: observed.append(
                None
                if prev is None
                else (prev.input_tokens or 0, prev.output_tokens or 0)
            )
        )
        runtime._started_turn_ids["turn-ours"] = None
        runtime._route_notification(
            self._notification_for(
                "thread-resumed",
                100,
                500,
                turn_id="turn-ours",
                last={"totalTokens": 600},
            )
        )
        self.assertEqual(observed, [(0, 0)])

    async def test_history_notifications_without_our_turn_still_baseline(self) -> None:
        runtime = self._runtime()
        observed: list[object] = []
        runtime.set_usage_recorder(
            lambda _tid, prev, _cur: observed.append(prev)
        )
        runtime._route_notification(
            self._notification_for(
                "thread-resumed",
                7000,
                900,
                turn_id="turn-history",
                last={"inputTokens": 400, "outputTokens": 100},
            )
        )
        self.assertEqual(observed, [None])


if __name__ == "__main__":
    unittest.main()


class DistillSchedulerLoopTests(unittest.IsolatedAsyncioTestCase):
    """The bridge lifecycle drives the retained, budget-gated worker (#388)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    async def _run_one_case(self, *, budget_tokens: int) -> tuple:
        import test_distill_worker as distill_fixtures

        from telegram_bot.core.bot import TelegramBot
        from telegram_bot.memory.distill_journal import DistillJournal

        journal = DistillJournal(self.root / "journal")
        journal.initialize()
        job = distill_fixtures.snapshot_done_job(journal)
        settings = _settings(self.root, provider="codex")
        settings.usage_budget_tokens_codex = budget_tokens
        settings.distill_extraction_poll_interval = 0.02
        settings.bot_data_dir = self.root / "bot-data"
        settings.ffmpeg_path = None
        handler = ProjectChatHandler(
            settings=settings, agent_runtime=_RecorderRuntime()
        )
        meter = handler.usage_meter
        assert meter is not None
        backend = distill_fixtures.SuccessfulBackend()
        worker = handler.build_distill_extraction_worker(
            journal, backend, owner_token="loop-test"
        )
        bot = TelegramBot(
            settings=settings,
            session_manager=None,
            project_chat=handler,
            distill_journal=journal,
            distill_extraction_worker=worker,
        )
        stop = asyncio.Event()
        loop_task = asyncio.create_task(bot._distill_extraction_loop(stop))
        try:
            deadline = asyncio.get_running_loop().time() + 1.0
            while not backend.calls and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
        finally:
            stop.set()
            await asyncio.wait_for(loop_task, timeout=2.0)
        return journal, job, backend

    async def test_capped_work_never_reaches_the_backend(self) -> None:
        from telegram_bot.memory.distill_types import DistillJobStatus

        # The job's bounded attempt cost (2058 tokens) can never fit the
        # 1000-token cap, so the prospective gate defers it before any claim.
        journal, job, backend = await self._run_one_case(budget_tokens=1000)
        self.assertEqual(backend.calls, [])
        persisted = journal.get(job.job_id)
        self.assertIs(persisted.status, DistillJobStatus.SNAPSHOT_DONE)
        self.assertEqual(persisted.extraction_attempts, 0)

    async def test_admitted_work_is_driven_to_extraction_by_the_loop(self) -> None:
        from telegram_bot.memory.distill_types import DistillJobStatus

        journal, job, backend = await self._run_one_case(budget_tokens=200000)
        self.assertEqual(len(backend.calls), 1)
        self.assertIs(
            journal.get(job.job_id).status, DistillJobStatus.EXTRACTION_DONE
        )


class RetryableFailureLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    async def test_transiently_failed_extraction_is_driven_again(self) -> None:
        import test_distill_worker as distill_fixtures

        from telegram_bot.core.bot import TelegramBot
        from telegram_bot.memory.codex_exec_backend import CodexDistillBackendError
        from telegram_bot.memory.distill_journal import DistillJournal
        from telegram_bot.memory.distill_types import DistillJobStatus

        class FlakyBackend:
            def __init__(self) -> None:
                self.calls = 0

            async def extract(self, extraction_input):
                self.calls += 1
                if self.calls == 1:
                    raise CodexDistillBackendError("codex_distill_timeout")
                return distill_fixtures.output_for(extraction_input)

        journal = DistillJournal(self.root / "journal")
        journal.initialize()
        job = distill_fixtures.snapshot_done_job(journal)
        settings = _settings(self.root, provider="codex")
        settings.usage_budget_tokens_codex = 200000
        settings.distill_extraction_poll_interval = 0.02
        settings.bot_data_dir = self.root / "bot-data"
        settings.ffmpeg_path = None
        handler = ProjectChatHandler(
            settings=settings, agent_runtime=_RecorderRuntime()
        )
        backend = FlakyBackend()
        worker = handler.build_distill_extraction_worker(
            journal, backend, owner_token="retry-loop-test"
        )
        bot = TelegramBot(
            settings=settings,
            session_manager=None,
            project_chat=handler,
            distill_journal=journal,
            distill_extraction_worker=worker,
        )
        stop = asyncio.Event()
        loop_task = asyncio.create_task(bot._distill_extraction_loop(stop))
        try:
            deadline = asyncio.get_running_loop().time() + 2.0
            while (
                journal.get(job.job_id).status
                is not DistillJobStatus.EXTRACTION_DONE
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.02)
        finally:
            stop.set()
            await asyncio.wait_for(loop_task, timeout=2.0)
        # One failed attempt, one successful retry driven by a later sweep.
        self.assertEqual(backend.calls, 2)
        self.assertIs(
            journal.get(job.job_id).status, DistillJobStatus.EXTRACTION_DONE
        )


class ClaudeTerminalLossTokenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    async def test_tokens_survive_a_lost_terminal_and_reconcile_without_doubling(
        self,
    ) -> None:
        # Reviewer probe: an accepted query yields an AssistantMessage with
        # 100/20 usage, then the reader dies — the tokens must already be
        # persisted. A later ResultMessage reconciles only the remainder.
        handler = ProjectChatHandler(settings=_settings(self.root))
        request = SimpleNamespace(user_id=7, chat_id=9)
        handler.record_claude_attempt(request)
        assistant = SimpleNamespace(
            usage={"input_tokens": 100, "output_tokens": 20},
            model_usage={},
            total_cost_usd=None,
        )
        handler.record_claude_observed_usage(request, assistant)

        day_buckets = next(iter(_meter_state(self.root)["days"].values()))
        self.assertEqual(
            day_buckets["claude"]["interactive"],
            {"input_tokens": 100, "output_tokens": 20, "requests": 1},
        )

        # Same-usage terminal: no double charge.
        result = SimpleNamespace(
            session_id="session-1",
            usage={"input_tokens": 100, "output_tokens": 20},
            model_usage={},
            total_cost_usd=None,
        )
        handler._record_claude_usage(request, result)
        day_buckets = next(iter(_meter_state(self.root)["days"].values()))
        self.assertEqual(
            day_buckets["claude"]["interactive"],
            {"input_tokens": 100, "output_tokens": 20, "requests": 1},
        )

        # A larger terminal reconciles only the remainder.
        bigger = SimpleNamespace(
            session_id="session-1",
            usage={"input_tokens": 120, "output_tokens": 25},
            model_usage={},
            total_cost_usd=None,
        )
        handler._record_claude_usage(request, bigger)
        day_buckets = next(iter(_meter_state(self.root)["days"].values()))
        self.assertEqual(
            day_buckets["claude"]["interactive"],
            {"input_tokens": 120, "output_tokens": 25, "requests": 1},
        )
