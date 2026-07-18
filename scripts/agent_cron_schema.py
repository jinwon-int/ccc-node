#!/usr/bin/env python3
"""Schema-derived, fail-closed validation for agent-cron task stores.

The checked-in JSON Schema is the structural source of truth.  This module
implements only the draft-2020-12 keywords used by that schema so agent-cron
does not depend on an optional system Python package at runtime.
"""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "agent-cron-task-store.schema.json"
)


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    """Load the repository-owned schema without mutating external state."""

    value = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("agent-cron schema must be a JSON object")
    return value


def _location(path: str) -> str:
    return path or "store"


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _json_type_matches(value: object, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return type(value) is bool
    if expected == "integer":
        return type(value) is int
    if expected == "number":
        return type(value) in {int, float}
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    raise ValueError(f"unsupported schema type: {expected}")


def _validate_type(value: object, schema: dict[str, Any], path: str) -> list[str]:
    declared = schema.get("type")
    if declared is None:
        return []
    expected = [declared] if isinstance(declared, str) else declared
    if not isinstance(expected, list) or not all(isinstance(item, str) for item in expected):
        raise ValueError(f"invalid type declaration at {_location(path)}")
    if any(_json_type_matches(value, item) for item in expected):
        return []
    return [f"{_location(path)} must be of type {' or '.join(expected)}"]


def _validate_scalar(value: object, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    location = _location(path)
    if "const" in schema and type(value) is not type(schema["const"]):
        errors.append(f"{location} must equal {schema['const']!r}")
    elif "const" in schema and value != schema["const"]:
        errors.append(f"{location} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{location} must be one of {schema['enum']!r}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{location} must contain at least {schema['minLength']} character(s)")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            errors.append(f"{location} must match {pattern}")
    if type(value) is int and "minimum" in schema and value < schema["minimum"]:
        errors.append(f"{location} must be at least {schema['minimum']}")
    if type(value) is int and "maximum" in schema and value > schema["maximum"]:
        errors.append(f"{location} must be at most {schema['maximum']}")
    return errors


def _validate_object(value: dict[str, Any], schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    for key in required:
        if key not in value:
            errors.append(f"{_join(path, key)} is required")
    if schema.get("additionalProperties") is False:
        for key in value:
            if key not in properties:
                errors.append(f"{_join(path, key)} is not allowed")
    for key, item in value.items():
        child_schema = properties.get(key)
        if isinstance(child_schema, dict):
            errors.extend(_validate_node(item, child_schema, _join(path, key)))
    return errors


def _validate_array(value: list[Any], schema: dict[str, Any], path: str) -> list[str]:
    item_schema = schema.get("items")
    if not isinstance(item_schema, dict):
        return []
    errors: list[str] = []
    for index, item in enumerate(value):
        errors.extend(_validate_node(item, item_schema, f"{path}[{index}]"))
    return errors


def _validate_node(value: object, schema: dict[str, Any], path: str) -> list[str]:
    type_errors = _validate_type(value, schema, path)
    if type_errors:
        return type_errors
    errors = _validate_scalar(value, schema, path)
    if isinstance(value, dict):
        errors.extend(_validate_object(value, schema, path))
    elif isinstance(value, list):
        errors.extend(_validate_array(value, schema, path))
    return errors


def _duplicate_id_errors(value: object) -> list[str]:
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), list):
        return []
    seen: set[str] = set()
    errors: list[str] = []
    for task in value["tasks"]:
        task_id = task.get("id") if isinstance(task, dict) else None
        if isinstance(task_id, str) and task_id in seen:
            errors.append(f"duplicate task id: {task_id}")
        elif isinstance(task_id, str):
            seen.add(task_id)
    return errors


def _payload_errors(value: object) -> list[str]:
    """Cross-field payload rules the keyword subset cannot express."""

    if not isinstance(value, dict) or not isinstance(value.get("tasks"), list):
        return []
    errors: list[str] = []
    for index, task in enumerate(value["tasks"]):
        if not isinstance(task, dict):
            continue
        payload = task.get("payload")
        if not isinstance(payload, dict):
            continue
        location = f"tasks[{index}].payload"
        kind = payload.get("kind")
        if kind == "command":
            if not payload.get("argv"):
                errors.append(f"{location}.argv is required for kind 'command'")
            if "model" in payload:
                errors.append(f"{location}.model is not allowed for kind 'command'")
        elif kind == "prompt":
            for forbidden in ("argv", "cwd"):
                if forbidden in payload:
                    errors.append(
                        f"{location}.{forbidden} is not allowed for kind 'prompt'"
                    )
    return errors


def validate_store(value: object) -> list[str]:
    """Return stable structural and store-semantic validation errors."""

    return (
        _validate_node(value, load_schema(), "")
        + _duplicate_id_errors(value)
        + _payload_errors(value)
    )


__all__ = ["SCHEMA_PATH", "load_schema", "validate_store"]
