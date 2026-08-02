"""Owner-only approval decision audit regressions (#870)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from telegram_bot.core.approval_audit import ApprovalAuditLedger, ApprovalAuditRecord, main
from telegram_bot.core.approval_contract import opaque_ref

_ASKED_AT = "2026-08-02T00:00:00.000Z"
_ANSWERED_AT = "2026-08-02T00:00:01.000Z"
_DISPLAY_FINGERPRINT = "sha256:" + "d" * 64


def _record(
    approval: str,
    *,
    event: str = "asked",
    decision: str | None = None,
    reason: str | None = None,
    latency_ms: int | None = None,
) -> ApprovalAuditRecord:
    return ApprovalAuditRecord(
        event=event,
        approval_ref=opaque_ref("approval", approval),
        provider="codex",
        action="command_execution",
        target_shape="command",
        session_ref=opaque_ref("session", approval),
        turn_ref=opaque_ref("turn", approval),
        request_ref=opaque_ref("request", approval),
        actor_ref=opaque_ref("actor", 7) if event == "answered" else None,
        request_fingerprint=opaque_ref("request-fingerprint", approval),
        display_fingerprint=_DISPLAY_FINGERPRINT,
        asked_at=_ASKED_AT,
        answered_at=_ANSWERED_AT if event == "answered" else None,
        decision=decision,
        reason=reason,
        latency_ms=latency_ms,
        redaction_flags=("credential_redacted",),
        displayed_fields=("provider", "action", "summary"),
    )


def _lines(ledger: ApprovalAuditLedger) -> list[dict[str, object]]:
    return [json.loads(line) for line in ledger.path.read_text().splitlines()]


def test_ledger_is_owner_only_and_dedups_asked_and_terminal_phases(tmp_path: Path) -> None:
    ledger = ApprovalAuditLedger(tmp_path / "approval-audit")
    asked = _record("one")
    answered = _record(
        "one", event="answered", decision="allow", reason="owner_allow", latency_ms=23
    )

    assert ledger.record(asked).written
    assert ledger.record(asked).deduped
    assert ledger.record(answered).written
    assert ledger.record(answered).deduped
    conflicting = _record(
        "one", event="answered", decision="timeout", reason="timeout", latency_ms=40
    )
    assert ledger.record(conflicting).deduped

    assert stat.S_IMODE(ledger.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(ledger.path.stat().st_mode) == 0o600
    records = _lines(ledger)
    assert [record["event"] for record in records] == ["asked", "answered"]
    payload = json.dumps(records)
    for forbidden in ("request-1", "cat ", "Authorization", "provider description"):
        assert forbidden not in payload


def test_ledger_bounds_rows_tolerates_malformed_and_reports_body_free_metrics(
    tmp_path: Path,
) -> None:
    ledger = ApprovalAuditLedger(tmp_path / "approval-audit", max_records=8)
    assert ledger.record(_record("one")).written
    with ledger.path.open("a", encoding="utf-8") as stream:
        stream.write('not-json\n[]\n"valid-json-string"\nnull\n')
    assert ledger.record(
        _record("one", event="answered", decision="deny", reason="owner_deny", latency_ms=10)
    ).written
    assert ledger.record(_record("two")).written
    assert ledger.record(
        _record("two", event="answered", decision="timeout", reason="timeout", latency_ms=30)
    ).written

    summary = ledger.summarize()
    assert summary["ok"] is True
    assert summary["terminal_records"] == 2
    assert summary["by_action"] == {"command_execution": 2}
    assert summary["by_decision"] == {"deny": 1, "timeout": 1}
    assert summary["by_reason"] == {"owner_deny": 1, "timeout": 1}
    assert summary["latency_ms"] == {"count": 2, "average": 20.0, "maximum": 30}
    assert summary["malformed_records"] == 4
    assert len(ledger.path.read_text().splitlines()) <= 8


def test_ledger_rejects_symlink_hardlink_and_unsafe_modes(tmp_path: Path) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir(mode=0o700)
    symlink_directory = tmp_path / "linked"
    symlink_directory.symlink_to(real_directory, target_is_directory=True)
    assert ApprovalAuditLedger(symlink_directory).record(_record("symlink")).reason == "write-error"

    ledger = ApprovalAuditLedger(tmp_path / "hardlink")
    assert ledger.record(_record("hardlink-first")).written
    os.link(ledger.path, tmp_path / "approval-copy.jsonl")
    assert ledger.record(_record("hardlink-second")).reason == "write-error"

    mode_ledger = ApprovalAuditLedger(tmp_path / "unsafe-file")
    assert mode_ledger.record(_record("mode-first")).written
    mode_ledger.path.chmod(0o644)
    assert mode_ledger.record(_record("mode-second")).reason == "write-error"

    directory_ledger = ApprovalAuditLedger(tmp_path / "unsafe-directory")
    assert directory_ledger.record(_record("directory-first")).written
    directory_ledger.directory.chmod(0o755)
    assert directory_ledger.record(_record("directory-second")).reason == "write-error"


def test_json_diagnostic_is_body_free(tmp_path: Path, capsys) -> None:
    ledger = ApprovalAuditLedger(tmp_path / "approval-audit")
    assert ledger.record(
        _record("diagnostic", event="answered", decision="allow", reason="owner_allow", latency_ms=5)
    ).written

    assert main(["--directory", str(ledger.directory), "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["by_decision"] == {"allow": 1}
    assert "approval_ref" not in output
    assert "request_fingerprint" not in output
