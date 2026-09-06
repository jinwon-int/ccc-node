"""Clear token PID metadata without replacing the inode that carries flock."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import stat


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):
        # An inaccessible or unrepresentable PID is not evidence of death.
        return True
    return True


def _safe_parent(path: Path) -> bool:
    for parent in path.parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode)
                or info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
            return False
    parent = path.parent.stat()
    return parent.st_uid == os.getuid() and not parent.st_mode & 0o022


def clear_token_claim(path: Path, expected_pids: tuple[int, ...] = (),
                      held_fd: int = 8) -> str:
    """Best-effort cleanup; a busy/unsafe/foreign claim is left untouched.

    The launcher deliberately passes its flock on fd 8 through exec. Dup that
    open description when available: opening the same file again would
    contend with our own lock. Closing our duplicate never unlocks the bot's
    inherited descriptor. Metadata uses a separate read/write descriptor,
    since the launcher opens fd 8 write-only. Other callers acquire a separate
    nonblocking lock.
    """
    try:
        path = Path(os.path.abspath(path))
        if not _safe_parent(path):
            return "unsafe"
        with ExitStack() as resources:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
            resources.callback(os.close, fd)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1):
                return "unsafe"
            try:
                inherited = os.fstat(held_fd)
            except (OSError, ValueError, OverflowError):
                inherited = None
            lock_fd = fd
            if inherited and (inherited.st_dev, inherited.st_ino) == (info.st_dev, info.st_ino):
                lock_fd = os.dup(held_fd)
                resources.callback(os.close, lock_fd)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "busy"
            # Load only after locking, never using a pre-lock PID decision. All
            # participating cleanup paths retain this inode, even after exit.
            recorded = os.pread(fd, 65, 0)
            if len(recorded) > 64:
                return "unsafe"
            value = recorded.strip()
            if value:
                if not value.isdigit():
                    return "unsafe"
                pid = int(value)
                if pid <= 1:
                    return "unsafe"
                if pid not in expected_pids and _alive(pid):
                    return "preserved"
            current = path.lstat()
            if ((current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                    or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1):
                return "unsafe"
            os.fchmod(fd, 0o600)
            os.ftruncate(fd, 0)
            os.fsync(fd)
            return "cleared"
    except FileNotFoundError:
        return "absent"
    except (OSError, ValueError, OverflowError):
        return "unsafe"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--expected-pid", type=int, action="append", default=[])
    parser.add_argument("--held-fd", type=int, default=8)
    args = parser.parse_args(argv)
    status = clear_token_claim(args.path, tuple(args.expected_pid), args.held_fd)
    print(json.dumps({"schema": "ccc.token-lock-cleanup.v1", "status": status}))
    # Cleanup is optional bookkeeping. Failure to prove it safe never
    # releases a live claim or makes shutdown raise an unrelated exception.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
