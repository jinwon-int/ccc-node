"""Transition lease and immutable phase records fail closed on unsafe evidence."""
import json
import os

import pytest

from telegram_bot import prepared_transition as journal


PAIR = [{"schema": "ccc.prepared-runtime.v1", "status": "ready", "source_dir": "/candidate"},
        {"schema": "ccc.prepared-runtime.v1", "status": "ready", "source_dir": "/previous"}]


def begin(tmp_path):
    return journal.begin(tmp_path / "transitions", {"source": "candidate"}, {"source": "previous"}, os.getpid())


@pytest.mark.parametrize("mask", [0o077, 0o022, 0o002])
@pytest.mark.parametrize("outcome", ["candidate_available", "recovered", "recovery_failed", "stop_failed", "rejected"])
def test_complete_phases_preserve_records_and_release_lease(tmp_path, mask, outcome):
    old = os.umask(mask)
    try:
        run = begin(tmp_path)
        first = (run / "00-intent.json").read_bytes()
        if outcome != "rejected":
            journal.advance(run, "validated", 0, PAIR)
            if outcome != "stop_failed":
                journal.advance(run, "launching", 0)
                if outcome != "candidate_available":
                    journal.advance(run, "candidate_failed", 4)
        journal.advance(run, outcome, 4)
    finally:
        os.umask(old)
    assert not (run.parent / "active").exists()
    assert (run / "lease/owner.json").exists()
    assert (run / "00-intent.json").read_bytes() == first
    records = sorted(run.glob("*.json"))
    last = json.loads(records[-1].read_text())
    assert last["controller_exit_code"] == journal.RESULT[outcome]
    assert last["command_exit_code"] == 4
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in records)
    assert run.stat().st_mode & 0o777 == 0o700
    assert begin(tmp_path) != run


def test_incomplete_attempt_blocks_reclaim_even_for_dead_driver(tmp_path):
    run = journal.begin(tmp_path / "transitions", {}, {}, 2147483647)
    snapshot = (run.parent / "active/owner.json").read_bytes()
    with pytest.raises(FileExistsError):
        begin(tmp_path)
    assert (run.parent / "active/owner.json").read_bytes() == snapshot
    assert len(list(run.parent.iterdir())) == 2


@pytest.mark.parametrize("phase", ["launching", "candidate_available", "candidate_failed", "recovered", "recovery_failed"])
def test_out_of_order_phase_does_not_release_or_overwrite(tmp_path, phase):
    run = begin(tmp_path)
    with pytest.raises(ValueError, match="phase_transition"):
        journal.advance(run, phase, 0)
    assert len(list(run.glob("*.json"))) == 1
    assert (run.parent / "active").is_dir()


@pytest.mark.parametrize("reports", [None, [], PAIR[:1], [PAIR[0], {}], [PAIR[0], {"schema": "ccc.prepared-runtime.v1", "status": "unready"}]])
def test_validation_requires_both_ready_reports(tmp_path, reports):
    run = begin(tmp_path)
    with pytest.raises(ValueError, match="validated_pair"):
        journal.advance(run, "validated", 0, reports)
    assert not (run / "01-validated.json").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "public", "wrong_owner"])
def test_unsafe_lease_metadata_is_rejected(tmp_path, kind):
    run = begin(tmp_path)
    owner = run.parent / "active/owner.json"
    if kind == "public":
        owner.chmod(0o644)
    elif kind == "wrong_owner":
        owner.write_text(json.dumps({"run": "another"}))
    else:
        saved = owner.with_name("saved")
        owner.rename(saved)
        if kind == "symlink":
            owner.symlink_to(saved)
        elif kind == "hardlink":
            os.link(saved, owner)
        elif kind == "fifo":
            os.mkfifo(owner)
        else:
            owner.mkdir()
    with pytest.raises((OSError, ValueError)):
        journal.advance(run, "rejected", 6)
    assert (run.parent / "active").exists()


