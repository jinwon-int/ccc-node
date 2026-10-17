"""Security and audience-isolation tests for provider working-state archives."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re

from telegram_bot.core.working_state_archive import (
    archive_working_state,
    select_working_state_environment,
)


def _private_file(path: Path, content: bytes = b"# Working state\nactive\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_bytes(content)
    path.chmod(0o600)


def test_environment_selection_excludes_unrelated_process_secrets() -> None:
    selected = select_working_state_environment(
        {
            "HOME": "/private/home",
            "CCC_STATE_DIR": "/private/state",
            "TELEGRAM_BOT_TOKEN": "must-not-retain",
            "OPENAI_API_KEY": "must-not-retain",
        }
    )

    assert selected == {
        "HOME": "/private/home",
        "CCC_STATE_DIR": "/private/state",
    }


def test_pre_compact_uses_claude_location_name_mode_and_retention(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    _private_file(state_dir / "working-state.md")
    checkpoint_dir = state_dir / "checkpoints"
    checkpoint_dir.mkdir(mode=0o700)
    for index in range(31):
        path = checkpoint_dir / f"working-state-old-{index:02d}.md"
        _private_file(path, f"old {index}".encode())
        os.utime(path, ns=(index + 1, index + 1))
    unrelated = checkpoint_dir / "keep.txt"
    _private_file(unrelated, b"unrelated")

    destination = archive_working_state(
        "pre_compact",
        environment={"CCC_STATE_DIR": str(state_dir)},
        session_id="raw/provider/session",
    )

    assert destination is not None
    assert destination.parent == checkpoint_dir
    assert re.fullmatch(r"working-state-\d{8}_\d{6}\.md", destination.name)
    assert "raw" not in destination.name
    assert destination.read_bytes() == b"# Working state\nactive\n"
    assert destination.stat().st_mode & 0o777 == 0o600
    assert len(tuple(checkpoint_dir.glob("working-state-*.md"))) == 30
    assert unrelated.read_bytes() == b"unrelated"


def test_session_end_is_private_content_addressed_and_idempotent(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _private_file(state_dir / "working-state.md", b"objective: finish\n")
    environment = {"CCC_STATE_DIR": str(state_dir)}

    first = archive_working_state(
        "session_end", environment=environment, session_id="secret-thread-id"
    )
    second = archive_working_state(
        "session_end", environment=environment, session_id="secret-thread-id"
    )

    assert first is not None
    assert first == second
    assert first.parent == state_dir / "session-archive"
    assert re.fullmatch(r"working-state-[0-9a-f]{24}\.md", first.name)
    assert "secret-thread-id" not in first.name
    assert first.read_bytes() == b"objective: finish\n"
    assert first.stat().st_mode & 0o777 == 0o600
    assert first.parent.stat().st_mode & 0o777 == 0o700
    assert len(tuple(first.parent.iterdir())) == 1

    first.write_bytes(b"objective: wrong!\n")
    first.chmod(0o600)
    repaired = archive_working_state(
        "session_end", environment=environment, session_id="secret-thread-id"
    )
    assert repaired == first
    assert repaired.read_bytes() == b"objective: finish\n"


def test_private_audience_legacy_fallback_never_crosses_into_shared(
    tmp_path: Path,
) -> None:
    legacy_dir = tmp_path / "legacy"
    scoped_dir = tmp_path / "private-scope"
    _private_file(legacy_dir / "working-state.md", b"private legacy\n")
    private_environment = {
        "CCC_STATE_DIR": str(scoped_dir),
        "CCC_MEMORY_AUDIENCE_SCOPED": "1",
        "CCC_MEMORY_AUDIENCE": "private",
        "CCC_MEMORY_LEGACY_STATE_DIR": str(legacy_dir),
    }

    private_archive = archive_working_state(
        "session_end",
        environment=private_environment,
        session_id="private-session",
    )
    assert private_archive is not None
    assert private_archive.parent == scoped_dir / "session-archive"
    assert private_archive.read_bytes() == b"private legacy\n"

    shared_archive = archive_working_state(
        "session_end",
        environment={**private_environment, "CCC_MEMORY_AUDIENCE": "shared"},
        session_id="shared-session",
    )
    assert shared_archive is None
    assert not (scoped_dir / "working-state.md").exists()


def test_unsafe_oversized_and_disabled_sources_are_refused(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    source = state_dir / "working-state.md"
    _private_file(source, b"too long")
    base = {"CCC_STATE_DIR": str(state_dir)}

    assert archive_working_state(
        "session_end",
        environment={**base, "CCC_WORKING_STATE_ARCHIVE_MAX_BYTES": "3"},
        session_id="oversized",
    ) is None
    assert archive_working_state(
        "session_end",
        environment={**base, "CCC_WORKING_STATE_ARCHIVE": "off"},
        session_id="disabled",
    ) is None

    source.chmod(0o660)
    assert archive_working_state(
        "session_end", environment=base, session_id="writable"
    ) is None

    source.unlink()
    target = tmp_path / "target.md"
    _private_file(target, b"link target")
    source.symlink_to(target)
    assert archive_working_state(
        "session_end", environment=base, session_id="symlink"
    ) is None

    source.unlink()
    _private_file(source, b"hard link")
    os.link(source, tmp_path / "second-link.md")
    assert archive_working_state(
        "session_end", environment=base, session_id="hardlink"
    ) is None


def test_existing_archive_directory_must_be_owner_private(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _private_file(state_dir / "working-state.md")
    archive_dir = state_dir / "session-archive"
    archive_dir.mkdir(mode=0o700)
    archive_dir.chmod(0o750)

    assert archive_working_state(
        "session_end",
        environment={"CCC_STATE_DIR": str(state_dir)},
        session_id="private-directory",
    ) is None
    assert tuple(archive_dir.iterdir()) == ()


def test_session_end_dedupes_identical_state_across_sessions(tmp_path: Path) -> None:
    """#2361: one unchanged working state is stored once, not once per session."""

    state_dir = tmp_path / "state"
    payload = b"objective: unchanged\n"
    _private_file(state_dir / "working-state.md", payload)
    environment = {"CCC_STATE_DIR": str(state_dir)}

    paths = {
        archive_working_state(
            "session_end", environment=environment, session_id=f"session-{index}"
        )
        for index in range(25)
    }

    assert len(paths) == 1
    (only,) = paths
    assert only is not None
    expected = hashlib.sha256(b"session_end\0" + payload).hexdigest()[:24]
    # Byte-identical to notify.sh `ccc_lifecycle_file_digest session_end`.
    assert only.name == f"working-state-{expected}.md"
    assert len(tuple(only.parent.iterdir())) == 1


