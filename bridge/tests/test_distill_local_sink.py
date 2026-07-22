"""Contract tests for the replay-safe Codex local memory sink (#465)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
from unittest.mock import patch

import pytest

from telegram_bot.memory.distill_extraction import DistillExtractionOutput
from telegram_bot.memory.distill_local_sink import CodexLocalMemorySink


JOB_ID = "a" * 64
THREAD_HASH = "b" * 64


def extraction_output(
    *,
    fact_text: str = "The user prefers focused pull requests.",
    last_activity: str = "Implemented a bounded local sink.",
) -> DistillExtractionOutput:
    return DistillExtractionOutput.model_validate(
        {
            "schema_version": 1,
            "provenance": {
                "provider": "codex",
                "source_thread_hash": THREAD_HASH,
                "trigger": "new_command",
                "distilled_at": "2026-07-22T08:00:00Z",
            },
            "honcho": (
                [
                    {
                        "kind": "preference",
                        "text": fact_text,
                        "subject": "user",
                    }
                ]
                if fact_text
                else []
            ),
            "wiki_candidates": [],
            "resume": {
                "last_activity": last_activity,
                "pending_action": "Wire the journal worker.",
                "awaiting_user": False,
                "open_question": "",
                "next_step": "Run the hermetic round trip.",
                "evidence": ["issue #465", "#000"],
            },
        }
    )


def read_facts(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_writes_bounded_private_facts_and_resume_with_hashed_provenance(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    sink = CodexLocalMemorySink(state_dir, audience="private")

    result = sink.write(extraction_output(), job_id=JOB_ID)

    facts_path = state_dir / "memory-facts.jsonl"
    resume_path = state_dir / "resume.md"
    facts = read_facts(facts_path)
    assert result.facts_added == 1
    assert result.resume_written is True
    assert len(facts) == 1
    assert facts[0]["privacy"] == "private"
    assert facts[0]["audience"] == "private"
    assert facts[0]["durability"] == "durable"
    assert facts[0]["observed_at"] == "2026-07-22T08:00:00Z"
    assert facts[0]["source"] == {
        "type": "distill",
        "provider": "codex",
        "job_id": JOB_ID,
        "thread_hash": THREAD_HASH,
        "trigger": "new_command",
        "schema_version": 1,
    }
    resume = resume_path.read_text()
    assert "provider=codex" in resume
    assert f"thread_hash={THREAD_HASH}" in resume
    assert "마지막 작업: Implemented a bounded local sink." in resume
    assert "thread-sensitive-value" not in facts_path.read_text() + resume
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(facts_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(resume_path.stat().st_mode) == 0o600


@pytest.mark.anyio
async def test_ten_concurrent_replays_append_each_fact_once(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    sinks = tuple(CodexLocalMemorySink(state_dir, audience="shared") for _ in range(10))
    output = extraction_output()

    results = await asyncio.gather(
        *(asyncio.to_thread(sink.write, output, job_id=JOB_ID) for sink in sinks)
    )

    facts = read_facts(state_dir / "memory-facts.jsonl")
    assert len(facts) == 1
    assert sum(result.facts_added for result in results) == 1
    assert facts[0]["privacy"] == "shared"
    assert facts[0]["audience"] == "shared"


def test_replay_after_partial_commit_does_not_duplicate_fact(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    sink = CodexLocalMemorySink(state_dir, audience="private")
    output = extraction_output()

    from telegram_bot.memory import distill_local_sink as module

    real_write = module._atomic_write_bytes
    calls = 0

    def fail_second_write(destination: Path, payload: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated crash without sensitive body")
        real_write(destination, payload)

    with patch.object(module, "_atomic_write_bytes", side_effect=fail_second_write):
        with pytest.raises(OSError, match="simulated crash"):
            sink.write(output, job_id=JOB_ID)

    replay = sink.write(output, job_id=JOB_ID)

    assert replay.facts_added == 0
    assert replay.resume_written is True
    assert len(read_facts(state_dir / "memory-facts.jsonl")) == 1


def test_empty_output_preserves_existing_files(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    facts_path = state_dir / "memory-facts.jsonl"
    resume_path = state_dir / "resume.md"
    facts_path.write_text('{"id":"existing","text":"keep"}\n')
    resume_path.write_text("keep resume\n")
    facts_path.chmod(0o600)
    resume_path.chmod(0o600)
    sink = CodexLocalMemorySink(state_dir, audience="private")
    value = extraction_output(fact_text="", last_activity="").model_copy(
        update={
            "resume": extraction_output().resume.model_copy(
                update={
                    "last_activity": "",
                    "pending_action": "",
                    "awaiting_user": False,
                    "open_question": "",
                    "next_step": "",
                    "evidence": (),
                }
            )
        }
    )

    result = sink.write(value, job_id=JOB_ID)

    assert result.facts_added == 0
    assert result.resume_written is False
    assert facts_path.read_text() == '{"id":"existing","text":"keep"}\n'
    assert resume_path.read_text() == "keep resume\n"


@pytest.mark.parametrize("target_name", ["memory-facts.jsonl", "resume.md"])
def test_rejects_symlink_targets_without_mutating_them(tmp_path: Path, target_name: str) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    (state_dir / target_name).symlink_to(outside)
    sink = CodexLocalMemorySink(state_dir, audience="private")

    with pytest.raises(PermissionError):
        sink.write(extraction_output(), job_id=JOB_ID)

    assert outside.read_text() == "unchanged"


def test_rejects_hardlinked_or_non_private_existing_state(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    facts_path = state_dir / "memory-facts.jsonl"
    facts_path.write_text("")
    facts_path.chmod(0o600)
    os.link(facts_path, tmp_path / "hardlink")
    sink = CodexLocalMemorySink(state_dir, audience="private")

    with pytest.raises(PermissionError):
        sink.write(extraction_output(), job_id=JOB_ID)

    (tmp_path / "hardlink").unlink()
    facts_path.chmod(0o640)
    with pytest.raises(PermissionError):
        sink.write(extraction_output(), job_id=JOB_ID)


@pytest.mark.parametrize("audience", ["legacy", "public", "private-user-1", ""])
def test_rejects_unscoped_or_invalid_audiences(tmp_path: Path, audience: str) -> None:
    with pytest.raises(ValueError, match="audience"):
        CodexLocalMemorySink(tmp_path / "state", audience=audience)
