"""Owner-only, bounded, fail-open lifecycle audit ledger (#645)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from telegram_bot.core.lifecycle_audit import LifecycleAuditLedger
from telegram_bot.core.lifecycle_observation import (
    LifecycleEventType,
    LifecycleObservation,
)


def _obs(i: int = 0) -> LifecycleObservation:
    return LifecycleObservation(
        event=LifecycleEventType.TOOL_COMPLETED,
        provider="codex",
        session_ref="sess",
        turn_ref="turn",
        tool="commandExecution",
        tool_status="success",
        correlation=f"corr-{i}",
    )


def test_record_writes_owner_only_and_dedups(tmp_path: Path) -> None:
    ledger = LifecycleAuditLedger(tmp_path / "audit")
    first = ledger.record(_obs(1))
    assert first.written and not first.deduped
    # Directory owner-only 0700, ledger file 0600.
    assert stat.S_IMODE((tmp_path / "audit").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "audit" / "lifecycle-audit.jsonl").stat().st_mode) == 0o600
    # Same observation → deduped, not appended twice.
    dup = ledger.record(_obs(1))
    assert not dup.written and dup.deduped
    lines = (tmp_path / "audit" / "lifecycle-audit.jsonl").read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["provider"] == "codex" and record["event"] == "tool_completed"
    assert "dedup" in record


def test_distinct_observations_append(tmp_path: Path) -> None:
    ledger = LifecycleAuditLedger(tmp_path / "audit")
    ledger.record(_obs(1))
    ledger.record(_obs(2))
    lines = (tmp_path / "audit" / "lifecycle-audit.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_ledger_is_bounded_to_newest_records(tmp_path: Path) -> None:
    ledger = LifecycleAuditLedger(tmp_path / "audit", max_records=3)
    for i in range(6):
        assert ledger.record(_obs(i)).written
    lines = (tmp_path / "audit" / "lifecycle-audit.jsonl").read_text().splitlines()
    assert len(lines) == 3
    corrs = {json.loads(line)["dedup"] for line in lines}
    # Newest three kept: corr-3, corr-4, corr-5.
    assert corrs == {_obs(i).dedup_key() for i in (3, 4, 5)}


def test_record_is_fail_open_on_unwritable_directory(tmp_path: Path) -> None:
    # A regular file where the ledger dir should be → writes fail, but no raise.
    blocker = tmp_path / "audit"
    blocker.write_text("not a dir")
    ledger = LifecycleAuditLedger(blocker)
    result = ledger.record(_obs(1))
    assert not result.written and result.reason == "write-error"


def test_oversize_record_is_rejected_body_free(tmp_path: Path) -> None:
    ledger = LifecycleAuditLedger(tmp_path / "audit")
    big = LifecycleObservation(
        event=LifecycleEventType.TOOL_COMPLETED, provider="codex",
        session_ref="s", tool="x" * 6000,  # blows the per-record byte bound
    )
    result = ledger.record(big)
    assert not result.written and result.reason == "oversize"
    assert not (tmp_path / "audit" / "lifecycle-audit.jsonl").exists()


def test_no_uid_leak_and_no_symlink(tmp_path: Path) -> None:
    ledger = LifecycleAuditLedger(tmp_path / "audit")
    ledger.record(_obs(1))
    meta = (tmp_path / "audit" / "lifecycle-audit.jsonl").lstat()
    assert not stat.S_ISLNK(meta.st_mode)
    if hasattr(os, "getuid"):
        assert meta.st_uid == os.getuid()
