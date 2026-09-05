"""Shared owner-only filesystem primitives for bridge persistence.

setup.sh installs this exact file as ``~/.claude/hooks/ccc_secure_fs.py`` and
``scripts/ccc_secure_fs.py`` re-exports it inside the repository, so every
standalone script (skill promotion, doctor, agent-cron, memory probe, Codex
materializer) shares one implementation of the owner-only read / append /
atomic-replace / flock / clock helpers (#1484). Keep it stdlib-only.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import secrets
import stat
import tempfile
import time
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SecureFsError(Exception):
    """An owner-only invariant failed.

    ``reason`` is one of ``"unsafe"`` (symlink, wrong owner/mode/link count,
    not a regular file, empty when non-empty was required), ``"too_large"``
    (size above the caller's bound before reading) or ``"changed"`` (the file
    changed or grew past the bound while being read). Plain ``OSError`` from
    ``lstat``/``open`` is deliberately *not* wrapped so callers keep their
    existing missing-file handling.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def utc_now_iso(*, timespec: str = "seconds") -> str:
    """Current UTC time as ISO-8601 with a ``Z`` suffix (``timespec`` as isoformat)."""
    return datetime.now(timezone.utc).isoformat(timespec=timespec).replace("+00:00", "Z")


def bounded_int_env(
    env: Mapping[str, str],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
    *,
    clamp: bool = False,
) -> int:
    """Integer from ``env[key]`` bounded to ``[minimum, maximum]``.

    Unparseable values fall back to ``default``. Out-of-range values fall back
    to ``default`` too unless ``clamp`` is set, in which case they are clamped.
    """
    raw = env.get(key)
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    if clamp:
        return min(max(value, minimum), maximum)
    return value if minimum <= value <= maximum else default


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def _stat_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _check_owner_only_regular(
    metadata: os.stat_result,
    *,
    owner_id: int,
    unsafe_mode_mask: int,
    exact_mode: int | None,
) -> None:
    if owner_only_regular_violation(
        metadata, owner_id=owner_id, unsafe_mode_mask=unsafe_mode_mask
    ) is not None:
        raise SecureFsError("unsafe")
    if exact_mode is not None and stat.S_IMODE(metadata.st_mode) != exact_mode:
        raise SecureFsError("unsafe")


