#!/usr/bin/env python3
"""Filesystem repository boundary for agent-cron task stores (#347)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agent_cron_schema import validate_store


def empty_doc() -> dict[str, Any]:
    return {"version": 1, "tasks": []}


def load_doc(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    if not path.exists():
        return empty_doc(), []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return None, [f"invalid JSON store: {error}"]
    errors = validate_store(data)
    return data if isinstance(data, dict) else None, errors


def write_doc(path: Path, data: dict[str, Any]) -> None:
    """Atomically replace a private task store after validating its structure."""

    errors = validate_store(data)
    if errors:
        raise ValueError(f"refusing invalid agent-cron store: {errors[0]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


__all__ = ["empty_doc", "load_doc", "write_doc"]
