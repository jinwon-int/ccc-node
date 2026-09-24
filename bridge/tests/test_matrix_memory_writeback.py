"""Matrix must schedule the same durable memory pipeline as Telegram."""
import asyncio
import hashlib

import pytest

from test_matrix_bot import FakeSink, FakeTransport, _bot, _job
from test_matrix_bot import matrix_config as _shared  # noqa: F401
from telegram_bot.core.memory_audience import resolve_memory_audience
from telegram_bot.memory.distill_journal import DistillJournal
from telegram_bot.memory.distill_types import DistillTrigger

matrix_config = _shared


@pytest.fixture
def anyio_backend():
    return "asyncio"


def wired_bot(tmp_path, **settings):
    bot, chat, manager = _bot(tmp_path, **settings)
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    bot._distill_journal = journal
    return bot, chat, manager, journal


@pytest.mark.anyio
async def test_new_preserves_old_matrix_thread_and_channel_audience(tmp_path, matrix_config):
    bot, chat, manager, journal = wired_bot(tmp_path, bridge_memory_mode="audience-scoped")
    await bot.run_turn(_job("remember"), sink=FakeSink(), session_id=None, room_kind="direct")
    user_id, chat_id, _ = bot._job_identity(_job("/new"), "direct")
    await bot.run_turn(_job("/new"), sink=FakeSink(), session_id=None, room_kind="direct")
    (job,) = journal.list_jobs()
    assert job.thread_id == "s-new" and job.trigger is DistillTrigger.NEW_COMMAND
    matrix = resolve_memory_audience(bot._settings, user_id=user_id, chat_id=chat_id, route="matrix")
    telegram = resolve_memory_audience(bot._settings, user_id=user_id, chat_id=chat_id, route="telegram")
    assert job.memory_scope == matrix.scope != telegram.scope
    assert job.memory_audience == "private"
    assert manager._row(bot._conversation_key(user_id, chat_id))["session_id"] is None


@pytest.mark.anyio
async def test_checkpoint_counts_identical_text_by_event_and_failed_turns_do_not_count(tmp_path, matrix_config):
    bot, chat, _, journal = wired_bot(tmp_path, memory_distill_checkpoint_turns=2)
    for event in ("$a", "$b"):
        await bot.run_turn(_job("same", event_id=event), sink=FakeSink(), session_id=None, room_kind="direct")
    assert [j.trigger for j in journal.list_jobs()] == [DistillTrigger.CHECKPOINT]
    chat.response.success = False
    for event in ("$c", "$d"):
        await bot.run_turn(_job("same", event_id=event), sink=FakeSink(), session_id=None, room_kind="direct")
    assert len(journal.list_jobs()) == 1


@pytest.mark.anyio
async def test_resets_and_shutdown_preserve_old_threads_once(tmp_path, matrix_config):
    bot, chat, manager, journal = wired_bot(tmp_path)
    await bot.run_turn(_job("first"), sink=FakeSink(), session_id=None, room_kind="direct")
    manager.auto_new = True
    chat.response.session_id = "s-next"
    await bot.run_turn(_job("second"), sink=FakeSink(), session_id=None, room_kind="direct")
    manager.auto_new = False
    bot._settings.agent_provider = "piri"
    chat.response.session_id = "piri-next"
    await bot.run_turn(_job("third"), sink=FakeSink(), session_id=None, room_kind="direct")
    await bot._enqueue_shutdown_distills()
    await bot._enqueue_shutdown_distills()
    jobs = journal.list_jobs()
    assert {(j.thread_id, j.trigger) for j in jobs} == {
        ("s-new", DistillTrigger.AUTO_NEW), ("s-next", DistillTrigger.PROVIDER_SWITCH),
        ("piri-next", DistillTrigger.SHUTDOWN),
    }


