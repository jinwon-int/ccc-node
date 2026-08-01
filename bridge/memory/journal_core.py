"""Provider-neutral owner-only JSON journal storage and process claims."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Iterator, Mapping

try:
    from telegram_bot.utils.secure_fs import (
        _atomic_write_bytes,
        _fsync_directory,
        _validate_storage_directory,
        ensure_private_directory,
    )
except ModuleNotFoundError:  # Standalone hook install beside ccc_secure_fs.py.
    from ccc_secure_fs import (
        _atomic_write_bytes,
        _fsync_directory,
        _validate_storage_directory,
        ensure_private_directory,
    )


_RECORD_ID_RE = re.compile(r"^[0-9a-f]{64}$")


class JsonJournalCore:
    """One process-safe directory of bounded JSON records.

    Schema adapters own record validation. This class alone owns filesystem
    validation, atomic serialization, directory durability and per-record
    process claims.
    """

    def __init__(self, root: Path, *, max_record_bytes: int = 1024 * 1024) -> None:
        if max_record_bytes <= 0:
            raise ValueError("max_record_bytes must be positive")
        self.root = Path(os.path.abspath(os.fspath(root)))
        self.max_record_bytes = max_record_bytes
        self._thread_lock = threading.RLock()
        self._lock_path = self.root / ".journal.lock"
        self._initialized = False

    def validate_path(self) -> None:
        _validate_storage_directory(self.root)
        if self.root.exists():
            self._validate_root()
        if self._lock_path.exists() or self._lock_path.is_symlink():
            self._validate_regular_file(self._lock_path)
        for path in self.root.glob("*.json") if self.root.exists() else ():
            self._validate_record_name(path)
            self._validate_regular_file(path)
        for path in self.root.glob("*.json.lock") if self.root.exists() else ():
            self._validate_claim_name(path)
            self._validate_regular_file(path)

    def initialize(self) -> None:
        if self._initialized:
            return
        ensure_private_directory(self.root)
        self._validate_root()
        lock_existed = self._lock_path.exists() or self._lock_path.is_symlink()
        if lock_existed:
            self._validate_regular_file(self._lock_path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self._lock_path, flags, 0o600)
        try:
            if not lock_existed:
                os.fchmod(fd, 0o600)
            self._validate_fd(fd, self._lock_path)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(self.root)
        self._initialized = True

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError("journal is not initialized")

    def _validate_root(self) -> None:
        metadata = self.root.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PermissionError(f"journal root must be a directory: {self.root}")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("journal root is not owned by this process")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode != 0o700:
            raise PermissionError(f"journal root must have mode 0700, got {mode:04o}")

    @staticmethod
    def _validate_fd(fd: int, path: Path) -> None:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError(f"journal state must be a regular file: {path}")
        if metadata.st_nlink != 1:
            raise PermissionError(f"journal state must not have hard links: {path}")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("journal state is not owned by this process")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError(f"journal state must have mode 0600: {path}")

    def _open_regular(self, path: Path, flags: int = os.O_RDONLY) -> int:
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as error:
            raise PermissionError(f"unsafe journal state: {path}") from error
        try:
            self._validate_fd(fd, path)
        except Exception:
            os.close(fd)
            raise
        return fd

    def _validate_regular_file(self, path: Path) -> None:
        fd = self._open_regular(path)
        os.close(fd)

    @staticmethod
    def _validate_record_id(record_id: str) -> None:
        if not _RECORD_ID_RE.fullmatch(record_id):
            raise ValueError("invalid journal record id")

    @classmethod
    def _validate_record_name(cls, path: Path) -> None:
        if path.suffix != ".json" or not _RECORD_ID_RE.fullmatch(path.stem):
            raise PermissionError(f"invalid journal record path: {path.name}")

    @staticmethod
    def _validate_claim_name(path: Path) -> None:
        name = path.name
        if not name.endswith(".json.lock") or not _RECORD_ID_RE.fullmatch(name[:-10]):
            raise PermissionError(f"invalid journal claim path: {name}")

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        self._require_initialized()
        with self._thread_lock:
            self._validate_root()
            fd = self._open_regular(self._lock_path, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def record_path(self, record_id: str) -> Path:
        self._validate_record_id(record_id)
        return self.root / f"{record_id}.json"

    def claim_path(self, record_id: str) -> Path:
        return Path(f"{self.record_path(record_id)}.lock")

    def _read_json_unlocked(self, record_id: str) -> dict[str, Any]:
        path = self.record_path(record_id)
        fd = self._open_regular(path)
        try:
            chunks: list[bytes] = []
            remaining = self.max_record_bytes + 1
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(fd)
        payload = b"".join(chunks)
        if len(payload) > self.max_record_bytes:
            raise ValueError("journal record exceeds maximum size")
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("journal record must be an object")
        return value

    def _write_json_unlocked(
        self, record_id: str, value: Mapping[str, Any]
    ) -> None:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(payload) > self.max_record_bytes:
            raise ValueError("journal record exceeds maximum size")
        path = self.record_path(record_id)
        if path.exists() or path.is_symlink():
            self._validate_regular_file(path)
        _atomic_write_bytes(path, payload)
        self._validate_regular_file(path)

    def list_record_ids(self) -> tuple[str, ...]:
        with self._exclusive():
            paths = sorted(self.root.glob("*.json"))
            for path in paths:
                self._validate_record_name(path)
            return tuple(path.stem for path in paths)

    @contextmanager
    def claim_record(self, record_id: str) -> Iterator[bool]:
        """Hold a nonblocking process claim until the context exits."""
        self._require_initialized()
        path = self.claim_path(record_id)
        if path.is_symlink():
            raise PermissionError("journal claim must not be a symlink")
        base_flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        base_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, base_flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(path, base_flags)
            created = False
        claimed = False
        try:
            if created:
                os.fchmod(fd, 0o600)
            self._validate_fd(fd, path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            claimed = True
            yield True
        finally:
            if claimed:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def is_claimable(self, record_id: str) -> bool:
        with self.claim_record(record_id) as claimed:
            return claimed

    def complete_claimed(self, record_id: str) -> None:
        """Durably remove a successfully processed record and its claim name."""
        with self._exclusive():
            path = self.record_path(record_id)
            self._validate_regular_file(path)
            path.unlink()
            try:
                self.claim_path(record_id).unlink()
            except FileNotFoundError:
                pass
            _fsync_directory(self.root)
