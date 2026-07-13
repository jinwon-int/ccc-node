"""Owner-only Telegram bridge for provider-neutral agent approvals."""

# mypy: disable-error-code="attr-defined"

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_bot.core.agent_runtime import ApprovalDecision, ApprovalRequestEvent

logger = logging.getLogger(__name__)

_APPROVAL_PREFIX = "ca"
_APPROVAL_ALLOW_TEXT = frozenset(
    {
        "승인",
        "승인해",
        "허용",
        "허용해",
        "진행",
        "진행해",
        "approve",
        "allow",
        "yes",
        "go ahead",
    }
)
_APPROVAL_DENY_TEXT = frozenset(
    {
        "거절",
        "거절해",
        "취소",
        "취소해",
        "중단",
        "중단해",
        "하지마",
        "하지 마",
        "안돼",
        "deny",
        "cancel",
        "reject",
        "no",
        "stop",
    }
)
_ACTION_LABELS = {
    "item/commandExecution/requestApproval": "run a command",
    "item/fileChange/requestApproval": "change files",
    "item/permissions/requestApproval": "grant permissions",
}


@dataclass(slots=True)
class _PendingCodexApproval:
    token: str
    user_id: int
    chat_id: int
    conversation_key: Any
    request_id: str
    generation: int
    future: asyncio.Future[ApprovalDecision]
    expires_at: float
    message_id: int | None = None


