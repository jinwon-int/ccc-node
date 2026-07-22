"""Durable Codex Honcho outbox and leased delivery contract (#465)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from test_distill_local_journal import extracted_job

from telegram_bot.memory.distill_honcho_worker import (
    CodexDistillHonchoSinkWorker,
    HonchoDeliveryError,
    HonchoHttpSender,
)
from telegram_bot.memory.distill_journal import DistillJournal
from telegram_bot.memory.distill_types import DistillHonchoSinkStatus
from telegram_bot.core.bot import TelegramBot


class RecordingSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.records: list[dict[str, object]] = []

    def send(self, record: dict[str, object]) -> None:
        self.records.append(record)
        if self.fail:
            raise HonchoDeliveryError("body-free", terminal=False)


@pytest.mark.anyio
async def test_worker_delivers_stable_body_safe_record_and_acks_outbox(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    sender = RecordingSender()
    outbox = tmp_path / "honcho-outbox"
    worker = CodexDistillHonchoSinkWorker(
        journal, outbox_dir=outbox, sender=sender, owner_token="honcho-worker"
    )

    result = await worker.write_once(job_id=job.job_id)

    assert result.honcho_sink_status is DistillHonchoSinkStatus.DONE
    assert result.honcho_sink_attempts == 1
    assert len(sender.records) == 1
    record = sender.records[0]
    assert record["idempotency_key"] == f"ccc-distill-{job.job_id}"
    assert record["session_id"] == f"codex-distill-{job.job_id[:24]}"
    assert record["provenance"] == {
        "provider": "codex",
        "source_thread_hash": job.thread_hash,
        "trigger": "new_command",
        "distilled_at": "2026-07-16T04:01:00Z",
    }
    serialized = json.dumps(record)
    assert job.thread_id not in serialized
    assert str(job.memory_scope) not in serialized
    assert list(outbox.glob("*.json")) == []


@pytest.mark.anyio
async def test_delivery_failure_preserves_one_outbox_and_retries_without_extraction(
    tmp_path: Path,
) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    sender = RecordingSender(fail=True)
    outbox = tmp_path / "honcho-outbox"
    worker = CodexDistillHonchoSinkWorker(
        journal, outbox_dir=outbox, sender=sender, owner_token="honcho-worker"
    )

    failed = await worker.write_once(job_id=job.job_id)

    assert failed.honcho_sink_status is DistillHonchoSinkStatus.RETRYABLE_FAILED
    assert failed.error_code == "honcho_sink_delivery_failed"
    assert failed.extraction_attempts == job.extraction_attempts
    assert len(list(outbox.glob("*.json"))) == 1

    sender.fail = False
    completed = await worker.write_once(job_id=job.job_id)
    assert completed.honcho_sink_status is DistillHonchoSinkStatus.DONE
    assert completed.extraction_attempts == job.extraction_attempts
    assert len(sender.records) == 2
    assert list(outbox.glob("*.json")) == []


@pytest.mark.anyio
async def test_ten_workers_claim_and_deliver_once(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    sender = RecordingSender()
    workers = tuple(
        CodexDistillHonchoSinkWorker(
            journal,
            outbox_dir=tmp_path / "honcho-outbox",
            sender=sender,
            owner_token=f"honcho-{index}",
        )
        for index in range(10)
    )

    await asyncio.gather(*(worker.write_once(job_id=job.job_id) for worker in workers))

    assert journal.get(job.job_id).honcho_sink_status is DistillHonchoSinkStatus.DONE
    assert len(sender.records) == 1


@pytest.mark.anyio
async def test_disabled_extraction_never_claims_honcho(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal, honcho_enabled=False)

    assert job.honcho_sink_status is DistillHonchoSinkStatus.DISABLED
    assert journal.claim_honcho_sink(job.job_id, owner_token="honcho") is None


def test_http_sender_uses_stable_idempotency_and_keeps_token_out_of_record(
    tmp_path: Path,
) -> None:
    config = tmp_path / "honcho.json"
    config.write_text(json.dumps({
        "baseUrl": "https://honcho.invalid",
        "workspace": "test space",
        "aiPeer": "peer-a",
        "authToken": "raw-token-must-not-persist",
    }))
    config.chmod(0o600)
    requests = []

    class Response:
        status = 201
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self
        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return False

    def urlopen(request, **kwargs):  # type: ignore[no-untyped-def]
        requests.append((request, kwargs))
        return Response()

    record = {
        "idempotency_key": "ccc-distill-" + "a" * 64,
        "session_id": "codex-distill-" + "a" * 24,
        "facts": [{"kind": "observation", "text": "harmless", "subject": "session"}],
        "provenance": {"provider": "codex", "source_thread_hash": "b" * 64},
    }
    with patch(
        "telegram_bot.memory.distill_honcho_worker.urllib_request.urlopen",
        side_effect=urlopen,
    ):
        HonchoHttpSender(config, node_label="jingun").send(record)

    assert len(requests) == 2
    assert requests[1][0].headers["Idempotency-key"] == record["idempotency_key"]
    assert requests[1][0].headers["Authorization"] == "Bearer raw-token-must-not-persist"
    assert "test%20space" in requests[1][0].full_url
    assert "raw-token" not in json.dumps(record)


def test_http_sender_rejects_symlink_config_without_opening_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text('{"authToken":"must-not-read"}')
    config = tmp_path / "honcho.json"
    config.symlink_to(target)

    with pytest.raises(HonchoDeliveryError) as caught:
        HonchoHttpSender(config).send({})

    assert caught.value.terminal is True
    assert "must-not-read" not in repr(caught.value)


@pytest.mark.anyio
async def test_lifecycle_drives_pending_honcho_work(tmp_path: Path) -> None:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    job = await extracted_job(journal)
    worker = CodexDistillHonchoSinkWorker(
        journal,
        outbox_dir=tmp_path / "outbox",
        sender=RecordingSender(),
        owner_token="honcho-loop",
    )
    bot = TelegramBot.__new__(TelegramBot)
    bot._distill_journal = journal
    bot._distill_honcho_sink_worker = worker
    bot._config = SimpleNamespace(distill_extraction_poll_interval=0.02)
    stop = asyncio.Event()
    task = asyncio.create_task(bot._distill_honcho_sink_loop(stop))
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while (
            journal.get(job.job_id).honcho_sink_status
            is not DistillHonchoSinkStatus.DONE
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    assert journal.get(job.job_id).honcho_sink_status is DistillHonchoSinkStatus.DONE