@pytest.mark.anyio
async def test_family_and_private_jobs_never_share_private_audience(tmp_path, matrix_config):
    from test_matrix_bot import FAMILY_ROOM, KID
    bot, chat, _, journal = wired_bot(
        tmp_path, bridge_memory_mode="audience-scoped", memory_distill_checkpoint_turns=1,
        execution_profile="strict-project",  # owner-operator refuses the kid (#1955)
    )
    await bot.run_turn(_job("private", event_id="$p"), sink=FakeSink(), session_id=None, room_kind="direct")
    chat.response.session_id = "family-thread"
    await bot.run_turn(_job("shared", sender=KID, room=FAMILY_ROOM, event_id="$s"), sink=FakeSink(), session_id=None, room_kind="family")
    jobs = {j.thread_id: j for j in journal.list_jobs()}
    assert jobs["s-new"].memory_scope.startswith("private-")
    assert jobs["family-thread"].memory_scope == "shared"
    assert jobs["family-thread"].memory_audience == "shared"


@pytest.mark.anyio
async def test_disabled_distill_never_enqueues(tmp_path, matrix_config, monkeypatch):
    bot, _, _, journal = wired_bot(tmp_path, memory_distill_provider="off", memory_distill_checkpoint_turns=1)
    await bot.run_turn(_job("hello"), sink=FakeSink(), session_id=None, room_kind="direct")
    await bot.run_turn(_job("/new"), sink=FakeSink(), session_id=None, room_kind="direct")
    await bot._enqueue_shutdown_distills()
    assert journal.list_jobs() == ()


@pytest.mark.anyio
async def test_serving_runs_real_snapshot_extraction_and_private_sink(tmp_path, matrix_config):
    from test_distill_worker import SuccessfulBackend
    from telegram_bot.memory.codex_snapshot import CodexThreadSnapshotter
    from telegram_bot.memory.distill_guard import DistillGuard
    from telegram_bot.memory.distill_worker import CodexDistillExtractionWorker
    from telegram_bot.memory.distill_local_worker import CodexDistillLocalSinkWorker
    from telegram_bot.memory.distill_types import CodexTranscriptSnapshot, DistillLocalSinkStatus
    from telegram_bot.core.usage_meter import UsageMeter
    from datetime import datetime, timezone

    bot, _, _, journal = wired_bot(tmp_path, bridge_memory_mode="audience-scoped", memory_distill_checkpoint_turns=1, distill_extraction_poll_interval=0.005)
    class Runtime:
        async def read_session_snapshot(self, session_id, **kwargs):
            return CodexTranscriptSnapshot(thread_hash=hashlib.sha256(session_id.encode()).hexdigest(), last_turn_id="test-turn", messages=(), byte_count=0, truncated=False, captured_at=datetime.now(timezone.utc).isoformat())
    backend = SuccessfulBackend()
    bot._distill_snapshot_worker = CodexThreadSnapshotter(journal, Runtime())
    bot._distill_extraction_worker = CodexDistillExtractionWorker(journal, backend,
        usage_meter=UsageMeter(tmp_path / "usage.json", budgets={"codex": 500000}),
        guard=DistillGuard(state_dir=tmp_path / "guard"), wiki_enabled=False)
    bot._distill_local_sink_worker = CodexDistillLocalSinkWorker(journal, audience_root=tmp_path / "audiences")
    async def script(transport):
        await bot.run_turn(_job("remember"), sink=FakeSink(), session_id=None, room_kind="direct")
        for _ in range(300):
            jobs = journal.list_jobs()
            if jobs and jobs[0].local_sink_status is DistillLocalSinkStatus.DONE:
                return
            await asyncio.sleep(.01)
        pytest.fail("Matrix did not execute the composed memory pipeline")
    bot._transport_factory = lambda config, runner: FakeTransport(config, runner, script=script)
    await asyncio.wait_for(bot.serve(), timeout=5)
    done = [j for j in journal.list_jobs() if j.local_sink_status is DistillLocalSinkStatus.DONE]
    assert len(done) == 1 and len(backend.calls) == 1
    path = tmp_path / "audiences" / done[0].memory_scope / "state" / "memory-facts.jsonl"
    assert "A harmless fact was retained." in path.read_text()
    assert not (tmp_path / "audiences/shared/state/memory-facts.jsonl").exists()


