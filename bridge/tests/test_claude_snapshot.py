from datetime import datetime, timezone
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from telegram_bot.memory.claude_snapshot import (
    ensure_private_transcripts_dir,
    prepare_transcripts_dir,
    read_claude_snapshot,
)
from telegram_bot.memory.distill_types import SnapshotUnavailableError, TranscriptBounds


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


@pytest.mark.parametrize("mode", [0o755, 0o775, 0o777])
def test_umask_transcripts_dir_is_tightened_and_read(
    tmp_path: Path, mode: int, caplog: pytest.LogCaptureFixture
) -> None:
    """#2306: an umask-created dir used to refuse every distill snapshot."""
    directory = tmp_path / "projects" / "-home-user"
    directory.mkdir(parents=True)
    _write_transcript(directory / "session-1.jsonl")
    directory.chmod(mode)

    with caplog.at_level("WARNING", logger="telegram_bot.memory.claude_snapshot"):
        snapshot = read_claude_snapshot(
            directory,
            "session-1",
            bounds=TranscriptBounds(),
            now=datetime(2026, 8, 5, 0, 1, tzinfo=timezone.utc),
        )

    assert [message.text for message in snapshot.messages] == ["question", "answer"]
    assert directory.stat().st_mode & 0o777 == mode & 0o700
    assert f"mode {mode:04o}" in caplog.text and "tightened to 0700" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING", logger="telegram_bot.memory.claude_snapshot"):
        assert ensure_private_transcripts_dir(directory) is False
    assert caplog.text == ""


def test_symlinked_transcripts_dir_is_still_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    real.chmod(0o755)
    _write_transcript(real / "session-1.jsonl")
    link = tmp_path / "link"
    link.symlink_to(real)

    with pytest.raises(ValueError, match="directory is unsafe"):
        read_claude_snapshot(link, "session-1", bounds=TranscriptBounds())
    assert real.stat().st_mode & 0o777 == 0o755


def test_non_directory_and_foreign_owner_are_still_refused(tmp_path: Path) -> None:
    not_dir = tmp_path / "file"
    not_dir.write_text("x")
    with pytest.raises(ValueError, match="directory is unsafe"):
        ensure_private_transcripts_dir(not_dir)

    directory = tmp_path / "dir"
    directory.mkdir()
    directory.chmod(0o755)
    with patch(
        "telegram_bot.memory.claude_snapshot.os.geteuid", return_value=os.geteuid() + 1
    ):
        with pytest.raises(ValueError, match="directory is unsafe"):
            ensure_private_transcripts_dir(directory)
    assert directory.stat().st_mode & 0o777 == 0o755


def test_missing_transcripts_dir_stays_retryable(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_claude_snapshot(tmp_path / "absent", "session-1", bounds=TranscriptBounds())


def test_missing_transcript_file_is_unavailable_not_empty(tmp_path: Path) -> None:
    """#2316: an absent transcript used to come back as an empty snapshot, so the
    job completed with no memory. It must raise the retryable contract error."""
    tmp_path.chmod(0o700)
    with pytest.raises(SnapshotUnavailableError) as raised:
        read_claude_snapshot(tmp_path, "session-absent", bounds=TranscriptBounds())
    # Retryable for the snapshot worker, never a terminal refusal.
    assert not isinstance(raised.value, ValueError)
    assert "session-absent" not in str(raised.value)


def test_tightened_dir_still_refuses_unsafe_transcript_file(tmp_path: Path) -> None:
    directory = tmp_path / "d"
    directory.mkdir()
    directory.chmod(0o775)
    _write_transcript(directory / "session-1.jsonl")
    (directory / "session-1.jsonl").chmod(0o644)

    with pytest.raises(ValueError, match="file is unsafe"):
        read_claude_snapshot(directory, "session-1", bounds=TranscriptBounds())


def test_prepare_creates_tightens_and_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    created = projects / "-srv"
    prepare_transcripts_dir(created)
    assert created.is_dir() and created.stat().st_mode & 0o777 == 0o700

    loose = projects / "-loose"
    loose.mkdir()
    loose.chmod(0o775)
    prepare_transcripts_dir(loose)
    assert loose.stat().st_mode & 0o777 == 0o700

    orphan = tmp_path / "no-parent" / "-x"
    prepare_transcripts_dir(orphan)
    assert not orphan.exists()

    real = tmp_path / "real"
    real.mkdir()
    link = projects / "-link"
    link.symlink_to(real)
    with caplog.at_level("WARNING", logger="telegram_bot.memory.claude_snapshot"):
        prepare_transcripts_dir(link)
    assert "check failed at bridge start" in caplog.text


def test_error_classification_matches_pre_2306(tmp_path: Path) -> None:
    """Parent-component errors stay retryable OSError; only the final component
    being unsafe is a terminal ValueError; an unreadable (0300) dir still reads."""
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(OSError) as raised:
        read_claude_snapshot(blocker / "child", "session-1", bounds=TranscriptBounds())
    assert not isinstance(raised.value, ValueError)

    exec_only = tmp_path / "exec-only"
    exec_only.mkdir()
    _write_transcript(exec_only / "session-1.jsonl")
    exec_only.chmod(0o300)
    try:
        if os.access(exec_only, os.R_OK):
            pytest.skip("running with read access to 0300 dirs (root)")
        snapshot = read_claude_snapshot(
            exec_only,
            "session-1",
            bounds=TranscriptBounds(),
            now=datetime(2026, 8, 5, 0, 1, tzinfo=timezone.utc),
        )
        assert [m.text for m in snapshot.messages] == ["question", "answer"]
        assert exec_only.stat().st_mode & 0o777 == 0o300
    finally:
        exec_only.chmod(0o700)


def test_failed_fchmod_is_refused_as_before(tmp_path: Path) -> None:
    directory = tmp_path / "ro"
    directory.mkdir()
    directory.chmod(0o755)
    with patch(
        "telegram_bot.memory.claude_snapshot.os.fchmod",
        side_effect=OSError(30, "Read-only file system"),
    ):
        with pytest.raises(ValueError, match="directory is unsafe"):
            ensure_private_transcripts_dir(directory)
