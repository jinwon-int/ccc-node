"""Local duration logging and simple forecast helpers for bridge requests."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)
_WARNED_ERROR_CLASSES: set[str] = set()


def _warn_once(error: Exception) -> None:
    key = type(error).__name__
    if key in _WARNED_ERROR_CLASSES:
        return
    _WARNED_ERROR_CLASSES.add(key)
    logger.warning("Duration log operation failed: %s", key)


def default_duration_log_path(bot_data_dir: Path) -> Path:
    return Path(bot_data_dir) / "duration.jsonl"


def _sample_record(
    *,
    user_id: int,
    chat_id: int,
    session_id: Optional[str],
    model: Optional[str],
    duration_ms: Optional[int],
    success: bool,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    return {
        "ts": (now or datetime.now(timezone.utc)).isoformat(),
        "user_id": user_id,
        "chat_id": chat_id,
        "session_id": session_id,
        "model": model,
        "duration_ms": int(duration_ms) if duration_ms is not None else None,
        "success": bool(success),
    }


def _trim_jsonl(path: Path, max_lines: int) -> None:
    if max_lines <= 0 or not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) <= max_lines:
        return
    path.write_text("\n".join(lines[-max_lines:]) + "\n", encoding="utf-8")


def append_duration_sample(
    *,
    path: Path,
    user_id: int,
    chat_id: int,
    session_id: Optional[str],
    model: Optional[str],
    duration_ms: Optional[int],
    success: bool,
    max_lines: int = 10000,
    now: Optional[datetime] = None,
) -> Optional[Path]:
    """Append one duration sample to JSONL, fail-open on I/O errors."""
    try:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        record = _sample_record(
            user_id=user_id,
            chat_id=chat_id,
            session_id=session_id,
            model=model,
            duration_ms=duration_ms,
            success=success,
            now=now,
        )
        with destination.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        _trim_jsonl(destination, max_lines=max_lines)
        return destination
    except Exception as exc:  # pragma: no cover - deliberately fail-open
        _warn_once(exc)
        return None


def _iter_samples(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row
    except FileNotFoundError:
        return
    except Exception as exc:  # pragma: no cover - deliberately fail-open
        _warn_once(exc)
        return


def recent_samples(
    path: Path,
    *,
    limit: int = 100,
    user_id: Optional[int] = None,
    model: Optional[str] = None,
    success_only: bool = True,
) -> list[dict[str, Any]]:
    """Return recent duration samples, optionally filtered by user/model."""
    rows = []
    for row in _iter_samples(path):
        if success_only and row.get("success") is not True:
            continue
        if user_id is not None and row.get("user_id") != user_id:
            continue
        if model is not None and row.get("model") != model:
            continue
        if isinstance(row.get("duration_ms"), int) and row["duration_ms"] >= 0:
            rows.append(row)
    return rows[-max(0, limit):]


def forecast_ms(
    path: Path,
    *,
    user_id: Optional[int] = None,
    model: Optional[str] = None,
    min_samples: int = 10,
    limit: int = 200,
) -> Optional[int]:
    """Return a median duration forecast using exact, user, then global fallback."""
    filters = []
    if user_id is not None and model is not None:
        filters.append({"user_id": user_id, "model": model})
    if user_id is not None:
        filters.append({"user_id": user_id, "model": None})
    filters.append({"user_id": None, "model": None})

    for flt in filters:
        rows = recent_samples(
            path,
            limit=limit,
            user_id=flt["user_id"],
            model=flt["model"],
            success_only=True,
        )
        values = [row["duration_ms"] for row in rows]
        if len(values) >= min_samples:
            return int(median(values))
    return None
