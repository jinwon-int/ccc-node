"""Recover terminal Claude task notifications from dead persisted sessions.

The Claude CLI persists an internal FIFO as ``queue-operation`` JSONL rows.
When no live SDK stream owns a Telegram conversation, completed task wrappers
can otherwise remain on disk forever.  This module replays the FIFO without
mutating the transcript and sends only remaining terminal task notifications.

Delivery is intentionally *at least once*: the durable marker is written after
Telegram confirms ``send_message``.  A process crash between those two steps can
produce one duplicate, but cannot silently mark an unsent notification done.
Per-scan caps and bounded marker retention prevent replay storms.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import stat
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

ENV_FLAG = "CCC_DEAD_SESSION_RECOVERY"
INTERVAL_ENV = "CCC_DEAD_SESSION_RECOVERY_INTERVAL_SECONDS"
_FALSE_VALUES = {"false", "0", "no", "off"}
MARKER_KEY = "delivered_background_notifications"
TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "canceled", "cancelled", "timeout", "timed_out"}
)
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")

DEFAULT_MAX_FILE_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_LINE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_LINES = 100_000
DEFAULT_MAX_QUEUE_DEPTH = 1_024
DEFAULT_MAX_SESSIONS = 100
DEFAULT_MAX_NOTIFICATIONS_PER_CONVERSATION = 3
DEFAULT_MAX_DELIVERY_ATTEMPTS_PER_SCAN = 10
DEFAULT_MARKER_RETENTION = DEFAULT_MAX_QUEUE_DEPTH
DEFAULT_SEND_TIMEOUT = 15.0
DEFAULT_LOCK_TIMEOUT = 0.05
DEFAULT_SCAN_INTERVAL = 60.0


class TranscriptRejected(ValueError):
    """The transcript cannot be replayed safely and must be skipped as a unit."""


@dataclass(frozen=True)
class TaskNotification:
    task_id: str
    status: str
    summary: str


@dataclass
class RecoveryStats:
    scanned: int = 0
    delivered: int = 0
    duplicate: int = 0
    failed: int = 0
    rejected: int = 0
    skipped_active: int = 0
    skipped_locked: int = 0


def recovery_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(ENV_FLAG, "true")).strip().lower() not in _FALSE_VALUES


def recovery_interval(environ: Optional[Mapping[str, str]] = None) -> float:
    env = os.environ if environ is None else environ
    try:
        value = float(env.get(INTERVAL_ENV, DEFAULT_SCAN_INTERVAL))
    except (TypeError, ValueError):
        return DEFAULT_SCAN_INTERVAL
    return min(max(value, 5.0), 3600.0)


def parse_conversation_route(value: Any) -> tuple[Any, int, int]:
    """Decode a persisted SessionStore suffix into storage key, user and chat."""
    raw = str(value)
    parts = raw.split(":")
    if len(parts) not in {1, 2} or any(not part for part in parts):
        raise ValueError("invalid Telegram conversation key")
    try:
        numbers = [int(part) for part in parts]
    except ValueError as error:
        raise ValueError("invalid Telegram conversation key") from error
    if len(numbers) == 1:
        user_id = numbers[0]
        return user_id, user_id, user_id
    return raw, numbers[0], numbers[1]


def _validated_session_id(value: Any) -> str:
    session_id = str(value or "")
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise TranscriptRejected("invalid persisted session id")
    return session_id


def _task_notification(content: Any) -> Optional[TaskNotification]:
    if not isinstance(content, str) or not content.startswith("<task-notification>"):
        return None
    if not content.endswith("</task-notification>"):
        return None
    if "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper():
        return None
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return None
    if root.tag != "task-notification" or root.attrib:
        return None

    task_nodes = root.findall("task-id")
    status_nodes = root.findall("status")
    summary_nodes = root.findall("summary")
    if len(task_nodes) != 1 or len(status_nodes) != 1 or len(summary_nodes) != 1:
        return None

    task_id = "".join(task_nodes[0].itertext()).strip()
    status_value = "".join(status_nodes[0].itertext()).strip().lower()
    summary = "".join(summary_nodes[0].itertext()).strip()
    if not task_id or len(task_id) > 256 or any(ord(char) < 32 for char in task_id):
        return None
    if status_value not in TERMINAL_STATUSES or not summary:
        return None
    return TaskNotification(task_id=task_id, status=status_value, summary=summary)


def _open_validated_transcript(path: Path, *, max_file_bytes: int):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as error:
        raise TranscriptRejected("transcript metadata unavailable") from error
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise TranscriptRejected("transcript is not a regular file")
    if metadata.st_nlink != 1:
        raise TranscriptRejected("transcript has multiple hard links")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise TranscriptRejected("transcript is not owned by the bridge user")
    if metadata.st_size > max_file_bytes:
        raise TranscriptRejected("transcript exceeds file-size limit")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise TranscriptRejected("transcript open failed") from error
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            raise TranscriptRejected("transcript changed during validation")
        return os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise


def scan_transcript(
    path: Path,
    expected_session_id: str,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    max_lines: int = DEFAULT_MAX_LINES,
    max_queue_depth: int = DEFAULT_MAX_QUEUE_DEPTH,
) -> list[TaskNotification]:
    """Replay one transcript FIFO and return remaining terminal task wrappers."""
    expected = _validated_session_id(expected_session_id)
    pending: deque[Any] = deque()
    try:
        stream = _open_validated_transcript(Path(path), max_file_bytes=max_file_bytes)
    except FileNotFoundError:
        return []

    bytes_seen = 0
    with stream:
        for line_number, raw_line in enumerate(stream, 1):
            bytes_seen += len(raw_line)
            if bytes_seen > max_file_bytes:
                raise TranscriptRejected("transcript exceeds file-size limit")
            if line_number > max_lines:
                raise TranscriptRejected("transcript exceeds line-count limit")
            if len(raw_line) > max_line_bytes:
                raise TranscriptRejected("transcript line exceeds size limit")
            try:
                row = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise TranscriptRejected(
                    "transcript contains a malformed line"
                ) from error
            if not isinstance(row, dict) or row.get("type") != "queue-operation":
                continue
            if row.get("sessionId") != expected:
                raise TranscriptRejected("queue row session id does not match owner")
            operation = row.get("operation")
            if operation == "enqueue":
                pending.append(row.get("content"))
                if len(pending) > max_queue_depth:
                    raise TranscriptRejected("transcript queue exceeds depth limit")
            elif operation in {"dequeue", "remove"}:
                if pending:
                    pending.popleft()
            else:
                raise TranscriptRejected("unknown queue operation")

    return [item for content in pending if (item := _task_notification(content))]


def notification_marker(session_id: str, item: TaskNotification) -> str:
    payload = "\0".join(
        (
            _validated_session_id(session_id),
            item.task_id,
            item.status,
            hashlib.sha256(item.summary.encode("utf-8")).hexdigest(),
        )
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _utf16_truncate(text: str, limit: int) -> str:
    encoded = text.encode("utf-16-le")
    if len(encoded) // 2 <= limit:
        return text
    suffix = "\n\n… (background result truncated)"
    suffix_units = len(suffix.encode("utf-16-le")) // 2
    raw = encoded[: max(0, limit - suffix_units) * 2]
    while raw:
        try:
            prefix = raw.decode("utf-16-le")
            break
        except UnicodeDecodeError:
            raw = raw[:-2]
    else:
        prefix = ""
    return prefix + suffix


def format_notification(item: TaskNotification) -> str:
    if item.status == "completed":
        prefix = "✅ Background task completed"
    elif item.status in {"canceled", "cancelled"}:
        prefix = "🛑 Background task canceled"
    elif item.status in {"timeout", "timed_out"}:
        prefix = "⏱️ Background task timed out"
    else:
        prefix = "❌ Background task failed"
    return _utf16_truncate(f"{prefix}\n\n{item.summary}", 4000)


def _safe_transcript_path(conversations_dir: Path, session_id: str) -> Path:
    session = _validated_session_id(session_id)
    try:
        root = Path(conversations_dir).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise TranscriptRejected("conversation root unavailable") from error
    raw = root / f"{session}.jsonl"
    try:
        resolved = raw.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise TranscriptRejected("unsafe transcript path") from error
    return raw


def _live_stream_owned(handler: Any, user_id: int, chat_id: int) -> bool:
    state = getattr(handler, "_streams", {}).get((user_id, chat_id))
    if state is None:
        return False
    reader = getattr(state, "reader_task", None)
    return bool(reader is not None and not reader.done())


async def recover_dead_session_notifications(
    bot: Any,
    session_manager: Any,
    project_handler: Any,
    conversations_dir: Optional[Path],
    *,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    max_notifications_per_conversation: int = DEFAULT_MAX_NOTIFICATIONS_PER_CONVERSATION,
    max_delivery_attempts_per_scan: int = DEFAULT_MAX_DELIVERY_ATTEMPTS_PER_SCAN,
    marker_retention: int = DEFAULT_MARKER_RETENTION,
    send_timeout: float = DEFAULT_SEND_TIMEOUT,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> RecoveryStats:
    """Scan persisted dead sessions once and deliver bounded terminal notices."""
    stats = RecoveryStats()
    if not recovery_enabled() or not conversations_dir:
        return stats
    try:
        sessions = await session_manager.list_sessions()
    except Exception as error:
        logger.warning("Dead-session recovery could not enumerate sessions: %s", type(error).__name__)
        stats.rejected += 1
        return stats
    if not isinstance(sessions, dict):
        stats.rejected += 1
        return stats

    delivery_attempts = 0
    for raw_key, snapshot in sorted(sessions.items(), key=lambda item: str(item[0]))[
        :max_sessions
    ]:
        try:
            storage_key, user_id, chat_id = parse_conversation_route(raw_key)
            if not isinstance(snapshot, dict):
                raise ValueError("invalid session entry")
            session_id = _validated_session_id(snapshot.get("session_id"))
        except (ValueError, TranscriptRejected):
            stats.rejected += 1
            continue

        lock = project_handler._get_conversation_lock(user_id, chat_id)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=lock_timeout)
        except asyncio.TimeoutError:
            stats.skipped_locked += 1
            continue
        try:
            if _live_stream_owned(project_handler, user_id, chat_id):
                stats.skipped_active += 1
                continue
            try:
                current = await session_manager.get_session(storage_key)
            except Exception:
                stats.rejected += 1
                continue
            if current.get("session_id") != session_id:
                continue
            markers_value = current.get(MARKER_KEY, [])
            if not isinstance(markers_value, list) or any(
                not isinstance(value, str) for value in markers_value
            ):
                stats.rejected += 1
                continue
            markers = list(markers_value[-marker_retention:])
            try:
                path = _safe_transcript_path(Path(conversations_dir), session_id)
                notifications = await asyncio.to_thread(scan_transcript, path, session_id)
            except TranscriptRejected as error:
                logger.warning(
                    "Dead-session transcript rejected for conversation %s: %s",
                    storage_key,
                    type(error).__name__,
                )
                stats.rejected += 1
                continue
            stats.scanned += 1
            sent_here = 0
            for item in notifications:
                marker = notification_marker(session_id, item)
                if marker in markers:
                    stats.duplicate += 1
                    continue
                if sent_here >= max_notifications_per_conversation:
                    break
                if delivery_attempts >= max_delivery_attempts_per_scan:
                    return stats
                delivery_attempts += 1
                try:
                    await asyncio.wait_for(
                        bot.send_message(chat_id=chat_id, text=format_notification(item)),
                        timeout=send_timeout,
                    )
                except Exception as error:
                    logger.warning(
                        "Dead-session Telegram delivery failed for conversation %s: %s",
                        storage_key,
                        type(error).__name__,
                    )
                    stats.failed += 1
                    break
                markers.append(marker)
                markers = markers[-marker_retention:]
                try:
                    await session_manager.update_session(
                        storage_key, {MARKER_KEY: list(markers)}
                    )
                except Exception as error:
                    logger.warning(
                        "Dead-session marker persistence failed for conversation %s: %s",
                        storage_key,
                        type(error).__name__,
                    )
                    stats.failed += 1
                    break
                stats.delivered += 1
                sent_here += 1
        finally:
            lock.release()
    return stats


async def run_periodic_dead_session_recovery(
    bot: Any,
    session_manager: Any,
    project_handler: Any,
    conversations_dir: Optional[Path],
    stop_event: asyncio.Event,
) -> None:
    """Run one bounded scan per interval until the lifecycle stop event is set."""
    interval = recovery_interval()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            continue
        except asyncio.TimeoutError:
            pass
        try:
            await recover_dead_session_notifications(
                bot, session_manager, project_handler, conversations_dir
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # pragma: no cover - final fail-open boundary
            logger.warning("Dead-session recovery tick failed: %s", type(error).__name__)
