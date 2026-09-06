"""Cleanup must not split the kernel lock's inode or steal another claim."""
import fcntl
import os
import select
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from telegram_bot import token_lock


def claim(tmp_path, value=None):
    path = tmp_path / "token.pid"
    path.write_text(f"{os.getpid() if value is None else value}\n")
    return path


@pytest.mark.parametrize("mode", ["r+", "a"])
def test_owner_clear_keeps_lock_until_original_descriptor_closes(tmp_path, mode):
    path = claim(tmp_path)
    inode = path.stat().st_ino
    with path.open(mode) as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert token_lock.clear_token_claim(path, (os.getpid(),), holder.fileno()) == "cleared"
        assert path.stat().st_ino == inode
        assert path.read_bytes() == b""
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with path.open("r+") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with path.open("r+") as next_holder:
        fcntl.flock(next_holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert os.fstat(next_holder.fileno()).st_ino == inode


def test_external_clear_does_not_read_or_release_busy_claim(tmp_path, monkeypatch):
    path = claim(tmp_path)
    with path.open("r+") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def unexpected_read(*args):
            pytest.fail("PID was read without owning the kernel lock")
        monkeypatch.setattr(token_lock.os, "pread", unexpected_read)
        assert token_lock.clear_token_claim(path, (os.getpid(),), -1) == "busy"
        assert path.read_text().strip() == str(os.getpid())


def test_live_foreign_claim_without_flock_is_preserved(tmp_path):
    path = claim(tmp_path)
    assert token_lock.clear_token_claim(path, (), -1) == "preserved"
    assert path.read_text().strip() == str(os.getpid())


def test_real_replacement_claim_survives_stale_cleanup(tmp_path):
    path = claim(tmp_path, 999999999)
    inode = path.stat().st_ino
    code = """import fcntl,os,sys
with open(sys.argv[1], 'r+') as f:
 fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
 f.seek(0); f.truncate(); f.write(str(os.getpid())+'\\n'); f.flush()
 print('held',flush=True)
 sys.stdin.readline()
"""
    child = subprocess.Popen([sys.executable, "-c", code, str(path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert select.select([child.stdout], [], [], 5)[0], "holder did not report readiness"
        assert child.stdout.readline().strip() == "held"
        assert token_lock.clear_token_claim(path, (999999999,), -1) == "busy"
        assert path.stat().st_ino == inode
        assert path.read_text().strip() == str(child.pid)
        with path.open("r+") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        try:
            child.communicate("done\n", timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)
    assert child.returncode == 0
    assert token_lock.clear_token_claim(path, (), -1) == "cleared"
    assert path.stat().st_ino == inode


@pytest.mark.parametrize("mode", [0o600, 0o644, 0o664, 0o666])
def test_clear_forces_private_mode_without_replacing_inode(tmp_path, mode):
    path = claim(tmp_path)
    path.chmod(mode)
    inode = path.stat().st_ino
    assert token_lock.clear_token_claim(path, (os.getpid(),), -1) == "cleared"
    assert path.stat().st_ino == inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("value", ["not-a-pid", "0", "1", "-3", "x" * 65])
def test_invalid_pid_is_left_untouched(tmp_path, value):
    path = claim(tmp_path, value)
    before = path.read_bytes()
    assert token_lock.clear_token_claim(path, (), -1) == "unsafe"
    assert path.read_bytes() == before


def test_unrepresentable_pid_is_not_assumed_dead(tmp_path):
    path = claim(tmp_path, "9" * 40)
    assert token_lock.clear_token_claim(path, (), -1) == "preserved"


def test_missing_file_is_not_created(tmp_path):
    path = tmp_path / "absent"
    assert token_lock.clear_token_claim(path, (), -1) == "absent"
    assert not path.exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "parent-symlink", "writable-parent"])
def test_unsafe_paths_are_not_modified(tmp_path, kind):
    target = claim(tmp_path)
    before = target.read_bytes()
    path = tmp_path / "unsafe"
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "parent-symlink":
        path.symlink_to(tmp_path, target_is_directory=True)
        path = path / target.name
    else:
        path = target
        tmp_path.chmod(0o777)
    assert token_lock.clear_token_claim(path, (os.getpid(),), -1) == "unsafe"
    assert target.read_bytes() == before


def test_replaced_path_is_not_cleared(tmp_path, monkeypatch):
    path = claim(tmp_path)
    original = tmp_path / "retained"
    pread = os.pread
    def replace_after_read(fd, length, offset):
        value = pread(fd, length, offset)
        path.rename(original)
        path.write_text("new-path\n")
        return value
    monkeypatch.setattr(token_lock.os, "pread", replace_after_read)
    assert token_lock.clear_token_claim(path, (os.getpid(),), -1) == "unsafe"
    assert path.read_text() == "new-path\n"
    assert original.read_text().strip() == str(os.getpid())


def test_unrelated_inherited_fd_is_not_touched(tmp_path):
    path = claim(tmp_path)
    other = tmp_path / "other"
    other.write_text("keep")
    with other.open("r+") as fd:
        assert token_lock.clear_token_claim(path, (os.getpid(),), fd.fileno()) == "cleared"
        assert other.read_text() == "keep"
        assert not fd.closed


def test_repeated_busy_cleanup_does_not_leak_descriptors(tmp_path):
    descriptors = Path("/proc/self/fd")
    if not descriptors.exists():
        pytest.skip("proc descriptor inventory unavailable")
    path = claim(tmp_path)
    with path.open("r+") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = len(list(descriptors.iterdir()))
        for _ in range(50):
            assert token_lock.clear_token_claim(path, (), -1) == "busy"
        assert len(list(descriptors.iterdir())) == before


def test_cli_reports_only_cleanup_status(tmp_path, capsys):
    path = claim(tmp_path)
    assert token_lock.main(["--path", str(path), "--expected-pid", str(os.getpid()),
                           "--held-fd", "-1"]) == 0
    assert capsys.readouterr().out == '{"schema": "ccc.token-lock-cleanup.v1", "status": "cleared"}\n'
