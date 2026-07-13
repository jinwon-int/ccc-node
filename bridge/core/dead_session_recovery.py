"""Recover terminal Claude task notifications from dead persisted sessions.

The Claude CLI persists an internal FIFO as ``queue-operation`` JSONL rows.
When no live SDK stream owns a Telegram conversation, completed task wrappers
can otherwise remain on disk forever.  This module replays the FIFO without
mutating the transcript and sends only remaining terminal task notifications.

Delivery is intentionally *at least once*: the durable marker is written after
Telegram confirms ``send_message``.  A process crash between those two steps can
produce one duplicate, but cannot silently mark an unsent notification done.
Per-scan caps and bounded marker retention prevent replay storms.

A transcript that fails safety validation (``TranscriptRejected``) is
*quarantined* instead of being rescanned every tick (issue #411 B): a stable
fingerprint — session id, reason code, and the file's identity/size/mtime — is
persisted in the conversation's session record, the owner is notified once with
a redacted reason, and the file is not parsed again until its identity changes
or the session rotates.  Retention is bounded by construction: one quarantine
record per conversation, replaced on re-evaluation and cleared when the
transcript parses again.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

ENV_FLAG = "CCC_DEAD_SESSION_RECOVERY"
INTERVAL_ENV = "CCC_DEAD_SESSION_RECOVERY_INTERVAL_SECONDS"
_FALSE_VALUES = {"false", "0", "no", "off"}
MARKER_KEY = "delivered_background_notifications"
QUARANTINE_KEY = "rejected_transcript_quarantine"
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
    quarantined: int = 0
    quarantine_skipped: int = 0


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


def _transcript_file_identity(path: Optional[Path]) -> Optional[dict[str, int]]:
    """Cheap lstat-based identity used to detect transcript change without parsing."""
    if path is None:
        return None
    try:
        meta = os.lstat(path)
    except OSError:
        return None
    return {
        "dev": meta.st_dev,
        "ino": meta.st_ino,
        "size": meta.st_size,
        "mtime_ns": meta.st_mtime_ns,
    }


def transcript_quarantine_record(
    session_id: str, reason: str, path: Optional[Path]
) -> dict[str, Any]:
    """Build a persistable quarantine record for one rejected transcript.

    The fingerprint binds the exact rejection: same session, same reason code,
    same file identity.  Reason strings are the module's own constant messages
    (never transcript content), so the record is redaction-safe by
    construction.
    """
    identity = _transcript_file_identity(path)
    payload = json.dumps([session_id, reason, identity], sort_keys=True, separators=(",", ":"))
    return {
        "fingerprint": "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "identity": identity,
        "session_id": session_id,
        "reason": reason,
        "quarantined_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "notified": False,
    }


def _valid_quarantine(record: Any, session_id: str) -> Optional[dict[str, Any]]:
    if not isinstance(record, dict):
        return None
    if record.get("session_id") != session_id:
        return None
    if not isinstance(record.get("fingerprint"), str):
        return None
    if not isinstance(record.get("reason"), str):
        return None
    return record


def quarantine_blocks_scan(record: Any, session_id: str, path: Path) -> bool:
    """True when the persisted quarantine still matches the unchanged file.

    Any identity drift — new inode, different size, newer mtime — re-enables a
    bounded re-evaluation; an operator can force one by touching or replacing
    the transcript, or by rotating the session.  An unknown identity never
    blocks: it cannot prove the file is unchanged.
    """
    valid = _valid_quarantine(record, session_id)
    if valid is None:
        return False
    identity = valid.get("identity")
    if not isinstance(identity, dict):
        return False
    return _transcript_file_identity(path) == identity


def format_quarantine_notice(reason: str) -> str:
    """Owner-facing quarantine notice; carries only the constant reason code."""
    return (
        "⚠️ Background-task recovery for this conversation is quarantined.\n\n"
        f"Reason: {reason}.\n\n"
        "The stored session transcript cannot be replayed safely, so completed "
        "background results recorded there will not be re-delivered "
        "automatically. Send a new message to continue; re-run the task if you "
        "still need its result."
    )


async def _send_quarantine_notice(
    bot: Any, chat_id: int, reason: str, send_timeout: float
) -> bool:
    try:
        await asyncio.wait_for(
            bot.send_message(chat_id=chat_id, text=format_quarantine_notice(reason)),
            timeout=send_timeout,
        )
    except Exception as error:
        logger.warning(
            "Quarantine notice delivery failed for chat %s: %s",
            chat_id,
            type(error).__name__,
        )
        return False
    return True


async def _persist_quarantine(session_manager: Any, storage_key: Any, record: Any) -> bool:
    try:
        await session_manager.update_session(storage_key, {QUARANTINE_KEY: record})
    except Exception as error:
        logger.warning(
            "Quarantine persistence failed for conversation %s: %s",
            storage_key,
            type(error).__name__,
        )
        return False
    return True


async def _quarantine_transcript(
    *,
    bot: Any,
    session_manager: Any,
    storage_key: Any,
    chat_id: int,
    session_id: str,
    reason: str,
    path: Optional[Path],
    existing: Optional[dict[str, Any]],
    stats: RecoveryStats,
    can_notify: bool,
    send_timeout: float,
) -> bool:
    """Quarantine one rejected transcript; True when a send attempt was consumed.

    The first rejection for a fingerprint notifies the owner once and persists
    the record; an identical rejection (same session, reason, and file
    identity) is deduplicated silently.  A failed notice leaves
    ``notified=False`` in the persisted record so later ticks retry only the
    cheap send, never the parse.
    """
    stats.rejected += 1
    logger.warning(
        "Dead-session transcript rejected for conversation %s: %s",
        storage_key,
        reason,
    )
    record = transcript_quarantine_record(session_id, reason, path)
    if existing is not None and existing.get("fingerprint") == record["fingerprint"]:
        return False
    consumed = False
    if can_notify:
        consumed = True
        record["notified"] = await _send_quarantine_notice(bot, chat_id, reason, send_timeout)
        if not record["notified"]:
            stats.failed += 1
    if await _persist_quarantine(session_manager, storage_key, record):
        stats.quarantined += 1
    return consumed


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
            quarantine = _valid_quarantine(current.get(QUARANTINE_KEY), session_id)
            path: Optional[Path] = None
            try:
                path = _safe_transcript_path(Path(conversations_dir), session_id)
                if quarantine is not None and quarantine_blocks_scan(
                    quarantine, session_id, path
                ):
                    # Identical rejected transcript: never parse it again. Only
                    # a pending owner notice is retried, within the send budget.
                    stats.quarantine_skipped += 1
                    if (
                        not quarantine.get("notified")
                        and delivery_attempts < max_delivery_attempts_per_scan
                    ):
                        delivery_attempts += 1
                        if await _send_quarantine_notice(
                            bot, chat_id, str(quarantine["reason"]), send_timeout
                        ):
                            await _persist_quarantine(
                                session_manager,
                                storage_key,
                                dict(quarantine, notified=True),
                            )
                        else:
                            stats.failed += 1
                    continue
                notifications = await asyncio.to_thread(scan_transcript, path, session_id)
            except TranscriptRejected as error:
                if await _quarantine_transcript(
                    bot=bot,
                    session_manager=session_manager,
                    storage_key=storage_key,
                    chat_id=chat_id,
                    session_id=session_id,
                    reason=str(error) or type(error).__name__,
                    path=path,
                    existing=quarantine,
                    stats=stats,
                    can_notify=delivery_attempts < max_delivery_attempts_per_scan,
                    send_timeout=send_timeout,
                ):
                    delivery_attempts += 1
                continue
            if quarantine is not None:
                # The transcript changed and parses again — lift the stale
                # quarantine so this conversation resumes normal recovery.
                await _persist_quarantine(session_manager, storage_key, None)
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
    on_stats: Optional[Any] = None,
) -> None:
    """Run one bounded scan per interval until the lifecycle stop event is set.

    ``on_stats`` (optional sync callable) receives each tick's ``RecoveryStats``
    so the caller can surface counters (e.g. quarantined transcripts) in health
    reporting without this module importing bridge internals.
    """
    interval = recovery_interval()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            continue
        except asyncio.TimeoutError:
            pass
        try:
            stats = await recover_dead_session_notifications(
                bot, session_manager, project_handler, conversations_dir
            )
            if on_stats is not None:
                on_stats(stats)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # pragma: no cover - final fail-open boundary
            logger.warning("Dead-session recovery tick failed: %s", type(error).__name__)
