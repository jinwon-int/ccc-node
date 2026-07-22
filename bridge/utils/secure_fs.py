"""Shared owner-only filesystem primitives for bridge persistence."""

from __future__ import annotations

import errno
import logging
import os
import secrets
import stat
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


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


def _ensure_storage_directory(path: Path) -> None:
    """Create a private state directory or validate an existing safe directory."""
    path = _absolute_path(path)
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
