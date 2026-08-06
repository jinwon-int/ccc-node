"""Security and bounds tests for Piri transcript snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest

from telegram_bot.memory.distill_types import TranscriptBounds
from telegram_bot.memory.piri_snapshot import (
    find_piri_session_directory,
    read_piri_snapshot,
)


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


def test_finder_locates_session_at_root(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions"
    _write_session(session_dir, "root-session", [])

    assert find_piri_session_directory(session_dir, "root-session") == session_dir


def test_finder_locates_session_in_cwd_slug_subdir(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    slug = root / "--workspace--"
    _write_session(slug, "nested-session", [])

    assert find_piri_session_directory(root, "nested-session") == slug


def test_finder_returns_none_when_missing(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    _write_session(root / "--a--", "other-session", [])

    assert find_piri_session_directory(root, "absent-session") is None
    assert find_piri_session_directory(tmp_path / "no-such-root", "x") is None


def test_finder_rejects_ambiguity_across_subdirs(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    _write_session(root / "--a--", "dup-session", [])
    _write_session(root / "--b--", "dup-session", [])

    with pytest.raises(ValueError, match="ambiguous"):
        find_piri_session_directory(root, "dup-session")


def test_finder_skips_symlinked_and_unsafe_subdirs(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    _write_session(outside, "hidden-session", [])
    (root / "link").symlink_to(outside, target_is_directory=True)
    world = root / "world"
    _write_session(world, "hidden-session", [])
    world.chmod(0o755)

    assert find_piri_session_directory(root, "hidden-session") is None


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
