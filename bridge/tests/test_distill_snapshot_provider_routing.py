"""A distill job is only ever read by its own provider's reader (#2316).

After a provider switch the departing session's job keeps its old provider,
but the snapshot worker used to read every job with the bridge's current
runtime. A Codex thread id is a UUID that also passes the Claude session-id
check, so after codex->claude the Claude reader returned an empty snapshot for
it and the job completed with no memory. All data here is synthetic.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core.claude_session_browser import ClaudeTranscriptSnapshotReader
from telegram_bot.memory.codex_snapshot import CodexThreadSnapshotter
from telegram_bot.memory.distill_journal import DistillJournal
from telegram_bot.memory.distill_types import (
    CodexTranscriptSnapshot,
    DistillJobStatus,
    DistillTrigger,
    TranscriptBounds,
)

CODEX_THREAD = "0f1e2d3c-4b5a-4968-8776-655443322110"
CLAUDE_SESSION = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"


class RecordingReader:
    """Wrap a reader and record every read it is asked to do."""

    def __init__(self, inner=None) -> None:
        self.inner = inner
        self.calls: list[str] = []

    async def read_session_snapshot(
        self,
        session_id: str,
        *,
        bounds: TranscriptBounds,
        memory_audience: str | None = None,
        memory_scope: str | None = None,
    ) -> CodexTranscriptSnapshot:
        self.calls.append(session_id)
        if self.inner is not None:
            return await self.inner.read_session_snapshot(
                session_id,
                bounds=bounds,
                memory_audience=memory_audience,
                memory_scope=memory_scope,
            )
        return CodexTranscriptSnapshot(
            thread_hash=hashlib.sha256(session_id.encode()).hexdigest(),
            last_turn_id="turn-final",
            messages=(),
            byte_count=0,
            truncated=False,
            captured_at=datetime.now(timezone.utc).isoformat(),
        )


def _transcripts_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "projects" / "-srv-project"
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    return directory


def _write_claude_transcript(directory: Path, session_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    rows = [
        {
            "type": "user",
            "uuid": "u-1",
            "timestamp": now,
            "message": {"role": "user", "content": "synthetic question"},
        },
        {
            "type": "assistant",
            "uuid": "a-1",
            "timestamp": now,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "synthetic answer"}],
            },
        },
    ]
    path = directory / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    path.chmod(0o600)


def _journal(tmp_path: Path) -> DistillJournal:
    journal = DistillJournal(tmp_path / "journal")
    journal.initialize()
    return journal


@pytest.mark.anyio
async def test_codex_job_after_codex_to_claude_switch_is_not_read_by_claude(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    journal = _journal(tmp_path)
    job = journal.enqueue_once(
        provider="codex",
        thread_id=CODEX_THREAD,
        trigger=DistillTrigger.PROVIDER_SWITCH,
        memory_audience="shared",
        memory_scope="shared",
    )
    claude = RecordingReader(ClaudeTranscriptSnapshotReader(_transcripts_dir(tmp_path)))
    worker = CodexThreadSnapshotter(
        journal, claude, provider="claude", owner_token="snapshot-worker"
    )

    with caplog.at_level("WARNING", logger="telegram_bot.memory.codex_snapshot"):
        result = await worker.snapshot_once(job_id=job.job_id)

    assert claude.calls == []
    assert result.status is DistillJobStatus.RETRYABLE_FAILED
    assert result.error_code == "snapshot_provider_mismatch"
    assert "snapshot_provider_mismatch" in caplog.text
    assert CODEX_THREAD not in caplog.text
    assert job.job_id not in caplog.text  # only the 12-char prefix is logged
    assert job.job_id[:12] in caplog.text


@pytest.mark.anyio
async def test_codex_job_is_not_read_even_if_a_claude_transcript_shares_its_id(
    tmp_path: Path,
) -> None:
    directory = _transcripts_dir(tmp_path)
    _write_claude_transcript(directory, CODEX_THREAD)
    journal = _journal(tmp_path)
    job = journal.enqueue_once(
        provider="codex",
        thread_id=CODEX_THREAD,
        trigger=DistillTrigger.PROVIDER_SWITCH,
    )
    claude = RecordingReader(ClaudeTranscriptSnapshotReader(directory))
    worker = CodexThreadSnapshotter(
        journal, claude, provider="claude", owner_token="snapshot-worker"
    )

    result = await worker.snapshot_once(job_id=job.job_id)

    assert claude.calls == []
    assert result.status is DistillJobStatus.RETRYABLE_FAILED


@pytest.mark.anyio
async def test_mismatched_job_stays_visible_as_max_attempts_exceeded(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    job = journal.enqueue_once(
        provider="codex",
        thread_id=CODEX_THREAD,
        trigger=DistillTrigger.PROVIDER_SWITCH,
    )
    worker = CodexThreadSnapshotter(
        journal,
        RecordingReader(ClaudeTranscriptSnapshotReader(_transcripts_dir(tmp_path))),
        provider="claude",
        owner_token="snapshot-worker",
    )

    for _ in range(6):
        result = await worker.snapshot_once(job_id=job.job_id)

    assert result.status is DistillJobStatus.TERMINAL_FAILED
    assert result.error_code == "max_attempts_exceeded"


@pytest.mark.anyio
async def test_claude_job_after_claude_to_codex_switch_reads_the_claude_transcript(
    tmp_path: Path,
) -> None:
    directory = _transcripts_dir(tmp_path)
    _write_claude_transcript(directory, CLAUDE_SESSION)
    journal = _journal(tmp_path)
    job = journal.enqueue_once(
        provider="claude",
        thread_id=CLAUDE_SESSION,
        trigger=DistillTrigger.PROVIDER_SWITCH,
        memory_audience="shared",
        memory_scope="shared",
    )
    codex = RecordingReader()
    claude = RecordingReader(ClaudeTranscriptSnapshotReader(directory))
    worker = CodexThreadSnapshotter(
        journal,
        codex,
        provider="codex",
        provider_readers={"claude": claude},
        owner_token="snapshot-worker",
    )

    result = await worker.snapshot_once(job_id=job.job_id)

    assert codex.calls == []
    assert claude.calls == [CLAUDE_SESSION]
    assert result.status is DistillJobStatus.SNAPSHOT_DONE
    assert result.snapshot is not None
    assert [m.text for m in result.snapshot.messages] == [
        "synthetic question",
        "synthetic answer",
    ]


@pytest.mark.anyio
async def test_claude_job_with_missing_transcript_under_codex_stays_retryable(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    job = journal.enqueue_once(
        provider="claude",
        thread_id=CLAUDE_SESSION,
        trigger=DistillTrigger.PROVIDER_SWITCH,
    )
    codex = RecordingReader()
    worker = CodexThreadSnapshotter(
        journal,
        codex,
        provider="codex",
        provider_readers={
            "claude": ClaudeTranscriptSnapshotReader(_transcripts_dir(tmp_path))
        },
        owner_token="snapshot-worker",
    )

    result = await worker.snapshot_once(job_id=job.job_id)

    assert codex.calls == []
    assert result.status is DistillJobStatus.RETRYABLE_FAILED
    assert result.error_code == "snapshot_read_failed"


@pytest.mark.anyio
async def test_claude_job_under_codex_without_claude_reader_is_not_read_by_codex(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    job = journal.enqueue_once(
        provider="claude",
        thread_id=CLAUDE_SESSION,
        trigger=DistillTrigger.PROVIDER_SWITCH,
    )
    codex = RecordingReader()
    worker = CodexThreadSnapshotter(
        journal, codex, provider="codex", owner_token="snapshot-worker"
    )

    result = await worker.snapshot_once(job_id=job.job_id)

    assert codex.calls == []
    assert result.status is DistillJobStatus.RETRYABLE_FAILED
    assert result.error_code == "snapshot_provider_mismatch"


@pytest.mark.anyio
async def test_piri_job_under_codex_gets_no_foreign_reader(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    job = journal.enqueue_once(
        provider="piri",
        thread_id="piri-session-1",
        trigger=DistillTrigger.PROVIDER_SWITCH,
    )
    codex = RecordingReader()
    claude = RecordingReader()
    worker = CodexThreadSnapshotter(
        journal,
        codex,
        provider="codex",
        provider_readers={"claude": claude},
        owner_token="snapshot-worker",
    )

    result = await worker.snapshot_once(job_id=job.job_id)

    assert codex.calls == [] and claude.calls == []
    assert result.error_code == "snapshot_provider_mismatch"


@pytest.mark.anyio
async def test_matching_provider_is_read_by_the_runtime(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    job = journal.enqueue_once(
        provider="codex",
        thread_id=CODEX_THREAD,
        trigger=DistillTrigger.NEW_COMMAND,
    )
    codex = RecordingReader()
    claude = RecordingReader()
    worker = CodexThreadSnapshotter(
        journal,
        codex,
        provider=" Codex ",
        provider_readers={"claude": claude},
        owner_token="snapshot-worker",
    )

    result = await worker.snapshot_once(job_id=job.job_id)

    assert codex.calls == [CODEX_THREAD] and claude.calls == []
    assert result.status is DistillJobStatus.SNAPSHOT_DONE


def test_reader_map_requires_a_runtime_provider_and_no_duplicate(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    with pytest.raises(ValueError):
        CodexThreadSnapshotter(
            journal, RecordingReader(), provider_readers={"claude": RecordingReader()}
        )
    with pytest.raises(ValueError):
        CodexThreadSnapshotter(
            journal,
            RecordingReader(),
            provider="claude",
            provider_readers={"Claude": RecordingReader()},
        )


def test_only_claude_gets_a_fallback_reader_and_only_off_claude(tmp_path: Path) -> None:
    from telegram_bot.__main__ import (
        _claude_transcripts_dir,
        _distill_fallback_snapshot_readers,
    )

    project = tmp_path / "project"
    project.mkdir()
    for provider in ("codex", "piri", "danso"):
        settings = SimpleNamespace(agent_provider=provider, project_root=str(project))
        readers = _distill_fallback_snapshot_readers(settings)  # type: ignore[arg-type]
        assert set(readers) == {"claude"}
        reader = readers["claude"]
        # By name: other suites reload telegram_bot modules, so class identity
        # depends on test order.
        assert type(reader).__qualname__ == "ClaudeTranscriptSnapshotReader"
        assert reader._transcripts_dir == _claude_transcripts_dir(settings)  # type: ignore[arg-type]
    claude_settings = SimpleNamespace(agent_provider="claude", project_root=str(project))
    assert _distill_fallback_snapshot_readers(claude_settings) == {}  # type: ignore[arg-type]
