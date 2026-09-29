"""Per-turn Claude ``session_id`` -> memory audience sidecar (#1921).

Piri isolates audiences structurally: its transcripts are written under
``<audience-root>/<scope>/piri/sessions``, so the nunchi collector knows each
transcript's audience from where it lives. Claude Code has no such knob — every
bridge session, whatever Telegram/Matrix surface served it, lands in the single
``~/.claude/projects/**/<session_id>.jsonl`` tree. Without a mapping the
audience-scoped collector cannot tell an owner DM from a family room, so it
refused Claude outright and collection sat at zero.

The bridge is the only component that knows both halves at the same instant:
after every successful Claude turn it holds the SDK ``session_id`` and the
resolved route (``resolve_memory_audience``). This module records that pair as
one body-free record beside the audience's other provider state::

    <audience-root>/<scope>/claude/session-map/<session_id>.json

    {"schema": "ccc.claude.session-audience.v1", "provider": "claude",
     "session_id": "<sid>", "memory_audience": "private|shared",
     "memory_scope": "shared|private-<32 hex>", "updated_at": "<utc iso>"}

Collector contract (``claude/hooks/nunchi/claude-audience-feed.py``):

* A session is routed only when EXACTLY ONE canonical scope holds a valid
  record for it. No record -> ``unmapped``; records under two scopes ->
  ``ambiguous``; a record whose schema/provider/kind/scope/filename disagree
  with the directory it sits in, or that is not an owner-only regular file ->
  ``invalid``. All three are skipped (fail-closed) and counted, never guessed.
* The record carries no message content, no raw Telegram/Matrix ids and no
  transcript path — only the opaque scope that already names the directory.

Writes are owner-only (directories 0700, file 0600), atomic (same-directory
temp file + rename) and idempotent: every turn rewrites the same file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from telegram_bot.core.memory_audience import (
    AUDIENCE_PRIVATE,
    AUDIENCE_SHARED,
    MemoryAudience,
    resolve_memory_audience,
)
from telegram_bot.memory.distill_types import validate_memory_route
from telegram_bot.utils.secure_fs import atomic_write_bytes, ensure_private_directory, utc_now_iso

logger = logging.getLogger(__name__)

SIDECAR_SCHEMA = "ccc.claude.session-audience.v1"
SIDECAR_PROVIDER = "claude"
# Claude SDK session ids are UUIDs. Accept a conservative filename-safe superset
# (no dots, no separators) so the id can never escape the map directory.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def claude_session_map_dir(audience: MemoryAudience) -> Path:
    """Directory holding this audience's Claude session sidecars."""

    return audience.scope_root / "claude" / "session-map"


def sidecar_record(audience: MemoryAudience, session_id: str, *, updated_at: str) -> dict[str, str]:
    """Return the body-free record for one session (validated, no content)."""

    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("invalid Claude session id for audience sidecar")
    if audience.kind not in {AUDIENCE_PRIVATE, AUDIENCE_SHARED}:
        raise ValueError("invalid memory audience for Claude sidecar")
    validate_memory_route(audience.kind, audience.scope)
    return {
        "schema": SIDECAR_SCHEMA,
        "provider": SIDECAR_PROVIDER,
        "session_id": session_id,
        "memory_audience": audience.kind,
        "memory_scope": audience.scope,
        "updated_at": updated_at,
    }


def write_claude_session_audience(audience: MemoryAudience, session_id: str) -> Path:
    """Atomically record ``session_id`` -> ``audience``; return the sidecar path.

    Raises on an invalid id/route or an unsafe directory (symlinked component,
    foreign owner, group/other-writable) — callers treat any failure as "this
    turn is unmapped", which the collector skips.
    """

    record = sidecar_record(audience, session_id, updated_at=utc_now_iso())
    directory = claude_session_map_dir(audience)
    ensure_private_directory(directory)
    path = directory / f"{session_id}.json"
    payload = (json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n").encode()
    atomic_write_bytes(path, payload, mode=0o600)
    return path


async def record_claude_turn_audience(
    provider: str, audience: MemoryAudience | None, session_id: str | None
) -> bool:
    """Per-turn hook for the frontends' ``_save_session_id``; never raises.

    A no-op unless the turn ran on Claude under an audience-scoped route. A
    write failure is logged body-free (error class only — no session id, scope
    or path) and leaves the session unmapped, which the collector fails closed
    on; it must never fail the user's turn.
    """

    if provider != SIDECAR_PROVIDER or audience is None or not session_id:
        return False
    try:
        await asyncio.to_thread(write_claude_session_audience, audience, session_id)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning(
            "Claude session audience sidecar write failed error=%s "
            "(this session stays unmapped; nunchi skips it)",
            type(error).__name__,
        )
        return False
    return True


async def record_bridge_started_turn(
    settings: object,
    *,
    user_id: int,
    chat_id: int,
    response: object,
    route: str = "telegram",
) -> bool:
    """Sidecar for a bridge-started turn that bypasses ``_save_session_id``.

    The Telegram external-wait resume and continuation runners bind the turn
    to a looked-up session and run it in the record's chat, but never persist
    the session. Recording the route they actually ran under means a session
    reused across surfaces shows up as ``ambiguous`` in the collector (and is
    skipped) instead of being silently routed by its older record. Never raises.
    """

    session_id = getattr(response, "session_id", None)
    if not getattr(response, "success", False) or not session_id:
        return False
    provider = str(getattr(settings, "agent_provider", "claude")).strip().lower()
    if provider != SIDECAR_PROVIDER:
        return False
    try:
        audience = resolve_memory_audience(
            settings, user_id=int(user_id), chat_id=int(chat_id), route=route
        )
    except Exception as error:
        logger.warning(
            "Claude session audience resolve failed error=%s (session stays unmapped)",
            type(error).__name__,
        )
        return False
    return await record_claude_turn_audience(provider, audience, str(session_id))
