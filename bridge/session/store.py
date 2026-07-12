import asyncio
import copy
import errno
import json
import logging
import math
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)


class SessionStoreCorruptionError(RuntimeError):
    """Raised when session state exists but no valid copy can be loaded."""


class SessionStoreDurabilityError(OSError):
    """Raised after an atomic replace whose directory sync could not be confirmed."""

    def __init__(self, destination: Path, cause: OSError):
        super().__init__(cause.errno, f"directory fsync failed for {destination}: {cause}")
        self.destination = destination


class SessionStoreValidationError(ValueError):
    """Raised when decoded session state does not match the persisted schema."""


def _validate_json_value(
    value: Any, location: str, active_containers: set[int], depth: int = 0
) -> None:
    """Require values whose Python types survive a JSON encode/decode unchanged."""
    if depth > 256:
        raise SessionStoreValidationError(
            f"JSON value exceeds maximum nesting depth at {location}"
        )
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise SessionStoreValidationError(
                f"non-finite number is not valid session JSON at {location}"
            )
        return
    if type(value) not in {dict, list}:
        raise SessionStoreValidationError(
            f"non-canonical JSON type at {location}: {type(value).__name__}"
        )

    marker = id(value)
    if marker in active_containers:
        raise SessionStoreValidationError(f"cyclic JSON value at {location}")
    active_containers.add(marker)
    try:
        if type(value) is dict:
            for key, nested_value in value.items():
                if type(key) is not str:
                    raise SessionStoreValidationError(
                        f"JSON object key at {location} must be a string, "
                        f"got {type(key).__name__}"
                    )
                _validate_json_value(
                    nested_value,
                    f"{location}.{key}",
                    active_containers,
                    depth + 1,
                )
        else:
            for index, nested_value in enumerate(value):
                _validate_json_value(
                    nested_value,
                    f"{location}[{index}]",
                    active_containers,
                    depth + 1,
                )
    finally:
        active_containers.remove(marker)


