"""RED-first contract for the durable Codex distill extraction worker (#532)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from telegram_bot.memory.codex_exec_backend import CodexDistillBackendError
from telegram_bot.memory.distill_extraction import (
    DISTILL_EXTRACTION_SCHEMA_VERSION,
    DistillExtractionInput,
    DistillExtractionOutput,
)
from telegram_bot.memory.distill_journal import DistillJournal
from telegram_bot.memory.distill_types import (
    CodexTranscriptSnapshot,
    DistillJob,
    DistillJobStatus,
    DistillTrigger,
    TranscriptMessage,
)
from telegram_bot.core.usage_meter import UsageMeter
from telegram_bot.memory.distill_worker import CodexDistillExtractionWorker


def utc(second: int = 0) -> datetime:
    return datetime(2026, 7, 16, 4, 0, second, tzinfo=timezone.utc)


def thread_hash(thread_id: str) -> str:
    return hashlib.sha256(thread_id.encode("utf-8")).hexdigest()


def snapshot(thread_id: str = "thread-532") -> CodexTranscriptSnapshot:
    text = "harmless durable fact"
    return CodexTranscriptSnapshot(
        thread_hash=thread_hash(thread_id),
        last_turn_id="turn-1",
        messages=(TranscriptMessage("user", text, "2026-07-16T04:00:00Z"),),
        byte_count=len(text.encode("utf-8")),
        truncated=False,
        captured_at="2026-07-16T04:00:00Z",
    )


def output_for(extraction_input: DistillExtractionInput) -> DistillExtractionOutput:
    return DistillExtractionOutput.model_validate(
        {
            "schema_version": DISTILL_EXTRACTION_SCHEMA_VERSION,
            "provenance": {
                "provider": extraction_input.provider,
                "source_thread_hash": extraction_input.source_thread_hash,
                "trigger": extraction_input.trigger.value,
                "distilled_at": "2026-07-16T04:01:00Z",
            },
            "honcho": [
                {
                    "kind": "observation",
                    "text": "A harmless fact was retained.",
                    "subject": "session",
                }
            ],
            "wiki_candidates": [],
            "resume": {
                "last_activity": "Extracted a harmless fact.",
                "pending_action": "",
                "awaiting_user": False,
                "open_question": "",
                "next_step": "",
                "evidence": ["issue #532"],
            },
        }
    )


def snapshot_done_job(
    journal: DistillJournal,
    *,
    thread_id: str = "thread-532",
) -> DistillJob:
    queued = journal.enqueue_once(
        provider="codex",
        thread_id=thread_id,
        trigger=DistillTrigger.NEW_COMMAND,
    )
    claimed = journal.claim(
        queued.job_id,
        owner_token="snapshot-worker",
        now=utc(),
    )
    assert claimed is not None
    return journal.mark_snapshot_done(
        queued.job_id,
        owner_token="snapshot-worker",
        lease_epoch=claimed.lease_epoch,
        snapshot=snapshot(thread_id),
        now=utc(1),
    )


class SuccessfulBackend:
    def __init__(self) -> None:
        self.calls: list[DistillExtractionInput] = []

    async def extract(
        self, extraction_input: DistillExtractionInput
    ) -> DistillExtractionOutput:
        self.calls.append(extraction_input)
        await asyncio.sleep(0.01)
        return output_for(extraction_input)


def wiki_output_for(
    extraction_input: DistillExtractionInput,
) -> DistillExtractionOutput:
    value = output_for(extraction_input).model_dump(mode="json")
    value["wiki_candidates"] = [
        {
            "title": "A bounded memory candidate",
            "suggested_path": "pages/nodes/bangtong/MEMORY.md",
            "summary": "A harmless candidate summary.",
            "evidence_excerpt": "A harmless evidence excerpt.",
        }
    ]
    return DistillExtractionOutput.model_validate(value)


@pytest.mark.anyio
async def test_concurrent_workers_extract_once_and_result_survives_reopen(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    journal = DistillJournal(root)
    journal.initialize()
    job = snapshot_done_job(journal)
    backend = SuccessfulBackend()
    workers = tuple(
        CodexDistillExtractionWorker(
            journal,
            backend,
            owner_token=f"extract-worker-{index}",
        )
        for index in range(10)
    )

    await asyncio.gather(*(worker.extract_once(job_id=job.job_id) for worker in workers))

    assert len(backend.calls) == 1
    extraction_input = backend.calls[0]
    assert extraction_input.source_thread_hash == thread_hash("thread-532")
    assert extraction_input.trigger is DistillTrigger.NEW_COMMAND
    assert extraction_input.content_trust == "untrusted"

    reopened = DistillJournal(root)
    reopened.initialize()
    persisted = reopened.get(job.job_id)
    assert persisted.status is DistillJobStatus.EXTRACTION_DONE
    assert persisted.snapshot == snapshot()
    assert persisted.extraction_attempts == 1
    result = reopened.get_extraction_output(job.job_id)
    assert result is not None
    assert result.honcho[0].text == "A harmless fact was retained."


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("codex_distill_spawn_failed", DistillJobStatus.EXTRACTION_RETRYABLE_FAILED),
        ("codex_distill_timeout", DistillJobStatus.EXTRACTION_RETRYABLE_FAILED),
        ("codex_distill_io_failed", DistillJobStatus.EXTRACTION_RETRYABLE_FAILED),
        ("codex_distill_nonzero_exit", DistillJobStatus.EXTRACTION_RETRYABLE_FAILED),
        ("codex_distill_schema_unsafe", DistillJobStatus.EXTRACTION_TERMINAL_FAILED),
        ("codex_distill_executable_unsafe", DistillJobStatus.EXTRACTION_TERMINAL_FAILED),
        ("codex_distill_output_invalid", DistillJobStatus.EXTRACTION_TERMINAL_FAILED),
    ],
)
@pytest.mark.anyio
async def test_backend_failure_is_classified_with_body_free_code(
    tmp_path: Path,
    code: str,
    expected_status: DistillJobStatus,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = snapshot_done_job(journal)

    class Backend:
        async def extract(self, extraction_input: DistillExtractionInput) -> Any:
            del extraction_input
            raise CodexDistillBackendError(code)

    result = await CodexDistillExtractionWorker(
        journal,
        Backend(),
        owner_token="extract-worker",
    ).extract_once(job_id=job.job_id)

    assert result.status is expected_status
    assert result.error_code == code
    assert result.snapshot == snapshot()
    assert result.extraction_output is None


@pytest.mark.anyio
async def test_claim_boundary_wiki_gate_and_max_attempts_fail_closed(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    queued = journal.enqueue_once(
        provider="codex",
        thread_id="thread-queued",
        trigger=DistillTrigger.NEW_COMMAND,
    )
    assert journal.claim_extraction(
        queued.job_id, owner_token="must-not-claim"
    ) is None

    job = snapshot_done_job(journal, thread_id="thread-wiki")

    class WikiBackend:
        async def extract(
            self, extraction_input: DistillExtractionInput
        ) -> DistillExtractionOutput:
            return wiki_output_for(extraction_input)

    blocked = await CodexDistillExtractionWorker(
        journal,
        WikiBackend(),
        owner_token="wiki-disabled-worker",
        wiki_enabled=False,
    ).extract_once(job_id=job.job_id)
    assert blocked.status is DistillJobStatus.EXTRACTION_TERMINAL_FAILED
    assert blocked.error_code == "distill_output_wiki_disabled"

    retry_journal = DistillJournal(tmp_path / "retry-journal")
    retry_journal.initialize()
    retry_job = snapshot_done_job(retry_journal, thread_id="thread-retry")

    class RetryBackend:
        def __init__(self) -> None:
            self.calls = 0

        async def extract(self, extraction_input: DistillExtractionInput) -> Any:
            del extraction_input
            self.calls += 1
            raise CodexDistillBackendError("codex_distill_timeout")

    retry_backend = RetryBackend()
    worker = CodexDistillExtractionWorker(
        retry_journal,
        retry_backend,
        owner_token="retry-worker",
        max_attempts=1,
    )
    first = await worker.extract_once(job_id=retry_job.job_id)
    assert first.status is DistillJobStatus.EXTRACTION_RETRYABLE_FAILED
    second = await worker.extract_once(job_id=retry_job.job_id)
    assert second.status is DistillJobStatus.EXTRACTION_TERMINAL_FAILED
    assert second.error_code == "extraction_max_attempts_exceeded"
    assert retry_backend.calls == 1


@pytest.mark.anyio
async def test_unknown_backend_failure_and_cancellation_are_retryable_and_body_free(
    tmp_path: Path,
) -> None:
    first = DistillJournal(tmp_path / "first")
    first.initialize()
    failed_job = snapshot_done_job(first)

    class FailingBackend:
        async def extract(self, extraction_input: DistillExtractionInput) -> Any:
            del extraction_input
            raise RuntimeError("authorization=raw-private-provider-body")

    failed = await CodexDistillExtractionWorker(
        first,
        FailingBackend(),
        owner_token="extract-worker",
    ).extract_once(job_id=failed_job.job_id)
    assert failed.status is DistillJobStatus.EXTRACTION_RETRYABLE_FAILED
    assert failed.error_code == "distill_backend_failed"
    assert "raw-private" not in repr(first.diagnostics(failed.job_id))

    second = DistillJournal(tmp_path / "second")
    second.initialize()
    cancelled_job = snapshot_done_job(second)
    started = asyncio.Event()
    never = asyncio.Event()

    class BlockingBackend:
        async def extract(self, extraction_input: DistillExtractionInput) -> Any:
            del extraction_input
            started.set()
            await never.wait()

    task = asyncio.create_task(
        CodexDistillExtractionWorker(
            second,
            BlockingBackend(),
            owner_token="extract-worker",
        ).extract_once(job_id=cancelled_job.job_id)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    cancelled = second.get(cancelled_job.job_id)
    assert cancelled.status is DistillJobStatus.EXTRACTION_RETRYABLE_FAILED
    assert cancelled.error_code == "distill_cancelled"
    assert cancelled.snapshot == snapshot()


def test_stale_extraction_lease_recovers_without_requeueing_snapshot(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = snapshot_done_job(journal)
    claimed = journal.claim_extraction(
        job.job_id,
        owner_token="extract-worker-a",
        now=utc(2),
        lease_seconds=5,
    )
    assert claimed is not None
    assert claimed.status is DistillJobStatus.RUNNING_EXTRACTION

    assert journal.recover_stale_running(now=utc(6)) == 0
    assert journal.recover_stale_running(now=utc(8)) == 1
    recovered = journal.get(job.job_id)
    assert recovered.status is DistillJobStatus.EXTRACTION_RETRYABLE_FAILED
    assert recovered.error_code == "extraction_lease_expired"
    assert recovered.snapshot == snapshot()
    assert recovered.attempts == 1

    next_claim = journal.claim_extraction(
        job.job_id,
        owner_token="extract-worker-b",
        now=utc(9),
    )
    assert next_claim is not None
    with pytest.raises(RuntimeError, match="owner or lease"):
        journal.mark_extraction_done(
            job.job_id,
            owner_token="extract-worker-a",
            lease_epoch=claimed.extraction_lease_epoch,
            extraction_output=output_for(
                DistillExtractionInput.model_validate(
                    {
                        "schema_version": 1,
                        "provider": "codex",
                        "content_trust": "untrusted",
                        "source_thread_hash": snapshot().thread_hash,
                        "trigger": "new_command",
                        "captured_at": snapshot().captured_at,
                        "truncated": False,
                        "messages": [{"role": "user", "text": "harmless durable fact"}],
                        "message_count": 1,
                        "byte_count": len("harmless durable fact"),
                    }
                )
            ),
        )


@pytest.mark.anyio
async def test_tampered_persisted_output_and_provenance_mismatch_fail_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    journal = DistillJournal(root)
    journal.initialize()
    job = snapshot_done_job(journal)
    await CodexDistillExtractionWorker(
        journal,
        SuccessfulBackend(),
        owner_token="extract-worker",
    ).extract_once(job_id=job.job_id)

    path = journal.job_path(job.job_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["extraction_output"] = record["extraction_output"].replace(
        "harmless fact", "tampered fact"
    )
    path.write_text(json.dumps(record), encoding="utf-8")
    path.chmod(0o600)
    reopened = DistillJournal(root)
    reopened.initialize()
    with pytest.raises(ValueError, match="extraction output"):
        reopened.get(job.job_id)

    mismatch = DistillJournal(tmp_path / "mismatch")
    mismatch.initialize()
    mismatch_job = snapshot_done_job(mismatch)

    class MismatchedBackend:
        async def extract(
            self, extraction_input: DistillExtractionInput
        ) -> DistillExtractionOutput:
            value = output_for(extraction_input).model_dump(mode="json")
            value["provenance"]["source_thread_hash"] = "b" * 64
            return DistillExtractionOutput.model_validate(value)

    result = await CodexDistillExtractionWorker(
        mismatch,
        MismatchedBackend(),
        owner_token="extract-worker",
    ).extract_once(job_id=mismatch_job.job_id)
    assert result.status is DistillJobStatus.EXTRACTION_TERMINAL_FAILED
    assert result.error_code == "distill_output_provenance_invalid"


def test_extraction_diagnostics_exclude_bodies_tokens_and_thread_id(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = snapshot_done_job(journal, thread_id="raw-thread-id-private")
    claimed = journal.claim_extraction(
        job.job_id,
        owner_token="raw-owner-token-private",
        now=utc(2),
    )
    assert claimed is not None

    diagnostics = repr(journal.diagnostics(job.job_id))
    assert "raw-thread-id" not in diagnostics
    assert "raw-owner-token" not in diagnostics
    assert "harmless durable fact" not in diagnostics
    assert set(journal.diagnostics(job.job_id)) >= {
        "thread_hash",
        "status",
        "extraction_attempts",
        "extraction_output_bytes",
        "error_code",
    }


class _FakeUsageMeter:
    """Structural AutonomousSpendGate double for the budget wiring (#388)."""

    def __init__(self, *, allowed: bool) -> None:
        self.allowed = allowed
        self.checks: list[str] = []
        self.records: list[tuple[str, str, int, int]] = []

    def check_autonomous_spend(self, provider: str) -> Any:
        self.checks.append(provider)

        class _Decision:
            allowed = self.allowed

            @staticmethod
            def reason() -> str:
                return "codex used 1000 of 1000 budget tokens (blocked)"

        return _Decision()

    def record(
        self,
        provider: str,
        mode: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        requests: int = 0,
    ) -> tuple[()]:
        self.records.append((provider, mode, input_tokens, requests))
        return ()


@pytest.mark.anyio
async def test_budget_blocked_extraction_defers_without_claiming_or_spending(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = snapshot_done_job(journal)
    backend = SuccessfulBackend()
    meter = _FakeUsageMeter(allowed=False)

    result = await CodexDistillExtractionWorker(
        journal,
        backend,
        owner_token="extract-worker",
        usage_meter=meter,
    ).extract_once(job_id=job.job_id)

    # The provider was never called, nothing was metered, and the job was not
    # claimed: no extraction attempt is burned while the budget blocks, so the
    # job replays untouched once the daily window resets.
    assert backend.calls == []
    assert meter.checks == ["codex"]
    assert meter.records == []
    assert result.status is DistillJobStatus.SNAPSHOT_DONE
    assert result.extraction_attempts == 0
    persisted = journal.get(job.job_id)
    assert persisted.status is DistillJobStatus.SNAPSHOT_DONE
    assert persisted.extraction_attempts == 0


@pytest.mark.anyio
async def test_budget_allowed_extraction_meters_one_autonomous_request(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = snapshot_done_job(journal)
    backend = SuccessfulBackend()
    meter = _FakeUsageMeter(allowed=True)

    result = await CodexDistillExtractionWorker(
        journal,
        backend,
        owner_token="extract-worker",
        usage_meter=meter,
    ).extract_once(job_id=job.job_id)

    assert result.status is DistillJobStatus.EXTRACTION_DONE
    assert len(backend.calls) == 1
    # The reservation charges 2048 overhead + byte_count // 2 for the
    # 21-byte snapshot, so the token budget is consumed even though the
    # backend cannot report actual usage yet.
    assert meter.records == [("codex", "autonomous", 2058, 1)]


@pytest.mark.anyio
async def test_repeated_autonomous_extraction_consumes_budget_and_blocks(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    meter = UsageMeter(
        tmp_path / "usage-meter.json",
        budgets={"codex": 5000},
        clock=lambda: 1784170800.0,  # fixed instant, one KST day
    )
    backend = SuccessfulBackend()
    worker = CodexDistillExtractionWorker(
        journal,
        backend,
        owner_token="extract-worker",
        usage_meter=meter,
    )
    jobs = [
        snapshot_done_job(journal, thread_id=f"thread-budget-{index}")
        for index in range(6)
    ]

    results = [await worker.extract_once(job_id=job.job_id) for job in jobs]

    # Each attempt reserves 2048 + byte_count // 2 = 2058 tokens, so the
    # 5000-token codex budget admits exactly three background extractions on
    # one day before autonomous spend is blocked; later jobs defer unclaimed.
    assert len(backend.calls) == 3
    assert [result.status for result in results[:3]] == (
        [DistillJobStatus.EXTRACTION_DONE] * 3
    )
    assert all(
        result.status is DistillJobStatus.SNAPSHOT_DONE for result in results[3:]
    )
    assert all(result.extraction_attempts == 0 for result in results[3:])
    assert meter.used_tokens("codex") == 3 * 2058
    assert meter.check_autonomous_spend("codex").allowed is False
