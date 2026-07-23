"""Fail-open Claude hook → lifecycle ledger CLI (#645)."""

from __future__ import annotations

import io
import json
from pathlib import Path

from telegram_bot.core import lifecycle_hook

GH_TOKEN = "ghp_" + "a" * 30


def _run(monkeypatch, tmp_path, event, payload, *, enabled=True):
    monkeypatch.setenv("CCC_LIFECYCLE_AUDIT", "true" if enabled else "false")
    monkeypatch.setenv("CCC_LIFECYCLE_AUDIT_DIR", str(tmp_path / "ledger"))
    stdin = io.StringIO(json.dumps(payload) if payload is not None else "not json")
    rc = lifecycle_hook.main(["lifecycle_hook", event], stdin=stdin)
    ledger = tmp_path / "ledger" / "lifecycle-audit.jsonl"
    records = [json.loads(line) for line in ledger.read_text().splitlines()] if ledger.exists() else []
    return rc, records


def test_disabled_is_a_noop(monkeypatch, tmp_path) -> None:
    rc, records = _run(monkeypatch, tmp_path, "PostToolUse",
                        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s"},
                        enabled=False)
    assert rc == 0 and records == []


def test_mutating_tool_is_recorded(monkeypatch, tmp_path) -> None:
    rc, records = _run(monkeypatch, tmp_path, "PostToolUse",
                       {"tool_name": "Edit", "tool_input": {"file_path": "/x"}, "session_id": "s"})
    assert rc == 0 and len(records) == 1
    assert records[0]["event"] == "tool_completed" and records[0]["provider"] == "claude"


def test_read_only_tool_is_not_recorded(monkeypatch, tmp_path) -> None:
    rc, records = _run(monkeypatch, tmp_path, "PostToolUse",
                       {"tool_name": "Read", "session_id": "s"})
    assert rc == 0 and records == []


def test_credential_prompt_flags_body_free(monkeypatch, tmp_path) -> None:
    rc, records = _run(monkeypatch, tmp_path, "UserPromptSubmit",
                       {"prompt": "token " + GH_TOKEN, "session_id": "s"})
    assert rc == 0 and len(records) == 1
    assert records[0]["flag"] == "possible-raw-credential"
    assert GH_TOKEN not in json.dumps(records[0])


def test_malformed_stdin_is_a_noop(monkeypatch, tmp_path) -> None:
    rc, records = _run(monkeypatch, tmp_path, "PostToolUse", None)  # invalid JSON
    assert rc == 0 and records == []


def test_unknown_event_is_a_noop(monkeypatch, tmp_path) -> None:
    rc, records = _run(monkeypatch, tmp_path, "Nope", {"session_id": "s"})
    assert rc == 0 and records == []
