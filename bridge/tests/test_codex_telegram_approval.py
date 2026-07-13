"""Fail-closed Telegram lifecycle tests for Codex approvals."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from telegram_bot.core.agent_runtime import ApprovalDecision, ApprovalRequestEvent
from telegram_bot.core.bot import TelegramBot


class FakeTelegramBot:
    def __init__(self, *, send_error: Exception | None = None, edit_error: Exception | None = None):
        self.send_error = send_error
        self.edit_error = edit_error
        self.sent: list[dict[str, object]] = []
        self.edits: list[dict[str, object]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None):
        if self.send_error:
            raise self.send_error
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_reply_markup(self, **kwargs):
        if self.edit_error:
            raise self.edit_error
        self.edits.append(kwargs)


class FakeProjectChat:
    def __init__(self):
        self.active: set[tuple[int, int, int]] = set()

    def is_agent_approval_active(self, user_id: int, chat_id: int, generation: int) -> bool:
        return (user_id, chat_id, generation) in self.active


@pytest.fixture
def approval_event() -> ApprovalRequestEvent:
    return ApprovalRequestEvent(
        "provider-request-secret",
        "item/commandExecution/requestApproval",
        {
            "command": "cat /private/secret",
            "threadId": "thread-secret",
            "turnId": "turn-secret",
        },
        "provider description secret",
    )


def _subject(
    *, timeout: float = 0.2, send_error=None, edit_error=None, bash_policy: str = "approve-each"
):
    subject = TelegramBot.__new__(TelegramBot)
    subject._config = SimpleNamespace(
        allowed_user_ids=[7],
        bash_policy=bash_policy,
        execution_profile="owner-operator",
        require_allowlist=True,
    )
    subject._project_chat = FakeProjectChat()
    subject._codex_approval_timeout_seconds = timeout
    subject._codex_approval_max_pending = 4
    subject._pending_codex_approvals = {}
    telegram = FakeTelegramBot(send_error=send_error, edit_error=edit_error)
    subject.application = SimpleNamespace(bot=telegram)
    return subject, telegram


async def _wait_pending(subject: TelegramBot, count: int = 1) -> None:
    async with asyncio.timeout(1):
        while len(subject._pending_codex_approvals) != count:
            await asyncio.sleep(0)


def _callback_data(telegram: FakeTelegramBot, row: int) -> str:
    markup = telegram.sent[-1]["reply_markup"]
    return markup.inline_keyboard[0][row].callback_data


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("button", "expected"),
    [(0, ApprovalDecision.ALLOW), (1, ApprovalDecision.DENY)],
)
async def test_owner_can_resolve_once_with_opaque_bounded_token(
    approval_event: ApprovalRequestEvent, button: int, expected: ApprovalDecision
) -> None:
    subject, telegram = _subject(edit_error=RuntimeError("edit failed"))
    subject._project_chat.active.add((7, 70, 3))
    task = asyncio.create_task(subject._codex_approval_callback(70, 7, approval_event, 3))
    await _wait_pending(subject)

    data = _callback_data(telegram, button)
    assert len(data.encode("utf-8")) <= 64
    for sensitive in (
        "cat /private/secret",
        "thread-secret",
        "turn-secret",
        approval_event.request_id,
        approval_event.action,
    ):
        assert sensitive not in data
    assert telegram.sent[0]["text"] == "Codex requests approval to run a command."

    assert await subject._resolve_codex_approval(7, 70, data) is True
    assert await task is expected
    assert await subject._resolve_codex_approval(7, 70, data) is False


@pytest.mark.anyio
async def test_wrong_owner_chat_unknown_and_stale_do_not_resume(
    approval_event: ApprovalRequestEvent,
) -> None:
    subject, telegram = _subject()
    subject._project_chat.active.add((7, 70, 4))
    task = asyncio.create_task(subject._codex_approval_callback(70, 7, approval_event, 4))
    await _wait_pending(subject)
    data = _callback_data(telegram, 0)

    assert await subject._resolve_codex_approval(8, 70, data) is False
    assert await subject._resolve_codex_approval(7, 71, data) is False
    assert await subject._resolve_codex_approval(7, 70, "approval:unknown:allow") is False
    assert not task.done()

    subject._project_chat.active.clear()
    assert await subject._resolve_codex_approval(7, 70, data) is True
    assert await task is ApprovalDecision.DENY


@pytest.mark.anyio
async def test_disabled_policy_denies_without_rendering_ui(
    approval_event: ApprovalRequestEvent,
) -> None:
    subject, telegram = _subject(bash_policy="disabled")
    subject._project_chat.active.add((7, 70, 1))

    decision = await subject._codex_approval_callback(70, 7, approval_event, 1)

    assert decision is ApprovalDecision.DENY
    assert telegram.sent == []
    assert subject._pending_codex_approvals == {}


@pytest.mark.anyio
async def test_timeout_send_failure_and_capacity_are_fail_closed(
    approval_event: ApprovalRequestEvent,
) -> None:
    timed, _ = _subject(timeout=0.01)
    timed._project_chat.active.add((7, 70, 1))
    assert await timed._codex_approval_callback(70, 7, approval_event, 1) is ApprovalDecision.DENY
    assert timed._pending_codex_approvals == {}

    broken, _ = _subject(send_error=RuntimeError("send failed"))
    broken._project_chat.active.add((7, 70, 1))
    assert await broken._codex_approval_callback(70, 7, approval_event, 1) is ApprovalDecision.DENY

    full, _ = _subject()
    full._codex_approval_max_pending = 0
    full._project_chat.active.add((7, 70, 1))
    assert await full._codex_approval_callback(70, 7, approval_event, 1) is ApprovalDecision.DENY


@pytest.mark.anyio
async def test_stop_new_shutdown_and_concurrent_chats_deny_only_selected_pending(
    approval_event: ApprovalRequestEvent,
) -> None:
    subject, _ = _subject()
    subject._project_chat.active.update({(7, 70, 1), (7, 71, 2)})
    first = asyncio.create_task(subject._codex_approval_callback(70, 7, approval_event, 1))
    second = asyncio.create_task(subject._codex_approval_callback(71, 7, approval_event, 2))
    await _wait_pending(subject, 2)

    assert subject._deny_codex_approvals(7, 70) == 1
    assert await first is ApprovalDecision.DENY
    assert not second.done()
    subject.application = None
    await subject._graceful_shutdown()
    assert await second is ApprovalDecision.DENY
    assert subject._pending_codex_approvals == {}
