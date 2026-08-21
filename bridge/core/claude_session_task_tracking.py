"""Body-free Claude background-task workload tracking."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import re
from typing import Any

from claude_agent_sdk import (
    TERMINAL_TASK_STATUSES as SDK_TERMINAL_TASK_STATUSES,
    Message,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    UserMessage,
)

from .agent_runtime import JsonValue


_BACKGROUND_TASK_STARTED = re.compile(
    r"\bbackground\s+(?:task\s+)?with\s+id\s*:\s*([A-Za-z0-9][A-Za-z0-9_.-]{0,255})",
    re.IGNORECASE,
)
_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
_BACKGROUND_TASK_FINISHED = re.compile(
    r"<task-notification>.*?<task-id>([^<]{1,256})</task-id>.*?"
    r"<status>(completed|failed|stopped|killed|canceled|cancelled|timed_out|timeout)"
    r"</status>.*?"
    r"</task-notification>",
    re.IGNORECASE | re.DOTALL,
)
# Transcript XML predates the SDK's typed task frames and retains broader legacy
# terminal spellings (``canceled``, ``timed_out``, and aliases). Keep that
# compatibility vocabulary in the regex above; it is intentionally independent
# of the SDK-owned typed-frame statuses and may evolve separately.


def _normalize_task_id(value: object) -> str | None:
    """Return one bounded opaque lifecycle key, never a task body."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _SAFE_TASK_ID.fullmatch(candidate) else None


class ClaudeSessionTaskTrackingMixin:
    """Track body-free Claude background-task workload for one live session."""

    _background_tasks: dict[str, float]
    _background_task_terminal_ids: set[str]
    _content_texts: Callable[[Any], list[str]]

    def background_workload_snapshot(self, now: float) -> tuple[int, float]:
        """Return body-free tracked Claude background-task workload."""

        if not self._background_tasks:
            return 0, 0.0
        oldest_started = min(self._background_tasks.values())
        return len(self._background_tasks), max(0.0, float(now) - oldest_started)

    def _observe_background_task_result(self, result: JsonValue) -> None:
        for text in self._content_texts(result):
            match = _BACKGROUND_TASK_STARTED.search(text)
            if match is not None:
                self._track_background_task_start(match.group(1))

    def _track_background_task_start(self, value: object) -> None:
        task_id = _normalize_task_id(value)
        if task_id is None or task_id in self._background_task_terminal_ids:
            return
        self._background_tasks.setdefault(task_id, asyncio.get_running_loop().time())

    def _finish_background_task(self, value: object) -> None:
        task_id = _normalize_task_id(value)
        if task_id is None:
            return
        self._background_tasks.pop(task_id, None)
        if task_id in self._background_task_terminal_ids:
            return
        self._background_task_terminal_ids.add(task_id)

    @staticmethod
    def _task_update_status(message: TaskUpdatedMessage) -> object:
        if message.status is not None:
            return message.status
        if isinstance(message.patch, Mapping):
            return message.patch.get("status")
        return None

    def _observe_background_task_system_message(self, message: SystemMessage) -> None:
        if message.subtype == "background_tasks_changed":
            self._reconcile_background_task_roster(message.data)
            return
        # Compatibility fallback for SDK parsers/stubs that preserve the raw
        # SystemMessage instead of constructing a typed subclass.
        data = message.data
        if message.subtype == "task_started":
            if data.get("task_type") == "local_bash":
                self._track_background_task_start(data.get("task_id"))
            return
        if message.subtype == "task_notification":
            self._finish_background_task(data.get("task_id"))
            return
        if message.subtype == "task_updated":
            patch = data.get("patch")
            status = patch.get("status") if isinstance(patch, Mapping) else None
            if status in SDK_TERMINAL_TASK_STATUSES:
                self._finish_background_task(data.get("task_id"))

    def _observe_background_task_notifications(self, message: Message) -> None:
        if isinstance(message, TaskStartedMessage):
            if message.task_type == "local_bash":
                self._track_background_task_start(message.task_id)
            return
        if isinstance(message, TaskNotificationMessage):
            # The SDK's TaskNotificationStatus Literal is terminal-only.
            self._finish_background_task(message.task_id)
            return
        if isinstance(message, TaskUpdatedMessage):
            if self._task_update_status(message) in SDK_TERMINAL_TASK_STATUSES:
                self._finish_background_task(message.task_id)
            return
        if isinstance(message, SystemMessage):
            self._observe_background_task_system_message(message)
            return
        if not isinstance(message, UserMessage):
            return
        for text in self._content_texts(message.content):
            for match in _BACKGROUND_TASK_FINISHED.finditer(text):
                self._finish_background_task(match.group(1))

    def _reconcile_background_task_roster(self, data: Mapping[str, Any]) -> None:
        """Replace the live shell roster, preserving known start timestamps."""

        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            # A roster is a full replacement set. Fail closed if the payload is
            # not one, rather than erasing genuine tracked work.
            return
        local_bash_ids: set[str] = set()
        for task in tasks:
            if not isinstance(task, Mapping):
                return
            task_id = _normalize_task_id(task.get("task_id"))
            task_type = task.get("task_type")
            if (
                task_id is None
                or not isinstance(task_type, str)
                or not task_type
            ):
                return
            if task_type == "local_bash":
                local_bash_ids.add(task_id)
        # A full roster removes every formerly tracked id not present. Treat
        # that removal as terminal evidence so a delayed start edge cannot
        # re-register work the authoritative level has already dropped.
        for task_id in self._background_tasks.keys() - local_bash_ids:
            self._finish_background_task(task_id)
        local_bash_ids.difference_update(self._background_task_terminal_ids)
        now = asyncio.get_running_loop().time()
        self._background_tasks = {
            task_id: self._background_tasks.get(task_id, now)
            for task_id in local_bash_ids
        }
