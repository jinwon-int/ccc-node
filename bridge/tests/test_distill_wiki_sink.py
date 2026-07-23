"""Security and replay contract for the Codex Wiki candidate queue (#465)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat

import pytest

from telegram_bot.memory.distill_extraction import DistillExtractionOutput
from telegram_bot.memory.distill_wiki_sink import CodexWikiCandidateSink


JOB_ID = "a" * 64
THREAD_HASH = "b" * 64
PRIVATE_SCOPE = "private-0123456789abcdef0123456789abcdef"


def extraction_output(*, candidates: bool = True) -> DistillExtractionOutput:
    return DistillExtractionOutput.model_validate(
        {
            "schema_version": 1,
            "provenance": {
                "provider": "codex",
                "source_thread_hash": THREAD_HASH,
                "trigger": "new_command",
                "distilled_at": "2026-07-23T08:00:00Z",
            },
            "honcho": [],
            "wiki_candidates": (
                [
                    {
                        "title": "Codex Wiki candidate queue contract",
                        "suggested_path": "pages/team/jingun/MEMORY.md",
                        "summary": "Keep candidates local until a human reviews them.",
                        "evidence_excerpt": "issue #465",
                    }
                ]
                if candidates
                else []
            ),
            "resume": {
                "last_activity": "",
                "pending_action": "",
                "awaiting_user": False,
                "open_question": "",
                "next_step": "",
                "evidence": [],
            },
        }
    )


def test_writes_owner_only_human_review_record_without_raw_identity(
    tmp_path: Path,
) -> None:
    queue = tmp_path / "wiki-candidates"
    sink = CodexWikiCandidateSink(queue)

    result = sink.write(extraction_output(), job_id=JOB_ID)

    path = queue / f"{JOB_ID}.json"
    record = json.loads(path.read_text())
    assert result.candidates_queued == 1
    assert result.record_written is True
    assert record == {
        "candidates": [
            {
                "evidence_excerpt": "issue #465",
                "suggested_path": "pages/team/jingun/MEMORY.md",
                "summary": "Keep candidates local until a human reviews them.",
                "title": "Codex Wiki candidate queue contract",
            }
        ],
        "job_id": JOB_ID,
        "provenance": {
            "distilled_at": "2026-07-23T08:00:00Z",
            "provider": "codex",
            "source_thread_hash": THREAD_HASH,
            "trigger": "new_command",
        },
        "review_status": "pending",
        "schema_version": 1,
    }
    serialized = path.read_text()
    assert "thread_id" not in serialized
    assert "memory_scope" not in serialized
    assert "telegram" not in serialized.lower()
    assert stat.S_IMODE(queue.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_routed_candidates_are_partitioned_and_audience_labelled(
    tmp_path: Path,
) -> None:
    queue = tmp_path / "wiki-candidates"
    other_scope = "private-fedcba9876543210fedcba9876543210"
    private_sink = CodexWikiCandidateSink(
        queue / PRIVATE_SCOPE,
        memory_audience="private",
        memory_scope=PRIVATE_SCOPE,
    )
    other_sink = CodexWikiCandidateSink(
        queue / other_scope,
        memory_audience="private",
        memory_scope=other_scope,
    )

    private_sink.write(extraction_output(), job_id=JOB_ID)
    other_sink.write(extraction_output(), job_id=JOB_ID)

    private_path = queue / PRIVATE_SCOPE / f"{JOB_ID}.json"
    other_path = queue / other_scope / f"{JOB_ID}.json"
    record = json.loads(private_path.read_text())
    assert record["memory_audience"] == "private"
    assert record["memory_scope"] == PRIVATE_SCOPE
    assert private_path.is_file()
    assert other_path.is_file()
    assert not (queue / f"{JOB_ID}.json").exists()
    assert stat.S_IMODE((queue / PRIVATE_SCOPE).stat().st_mode) == 0o700
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("memory_audience", "memory_scope"),
    [
        ("private", None),
        (None, PRIVATE_SCOPE),
        ("private", "../unsafe"),
        ("shared", PRIVATE_SCOPE),
    ],
)
def test_rejects_invalid_candidate_memory_route(
    tmp_path: Path,
    memory_audience: str | None,
    memory_scope: str | None,
) -> None:
    with pytest.raises(ValueError, match="memory audience route"):
        CodexWikiCandidateSink(
            tmp_path / "wiki-candidates",
            memory_audience=memory_audience,
            memory_scope=memory_scope,
        )


@pytest.mark.anyio
async def test_ten_concurrent_replays_create_one_identical_record(tmp_path: Path) -> None:
    queue = tmp_path / "wiki-candidates"
    sinks = tuple(CodexWikiCandidateSink(queue) for _ in range(10))

    results = await asyncio.gather(
        *(
            asyncio.to_thread(sink.write, extraction_output(), job_id=JOB_ID)
            for sink in sinks
        )
    )

    assert len(list(queue.glob("*.json"))) == 1
    assert sum(result.record_written for result in results) == 1


def test_empty_candidates_do_not_create_a_record(tmp_path: Path) -> None:
    queue = tmp_path / "wiki-candidates"

    result = CodexWikiCandidateSink(queue).write(
        extraction_output(candidates=False), job_id=JOB_ID
    )

    assert result.candidates_queued == 0
    assert result.record_written is False
    assert list(queue.glob("*.json")) == []


def test_rejects_symlink_or_hardlinked_record_without_mutation(tmp_path: Path) -> None:
    queue = tmp_path / "wiki-candidates"
    queue.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    record = queue / f"{JOB_ID}.json"
    record.symlink_to(outside)

    with pytest.raises(PermissionError):
        CodexWikiCandidateSink(queue).write(extraction_output(), job_id=JOB_ID)
    assert outside.read_text() == "unchanged"

    record.unlink()
    record.write_text("{}")
    record.chmod(0o600)
    os.link(record, tmp_path / "hardlink")
    with pytest.raises(PermissionError):
        CodexWikiCandidateSink(queue).write(extraction_output(), job_id=JOB_ID)


def test_existing_different_record_is_a_terminal_collision(tmp_path: Path) -> None:
    queue = tmp_path / "wiki-candidates"
    queue.mkdir(mode=0o700)
    path = queue / f"{JOB_ID}.json"
    path.write_text('{"review_status":"changed"}')
    path.chmod(0o600)

    with pytest.raises(ValueError, match="collision"):
        CodexWikiCandidateSink(queue).write(extraction_output(), job_id=JOB_ID)


def test_rejects_oversized_existing_record_before_read(tmp_path: Path) -> None:
    queue = tmp_path / "wiki-candidates"
    queue.mkdir(mode=0o700)
    path = queue / f"{JOB_ID}.json"
    path.write_bytes(b"x" * (16 * 1024 + 1))
    path.chmod(0o600)

    with pytest.raises(PermissionError, match="safe read bound"):
        CodexWikiCandidateSink(queue).write(extraction_output(), job_id=JOB_ID)
