"""Security and bounds tests for Piri transcript snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest

from telegram_bot.memory.distill_types import TranscriptBounds
from telegram_bot.memory.piri_snapshot import read_piri_snapshot


def _write_session(directory: Path, session_id: str, entries: list[dict]) -> Path:
    directory.mkdir(mode=0o700)
    path = directory / f"2026-08-05T00-00-00-000Z_{session_id}.jsonl"
    values = [
        {
            "type": "session",
            "version": 3,
            "id": session_id,
            "timestamp": "2026-08-05T00:00:00Z",
            "cwd": "/workspace",
        },
        *entries,
    ]
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in values),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_reads_only_bounded_user_and_assistant_text(tmp_path: Path) -> None:
    session_id = "piri-session"
    session_dir = tmp_path / "sessions"
    _write_session(
        session_dir,
        session_id,
        [
            {
                "type": "message",
                "id": "m1",
                "timestamp": "2026-08-05T00:00:01Z",
                "message": {"role": "user", "content": "remember this"},
            },
            {
                "type": "message",
                "id": "tool",
                "timestamp": "2026-08-05T00:00:02Z",
                "message": {"role": "toolResult", "content": "raw tool output"},
            },
            {
                "type": "message",
                "id": "m2",
                "timestamp": "2026-08-05T00:00:03Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "stored safely"}],
                },
            },
        ],
    )

    snapshot = read_piri_snapshot(
        session_dir,
        session_id,
        bounds=TranscriptBounds(max_messages=2, max_bytes=128),
        now=datetime(2026, 8, 5, 0, 1, tzinfo=timezone.utc),
    )

    assert [(item.role, item.text) for item in snapshot.messages] == [
        ("user", "remember this"),
        ("assistant", "stored safely"),
    ]
    assert snapshot.last_turn_id == "m2"
    assert snapshot.byte_count == len("remember thisstored safely".encode())


def test_rejects_identity_mismatch_and_symlinks(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions"
    path = _write_session(session_dir, "expected", [])
    payload = path.read_text().replace('"id": "expected"', '"id": "other"', 1)
    path.write_text(payload)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="identity"):
        read_piri_snapshot(
            session_dir,
            "expected",
            bounds=TranscriptBounds(),
        )

    path.unlink()
    target = tmp_path / "outside.jsonl"
    target.write_text("{}\n")
    os.symlink(target, path)
    with pytest.raises(ValueError, match="unsafe"):
        read_piri_snapshot(
            session_dir,
            "expected",
            bounds=TranscriptBounds(),
        )