@pytest.mark.anyio
async def test_clean_transport_exit_cancels_memory_worker(tmp_path, matrix_config):
    bot, _, _, journal = wired_bot(tmp_path)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def blocked(stop):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
    bot._distill_snapshot_worker = object()
    bot._distill_snapshot_loop = blocked
    async def script(transport):
        await entered.wait()
    bot._transport_factory = lambda config, runner: FakeTransport(config, runner, script=script)
    await asyncio.wait_for(bot.serve(), timeout=1)
    assert cancelled.is_set()


@pytest.mark.anyio
async def test_explicit_distill_is_queued_once_per_completed_turn(tmp_path, matrix_config):
    bot, _, _, journal = wired_bot(tmp_path)
    await bot.run_turn(_job("remember"), sink=FakeSink(), session_id=None, room_kind="direct")
    for _ in range(2):
        result = await bot.run_turn(_job("/distill"), sink=FakeSink(), session_id=None, room_kind="direct")
        assert "queued" in result.text
    assert [job.trigger for job in journal.list_jobs()] == [DistillTrigger.EXPLICIT]


@pytest.mark.anyio
async def test_model_provider_change_queues_previous_thread_before_reset(tmp_path, matrix_config):
    bot, _, manager, journal = wired_bot(tmp_path, bridge_memory_mode="audience-scoped")
    user_id, chat_id, _ = bot._job_identity(_job("/model gpt-5"), "direct")
    key = bot._conversation_key(user_id, chat_id)
    manager.rows[key] = {"provider": "claude", "session_id": "previous-claude"}
    await bot.run_turn(_job("/model gpt-5"), sink=FakeSink(), session_id=None, room_kind="direct")
    (job,) = journal.list_jobs()
    assert (job.thread_id, job.provider, job.trigger) == ("previous-claude", "claude", DistillTrigger.PROVIDER_SWITCH)
    assert job.memory_scope == resolve_memory_audience(bot._settings, user_id=user_id, chat_id=chat_id, route="matrix").scope
    assert manager._row(key)["session_id"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("provider,command", [("codex", "1"), ("piri", "/resume selected")])
async def test_resume_queues_departing_thread_only_when_selection_changes(tmp_path, matrix_config, provider, command):
    bot, _, manager, journal = wired_bot(tmp_path, agent_provider=provider)
    user_id, chat_id, _ = bot._job_identity(_job(command), "direct")
    key = bot._conversation_key(user_id, chat_id)
    manager.rows[key] = {"provider": provider, "session_id": "previous", "resume_list": [["selected", "selected", provider]]}
    await bot.run_turn(_job(command), sink=FakeSink(), session_id=None, room_kind="direct")
    assert manager._row(key)["session_id"] == "selected"
    (job,) = journal.list_jobs()
    assert job.thread_id == "previous" and job.trigger is DistillTrigger.EXPLICIT
    manager._row(key)["resume_list"] = [["selected", "selected", provider]]
    await bot.run_turn(_job(command), sink=FakeSink(), session_id=None, room_kind="direct")
    assert len(journal.list_jobs()) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("success", [True, False])
async def test_skills_preserves_previous_thread_and_checkpoints_only_success(tmp_path, matrix_config, success):
    bot, chat, manager, journal = wired_bot(tmp_path, memory_distill_checkpoint_turns=1)
    user_id, chat_id, _ = bot._job_identity(_job("/skills"), "direct")
    key = bot._conversation_key(user_id, chat_id)
    manager.rows[key] = {"provider": "codex", "session_id": "previous"}
    chat.response.success = success
    await bot.run_turn(_job("/skills", event_id="$skills"), sink=FakeSink(), session_id=None, room_kind="direct")
    expected = {("previous", DistillTrigger.NEW_COMMAND)}
    if success:
        expected.add(("s-new", DistillTrigger.CHECKPOINT))
    assert {(job.thread_id, job.trigger) for job in journal.list_jobs()} == expected
    assert manager._row(key)["session_id"] == ("s-new" if success else "previous")