def read_owner_only_bytes(
    path: str | os.PathLike[str],
    *,
    max_bytes: int,
    owner_id: int | None = None,
    unsafe_mode_mask: int = 0o022,
    exact_mode: int | None = None,
    require_nonempty: bool = False,
) -> tuple[bytes, os.stat_result]:
    """Read a small owner-only regular file without following link races.

    ``lstat`` refuses symlinks up front, the file is opened ``O_NOFOLLOW`` and
    the descriptor is re-checked (same dev/ino, regular, single link, owned by
    ``owner_id`` — default the effective uid — mode not matching
    ``unsafe_mode_mask``, optionally exactly ``exact_mode``). At most
    ``max_bytes`` are accepted; a post-read ``fstat`` must match the pre-read
    one or ``SecureFsError("changed")`` is raised. Returns the payload and the
    final ``fstat`` result.
    """
    owner = os.geteuid() if owner_id is None else owner_id
    linked = os.lstat(path)
    if stat.S_ISLNK(linked.st_mode):
        raise SecureFsError("unsafe")
    descriptor = os.open(path, os.O_RDONLY | _CLOEXEC | _NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (linked.st_dev, linked.st_ino):
            raise SecureFsError("unsafe")
        _check_owner_only_regular(
            before, owner_id=owner, unsafe_mode_mask=unsafe_mode_mask, exact_mode=exact_mode
        )
        if require_nonempty and before.st_size <= 0:
            raise SecureFsError("unsafe")
        if before.st_size > max_bytes:
            raise SecureFsError("too_large")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(payload) > max_bytes or _stat_signature(before) != _stat_signature(after):
            raise SecureFsError("changed")
        return payload, after
    finally:
        os.close(descriptor)


def parse_jsonl_rows(text: str, *, on_invalid: str = "skip") -> list[dict[str, Any]]:
    """Per-line ``json.loads`` keeping only objects.

    ``on_invalid="skip"`` drops undecodable lines; ``"raise"`` re-raises the
    ``json.JSONDecodeError``. Non-object rows are always dropped.
    """
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if on_invalid == "raise":
                raise
            continue
        if isinstance(record, dict):
            rows.append(record)
    return rows


def read_jsonl_rows(
    path: str | os.PathLike[str],
    *,
    max_bytes: int,
    on_invalid: str = "skip",
    owner_id: int | None = None,
    unsafe_mode_mask: int = 0o022,
    exact_mode: int | None = None,
) -> list[dict[str, Any]]:
    """``read_owner_only_bytes`` + UTF-8 decode + ``parse_jsonl_rows``."""
    payload, _ = read_owner_only_bytes(
        path,
        max_bytes=max_bytes,
        owner_id=owner_id,
        unsafe_mode_mask=unsafe_mode_mask,
        exact_mode=exact_mode,
    )
    return parse_jsonl_rows(payload.decode("utf-8"), on_invalid=on_invalid)


def json_line(value: object) -> str:
    """Canonical compact JSONL serialization (sorted keys, no ASCII escaping)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def append_jsonl_line(
    path: str | os.PathLike[str],
    record: object,
    *,
    mode: int = 0o600,
    fsync: bool = True,
    owner_id: int | None = None,
) -> None:
    """Append ``json_line(record)`` + newline to an owner-only JSONL file.

    The file is created ``mode`` when missing and opened ``O_APPEND`` /
    ``O_NOFOLLOW``; the descriptor must then be a single-link regular file
    owned by ``owner_id`` (default effective uid) with exactly ``mode``. A
    symlink or any violated invariant raises ``SecureFsError("unsafe")``
    before a byte is written.
    """
    owner = os.geteuid() if owner_id is None else owner_id
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | _CLOEXEC | _NOFOLLOW
    try:
        descriptor = os.open(path, flags, mode)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SecureFsError("unsafe") from None
        raise
    try:
        _check_owner_only_regular(
            os.fstat(descriptor), owner_id=owner, unsafe_mode_mask=0, exact_mode=mode
        )
        _write_all(descriptor, (json_line(record) + "\n").encode("utf-8"))
        if fsync:
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(
    path: str | os.PathLike[str],
    payload: bytes,
    *,
    mode: int | None = None,
    durable: bool = True,
    resolve_symlink: bool = False,
) -> bool:
    """Replace ``path`` through a private same-directory temp file and rename.

    A reader sees either the old or the new file, never a truncated one, and
    the temp file is removed on any failure. ``mode`` is applied to the temp
    file before the rename so the target never flips permissions; ``None``
    keeps an existing regular target's mode (like setup.sh's atomic_install)
    and falls back to ``0o600``. ``durable`` fsyncs the file and then the
    directory (tolerating filesystems without directory fsync). With
    ``resolve_symlink`` a symlinked destination is written through to its
    target so the link itself survives; otherwise the link entry is replaced.
    Returns whether the directory sync was confirmed.
    """
    destination = Path(path)
    if resolve_symlink and destination.is_symlink():
        destination = destination.resolve()
    if mode is None:
        mode = 0o600
        try:
            existing = destination.lstat()
        except OSError:
            existing = None
        if existing is not None and stat.S_ISREG(existing.st_mode):
            mode = stat.S_IMODE(existing.st_mode)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(raw_temp)
    try:
        try:
            os.fchmod(descriptor, mode)
            _write_all(descriptor, payload)
            if durable:
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    if not durable:
        return False
    directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        return fsync_directory_fd(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
    durable: bool = True,
    resolve_symlink: bool = False,
) -> bool:
    """``atomic_write_bytes`` for text."""
    return atomic_write_bytes(
        path,
        text.encode(encoding),
        mode=mode,
        durable=durable,
        resolve_symlink=resolve_symlink,
    )


def open_lock_descriptor(
    path: str | os.PathLike[str],
    *,
    dir_fd: int | None = None,
    mode: int = 0o600,
    owner_id: int | None = None,
    unsafe_mode_mask: int = 0o022,
    exact_mode: int | None = None,
) -> int:
    """Open (creating ``mode``) and validate an owner-only lock file.

    ``O_NOFOLLOW`` + ``O_CLOEXEC``; the descriptor must be a single-link
    regular file owned by ``owner_id`` (default effective uid) whose mode does
    not match ``unsafe_mode_mask`` (optionally exactly ``exact_mode``), and is
    then ``fchmod``-ed to ``mode``. A symlink or violated invariant raises
    ``SecureFsError("unsafe")``; other ``OSError`` propagate. The caller owns
    the returned descriptor.
    """
    owner = os.geteuid() if owner_id is None else owner_id
    flags = os.O_RDWR | os.O_CREAT | _CLOEXEC | _NOFOLLOW
    try:
        descriptor = os.open(path, flags, mode, dir_fd=dir_fd)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SecureFsError("unsafe") from None
        raise
    try:
        _check_owner_only_regular(
            os.fstat(descriptor),
            owner_id=owner,
            unsafe_mode_mask=unsafe_mode_mask,
            exact_mode=exact_mode,
        )
        os.fchmod(descriptor, mode)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def acquire_flock(
    descriptor: int, *, blocking: bool = False, timeout: float | None = None
) -> bool:
    """Take ``LOCK_EX`` on ``descriptor``.

    ``blocking`` waits indefinitely. Otherwise ``timeout=None`` tries once and
    ``timeout=N`` polls (50 ms steps) for up to ``N`` seconds. Returns whether
    the lock was acquired; contention never raises.
    """
    if blocking:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return True
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if deadline is None or time.monotonic() >= deadline:
                return False
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


@contextlib.contextmanager
def flock_guard(
    path: str | os.PathLike[str],
    *,
    blocking: bool = False,
    timeout: float | None = None,
    dir_fd: int | None = None,
    mode: int = 0o600,
    owner_id: int | None = None,
    unsafe_mode_mask: int = 0o022,
    exact_mode: int | None = None,
) -> Iterator[bool]:
    """Hold an owner-only flock for the block; yields whether it was acquired.

    Combines ``open_lock_descriptor`` and ``acquire_flock``; the lock is
    released and the descriptor closed on exit. The parent directory must
    already exist (callers validate/create it under their own policy).
    """
    descriptor = open_lock_descriptor(
        path,
        dir_fd=dir_fd,
        mode=mode,
        owner_id=owner_id,
        unsafe_mode_mask=unsafe_mode_mask,
        exact_mode=exact_mode,
    )
    acquired = False
    try:
        acquired = acquire_flock(descriptor, blocking=blocking, timeout=timeout)
        yield acquired
    finally:
        try:
            if acquired:
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def owner_only_regular_violation(
    metadata: os.stat_result | object,
    *,
    owner_id: int,
    unsafe_mode_mask: int = 0o022,
) -> str | None:
    """Return the first owner-only regular-file invariant violation, if any."""
    mode = int(getattr(metadata, "st_mode"))
    if not stat.S_ISREG(mode):
        return "not_regular"
    if int(getattr(metadata, "st_nlink")) != 1:
        return "multiple_links"
    if int(getattr(metadata, "st_uid")) != owner_id:
        return "wrong_owner"
    if stat.S_IMODE(mode) & unsafe_mode_mask:
        return "unsafe_mode"
    return None


def fsync_directory_fd(dir_fd: int) -> bool:
    """Sync an open directory, reporting known unsupported filesystems."""
    try:
        os.fsync(dir_fd)
        return True
    except OSError as error:
        if error.errno in {errno.EINVAL, errno.ENOTSUP, errno.EROFS}:
            return False
        raise


def atomic_write_bytes_at(dir_fd: int, name: str, payload: bytes) -> bool:
    """Atomically replace a file relative to an already-validated directory."""
    temp_name = f".{name}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(temp_name, flags, 0o600, dir_fd=dir_fd)
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(errno.EIO, "short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        return fsync_directory_fd(dir_fd)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(temp_name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass


class SessionStoreDurabilityError(OSError):
    """Raised after an atomic replace whose directory sync could not be confirmed."""

    def __init__(self, destination: Path, cause: OSError):
        super().__init__(cause.errno, f"directory fsync failed for {destination}: {cause}")
        self.destination = destination


def _absolute_path(path: Path) -> Path:
    """Normalize `.`/`..` lexically without resolving symlinks."""
    return Path(os.path.abspath(os.fspath(path)))


def _termux_app_roots() -> tuple[Path, ...]:
    """Return canonical private-data aliases for a validated Termux PREFIX."""
    prefix = os.environ.get("PREFIX")
    if not prefix:
        return ()
    prefix_path = _absolute_path(Path(prefix))
    parts = prefix_path.parts
    if parts == ("/", "data", "data", "com.termux", "files", "usr"):
        user_id = "0"
    elif (
        len(parts) == 7
        and parts[:2] == ("/", "data")
        and parts[2] in {"user", "user_de"}
        and parts[3].isascii()
        and parts[3].isdecimal()
        and (parts[3] == "0" or not parts[3].startswith("0"))
        and parts[4:] == ("com.termux", "files", "usr")
    ):
        user_id = parts[3]
    else:
        return ()

    try:
        prefix_metadata = prefix_path.lstat()
    except OSError:
        return ()
    prefix_mode = stat.S_IMODE(prefix_metadata.st_mode)
    if (
        stat.S_ISLNK(prefix_metadata.st_mode)
        or not stat.S_ISDIR(prefix_metadata.st_mode)
        or prefix_metadata.st_uid != os.getuid()
        or prefix_mode & 0o022
    ):
        return ()

    roots = (
        Path(f"/data/user/{user_id}/com.termux/files"),
        Path(f"/data/user_de/{user_id}/com.termux/files"),
    )
    if user_id == "0":
        return (Path("/data/data/com.termux/files"), *roots)
    return roots


def _is_owned_termux_private_ancestor(path: Path, metadata: os.stat_result) -> bool:
    """Recognize only the current Termux app's exact private files root."""
    path = _absolute_path(path)
    mode = stat.S_IMODE(metadata.st_mode)
    process_uid = os.getuid()
    process_gid = os.getgid()
    return (
        path in _termux_app_roots()
        and metadata.st_uid == process_uid
        and metadata.st_gid == process_gid
        and process_uid == process_gid
        and not mode & 0o002
    )


