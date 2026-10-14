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
    provider: str = "codex",
) -> DistillExtractionOutput:
    return DistillExtractionOutput.model_validate(
        {
            "schema_version": 1,
            "provenance": {
                "provider": provider,
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


def test_piri_provenance_reaches_facts_resume_and_rollback_ledger(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    sink = CodexLocalMemorySink(state_dir, audience="private")

    sink.write(extraction_output(provider="piri"), job_id=JOB_ID)

    facts = read_facts(state_dir / "memory-facts.jsonl")
    assert facts[0]["source"]["provider"] == "piri"
    assert "provider=piri" in (state_dir / "resume.md").read_text()
    action_id = (state_dir / "memory-rollback" / "HEAD").read_text().strip()
    manifest = json.loads(
        (state_dir / "memory-rollback" / "actions" / action_id / "manifest.json").read_text()
    )
    assert manifest["provider"] == "piri"


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


def test_replay_after_partial_commit_recovers_both_targets_then_writes_once(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    sink = CodexLocalMemorySink(state_dir, audience="private")
    output = extraction_output()

    from telegram_bot.memory.local_memory_transaction import LocalMemoryTransaction

    real_write = LocalMemoryTransaction._write_target
    calls = 0

    def fail_second_write(
        transaction: LocalMemoryTransaction,
        name: str,
        payload: bytes | None,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated crash without sensitive body")
        real_write(transaction, name, payload)

    with patch.object(
        LocalMemoryTransaction,
        "_write_target",
        autospec=True,
        side_effect=fail_second_write,
    ):
        with pytest.raises(OSError, match="simulated crash"):
            sink.write(output, job_id=JOB_ID)

    replay = sink.write(output, job_id=JOB_ID)

    assert replay.facts_added == 1
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


@pytest.mark.parametrize(
    ("kind", "expected_durability"),
    [
        ("preference", "durable"),
        ("decision", "durable"),
        ("observation", "durable"),
        ("context", "durable"),
        ("procedure", "durable"),
        ("constraint", "durable"),
        ("task-progress", "volatile"),
    ],
)
def test_fact_durability_follows_kind(
    tmp_path: Path, kind: str, expected_durability: str
) -> None:
    # #871: the sink must not stamp every fact "durable" — task-progress ages
    # like the index default, retention-relevant kinds stay durable.
    sink = CodexLocalMemorySink(tmp_path / "state", audience="private")
    output = extraction_output()
    mutated = output.model_dump()
    mutated["honcho"][0]["kind"] = kind
    output = DistillExtractionOutput.model_validate(mutated)

    sink.write(output, job_id=JOB_ID)

    facts = read_facts(tmp_path / "state" / "memory-facts.jsonl")
    assert facts[0]["kind"] == kind
    assert facts[0]["durability"] == expected_durability


def _output_with_distilled_at(distilled_at: str, activity: str) -> DistillExtractionOutput:
    payload = extraction_output(last_activity=activity).model_dump(mode="json")
    payload["provenance"]["distilled_at"] = distilled_at
    return DistillExtractionOutput.model_validate(payload)


@pytest.mark.parametrize("max_resume_bytes", [256, 300, 512, 4000])
@pytest.mark.parametrize(
    "distilled_at",
    [
        "2026-10-14T00:00:00Z",
        # #2301 L1: model-authored, fromisoformat accepts unbounded fractions.
        "2026-10-14T00:00:00." + "1" * 4000 + "+00:00",
    ],
    ids=["plain", "4000-digit-fraction"],
)
def test_captured_at_survives_any_distilled_at_and_budget(
    tmp_path: Path, max_resume_bytes: int, distilled_at: str
) -> None:
    """#2301 L1/L2: the ordering key is never truncated, and an older replay
    never overwrites a newer resume — whatever the budget or model output."""
    state_dir = tmp_path / "state"
    sink = CodexLocalMemorySink(
        state_dir,
        audience="private",
        max_resume_bytes=max_resume_bytes,
        resume_never_regresses=True,
    )
    newer = sink.write(
        _output_with_distilled_at(distilled_at, "newer session " + "x" * 140),
        job_id="1" * 64,
        captured_at="2026-10-14T09:00:00Z",
    )
    resume = state_dir / "resume.md"
    payload = resume.read_bytes()
    first_line = payload.split(b"\n", 1)[0].decode()
    assert newer.resume_written
    assert len(payload) <= max_resume_bytes
    assert first_line.startswith(
        "<!-- ccc-node:distill captured_at=2026-10-14T09:00:00.000000Z "
    )
    assert first_line.endswith(" -->")
    assert "1" * 100 not in first_line

    older = sink.write(
        _output_with_distilled_at(distilled_at, "older replay"),
        job_id="2" * 64,
        captured_at="2026-10-01T09:00:00Z",
    )
    assert not older.resume_written
    assert resume.read_bytes() == payload


def test_distilled_at_is_normalized_in_header(tmp_path: Path) -> None:
    sink = CodexLocalMemorySink(tmp_path / "state", audience="private")
    sink.write(
        _output_with_distilled_at("2026-10-14T09:00:00.123456789+09:00", "activity"),
        job_id="3" * 64,
    )
    first_line = (tmp_path / "state" / "resume.md").read_text().splitlines()[0]
    # Audience sinks stamp no capture time; provenance order is unchanged.
    assert first_line == (
        "<!-- ccc-node:distill schema=1 provider=codex "
        f"thread_hash={THREAD_HASH} trigger=new_command "
        "distilled_at=2026-10-14T00:00:00.123456Z -->"
    )


def test_pre_2301_header_with_trailing_captured_at_still_orders(tmp_path: Path) -> None:
    """Resumes written before #2301 carry captured_at last; still honored."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    resume = state_dir / "resume.md"
    legacy = (
        "<!-- ccc-node:distill schema=1 provider=claude "
        f"thread_hash={THREAD_HASH} trigger=shutdown "
        "distilled_at=2026-10-13T00:00:00Z captured_at=2026-10-13T10:47:00.000000Z -->\n"
        "- 마지막 작업: legacy newer resume\n"
    )
    resume.write_text(legacy)
    resume.chmod(0o600)
    sink = CodexLocalMemorySink(
        state_dir, audience="private", resume_never_regresses=True
    )
    result = sink.write(
        extraction_output(last_activity="older replay"),
        job_id="4" * 64,
        captured_at="2026-10-01T00:00:00Z",
    )
    assert not result.resume_written
    assert resume.read_text() == legacy
    result = sink.write(
        extraction_output(last_activity="newer capture"),
        job_id="5" * 64,
        captured_at="2026-10-14T00:00:00Z",
    )
    assert result.resume_written
    assert "newer capture" in resume.read_text()


@pytest.mark.parametrize(
    "distilled_at", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:00:00-05:00"]
)
def test_out_of_range_distilled_at_never_aborts_the_write(
    tmp_path: Path, distilled_at: str
) -> None:
    """Valid ISO input that overflows on UTC conversion must not lose facts."""
    state_dir = tmp_path / "state"
    sink = CodexLocalMemorySink(
        state_dir, audience="private", resume_never_regresses=True
    )
    result = sink.write(
        _output_with_distilled_at(distilled_at, "edge offset"),
        job_id="6" * 64,
        captured_at="2026-10-14T00:00:00Z",
    )
    assert result.facts_added == 1 and result.resume_written
    first_line = (state_dir / "resume.md").read_text().splitlines()[0]
    assert first_line.startswith(
        "<!-- ccc-node:distill captured_at=2026-10-14T00:00:00.000000Z "
    )
    assert "distilled_at=" not in first_line


def test_pre_1000_capture_time_keeps_its_ordering_key(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    sink = CodexLocalMemorySink(
        state_dir, audience="private", resume_never_regresses=True
    )
    sink.write(extraction_output(), job_id="7" * 64, captured_at="0999-01-01T00:00:00Z")
    first_line = (state_dir / "resume.md").read_text().splitlines()[0]
    assert first_line.startswith("<!-- ccc-node:distill captured_at=0999-01-01T00:00:00.000000Z ")
    assert CodexLocalMemorySink._current_resume_captured_at(first_line.encode()) is not None


def test_hostile_resume_first_line_is_rejected_in_linear_time() -> None:
    import time

    hostile = ("<!-- ccc-node:distill " + "captured_at=x " * 20000).encode()
    started = time.monotonic()
    assert CodexLocalMemorySink._current_resume_captured_at(hostile) is None
    assert time.monotonic() - started < 0.5