def _validate_session_data(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise SessionStoreValidationError(
            f"session store root must be an object, got {type(data).__name__}"
        )
    for key, value in data.items():
        if not isinstance(key, str):
            raise SessionStoreValidationError(
                f"session store key must be a string, got {type(key).__name__}"
            )
        prefix, separator, user_id = key.partition(":")
        if prefix != "telegram_session" or separator != ":" or not user_id:
            raise SessionStoreValidationError(f"invalid session store key: {key!r}")
        try:
            for component in user_id.split(":"):
                if not component:
                    raise ValueError("empty conversation key component")
                int(component)
        except ValueError as error:
            raise SessionStoreValidationError(
                f"invalid session conversation key: {key!r}"
            ) from error
        if not isinstance(value, dict):
            raise SessionStoreValidationError(
                f"session entry {key!r} must be an object, got {type(value).__name__}"
            )
        provider = value.get("provider")
        if provider is not None and provider not in {"claude", "codex"}:
            raise SessionStoreValidationError(
                f"session entry {key!r} has invalid provider: {provider!r}"
            )
        _validate_json_value(value, key, set())
    return data


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SessionStoreValidationError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _decode_json_object(payload: bytes, source: Path) -> Dict[str, Any]:
    try:
        data = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise
    except (SessionStoreValidationError, RecursionError) as error:
        raise SessionStoreValidationError(
            f"invalid session data in {source}: {error}"
        ) from error
    try:
        return _validate_session_data(data)
    except SessionStoreValidationError as error:
        raise SessionStoreValidationError(
            f"invalid session data in {source}: {error}"
        ) from error


_CORRUPTION_ERRORS = (
    UnicodeDecodeError,
    json.JSONDecodeError,
    SessionStoreValidationError,
)


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
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise PermissionError(f"session state must be a regular file: {path}")
    if metadata.st_nlink != 1:
        raise PermissionError(f"session state must not have multiple hard links: {path}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
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


class SessionStore:
    """Process-local JSON store; one live instance must own each storage path."""

    def __init__(self, storage_path: Path):
        self._local_data: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._storage_path = _absolute_path(Path(storage_path))
        self._initialized = False

    def validate_path(self) -> None:
        """Validate the configured storage path without creating runtime state."""
        _validate_storage_directory(self._storage_path.parent)

    def initialize(self) -> None:
        """Create and load the session store at the explicit runtime boundary."""
        if self._initialized:
            return
        _ensure_storage_directory(self._storage_path.parent)
        _secure_existing_state_file(self._storage_path)
        _secure_existing_state_file(self._backup_path)
        self._load_local_data()
        self._initialized = True
        logger.info(f"Using local JSON storage at {self._storage_path}")

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError("SessionStore is not initialized")

    @property
    def _backup_path(self) -> Path:
        return self._storage_path.with_name(f"{self._storage_path.name}.bak")

    @staticmethod
    def _read_json_object(path: Path) -> Dict[str, Any]:
        return _decode_json_object(path.read_bytes(), path)

    def _load_local_data(self):
        primary_error = None
        if not self._storage_path.exists():
            if not self._backup_path.exists():
                return
            logger.error(
                "Local session data is missing; attempting previous-good backup %s",
                self._backup_path,
            )
        else:
            try:
                self._local_data = self._read_json_object(self._storage_path)
                return
            except _CORRUPTION_ERRORS as error:
                primary_error = error
                logger.error("Confirmed corrupt local session data: %s", error)

        if not self._backup_path.exists():
            raise SessionStoreCorruptionError(
                f"Session store {self._storage_path} is corrupt and has no valid backup"
            ) from primary_error

        try:
            recovered = self._read_json_object(self._backup_path)
        except _CORRUPTION_ERRORS as backup_error:
            raise SessionStoreCorruptionError(
                f"Session store {self._storage_path} is corrupt and has no valid backup"
            ) from backup_error

        payload = (
            json.dumps(
                recovered, ensure_ascii=False, indent=2, allow_nan=False
            )
            + "\n"
        ).encode("utf-8")
        _atomic_write_bytes(self._storage_path, payload)
        self._local_data = recovered
        logger.warning(
            "Recovered local session data from previous-good backup %s",
            self._backup_path,
        )

    def _save_local_data(self):
        try:
            _validate_session_data(self._local_data)
            payload = (
                json.dumps(
                    self._local_data,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                ) + "\n"
            ).encode("utf-8")
            if self._storage_path.exists():
                previous_payload = self._storage_path.read_bytes()
                # Parse the exact bytes destined for backup to avoid a TOCTOU
                # gap between validation and previous-good preservation.
                _decode_json_object(previous_payload, self._storage_path)
                _atomic_write_bytes(self._backup_path, previous_payload)
            _atomic_write_bytes(self._storage_path, payload)
        except Exception as e:
            logger.error(f"Failed to save local session data: {e}")
            raise

    def _commit(self, mutation) -> None:
        """Apply one in-memory mutation and roll it back if persistence fails."""
        previous = copy.deepcopy(self._local_data)
        try:
            mutation()
            self._save_local_data()
        except SessionStoreDurabilityError as error:
            # If the primary rename committed, disk already contains the new
            # state. Preserve the matching memory state while surfacing that
            # crash-durability could not be confirmed. Backup-only failures
            # happen before the primary replace and still require rollback.
            if error.destination != self._storage_path:
                self._local_data = previous
            raise
        except Exception:
            self._local_data = previous
            raise

    def _key(self, user_id: int) -> str:
        return f"telegram_session:{user_id}"

    async def list_sessions(self) -> Dict[str, Dict[str, Any]]:
        """Return a deep-copied map of conversation-key suffixes to sessions."""
        self._require_initialized()
        prefix = "telegram_session:"
        async with self._lock:
            return {
                key[len(prefix) :]: copy.deepcopy(value)
                for key, value in self._local_data.items()
                if key.startswith(prefix) and isinstance(value, dict)
            }

    async def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        # Return a deep copy, never the live stored dict. Callers throughout the
        # bot mutate the returned session in place and only persist via
        # update_session(); handing out the live reference let those mutations
        # (and, under concurrent same-user handlers, each other's edits) leak
        # into the store before — or without — an explicit commit.
        self._require_initialized()
        key = self._key(user_id)
        async with self._lock:
            value = self._local_data.get(key)
            return copy.deepcopy(value) if value is not None else None

    async def set(
        self, user_id: int, data: Dict[str, Any], ttl: Optional[int] = None
    ) -> None:
        self._require_initialized()
        del ttl  # kept for API compatibility; local JSON storage has no TTL
        key = self._key(user_id)
        value = copy.deepcopy(data)
        async with self._lock:
            self._commit(lambda: self._local_data.__setitem__(key, value))

    async def delete(self, user_id: int) -> None:
        self._require_initialized()
        key = self._key(user_id)
        async with self._lock:
            if key in self._local_data:
                self._commit(lambda: self._local_data.pop(key))

    async def update(self, user_id: int, updates: Dict[str, Any]) -> None:
        # Build the new state from a private copy of the stored dict so an
        # earlier get()'s returned object can't alias the base being updated.
        self._require_initialized()
        key = self._key(user_id)
        async with self._lock:
            data = copy.deepcopy(self._local_data.get(key, {}))
            data.update(copy.deepcopy(updates))
            self._commit(lambda: self._local_data.__setitem__(key, data))

    async def patch(
        self,
        user_id: int,
        *,
        updates: Optional[Mapping[str, Any]] = None,
        remove_fields: Iterable[str] = (),
    ) -> None:
        """Atomically merge fields and remove fields without stale replacement."""
        self._require_initialized()
        key = self._key(user_id)
        update_copy = copy.deepcopy(dict(updates or {}))
        removals = tuple(remove_fields)
        async with self._lock:
            data = copy.deepcopy(self._local_data.get(key, {}))
            changed = False
            for field in removals:
                if field in data:
                    data.pop(field)
                    changed = True
            for field, value in update_copy.items():
                if field not in data or data[field] != value:
                    data[field] = value
                    changed = True
            if changed:
                self._commit(lambda: self._local_data.__setitem__(key, data))

    async def patch_if(
        self,
        user_id: int,
        *,
        expected: Mapping[str, Any],
        updates: Optional[Mapping[str, Any]] = None,
        remove_fields: Iterable[str] = (),
    ) -> bool:
        """Compare and patch one session atomically under the per-store lock."""
        self._require_initialized()
        key = self._key(user_id)
        expected_copy = copy.deepcopy(dict(expected))
        update_copy = copy.deepcopy(dict(updates or {}))
        removals = tuple(remove_fields)
        async with self._lock:
            data = copy.deepcopy(self._local_data.get(key, {}))
            if any(
                field not in data or data[field] != value
                for field, value in expected_copy.items()
            ):
                return False
            for field in removals:
                data.pop(field, None)
            data.update(update_copy)
            self._commit(lambda: self._local_data.__setitem__(key, data))
            return True
