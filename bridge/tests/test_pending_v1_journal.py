"""Shared-core and legacy pending-v1 contracts for Claude distill recovery."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import stat
import sys

import pytest


ADAPTER_PATH = Path(__file__).parents[2] / "claude/hooks/distill/pending_journal.py"
SPEC = importlib.util.spec_from_file_location("ccc_pending_journal_test", ADAPTER_PATH)
assert SPEC is not None and SPEC.loader is not None
pending = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pending
SPEC.loader.exec_module(pending)
JsonJournalCore = pending.JsonJournalCore


def record(transcript: Path, *, session: str = "session-584") -> dict[str, object]:
    transcript_hash = hashlib.sha256(transcript.read_bytes()).hexdigest()
    job_id = pending._job_id(session, transcript_hash)
    return {
        "schema": pending.SCHEMA,
        "job_id": job_id,
        "transcript_sha256": transcript_hash,
        "session_id": session,
        "transcript_path": str(transcript),
        "source_cwd": "/workspace",
        "source_project": "-workspace",
        "trigger": "sessionend",
        "dryrun": 0,
        "created_at": "2026-08-01T00:00:00Z",
        "isolation_profile": "fleet",
        "wiki_memory_enabled": "1",
        "memory_audience_scoped": "0",
        "memory_audience": "legacy",
        "memory_scope": "",
        "honcho_memory_enabled": "1",
        "memory_user_label": "Owner",
        "memory_assistant_label": "Assistant",
    }


def test_legacy_id_is_byte_compatible_and_adapter_uses_shared_core(tmp_path: Path) -> None:
    assert pending._job_id("session-584", "a" * 64) == (
        "8cb22e36319eaa54d0d35edf6c1f85b10b7b2e16a33a25124a2d1c3c3c5c60b8"
    )
    assert issubclass(pending.PendingV1Journal, JsonJournalCore)
    assert Path(sys.modules[JsonJournalCore.__module__].__file__).resolve() == (
        Path(__file__).parents[1] / "memory/journal_core.py"
    ).resolve()

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("body", encoding="utf-8")
    journal = pending.PendingV1Journal(tmp_path / "queue")
    journal.initialize()
    job_id, created = journal.enqueue(record(transcript))

    assert created is True
    assert stat.S_IMODE(journal.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(journal.record_path(job_id).stat().st_mode) == 0o600
    assert stat.S_IMODE((journal.root / ".journal.lock").stat().st_mode) == 0o600


def test_existing_v1_without_audience_fields_drains_in_place(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("legacy body", encoding="utf-8")
    value = record(transcript)
    for field in (
        "memory_audience_scoped",
        "memory_audience",
        "memory_scope",
        "honcho_memory_enabled",
        "memory_user_label",
        "memory_assistant_label",
    ):
        value.pop(field)
    journal = pending.PendingV1Journal(tmp_path / "queue")
    journal.initialize()
    job_id = str(value["job_id"])
    with journal._exclusive():
        journal._write_json_unlocked(job_id, value)

    loaded = journal.read(job_id)

    assert loaded["memory_audience_scoped"] == "0"
    assert loaded["memory_audience"] == "legacy"
    assert journal.record_path(job_id).is_file()


def test_dedup_rejects_incompatible_environment_metadata(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("body", encoding="utf-8")
    journal = pending.PendingV1Journal(tmp_path / "queue")
    journal.initialize()
    value = record(transcript)
    journal.enqueue(value)
    collision = dict(value, isolation_profile="external")

    with pytest.raises(RuntimeError, match="metadata_collision"):
        journal.enqueue(collision)


def test_unscoped_child_removes_inherited_audience_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("body", encoding="utf-8")
    value = record(transcript)
    monkeypatch.setenv("CCC_MEMORY_AUDIENCE_SCOPED", "1")
    monkeypatch.setenv("CCC_MEMORY_AUDIENCE", "private")
    monkeypatch.setenv("CCC_MEMORY_SCOPE", "private-" + "a" * 32)

    child = pending._child_environment(value)

    assert "CCC_MEMORY_AUDIENCE_SCOPED" not in child
    assert "CCC_MEMORY_AUDIENCE" not in child
    assert "CCC_MEMORY_SCOPE" not in child


def test_scoped_routes_validate_and_reject_inherited_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("body", encoding="utf-8")
    value = dict(
        record(transcript),
        memory_audience_scoped="1",
        memory_audience="private",
        memory_scope="private-" + "a" * 32,
    )
    pending.PendingV1Journal(tmp_path / "queue").validate_record(
        str(value["job_id"]), value
    )
    monkeypatch.setenv("CCC_MEMORY_AUDIENCE", "shared")

    with pytest.raises(RuntimeError, match="memory_route_collision"):
        pending._child_environment(value)

    value["memory_scope"] = "private-raw-user"
    with pytest.raises(ValueError, match="invalid_memory_route"):
        pending.PendingV1Journal(tmp_path / "other").validate_record(
            str(value["job_id"]), value
        )


def test_held_records_do_not_consume_discovery_batch(tmp_path: Path) -> None:
    first_transcript = tmp_path / "first.jsonl"
    second_transcript = tmp_path / "second.jsonl"
    first_transcript.write_text("first", encoding="utf-8")
    second_transcript.write_text("second", encoding="utf-8")
    journal = pending.PendingV1Journal(tmp_path / "queue")
    journal.initialize()
    first_id, _ = journal.enqueue(record(first_transcript, session="first"))
    second_id, _ = journal.enqueue(record(second_transcript, session="second"))

    with journal.claim_record(first_id) as claimed:
        assert claimed
        assert journal.discover_claimable(1) == (second_id,)
        assert stat.S_IMODE(journal.claim_path(first_id).stat().st_mode) == 0o600


def test_corrupt_record_is_retained_and_diagnostic_is_body_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = pending.PendingV1Journal(tmp_path / "queue")
    journal.initialize()
    job_id = "a" * 64
    secret = "raw-transcript-secret"
    with journal._exclusive():
        journal._write_json_unlocked(job_id, {"schema": secret})

    rc = pending.main(
        ["run", str(journal.root), str(journal.record_path(job_id)), "/bin/false"]
    )

    assert rc == pending.INVALID_EXIT
    assert journal.record_path(job_id).is_file()
    assert secret not in capsys.readouterr().err
