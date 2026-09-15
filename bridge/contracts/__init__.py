"""Provider-neutral runtime contracts for the bridge (#1756).

This package is the seam between the bridge orchestration and the provider
adapters. It is deliberately dependency-light: importing it must not drag in
``telegram``, the Claude/Codex SDKs, or bridge configuration, so a provider
adapter can be type-checked and unit-tested against the contract alone.

The facade below re-exports the :mod:`contracts.agent_runtime` value and
Protocol types. ``contracts.codex_runtime`` is intentionally *not* re-exported
here: it is a stable import path for a concrete adapter, and pulling it into
the package ``__init__`` would make every contract import pay for the Codex
app-server stack. Import it explicitly when you want the adapter.

See ``README.md`` in this directory for the stability policy.
"""

from __future__ import annotations

from .agent_runtime import (
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
