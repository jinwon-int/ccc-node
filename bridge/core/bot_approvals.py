"""Owner-only Telegram bridge for provider-neutral agent approvals."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from telegram_bot.core.agent_runtime import ApprovalDecision, ApprovalRequestEvent
from telegram_bot.core.approval_audit import (
    ApprovalAuditDecision,
    ApprovalAuditLedger,
    ApprovalAuditReason,
    ApprovalAuditRecord,
)
from telegram_bot.core.approval_contract import (
    ApprovalDisplaySnapshot,
    build_approval_snapshot,
    opaque_ref,
)

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


@dataclass(slots=True)
class _PendingCodexApproval:
    token: str
    user_id: int
    chat_id: int
    conversation_key: Any
    request_id: str
    generation: int
    snapshot: ApprovalDisplaySnapshot
    approval_ref: str
    session_ref: str
    turn_ref: str
    request_ref: str
    asked_at: str
    asked_monotonic: float
    future: asyncio.Future[ApprovalDecision]
    expires_at: float
    message_id: int | None = None
    resolved: bool = False
    terminal_audit_recorded: bool = False


class _ApprovalConfig(Protocol):
    @property
    def allowed_user_ids(self) -> Sequence[int]: ...


class _ApprovalProjectChat(Protocol):
    def invalidate_agent_approvals(
        self, user_id: int, chat_id: int | None = None
    ) -> None: ...

    def is_agent_approval_active(
        self, user_id: int, chat_id: int, generation: int
    ) -> bool: ...


class BotApprovalMixin:
    """Manage bounded, one-shot approval tokens without exposing request data."""

    _config: _ApprovalConfig
    _project_chat: _ApprovalProjectChat
    _pending_codex_approvals: dict[str, _PendingCodexApproval]
    _active_codex_approval_fingerprints: dict[
        tuple[int, int, int, str], tuple[str, str]
    ]
    _approval_audit_ledger: ApprovalAuditLedger | None
    _codex_approval_timeout_seconds: float
    _codex_approval_max_pending: int
    _bash_policy: Callable[[], str]
    _conversation_key: Callable[[int, int | None], Any]
    _require_application: Callable[
        [],
        Application[Any, Any, Any, Any, Any, Any],
    ]

    def _initialize_codex_approvals(self) -> None:
        self._pending_codex_approvals = {}
        self._active_codex_approval_fingerprints = {}
        self._codex_approval_timeout_seconds = 60.0
        self._codex_approval_max_pending = 32
        bot_data_dir = getattr(self._config, "bot_data_dir", None)
        self._approval_audit_ledger = (
            ApprovalAuditLedger(Path(bot_data_dir) / "approval-audit")
            if bot_data_dir is not None
            else None
        )

    def _approval_fingerprints(
        self,
    ) -> dict[tuple[int, int, int, str], tuple[str, str]]:
        fingerprints = getattr(self, "_active_codex_approval_fingerprints", None)
        if fingerprints is None:
            fingerprints = {}
            self._active_codex_approval_fingerprints = fingerprints
        return fingerprints

    def _approval_ledger(self) -> ApprovalAuditLedger | None:
        ledger = getattr(self, "_approval_audit_ledger", None)
        if ledger is not None:
            return ledger
        if hasattr(self, "_approval_audit_ledger"):
            return None
        bot_data_dir = getattr(getattr(self, "_config", None), "bot_data_dir", None)
        ledger = (
            ApprovalAuditLedger(Path(bot_data_dir) / "approval-audit")
            if bot_data_dir is not None
            else None
        )
        self._approval_audit_ledger = ledger
        return ledger

    @staticmethod
    def _approval_identity(
        pending: _PendingCodexApproval,
    ) -> tuple[int, int, int, str]:
        return (
            pending.user_id,
            pending.chat_id,
            pending.generation,
            pending.request_id,
        )

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )

    def _record_approval_asked(self, pending: _PendingCodexApproval) -> None:
        ledger = self._approval_ledger()
        if ledger is None:
            return
        snapshot = pending.snapshot
        ledger.record(
            ApprovalAuditRecord(
                event="asked",
                approval_ref=pending.approval_ref,
                provider=snapshot.provider,
                action=snapshot.action,
                target_shape=snapshot.target_shape,
                session_ref=pending.session_ref,
                turn_ref=pending.turn_ref,
                request_ref=pending.request_ref,
                actor_ref=None,
                request_fingerprint=snapshot.request_fingerprint,
                display_fingerprint=snapshot.display_fingerprint,
                asked_at=pending.asked_at,
                redaction_flags=snapshot.redaction_flags,
                displayed_fields=snapshot.displayed_fields,
            )
        )

    def _record_approval_terminal(
        self,
        pending: _PendingCodexApproval,
        *,
        reason: ApprovalAuditReason,
        actor_user_id: int | None,
    ) -> None:
        if pending.terminal_audit_recorded:
            return
        pending.terminal_audit_recorded = True
        ledger = self._approval_ledger()
        if ledger is None:
            return
        decision: ApprovalAuditDecision
        if reason == "owner_allow":
            decision = "allow"
        elif reason == "owner_deny":
            decision = "deny"
        elif reason == "timeout":
            decision = "timeout"
        else:
            decision = "invalidated"
        loop = asyncio.get_running_loop()
        snapshot = pending.snapshot
        ledger.record(
            ApprovalAuditRecord(
                event="answered",
                approval_ref=pending.approval_ref,
                provider=snapshot.provider,
                action=snapshot.action,
                target_shape=snapshot.target_shape,
                session_ref=pending.session_ref,
                turn_ref=pending.turn_ref,
                request_ref=pending.request_ref,
                actor_ref=(
                    opaque_ref("approval-actor", actor_user_id)
                    if actor_user_id is not None
                    else None
                ),
                request_fingerprint=snapshot.request_fingerprint,
                display_fingerprint=snapshot.display_fingerprint,
                asked_at=pending.asked_at,
                answered_at=self._utc_now(),
                decision=decision,
                reason=reason,
                latency_ms=max(0, round((loop.time() - pending.asked_monotonic) * 1000)),
                redaction_flags=snapshot.redaction_flags,
                displayed_fields=snapshot.displayed_fields,
            )
        )

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
        *,
        reason: ApprovalAuditReason | None = None,
        actor_user_id: int | None = None,
    ) -> tuple[ApprovalDecision, ApprovalAuditReason] | None:
        pending_approvals = getattr(self, "_pending_codex_approvals", None)
        if pending_approvals is None or pending.resolved:
            return None
        loop = asyncio.get_running_loop()
        active = self._project_chat.is_agent_approval_active(
            pending.user_id, pending.chat_id, pending.generation
        )
        identity = self._approval_identity(pending)
        current_fingerprints = self._approval_fingerprints().get(identity)
        expected_fingerprints = (
            pending.snapshot.request_fingerprint,
            pending.snapshot.display_fingerprint,
        )
        if reason is None:
            if current_fingerprints != expected_fingerprints:
                reason = "fingerprint_mismatch"
            elif not active:
                reason = "stale_generation"
            elif loop.time() >= pending.expires_at:
                reason = "expired"
            elif requested is ApprovalDecision.ALLOW:
                reason = "owner_allow"
            else:
                reason = "owner_deny"
        decision = (
            requested
            if reason in {"owner_allow", "owner_deny"}
            else ApprovalDecision.DENY
        )
        # Consume before resolution so button/text races and replay cannot win twice.
        if pending_approvals.pop(pending.token, None) is not pending:
            return None
        pending.resolved = True
        if self._approval_fingerprints().get(identity) == expected_fingerprints:
            self._approval_fingerprints().pop(identity, None)
        self._record_approval_terminal(
            pending,
            reason=reason,
            actor_user_id=actor_user_id,
        )
        if not pending.future.done():
            pending.future.set_result(decision)
        return decision, reason

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
        consumed = self._consume_codex_approval(
            pending,
            requested,
            actor_user_id=user_id,
        )
        if consumed is None:
            return None
        decision, reason = consumed
        if reason in {"expired", "stale_generation"}:
            return "expired"
        return "allowed" if decision is ApprovalDecision.ALLOW else "denied"

    def _admit_codex_approval_snapshot(
        self,
        *,
        user_id: int,
        chat_id: int,
        generation: int,
        request_id: str,
        snapshot: ApprovalDisplaySnapshot,
    ) -> bool:
        """Reject exact duplicates and invalidate changed same-id requests."""
        for existing in tuple(self._pending_codex_approvals.values()):
            if not (
                existing.user_id == user_id
                and existing.chat_id == chat_id
                and existing.generation == generation
                and existing.request_id == request_id
            ):
                continue
            existing_fingerprints = (
                existing.snapshot.request_fingerprint,
                existing.snapshot.display_fingerprint,
            )
            new_fingerprints = (
                snapshot.request_fingerprint,
                snapshot.display_fingerprint,
            )
            if existing_fingerprints == new_fingerprints:
                return False
            self._consume_codex_approval(
                existing,
                ApprovalDecision.DENY,
                reason="fingerprint_mismatch",
            )
        return len(self._pending_codex_approvals) < self._codex_approval_max_pending

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

        try:
            snapshot = build_approval_snapshot(event)
        except Exception:
            logger.warning("Approval snapshot rejected an unsafe provider request")
            return ApprovalDecision.DENY
        if (
            user_id != self._sole_owner_id()
            or not self._project_chat.is_agent_approval_active(user_id, chat_id, generation)
        ):
            return ApprovalDecision.DENY

        if not self._admit_codex_approval_snapshot(
            user_id=user_id,
            chat_id=chat_id,
            generation=generation,
            request_id=event.request_id,
            snapshot=snapshot,
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
        conversation_key = self._conversation_key(user_id, chat_id)
        asked_at = self._utc_now()
        pending = _PendingCodexApproval(
            token=token,
            user_id=user_id,
            chat_id=chat_id,
            conversation_key=conversation_key,
            request_id=event.request_id,
            generation=generation,
            snapshot=snapshot,
            approval_ref=opaque_ref("approval", token),
            session_ref=opaque_ref("approval-session", conversation_key),
            turn_ref=opaque_ref("approval-turn", conversation_key, generation),
            request_ref=opaque_ref("approval-request", event.request_id),
            asked_at=asked_at,
            asked_monotonic=loop.time(),
            future=future,
            expires_at=loop.time() + min(self._codex_approval_timeout_seconds, 60.0),
        )
        self._pending_codex_approvals[token] = pending
        self._approval_fingerprints()[self._approval_identity(pending)] = (
            snapshot.request_fingerprint,
            snapshot.display_fingerprint,
        )
        self._record_approval_asked(pending)
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
                text=snapshot.prompt_text,
                reply_markup=keyboard,
                parse_mode=None,
            )
            pending.message_id = getattr(message, "message_id", None)
            try:
                decision = await asyncio.wait_for(
                    future,
                    timeout=max(0.0, min(self._codex_approval_timeout_seconds, 60.0)),
                )
            except TimeoutError:
                consumed = self._consume_codex_approval(
                    pending,
                    ApprovalDecision.DENY,
                    reason="timeout",
                )
                decision = consumed[0] if consumed is not None else ApprovalDecision.DENY
            return decision if isinstance(decision, ApprovalDecision) else ApprovalDecision.DENY
        except asyncio.CancelledError:
            self._consume_codex_approval(
                pending,
                ApprovalDecision.DENY,
                reason="cancelled",
            )
            raise
        except Exception:
            logger.exception("Failed to request Codex approval through Telegram")
            self._consume_codex_approval(
                pending,
                ApprovalDecision.DENY,
                reason="send_failure",
            )
            return ApprovalDecision.DENY
        finally:
            if self._pending_codex_approvals.get(token) is pending:
                self._consume_codex_approval(
                    pending,
                    ApprovalDecision.DENY,
                    reason="cancelled",
                )
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
        return self._consume_codex_approval(
            pending,
            requested,
            actor_user_id=user_id,
        ) is not None

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
        for _token, pending in selected:
            if self._consume_codex_approval(
                pending,
                ApprovalDecision.DENY,
                reason="shutdown",
            ) is not None:
                denied += 1
        return denied
