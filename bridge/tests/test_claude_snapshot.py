from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from telegram_bot.memory.claude_snapshot import read_claude_snapshot
from telegram_bot.memory.distill_types import TranscriptBounds


def _write_transcript(path: Path) -> None:
    rows = [
        {
            "type": "user",
            "uuid": "user-1",
            "timestamp": "2026-08-05T00:00:00Z",
            "message": {"role": "user", "content": "question"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-1",
            "timestamp": "2026-08-05T00:00:01Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "answer"}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    path.chmod(0o600)


def test_claude_snapshot_uses_shared_bounded_contract(tmp_path: Path) -> None:
    path = tmp_path / "session-1.jsonl"
    _write_transcript(path)

    snapshot = read_claude_snapshot(
        tmp_path,
        "session-1",
        bounds=TranscriptBounds(),
        now=datetime(2026, 8, 5, 0, 1, tzinfo=timezone.utc),
    )

    assert [message.role for message in snapshot.messages] == ["user", "assistant"]
    assert [message.text for message in snapshot.messages] == ["question", "answer"]
    assert snapshot.byte_count == len("questionanswer".encode())
    assert snapshot.last_turn_id == "assistant-1"


def test_claude_snapshot_rejects_symlinked_transcript(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    _write_transcript(outside)
    (tmp_path / "session-1.jsonl").symlink_to(outside)

    with pytest.raises(OSError):
        read_claude_snapshot(
            tmp_path,
            "session-1",
            bounds=TranscriptBounds(),
        )
