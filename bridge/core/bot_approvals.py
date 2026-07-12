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

    async def _codex_approval_callback(
        self,
        chat_id: int,
        user_id: int,
        event: ApprovalRequestEvent,
        generation: int,
    ) -> ApprovalDecision:
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
                text=f"Codex requests approval to {label}.",
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

        loop = asyncio.get_running_loop()
        active = self._project_chat.is_agent_approval_active(
            pending.user_id, pending.chat_id, pending.generation
        )
        decision = (
            ApprovalDecision.ALLOW
            if active and loop.time() < pending.expires_at and parts[2] == "a"
            else ApprovalDecision.DENY
        )
        # Consume before resolution so replay and duplicate clicks cannot race.
        if self._pending_codex_approvals.pop(token, None) is not pending:
            return False
        if not pending.future.done():
            pending.future.set_result(decision)
        return True

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
