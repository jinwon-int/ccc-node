#!/usr/bin/env python3
"""Pure model projections and lookup helpers for agent-cron (#347)."""

from __future__ import annotations

from typing import Any


def task_by_id(data: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    for task in data.get("tasks", []):
        if isinstance(task, dict) and task.get("id") == task_id:
            return task
    return None


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Return the stable, prompt-free task listing projection."""

    tasks: list[dict[str, Any]] = []
    for task in data.get("tasks", []):
        history = task.get("runHistory", [])
        tasks.append(
            {
                "id": task.get("id"),
                "name": task.get("name") or task.get("id"),
                "schedule": task.get("schedule"),
                "enabled": bool(task.get("enabled")),
                "notify": task.get("notify", "none"),
                "allowedTools": task.get("allowedTools", []),
                "permissionMode": task.get("permissionMode") or "default",
                "attachMemory": task.get("attachMemory", []),
                "attachSkills": task.get("attachSkills", []),
                "timezone": task.get("timezone", "UTC"),
                "catchUpPolicy": task.get("catchUpPolicy", "skip"),
                "maxCatchup": task.get("maxCatchup", 1),
                "lockTimeoutSec": task.get("lockTimeoutSec", 0),
                "maxRunHistory": task.get("maxRunHistory", 20),
                "redactProfile": task.get("redactProfile", "default"),
                "lastRunAt": task.get("lastRunAt"),
                "lastStatus": task.get("lastStatus"),
                "lastRunId": task.get("lastRunId"),
                "runHistoryCount": len(history) if isinstance(history, list) else 0,
                "retryPolicy": task.get("retryPolicy"),
                "retryState": task.get("retryState"),
            }
        )
    tasks.sort(key=lambda item: item.get("id") or "")
    return {"version": 1, "tasks": tasks}


__all__ = ["normalize", "task_by_id"]
