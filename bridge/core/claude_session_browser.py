"""Stored Claude transcript browsing for the provider-neutral runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
import re

from telegram_bot.memory.claude_snapshot import read_claude_snapshot
from telegram_bot.memory.distill_types import (
    CodexTranscriptSnapshot,
    TranscriptBounds,
    validate_memory_route,
)

from .agent_runtime import (
    SessionHistory,
    SessionHistoryMessage,
    SessionSummary,
)
from .project_chat_history import _first_text_block, iter_transcript_messages


# Session ids become transcript filenames; reject anything that could escape
# the transcripts directory (separators, a leading dot, empty).
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PREVIEW_SCAN_LIMIT = 50


class ClaudeSessionBrowserMixin:
    """SessionBrowser implementation over stored Claude SDK transcripts."""

    _transcripts_dir: Path | None

    @property
    def supports_session_browsing(self) -> bool:
        return self._transcripts_dir is not None

    async def list_sessions(self, *, limit: int = 10) -> Sequence[SessionSummary]:
        """List stored SDK transcripts newest-first, bounded and normalized."""

        directory = self._transcripts_dir
        if limit <= 0 or directory is None or not directory.is_dir():
            return ()
        bounded_limit = min(limit, 100)
        candidates: list[tuple[float, Path]] = []
        for path in directory.glob("*.jsonl"):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
        candidates.sort(key=lambda entry: entry[0], reverse=True)
        summaries: list[SessionSummary] = []
        for mtime, path in candidates[:bounded_limit]:
            summaries.append(
                SessionSummary(
                    id=path.stem,
                    preview=self._first_user_preview(path),
                    updated_at=mtime,
                )
            )
        return tuple(summaries)

    async def read_session(self, session_id: str, *, limit: int = 5) -> SessionHistory:
        """Return bounded user/assistant text from one stored transcript."""

        if not session_id:
            raise ValueError("session id must not be empty")
        directory = self._transcripts_dir
        if limit <= 0 or directory is None or not _SAFE_SESSION_ID.match(session_id):
            return SessionHistory(session_id, ())
        path = directory / f"{session_id}.jsonl"
        messages: list[SessionHistoryMessage] = []
        for _index, role, content, timestamp in iter_transcript_messages(path):
            text = _first_text_block(content)[:2000].strip()
            if not text:
                continue
            if role == "user":
                messages.append(SessionHistoryMessage("user", text, timestamp or None))
            elif role == "assistant":
                messages.append(SessionHistoryMessage("assistant", text, timestamp or None))
        return SessionHistory(session_id, tuple(messages[-min(limit, 50):]))

    async def read_session_snapshot(
        self,
        session_id: str,
        *,
        bounds: TranscriptBounds,
        memory_audience: str | None = None,
        memory_scope: str | None = None,
    ) -> CodexTranscriptSnapshot:
        """Read one Claude transcript through the shared distill snapshot seam."""

        validate_memory_route(memory_audience, memory_scope)
        if self._transcripts_dir is None or not _SAFE_SESSION_ID.fullmatch(session_id):
            raise ValueError("Claude snapshot session route is unavailable")
        return await asyncio.to_thread(
            read_claude_snapshot,
            self._transcripts_dir,
            session_id,
            bounds=bounds,
        )

    @staticmethod
    def _first_user_preview(path: Path) -> str | None:
        for scanned, (_index, _role, content, _timestamp) in enumerate(
            iter_transcript_messages(path, types=("user",))
        ):
            if scanned >= _PREVIEW_SCAN_LIMIT:
                return None
            text = _first_text_block(content).strip()
            if text and not text.startswith("<"):
                return text[:100]
        return None