def test_symlinked_ancestor_does_not_create_state_outside_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        journal.begin(link / "transitions", {}, {}, os.getpid())
    assert list(outside.iterdir()) == []


def test_fsync_failure_preserves_incomplete_lease(tmp_path, monkeypatch):
    run = begin(tmp_path)
    monkeypatch.setattr(journal.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("fixture disk failure")))
    with pytest.raises(OSError):
        journal.advance(run, "rejected", 6)
    assert (run.parent / "active").exists()
    with pytest.raises(FileExistsError):
        begin(tmp_path)


def test_existing_record_is_never_overwritten(tmp_path):
    run = begin(tmp_path)
    path = run / "00-intent.json"
    data = path.read_bytes()
    with pytest.raises(FileExistsError):
        journal.write_record(path, {"replacement": True})
    assert path.read_bytes() == data


def test_terminal_archive_failure_stays_fail_closed(tmp_path):
    run = begin(tmp_path)
    (run / "lease").mkdir()
    with pytest.raises(ValueError, match="archive_exists"):
        journal.advance(run, "rejected", 6)
    assert (run.parent / "active").exists()
    assert (run / "01-rejected.json").exists()


def test_cli_rejects_identical_pair_before_claim(tmp_path, capsys):
    args = ["begin", "--root", str(tmp_path / "state"), "--candidate-source", str(tmp_path),
            "--previous-source", str(tmp_path), "--candidate-runtime", str(tmp_path / "job"),
            "--previous-runtime", str(tmp_path / "job"), "--launcher-pid", str(os.getpid())]
    assert journal.main(args) == 1
    assert not (tmp_path / "state").exists()
    assert "inspect active lease" in capsys.readouterr().err


def test_concurrent_drivers_cannot_both_claim(tmp_path):
    import subprocess
    import sys

    root = tmp_path / "transitions"
    root.mkdir(mode=0o700)
    args = [sys.executable, "-I", "-B", journal.__file__, "begin", "--root", str(root),
            "--candidate-source", str(tmp_path / "candidate"), "--candidate-runtime", str(tmp_path / "new"),
            "--previous-source", str(tmp_path / "previous"), "--previous-runtime", str(tmp_path / "old"),
            "--launcher-pid", str(os.getpid())]
    children = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
    try:
        for child in children:
            child.communicate(timeout=10)
        assert sorted(child.returncode for child in children) == [0, 1, 1, 1]
        assert len(list(root.iterdir())) == 2
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


def test_first_creation_syncs_parent_before_claim(tmp_path, monkeypatch):
    synced = []
    real = journal.sync_directory

    def observe(path):
        synced.append(path)
        real(path)

    monkeypatch.setattr(journal, "sync_directory", observe)
    run = begin(tmp_path)
    assert synced[0] == tmp_path
    assert run.parent in synced[1:]


def test_parent_sync_failure_refuses_before_claim(tmp_path, monkeypatch):
    def fail_parent(path):
        if path == tmp_path:
            raise OSError("fixture parent sync failure")

    monkeypatch.setattr(journal, "sync_directory", fail_parent)
    with pytest.raises(OSError):
        begin(tmp_path)
    assert not (tmp_path / "transitions/active").exists()


def test_post_archive_sync_failure_retains_evidence_but_may_release_lease(tmp_path, monkeypatch):
    run = begin(tmp_path)
    real = journal.sync_directory

    def fail_after_archive(path):
        if path == run and (run / "lease").exists():
            raise OSError("fixture post-archive failure")
        real(path)

    monkeypatch.setattr(journal, "sync_directory", fail_after_archive)
    with pytest.raises(OSError):
        journal.advance(run, "rejected", 6)
    assert (run / "lease/owner.json").exists()
    assert (run / "01-rejected.json").exists()
    assert not (run.parent / "active").exists()
    assert begin(tmp_path) != run
