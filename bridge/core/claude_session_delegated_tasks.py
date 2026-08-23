"""Body-free Claude delegated-task ledger for result deferral."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from claude_agent_sdk import (
    TERMINAL_TASK_STATUSES as SDK_TERMINAL_TASK_STATUSES,
    Message,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
)

from .agent_runtime import DelegatedTaskLifecycleEvent
from .claude_session_task_tracking import (
    ClaudeSessionTaskTrackingMixin,
    _normalize_task_id,
)

if TYPE_CHECKING:
    from .claude_runtime import _ActiveTurn


# Only bounded delegated task types distinguish a result that ends one model
# turn from a result that ends the whole run. Shells, monitors, teammates, and
# remote agents may be intentionally long-lived and must not keep an interactive
# request open forever.
_RESULT_DEFERRING_TASK_TYPES = frozenset({"local_agent", "local_workflow"})


class ClaudeSessionDelegatedTasksMixin:
    """Mirror the Agent SDK run-boundary task ledger without task bodies."""

    @staticmethod
    def _delegated_task_event(
        active: _ActiveTurn,
        *,
        activity: Literal["started", "updated", "terminal"],
        now: float,
    ) -> DelegatedTaskLifecycleEvent:
        oldest_age = None
        if active.result_deferring_tasks:
            oldest_age = max(0.0, now - min(active.result_deferring_tasks.values()))
        return DelegatedTaskLifecycleEvent(
            active_count=len(active.result_deferring_tasks),
            oldest_age_seconds=oldest_age,
            activity=activity,
        )

    @staticmethod
    def _delegated_task_change(
        message: Message,
    ) -> tuple[Literal["started", "updated", "terminal"], str, object | None] | None:
        """Normalize one SDK task frame without retaining any task body."""
        if isinstance(message, TaskStartedMessage):
            task_id = _normalize_task_id(message.task_id)
            return ("started", task_id, message.task_type) if task_id is not None else None
        if isinstance(message, TaskNotificationMessage):
            task_id = _normalize_task_id(message.task_id)
            return ("terminal", task_id, None) if task_id is not None else None
        if isinstance(message, TaskUpdatedMessage):
            task_id = _normalize_task_id(message.task_id)
            if task_id is None:
                return None
            status = ClaudeSessionTaskTrackingMixin._task_update_status(message)
            typed_action: Literal["updated", "terminal"] = (
                "terminal" if status in SDK_TERMINAL_TASK_STATUSES else "updated"
            )
            return (typed_action, task_id, None)
        if not isinstance(message, SystemMessage):
            return None
        data = message.data
        task_id = _normalize_task_id(data.get("task_id"))
        if task_id is None:
            return None
        if message.subtype == "task_started":
            return ("started", task_id, data.get("task_type"))
        if message.subtype == "task_notification":
            return ("terminal", task_id, None)
        if message.subtype == "task_updated":
            patch = data.get("patch")
            status = patch.get("status") if isinstance(patch, Mapping) else None
            system_action: Literal["updated", "terminal"] = (
                "terminal" if status in SDK_TERMINAL_TASK_STATUSES else "updated"
            )
            return (system_action, task_id, None)
        return None

    @staticmethod
    def _observe_result_deferring_task(
        active: _ActiveTurn,
        message: Message,
    ) -> DelegatedTaskLifecycleEvent | None:
        """Mirror the Agent SDK's run-boundary task ledger without task bodies."""

        change = ClaudeSessionDelegatedTasksMixin._delegated_task_change(message)
        if change is None:
            return None
        action, task_id, task_type = change
        now = asyncio.get_running_loop().time()
        if action == "started":
            if (
                task_type not in _RESULT_DEFERRING_TASK_TYPES
                or task_id in active.result_deferring_terminal_tasks
                or task_id in active.result_deferring_tasks
            ):
                return None
            active.result_deferring_tasks[task_id] = now
            return ClaudeSessionDelegatedTasksMixin._delegated_task_event(
                active, activity="started", now=now
            )
        if action == "terminal":
            active.result_deferring_terminal_tasks.add(task_id)
            if active.result_deferring_tasks.pop(task_id, None) is None:
                return None
            return ClaudeSessionDelegatedTasksMixin._delegated_task_event(
                active, activity="terminal", now=now
            )
        if task_id not in active.result_deferring_tasks:
            return None
        return ClaudeSessionDelegatedTasksMixin._delegated_task_event(
            active, activity="updated", now=now
        )
