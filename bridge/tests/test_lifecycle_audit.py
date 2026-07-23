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


class _ToolCompletedEvent:
    __name__ = "ToolCompletedEvent"

    def __init__(self, tool_name="Bash", success=True, tool_call_id="c1"):
        self.tool_name = tool_name
        self.success = success
        self.tool_call_id = tool_call_id


def test_observer_records_agent_event(tmp_path: Path) -> None:
    from telegram_bot.core.lifecycle_audit import LifecycleObserver
    ledger = LifecycleAuditLedger(tmp_path / "audit")
    observer = LifecycleObserver(ledger, provider="codex")
    ev = _ToolCompletedEvent()
    ev.__class__.__name__ = "ToolCompletedEvent"
    observer.observe(ev, session_id="s1")
    lines = (tmp_path / "audit" / "lifecycle-audit.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["event"] == "tool_completed"


def test_observer_is_fail_open_on_bad_event(tmp_path: Path) -> None:
    from telegram_bot.core.lifecycle_audit import LifecycleObserver
    observer = LifecycleObserver(LifecycleAuditLedger(tmp_path / "audit"), provider="codex")
    # Neither a known event nor a recordable one → no raise, no write.
    observer.observe(object(), session_id="s1")
    assert not (tmp_path / "audit" / "lifecycle-audit.jsonl").exists()


def test_build_observer_is_opt_in(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from telegram_bot.core.lifecycle_audit import build_lifecycle_observer, LifecycleObserver
    # Default off → no observer (a default node builds nothing).
    assert build_lifecycle_observer(SimpleNamespace(lifecycle_audit_enabled=False, agent_provider="codex")) is None
    # On + supported provider → observer.
    on = build_lifecycle_observer(
        SimpleNamespace(lifecycle_audit_enabled=True, agent_provider="codex", bot_data_dir=tmp_path)
    )
    assert isinstance(on, LifecycleObserver)
    # On but unsupported provider → None (fail-closed).
    assert build_lifecycle_observer(SimpleNamespace(lifecycle_audit_enabled=True, agent_provider="gpt", bot_data_dir=tmp_path)) is None


class _CompletionEvent:
    __name__ = "CompletionEvent"

    def __init__(self, stop_reason="end_turn"):
        self.stop_reason = stop_reason


def _tool(name, command=None):
    ev = _ToolCompletedEvent(tool_name=name)
    ev.__class__.__name__ = "ToolCompletedEvent"
    if command is not None:
        ev.arguments = {"command": command}
    return ev


def _turn():
    ev = _CompletionEvent()
    ev.__class__.__name__ = "CompletionEvent"
    return ev


def _records(tmp_path):
    p = tmp_path / "audit" / "lifecycle-audit.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def test_observer_surfaces_missing_evidence_warning(tmp_path: Path) -> None:
    from telegram_bot.core.lifecycle_audit import LifecycleObserver
    observer = LifecycleObserver(LifecycleAuditLedger(tmp_path / "audit"), provider="codex")
    observer.observe(_tool("Edit"), session_id="s1")          # file change
    observer.observe(_tool("Bash", command="rm -rf x"), session_id="s1")  # no verify
    observer.observe(_turn(), session_id="s1")                # turn end → warn
    warnings = [r for r in _records(tmp_path) if r.get("flag") == "evidence-missing"]
    assert len(warnings) == 1 and warnings[0]["event"] == "provider_notification"


def test_observer_no_warning_when_verified(tmp_path: Path) -> None:
    from telegram_bot.core.lifecycle_audit import LifecycleObserver
    observer = LifecycleObserver(LifecycleAuditLedger(tmp_path / "audit"), provider="codex")
    observer.observe(_tool("Write"), session_id="s1")
    observer.observe(_tool("Bash", command="pytest -q"), session_id="s1")  # verification
    observer.observe(_turn(), session_id="s1")
    assert not [r for r in _records(tmp_path) if r.get("flag") == "evidence-missing"]


def test_observer_no_warning_without_file_change(tmp_path: Path) -> None:
    from telegram_bot.core.lifecycle_audit import LifecycleObserver
    observer = LifecycleObserver(LifecycleAuditLedger(tmp_path / "audit"), provider="codex")
    observer.observe(_tool("Bash", command="ls"), session_id="s1")  # not a file change
    observer.observe(_turn(), session_id="s1")
    assert not [r for r in _records(tmp_path) if r.get("flag") == "evidence-missing"]


def test_observer_spools_owner_notice_when_notify_on(tmp_path: Path) -> None:
    from telegram_bot.core.lifecycle_audit import LifecycleObserver
    spool = tmp_path / "spool"
    observer = LifecycleObserver(
        LifecycleAuditLedger(tmp_path / "audit"), provider="codex",
        spool_dir=spool, notify=True,
    )
    observer.observe(_tool("Edit"), session_id="s1")
    observer.observe(_tool("Bash", command="rm -rf x"), session_id="s1")
    observer.observe(_turn(), session_id="s1")
    files = list(spool.glob("*.json"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text())
    assert rec["event"] == "LifecycleEvidenceGate" and "verification" in rec["text"]
    # Body-free: no session id or command in the notice.
    assert "s1" not in json.dumps(rec) and "rm -rf" not in json.dumps(rec)


def test_observer_does_not_spool_when_notify_off(tmp_path: Path) -> None:
    from telegram_bot.core.lifecycle_audit import LifecycleObserver
    spool = tmp_path / "spool"
    observer = LifecycleObserver(
        LifecycleAuditLedger(tmp_path / "audit"), provider="codex", spool_dir=spool, notify=False,
    )
    observer.observe(_tool("Edit"), session_id="s1")
    observer.observe(_turn(), session_id="s1")
    # Ledger warning still recorded, but no owner notice spooled.
    assert [r for r in _records(tmp_path) if r.get("flag") == "evidence-missing"]
    assert not spool.exists() or not list(spool.glob("*.json"))
