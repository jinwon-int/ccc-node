"""Safety and crash-recovery tests for the local-memory rollback head (#386)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from telegram_bot.memory.local_memory_transaction import (
    LocalMemoryConflict,
    LocalMemorySecurityError,
    LocalMemoryTransaction,
)


def private_state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    return state


def write_state(state: Path, name: str, payload: bytes) -> None:
    path = state / name
    path.write_bytes(payload)
    path.chmod(0o600)


def replace_both(facts: bytes, resume: bytes):
    def transform(_current: dict[str, bytes | None]) -> dict[str, bytes]:
        return {"memory-facts.jsonl": facts, "resume.md": resume}

    return transform


def test_commit_and_idempotent_rollback_restore_exact_private_preimages(
    tmp_path: Path,
) -> None:
    state = private_state(tmp_path)
    secret = "sk-" + "sensitive-memory-value-" * 2
    write_state(state, "memory-facts.jsonl", f"old facts {secret}\n".encode())
    write_state(state, "resume.md", f"old resume {secret}\n".encode())
    transaction = LocalMemoryTransaction(state)

    committed = transaction.commit(
        replace_both(b"new facts\n", b"new resume\n"),
        provider="codex",
        actor="distill",
        tool="local-memory-sink",
        session="raw-session-identifier",
        diff="mode-both",
    )

    assert committed.action_id is not None
    assert committed.changed_targets == ("memory-facts.jsonl", "resume.md")
    rollback_root = state / "memory-rollback"
    action_root = rollback_root / "actions" / committed.action_id
    assert stat.S_IMODE(rollback_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(action_root.stat().st_mode) == 0o700
    assert stat.S_IMODE((rollback_root / "ledger.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE((action_root / "before-memory-facts.jsonl").stat().st_mode) == 0o600
    body_free = (rollback_root / "ledger.jsonl").read_text()
    body_free += (action_root / "manifest.json").read_text()
    assert secret not in body_free
    assert "raw-session-identifier" not in body_free
    assert json.loads((action_root / "manifest.json").read_text())["session"]

    rolled_back = transaction.rollback(committed.action_id)
    assert rolled_back.status == "rolled-back"
    assert (state / "memory-facts.jsonl").read_bytes() == f"old facts {secret}\n".encode()
    assert (state / "resume.md").read_bytes() == f"old resume {secret}\n".encode()
    assert transaction.rollback(committed.action_id).status == "already-rolled-back"
    assert not list(action_root.glob("before-*"))


def test_only_latest_head_is_undoable_and_manual_changes_fail_cas(tmp_path: Path) -> None:
    state = private_state(tmp_path)
    first = LocalMemoryTransaction(state).commit(
        replace_both(b"facts one\n", b"resume one\n"),
        provider="claude",
        actor="distill",
        tool="local-memory-commit",
        session="one",
        diff="mode-both",
    )
    second = LocalMemoryTransaction(state).commit(
        replace_both(b"facts two\n", b"resume two\n"),
        provider="claude",
        actor="distill",
        tool="local-memory-commit",
        session="two",
        diff="mode-both",
    )
    assert first.action_id and second.action_id
    first_action = state / "memory-rollback" / "actions" / first.action_id
    assert not list(first_action.glob("before-*"))

    with pytest.raises(LocalMemoryConflict, match="latest"):
        LocalMemoryTransaction(state).rollback(first.action_id)

    write_state(state, "resume.md", b"manual edit\n")
    with pytest.raises(LocalMemoryConflict, match="post-image"):
        LocalMemoryTransaction(state).rollback(second.action_id)
    assert (state / "memory-facts.jsonl").read_bytes() == b"facts two\n"
    assert (state / "resume.md").read_bytes() == b"manual edit\n"


def test_rollback_removes_targets_that_were_absent_before_commit(tmp_path: Path) -> None:
    state = private_state(tmp_path)
    result = LocalMemoryTransaction(state).commit(
        replace_both(b"created facts\n", b"created resume\n"),
        provider="codex",
        actor="distill",
        tool="local-memory-sink",
        session="job",
        diff="mode-both",
    )
    assert result.action_id

    LocalMemoryTransaction(state).rollback(result.action_id)

    assert not (state / "memory-facts.jsonl").exists()
    assert not (state / "resume.md").exists()


def test_recovery_aborts_a_partial_commit_before_starting_the_next(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = private_state(tmp_path)
    write_state(state, "memory-facts.jsonl", b"old facts\n")
    write_state(state, "resume.md", b"old resume\n")
    interrupted = LocalMemoryTransaction(state)
    real_write = interrupted._write_target
    calls = 0

    def fail_after_first(name: str, payload: bytes | None) -> None:
        nonlocal calls
        real_write(name, payload)
        calls += 1
        if calls == 1:
            raise OSError("simulated process loss")

    monkeypatch.setattr(interrupted, "_write_target", fail_after_first)
    with pytest.raises(OSError, match="simulated"):
        interrupted.commit(
            replace_both(b"partial facts\n", b"partial resume\n"),
            provider="claude",
            actor="distill",
            tool="local-memory-commit",
            session="partial",
            diff="mode-both",
        )
    aborted_action = next((state / "memory-rollback" / "actions").iterdir())

    recovered = LocalMemoryTransaction(state).commit(
        replace_both(b"final facts\n", b"final resume\n"),
        provider="claude",
        actor="distill",
        tool="local-memory-commit",
        session="next",
        diff="mode-both",
    )
    assert recovered.action_id
    assert json.loads((aborted_action / "manifest.json").read_text())["state"] == "aborted"
    assert not list(aborted_action.glob("before-*"))
    LocalMemoryTransaction(state).rollback(recovered.action_id)
    assert (state / "memory-facts.jsonl").read_bytes() == b"old facts\n"
    assert (state / "resume.md").read_bytes() == b"old resume\n"


def test_recovery_finishes_a_partial_undo_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = private_state(tmp_path)
    write_state(state, "memory-facts.jsonl", b"old facts\n")
    write_state(state, "resume.md", b"old resume\n")
    result = LocalMemoryTransaction(state).commit(
        replace_both(b"new facts\n", b"new resume\n"),
        provider="codex",
        actor="distill",
        tool="local-memory-sink",
        session="job",
        diff="mode-both",
    )
    assert result.action_id
    interrupted = LocalMemoryTransaction(state)
    real_write = interrupted._write_target
    calls = 0

    def fail_after_first(name: str, payload: bytes | None) -> None:
        nonlocal calls
        real_write(name, payload)
        calls += 1
        if calls == 1:
            raise OSError("simulated undo loss")

    monkeypatch.setattr(interrupted, "_write_target", fail_after_first)
    with pytest.raises(OSError, match="simulated"):
        interrupted.rollback(result.action_id)

    retried = LocalMemoryTransaction(state).rollback(result.action_id)
    assert retried.status == "already-rolled-back"
    assert (state / "memory-facts.jsonl").read_bytes() == b"old facts\n"
    assert (state / "resume.md").read_bytes() == b"old resume\n"


def test_rollback_authenticates_all_preimages_before_mutating_targets(
    tmp_path: Path,
) -> None:
    state = private_state(tmp_path)
    write_state(state, "memory-facts.jsonl", b"old facts\n")
    write_state(state, "resume.md", b"old resume\n")
    result = LocalMemoryTransaction(state).commit(
        replace_both(b"new facts\n", b"new resume\n"),
        provider="codex",
        actor="distill",
        tool="local-memory-sink",
        session="job",
        diff="mode-both",
    )
    assert result.action_id
    action = state / "memory-rollback" / "actions" / result.action_id
    (action / "before-resume.md").write_bytes(b"corrupt preimage\n")

    with pytest.raises(LocalMemorySecurityError, match="pre-image hash"):
        LocalMemoryTransaction(state).rollback(result.action_id)

    assert (state / "memory-facts.jsonl").read_bytes() == b"new facts\n"
    assert (state / "resume.md").read_bytes() == b"new resume\n"


def test_corrupt_manifest_metadata_is_rejected_without_entering_the_ledger(
    tmp_path: Path,
) -> None:
    state = private_state(tmp_path)
    result = LocalMemoryTransaction(state).commit(
        replace_both(b"new facts\n", b"new resume\n"),
        provider="codex",
        actor="distill",
        tool="local-memory-sink",
        session="job",
        diff="mode-both",
    )
    assert result.action_id
    rollback = state / "memory-rollback"
    manifest_path = rollback / "actions" / result.action_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["actor"] = "raw secret body"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(LocalMemorySecurityError, match="actor"):
        LocalMemoryTransaction(state).rollback(result.action_id)

    assert "raw secret body" not in (rollback / "ledger.jsonl").read_text()


def test_action_with_unexpected_entry_is_rejected(tmp_path: Path) -> None:
    state = private_state(tmp_path)
    result = LocalMemoryTransaction(state).commit(
        replace_both(b"new facts\n", b"new resume\n"),
        provider="codex",
        actor="distill",
        tool="local-memory-sink",
        session="job",
        diff="mode-both",
    )
    assert result.action_id
    action = state / "memory-rollback" / "actions" / result.action_id
    (action / "unexpected").write_text("must not be accepted")

    with pytest.raises(LocalMemorySecurityError, match="unsafe entry"):
        LocalMemoryTransaction(state).rollback(result.action_id)


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_commit_rejects_linked_targets_without_mutating_the_peer(
    tmp_path: Path,
    link_kind: str,
) -> None:
    state = private_state(tmp_path)
    peer = tmp_path / "peer"
    peer.write_bytes(b"unchanged\n")
    peer.chmod(0o600)
    target = state / "memory-facts.jsonl"
    if link_kind == "symlink":
        target.symlink_to(peer)
    else:
        os.link(peer, target)

    with pytest.raises(LocalMemorySecurityError):
        LocalMemoryTransaction(state).commit(
            replace_both(b"new facts\n", b"new resume\n"),
            provider="codex",
            actor="distill",
            tool="local-memory-sink",
            session="job",
            diff="mode-both",
        )

    assert peer.read_bytes() == b"unchanged\n"