def _is_trusted_android_platform_ancestor(
    path: Path, metadata: os.stat_result
) -> bool:
    """Recognize OS-owned ancestors on a validated Termux app-data path."""
    path = _absolute_path(path)
    if path == Path("/") or not any(
        path in root.parents for root in _termux_app_roots()
    ):
        return False
    mode = stat.S_IMODE(metadata.st_mode)
    process_groups = {os.getgid(), *os.getgroups()}
    return (
        metadata.st_uid in {0, 1000}
        and metadata.st_gid not in process_groups
        and not mode & 0o002
    )


def _validate_existing_directory_components(path: Path) -> None:
    """Reject symlink components and ancestors writable by process peers."""
    path = _absolute_path(path)
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise PermissionError(
                f"session store directory path contains a symlink: {current}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(
                f"session store directory component is not a directory: {current}"
            )
        if current == path:
            continue
        mode = stat.S_IMODE(metadata.st_mode)
        trusted_platform_owner = (
            metadata.st_uid in {0, os.getuid()}
            or _is_owned_termux_private_ancestor(current, metadata)
            or _is_trusted_android_platform_ancestor(current, metadata)
        )
        if not trusted_platform_owner:
            raise PermissionError(
                f"session store path has an unsafe owner ancestor: "
                f"{current} (uid={metadata.st_uid}, mode={mode:04o})"
            )
        sticky_bit = getattr(stat, "S_ISVTX", 0o1000)
        trusted_sticky = bool(
            mode & sticky_bit and metadata.st_uid in {0, os.getuid()}
        )
        if (
            mode & 0o022
            and not trusted_sticky
            and not _is_owned_termux_private_ancestor(current, metadata)
            and not _is_trusted_android_platform_ancestor(current, metadata)
        ):
            raise PermissionError(
                f"session store path has an unsafe writable ancestor: "
                f"{current} ({mode:04o})"
            )


