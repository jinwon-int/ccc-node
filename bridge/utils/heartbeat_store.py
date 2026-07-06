"""Persistent registry of live heartbeat (``⏳ Working``) message ids.

The transient heartbeat message is bound to an in-memory ``_PendingRequest``.
When the bridge is SIGTERM-killed mid-request (exit 143 — a frequent event on
Android/Termux), that request dies with the process and its heartbeat message is
never deleted: the restarted bridge has no in-memory record of it, so the frozen
``⏳ Working — Nm`` line lingers as the last chat message forever.

This mirrors ``orphan_reaper`` but for Telegram messages instead of processes:
the id of every heartbeat message is recorded to a small JSON file when created
and discarded when cleanly deleted. On startup the bridge drains the file and
deletes any survivors left over from a killed run. Pure stdlib, fail-open — a
store hiccup must never break message delivery.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# (chat_id, message_id)
HeartbeatRef = Tuple[int, int]


def default_heartbeat_store_path(bot_data_dir: Path) -> Path:
    return Path(bot_data_dir) / "heartbeats.json"


def store_path_for(
    bot_data_dir: Optional[Path], override: Optional[Path] = None
) -> Optional[Path]:
    """Resolve the store path from config, or None when no data dir is known."""
    if override:
        return Path(override)
    if bot_data_dir:
        return default_heartbeat_store_path(Path(bot_data_dir))
    return None


def _read(path: Path) -> List[HeartbeatRef]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except Exception as exc:  # pragma: no cover - deliberately fail-open
        logger.warning("Heartbeat store read failed: %s", type(exc).__name__)
        return []
    try:
        data = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError:
        return []
    refs: List[HeartbeatRef] = []
    if isinstance(data, list):
        for item in data:
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and isinstance(item[0], int)
                and isinstance(item[1], int)
            ):
                refs.append((item[0], item[1]))
    return refs


def _write(path: Path, refs: List[HeartbeatRef]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    tmp.write_text(
        json.dumps([list(ref) for ref in refs], separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(tmp, destination)


def record_heartbeat(path: Path, chat_id: int, message_id: int) -> None:
    """Persist a live heartbeat message id (idempotent, fail-open)."""
    try:
        ref = (int(chat_id), int(message_id))
        refs = _read(path)
        if ref not in refs:
            refs.append(ref)
            _write(path, refs)
    except Exception as exc:  # pragma: no cover - deliberately fail-open
        logger.warning("Heartbeat store record failed: %s", type(exc).__name__)


def discard_heartbeat(path: Path, chat_id: int, message_id: int) -> None:
    """Drop a heartbeat id after it has been cleanly deleted (fail-open)."""
    try:
        ref = (int(chat_id), int(message_id))
        refs = _read(path)
        if ref in refs:
            _write(path, [r for r in refs if r != ref])
    except Exception as exc:  # pragma: no cover - deliberately fail-open
        logger.warning("Heartbeat store discard failed: %s", type(exc).__name__)


def drain_heartbeats(path: Path) -> List[HeartbeatRef]:
    """Return every stored heartbeat ref and clear the store (fail-open).

    Called once at startup: the survivors are messages from a previous run that
    died before they could be deleted, so the caller deletes them and the store
    starts empty for the new run.
    """
    refs = _read(path)
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:  # pragma: no cover - deliberately fail-open
        logger.warning("Heartbeat store drain-clear failed: %s", type(exc).__name__)
    return refs