class BotApprovalMixin:
    """Manage bounded, one-shot approval tokens without exposing request data."""

    def _initialize_codex_approvals(self) -> None:
        self._pending_codex_approvals: dict[str, _PendingCodexApproval] = {}
        self._codex_approval_timeout_seconds = 60.0
        self._codex_approval_max_pending = 32

    def _invalidate_codex_approvals(self, user_id: int, chat_id: int) -> None:
        invalidate = getattr(self._project_chat, "invalidate_agent_approvals", None)
        if callable(invalidate):
            invalidate(user_id, chat_id)

    def _sole_owner_id(self) -> int | None:
        owners: Any = getattr(self._config, "allowed_user_ids", ())
        if len(owners) != 1:
            return None
        owner = owners[0]
        return owner if isinstance(owner, int) else None

    @staticmethod
    def _approval_callback_data(token: str, decision: ApprovalDecision) -> str:
        suffix = "a" if decision is ApprovalDecision.ALLOW else "d"
        data = f"{_APPROVAL_PREFIX}:{token}:{suffix}"
        if len(data.encode("utf-8")) > 64:
            raise ValueError("Telegram approval callback data exceeds 64 bytes")
        return data

    @staticmethod
    def _approval_text_decision(text: str) -> ApprovalDecision | None:
        normalized = " ".join(text.strip().casefold().split()).rstrip(".!。！")
        if normalized in _APPROVAL_ALLOW_TEXT:
            return ApprovalDecision.ALLOW
        if normalized in _APPROVAL_DENY_TEXT:
            return ApprovalDecision.DENY
        return None

    def _consume_codex_approval(
        self,
        pending: _PendingCodexApproval,
        requested: ApprovalDecision,
    ) -> tuple[ApprovalDecision, bool] | None:
        pending_approvals = getattr(self, "_pending_codex_approvals", None)
        if pending_approvals is None:
            return None
        loop = asyncio.get_running_loop()
        active = self._project_chat.is_agent_approval_active(
            pending.user_id, pending.chat_id, pending.generation
        )
        expired = not active or loop.time() >= pending.expires_at
        decision = ApprovalDecision.DENY if expired else requested
        # Consume before resolution so button/text races and replay cannot win twice.
        if pending_approvals.pop(pending.token, None) is not pending:
            return None
        if not pending.future.done():
            pending.future.set_result(decision)
        return decision, expired

    async def _resolve_codex_approval_text(
        self, user_id: int, chat_id: int, text: str
    ) -> str | None:
        requested = self._approval_text_decision(text)
        if requested is None or user_id != self._sole_owner_id():
            return None
        pending_approvals = getattr(self, "_pending_codex_approvals", None)
        if not pending_approvals:
            return None
        conversation_key = self._conversation_key(user_id, chat_id)
        matches = [
            pending
            for pending in pending_approvals.values()
            if pending.user_id == user_id
            and pending.chat_id == chat_id
            and pending.conversation_key == conversation_key
        ]
        if not matches:
            return None
        if len(matches) != 1:
            return "ambiguous"

        pending = matches[0]
        consumed = self._consume_codex_approval(pending, requested)
        if consumed is None:
            return None
        decision, expired = consumed
        if expired:
            return "expired"
        return "allowed" if decision is ApprovalDecision.ALLOW else "denied"

    async def _codex_approval_callback(
        self,
        chat_id: int,
        user_id: int,
        event: ApprovalRequestEvent,
        generation: int,
    ) -> ApprovalDecision:
        bash_policy = self._bash_policy()
        if bash_policy == "auto-approve":
            return ApprovalDecision.ALLOW
        if bash_policy != "approve-each":
            return ApprovalDecision.DENY

        label = _ACTION_LABELS.get(event.action)
        if (
            label is None
            or user_id != self._sole_owner_id()
            or len(self._pending_codex_approvals) >= self._codex_approval_max_pending
            or not self._project_chat.is_agent_approval_active(user_id, chat_id, generation)
        ):
            return ApprovalDecision.DENY

        if any(
            pending.user_id == user_id
            and pending.chat_id == chat_id
            and pending.generation == generation
            and pending.request_id == event.request_id
            for pending in self._pending_codex_approvals.values()
        ):
            return ApprovalDecision.DENY

        loop = asyncio.get_running_loop()
        token = ""
        for _ in range(4):
            candidate = secrets.token_urlsafe(18)
            if candidate not in self._pending_codex_approvals:
                token = candidate
                break
        if not token:
            return ApprovalDecision.DENY
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        pending = _PendingCodexApproval(
            token=token,
            user_id=user_id,
            chat_id=chat_id,
            conversation_key=self._conversation_key(user_id, chat_id),
            request_id=event.request_id,
            generation=generation,
            future=future,
            expires_at=loop.time() + min(self._codex_approval_timeout_seconds, 60.0),
        )
        self._pending_codex_approvals[token] = pending
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "Allow once",
                    callback_data=self._approval_callback_data(token, ApprovalDecision.ALLOW),
                ),
                InlineKeyboardButton(
                    "Deny",
                    callback_data=self._approval_callback_data(token, ApprovalDecision.DENY),
                ),
            ]]
        )
        try:
            app = self._require_application()
            message = await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"Codex requests approval to {label}.\n"
                    "Reply with 승인 or 거절, or use the buttons."
                ),
                reply_markup=keyboard,
            )
            pending.message_id = getattr(message, "message_id", None)
            try:
                decision = await asyncio.wait_for(
                    future,
                    timeout=max(0.0, min(self._codex_approval_timeout_seconds, 60.0)),
                )
            except TimeoutError:
                decision = ApprovalDecision.DENY
            return decision if isinstance(decision, ApprovalDecision) else ApprovalDecision.DENY
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to request Codex approval through Telegram")
            return ApprovalDecision.DENY
        finally:
            if self._pending_codex_approvals.get(token) is pending:
                self._pending_codex_approvals.pop(token, None)
            await self._cleanup_codex_approval(pending)

    async def _cleanup_codex_approval(self, pending: _PendingCodexApproval) -> None:
        if pending.message_id is None:
            return
        try:
            app = self._require_application()
            await asyncio.wait_for(
                app.bot.edit_message_reply_markup(
                    chat_id=pending.chat_id,
                    message_id=pending.message_id,
                    reply_markup=None,
                ),
                timeout=2.0,
            )
        except Exception:
            logger.debug("Could not remove Codex approval buttons", exc_info=True)

    async def _resolve_codex_approval(self, user_id: int, chat_id: int, data: str) -> bool:
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != _APPROVAL_PREFIX or parts[2] not in {"a", "d"}:
            return False
        token = parts[1]
        pending = self._pending_codex_approvals.get(token)
        if pending is None:
            return False
        if (
            user_id != self._sole_owner_id()
            or pending.user_id != user_id
            or pending.chat_id != chat_id
            or pending.conversation_key != self._conversation_key(user_id, chat_id)
        ):
            return False

        requested = (
            ApprovalDecision.ALLOW if parts[2] == "a" else ApprovalDecision.DENY
        )
        return self._consume_codex_approval(pending, requested) is not None

    def _deny_codex_approvals(
        self, user_id: int | None = None, chat_id: int | None = None
    ) -> int:
        pending_approvals = getattr(self, "_pending_codex_approvals", None)
        if pending_approvals is None:
            return 0
        selected = [
            (token, pending)
            for token, pending in pending_approvals.items()
            if (user_id is None or pending.user_id == user_id)
            and (chat_id is None or pending.chat_id == chat_id)
        ]
        denied = 0
        for token, pending in selected:
            if pending_approvals.pop(token, None) is not pending:
                continue
            denied += 1
            if not pending.future.done():
                pending.future.set_result(ApprovalDecision.DENY)
        return denied