def test_session_end_restamps_an_identical_snapshot(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _private_file(state_dir / "working-state.md", b"objective: same\n")
    environment = {"CCC_STATE_DIR": str(state_dir)}

    first = archive_working_state("session_end", environment=environment, session_id="a")
    assert first is not None
    os.utime(first, (1_000_000_000, 1_000_000_000))
    second = archive_working_state("session_end", environment=environment, session_id="b")

    assert second == first
    assert first.stat().st_mtime > 1_000_000_000


def test_session_end_keeps_only_the_newest_snapshots(tmp_path: Path) -> None:
    """#2361: bounded retention; the snapshot just written always survives."""

    state_dir = tmp_path / "state"
    working_state = state_dir / "working-state.md"
    environment = {"CCC_STATE_DIR": str(state_dir), "CCC_SESSION_ARCHIVE_KEEP": "3"}
    written = []
    for index in range(6):
        _private_file(working_state, f"objective: step {index}\n".encode())
        path = archive_working_state(
            "session_end", environment=environment, session_id=f"s{index}"
        )
        assert path is not None
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
        written.append(path)

    remaining = sorted(p.name for p in (state_dir / "session-archive").iterdir())
    assert remaining == sorted(p.name for p in written[-3:])


def test_session_end_retention_defaults_and_opt_out(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    working_state = state_dir / "working-state.md"
    archive_dir = state_dir / "session-archive"

    for index in range(4):
        _private_file(working_state, f"objective: keep {index}\n".encode())
        archive_working_state(
            "session_end",
            environment={"CCC_STATE_DIR": str(state_dir), "CCC_SESSION_ARCHIVE_KEEP": "0"},
            session_id=f"k{index}",
        )
    assert len(tuple(archive_dir.iterdir())) == 4  # "0" disables pruning

    for raw in ("", "not-a-number", "-5"):
        _private_file(working_state, f"objective: default {raw!r}\n".encode())
        archive_working_state(
            "session_end",
            environment={"CCC_STATE_DIR": str(state_dir), "CCC_SESSION_ARCHIVE_KEEP": raw},
            session_id="d",
        )
    # Invalid/blank fall back to the default (200), so nothing is pruned here.
    assert len(tuple(archive_dir.iterdir())) == 7
