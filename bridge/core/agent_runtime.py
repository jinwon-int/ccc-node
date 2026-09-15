"""Back-compatible import shim for the provider-neutral runtime contracts.

The contracts themselves moved to :mod:`telegram_bot.contracts.agent_runtime`
(#1756) so the seam every provider adapter codes against no longer lives inside
``core``, the package that also holds the adapters and the Telegram-facing
orchestration. Nothing about the contracts changed in that move.

This module stays because ``from telegram_bot.core.agent_runtime import ...``
(and the in-package ``from .agent_runtime import ...``) appears throughout the
bridge, and a rename-everything commit would have buried a pure relocation in
call-site churn. Every public name is re-exported by ``import``, not
re-declared, so ``core.agent_runtime.TextDeltaEvent`` and
``contracts.agent_runtime.TextDeltaEvent`` are the *same class object*:
``isinstance`` checks and dataclass identity hold across either import path.

New code should import from ``telegram_bot.contracts``. See
``bridge/contracts/README.md`` for the stability policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Type checkers resolve ``bridge/`` as a source root (``mypy_path = "bridge"``
# in the repository pyproject.toml), so the real symbols are only visible to
# them under the root-relative ``contracts.*`` name; the installed distribution
# exposes the same modules under the ``telegram_bot.*`` package. Importing the
# same names both ways keeps static analysis on the genuine Protocol and
# dataclass definitions instead of silently degrading them to ``Any``, which
# would make the structural conformance assertions in the contract tests
# vacuous. This mirrors the split already used in
# ``bridge/tests/test_agent_runtime_contract.py``.
if TYPE_CHECKING:
    from contracts.agent_runtime import (
        AgentEvent,
        AgentRuntime,
        AgentSession,
        ApprovalDecision,
        ApprovalHandler,
        ApprovalRequestEvent,
        ApprovalResolvedEvent,
        AsyncCompletionCapability,
        AsyncCompletionRuntime,
        CompletionEvent,
        DelegatedTaskLifecycleEvent,
        ErrorEvent,
        JsonValue,
        MessageCompletedEvent,
        ModelInfo,
        ReasoningDeltaEvent,
        ResultEvent,
        SessionBrowser,
        SessionHistory,
        SessionHistoryMessage,
        SessionRequest,
        SessionSummary,
        TaskProgressEvent,
        TextDeltaEvent,
        ToolCompletedEvent,
        ToolStartedEvent,
        approval_target_kind,
        deny_approval,
        freeze_json,
    )
else:
    from telegram_bot.contracts.agent_runtime import (
        AgentEvent,
        AgentRuntime,
        AgentSession,
        ApprovalDecision,
        ApprovalHandler,
        ApprovalRequestEvent,
        ApprovalResolvedEvent,
        AsyncCompletionCapability,
        AsyncCompletionRuntime,
        CompletionEvent,
        DelegatedTaskLifecycleEvent,
        ErrorEvent,
        JsonValue,
        MessageCompletedEvent,
        ModelInfo,
        ReasoningDeltaEvent,
        ResultEvent,
        SessionBrowser,
        SessionHistory,
        SessionHistoryMessage,
        SessionRequest,
        SessionSummary,
        TaskProgressEvent,
        TextDeltaEvent,
        ToolCompletedEvent,
        ToolStartedEvent,
        approval_target_kind,
        deny_approval,
        freeze_json,
    )

__all__ = [
    "AgentEvent",
    "AgentRuntime",
    "AgentSession",
    "ApprovalDecision",
    "ApprovalHandler",
    "ApprovalRequestEvent",
    "ApprovalResolvedEvent",
    "AsyncCompletionCapability",
    "AsyncCompletionRuntime",
    "CompletionEvent",
    "DelegatedTaskLifecycleEvent",
    "ErrorEvent",
    "JsonValue",
    "MessageCompletedEvent",
    "ModelInfo",
    "ReasoningDeltaEvent",
    "ResultEvent",
    "SessionBrowser",
    "SessionHistory",
    "SessionHistoryMessage",
    "SessionRequest",
    "SessionSummary",
    "TaskProgressEvent",
    "TextDeltaEvent",
    "ToolCompletedEvent",
    "ToolStartedEvent",
    "approval_target_kind",
    "deny_approval",
    "freeze_json",
]