def _create_missing_directory_components(path: Path) -> None:
    """Create components one at a time without following an existing symlink."""
    path = _absolute_path(path)
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                os.mkdir(current, mode=0o700)
            except FileExistsError:
                # A concurrent creator must still pass the no-symlink check.
                pass
            metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise PermissionError(
                f"session store directory path contains a symlink: {current}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(
                f"session store directory component is not a directory: {current}"
            )


def _validate_storage_directory(path: Path) -> None:
    """Validate an existing storage parent without creating or chmodding anything."""
    path = _absolute_path(path)
    _validate_existing_directory_components(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"session store parent is not a directory: {path}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise PermissionError(f"session store parent is not owned by this process: {path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o022:
        raise PermissionError(
            f"session store parent is writable by group or others: {path} ({mode:04o})"
        )


# Directories that already passed a full ancestor walk, keyed by absolute
# path, valued with the leaf's (st_dev, st_ino, st_mode, st_uid) fingerprint
# at validation time plus that time (monotonic). Atomic writes re-validated
# every component (~3 lstats per component) on every single write; with the
# fingerprint an unchanged leaf needs one lstat. Semantics: a matching
# fingerprint means traversing the path now reaches the same physical
# directory that passed the full walk (an ancestor swapped to redirect the
# path would resolve the leaf to a different dev/ino and miss the cache),
# and the leaf's owner/mode checks below stay dynamic. The TTL bounds how
# long a later ancestor mode/owner drift can go unnoticed: the full walk
# still re-runs at least once per minute per directory.
_VALIDATED_DIRECTORY_FINGERPRINTS: dict[
    str, tuple[tuple[int, int, int, int], float]
] = {}
_VALIDATED_DIRECTORY_FINGERPRINTS_MAX = 128
_VALIDATED_DIRECTORY_TTL_SECONDS = 60.0


def _directory_fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid)


def _ensure_storage_directory(path: Path) -> None:
    """Create a private state directory or validate an existing safe directory."""
    path = _absolute_path(path)
    cache_key = str(path)
    cached = _VALIDATED_DIRECTORY_FINGERPRINTS.get(cache_key)
    if cached is not None:
        fingerprint, validated_monotonic = cached
        try:
            metadata = path.lstat()
        except OSError:
            metadata = None
        if (
            metadata is not None
            and time.monotonic() - validated_monotonic
            < _VALIDATED_DIRECTORY_TTL_SECONDS
            and stat.S_ISDIR(metadata.st_mode)
            and _directory_fingerprint(metadata) == fingerprint
            and not stat.S_IMODE(metadata.st_mode) & 0o022
            and (not hasattr(os, "getuid") or metadata.st_uid == os.getuid())
        ):
            return
        _VALIDATED_DIRECTORY_FINGERPRINTS.pop(cache_key, None)
    _validate_existing_directory_components(path)
    try:
        path.lstat()
        existed = True
    except FileNotFoundError:
        existed = False

    _create_missing_directory_components(path)
    _validate_existing_directory_components(path)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"session store parent is not a directory: {path}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise PermissionError(f"session store parent is not owned by this process: {path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if not existed and mode != 0o700:
        path.chmod(0o700)
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o022:
        raise PermissionError(
            f"session store parent is writable by group or others: {path} ({mode:04o})"
        )
    if len(_VALIDATED_DIRECTORY_FINGERPRINTS) >= _VALIDATED_DIRECTORY_FINGERPRINTS_MAX:
        _VALIDATED_DIRECTORY_FINGERPRINTS.clear()
    _VALIDATED_DIRECTORY_FINGERPRINTS[cache_key] = (
        _directory_fingerprint(metadata),
        time.monotonic(),
    )


def ensure_private_directory(path: Path) -> None:
    """Create or validate a process-owned directory without following symlinks."""
    _ensure_storage_directory(path)


def _secure_existing_state_file(path: Path) -> None:
    """Tighten a legacy state file without following symlinks or hard links."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    violation = owner_only_regular_violation(
        metadata,
        owner_id=os.getuid(),
        unsafe_mode_mask=0,
    )
    if violation == "not_regular" or path.is_symlink():
        raise PermissionError(f"session state must be a regular file: {path}")
    if violation == "multiple_links":
        raise PermissionError(f"session state must not have multiple hard links: {path}")
    if violation == "wrong_owner":
        raise PermissionError(f"session state is not owned by this process: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        path.chmod(0o600)


def _fsync_directory(path: Path) -> None:
    """Durably record a rename, tolerating only known unsupported operations."""
    fd = None
    unsupported_errors = {
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(path, flags)
        os.fsync(fd)
    except OSError as error:
        if error.errno not in unsupported_errors:
            raise
        logger.warning("Directory fsync unavailable for %s: %s", path, error)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError as error:
                # Closing an already-fsynced directory descriptor cannot undo
                # the rename and must not trigger an in-memory rollback.
                logger.warning("Directory close failed for %s: %s", path, error)


def _atomic_write_bytes(destination: Path, payload: bytes) -> None:
    """Write *payload* via a private same-directory temp file and replace."""
    _ensure_storage_directory(destination.parent)
    fd, raw_temp_path = tempfile.mkstemp(
        prefix=f".{destination.name}.tmp-", dir=destination.parent
    )
    temp_path = Path(raw_temp_path)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, destination)
        try:
            _fsync_directory(destination.parent)
        except OSError as error:
            raise SessionStoreDurabilityError(destination, error) from error
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError as close_error:
                logger.warning("Temporary file close failed for %s: %s", temp_path, close_error)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError as unlink_error:
            logger.warning("Temporary file cleanup failed for %s: %s", temp_path, unlink_error)
        raise
