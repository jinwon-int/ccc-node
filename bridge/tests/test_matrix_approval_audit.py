"""Matrix approvals post the redacted snapshot and write the audit ledger (#1959).

The Matrix room sink used to post ``description + json.dumps(arguments)`` —
raw provider arguments, secrets included — and kept no audit trail. Telegram
posts ``build_approval_snapshot`` text and records body-free asked/answered
rows in the owner-only ``ApprovalAuditLedger``. These tests pin the same
contract on Matrix.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import types
from typing import Any

import pytest

from telegram_bot.core.agent_runtime import ApprovalDecision, ApprovalRequestEvent
from telegram_bot.core.approval_contract import build_approval_snapshot
from telegram_bot.core.matrix.bot import MatrixBot
from test_matrix_bot import (
    FAMILY_ROOM,
    OWNER,
    FakeProjectChat,
    FakeSessionManager,
    _job,
    _settings,
)

pytestmark = pytest.mark.anyio

SECRETS = ("sk-live-SECRET123456", "hunter2hunter2", "AKIAZZZZZZZZ", "patch-body-secret")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def matrix_config(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    config = {
        "homeserver": "https://matrix.example.org",
        "account": "@bridge:example.org",
        "device_id": "DEV",
        "owner": OWNER,
        "rooms": [FAMILY_ROOM],
        "family_rooms": [FAMILY_ROOM],
        "family_users": [OWNER],
        "not_before_ms": 0,
    }
    module = types.ModuleType("telegram_bot.core.matrix.state")
    module.load_config = lambda path: dict(config)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram_bot.core.matrix.state", module)
    return config


class OutcomeSink:
    """Room sink exposing the #1959 ``approval_outcome`` seam."""

    def __init__(self, outcome: Any = "allow") -> None:
        self.outcome = outcome
        self.texts: list[str] = []

    async def typing(self) -> None:
        return None

    async def interim(self, text: str) -> None:
        del text

    async def status(self, text: str | None) -> None:
        del text

    async def approval(self, description: str, arguments: Any) -> bool:  # pragma: no cover - not used
        raise AssertionError("the bool form must not be used when approval_outcome exists")

    async def approval_outcome(self, text: str) -> str:
        self.texts.append(text)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return str(self.outcome)


def _secret_event() -> ApprovalRequestEvent:
    return ApprovalRequestEvent(
        request_id="req-7",
        action="bash",
        arguments={
            "command": (
                "curl -H 'Authorization: Bearer sk-live-SECRET123456' https://api.example.invalid "
                "--password hunter2hunter2 && AWS_ACCESS_KEY_ID=AKIAZZZZZZZZ make deploy"
            ),
            "patch": "patch-body-secret",
        },
        description="Run deploy",
    )


async def _decide(
    tmp_path: Path, sink: OutcomeSink, event: ApprovalRequestEvent | None = None
) -> tuple[MatrixBot, ApprovalDecision]:
    settings = _settings(tmp_path, execution_profile="strict-project")
    chat = FakeProjectChat()
    bot = MatrixBot(settings, project_chat=chat, session_manager=FakeSessionManager(), clock=None)
    seen: dict[str, Any] = {}

    async def drive(kwargs: dict[str, Any]) -> None:
        seen["decision"] = await kwargs["approval_callback"](
            kwargs["chat_id"], kwargs["user_id"], event or _secret_event(), 3
        )

    chat.on_process = drive
    await bot.run_turn(_job("go", room=FAMILY_ROOM), sink=sink, session_id=None, room_kind="family")
    return bot, seen["decision"]


def _ledger_rows(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "data" / "approval-audit" / "approval-audit.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def test_room_sees_only_the_redacted_snapshot(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    sink = OutcomeSink("allow")
    _bot, decision = await _decide(tmp_path, sink)
    assert decision is ApprovalDecision.ALLOW
    (text,) = sink.texts
    assert text == build_approval_snapshot(_secret_event(), reply_hint=None).prompt_text
    for secret in SECRETS:
        assert secret not in text
    # Matrix appends its own /approve controls; the Telegram button hint is gone.
    assert "use the buttons" not in text


async def test_allow_writes_body_free_asked_and_answered_records(
    tmp_path: Path, matrix_config: dict[str, Any]
) -> None:
    await _decide(tmp_path, OutcomeSink("allow"))
    asked, answered = _ledger_rows(tmp_path)
    assert asked["event"] == "asked" and answered["event"] == "answered"
    assert answered["decision"] == "allow" and answered["reason"] == "owner_allow"
    assert answered.get("actor_ref") and answered["approval_ref"] == asked["approval_ref"]
    snapshot = build_approval_snapshot(_secret_event(), reply_hint=None)
    assert asked["display_fingerprint"] == snapshot.display_fingerprint
    assert "sensitive_fields_omitted" in asked["redaction_flags"]
    raw = (tmp_path / "data" / "approval-audit" / "approval-audit.jsonl").read_text(encoding="utf-8")
    for secret in (*SECRETS, "curl", "deploy"):
        assert secret not in raw


@pytest.mark.parametrize(
    ("outcome", "decision", "reason", "actor"),
    [
        ("deny", ApprovalDecision.DENY, "owner_deny", True),
        ("timeout", ApprovalDecision.DENY, "timeout", False),
        ("unavailable", ApprovalDecision.DENY, "send_failure", False),
        (RuntimeError("ui"), ApprovalDecision.DENY, "send_failure", False),
    ],
)
async def test_outcomes_map_to_telegram_audit_reasons(
    tmp_path: Path, matrix_config: dict[str, Any], outcome: Any, decision: ApprovalDecision, reason: str, actor: bool
) -> None:
    _bot, got = await _decide(tmp_path, OutcomeSink(outcome))
    assert got is decision
    _asked, answered = _ledger_rows(tmp_path)
    assert answered["reason"] == reason
    assert bool(answered.get("actor_ref")) is actor


async def test_cancelled_prompt_is_audited_and_propagates(tmp_path: Path, matrix_config: dict[str, Any]) -> None:
    with pytest.raises(asyncio.CancelledError):
        await _decide(tmp_path, OutcomeSink(asyncio.CancelledError()))
    _asked, answered = _ledger_rows(tmp_path)
    assert answered["reason"] == "cancelled" and answered["decision"] == "invalidated"


async def test_audit_failure_never_changes_the_decision(
    tmp_path: Path, matrix_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from telegram_bot.core.approval_audit import ApprovalAuditLedger

    def broken(self: Any, record: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(ApprovalAuditLedger, "record", broken)
    _bot, decision = await _decide(tmp_path, OutcomeSink("allow"))
    assert decision is ApprovalDecision.ALLOW
