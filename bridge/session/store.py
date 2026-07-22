import asyncio
import copy
import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from telegram_bot.utils import secure_fs

logger = logging.getLogger(__name__)


class SessionStoreCorruptionError(RuntimeError):
    """Raised when session state exists but no valid copy can be loaded."""


SessionStoreDurabilityError = secure_fs.SessionStoreDurabilityError


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
        if provider is not None and (
            not isinstance(provider, str) or provider not in {"claude", "codex"}
        ):
            raise SessionStoreValidationError(
                f"session entry {key!r} has invalid provider: {provider!r}"
            )
        effort = value.get("effort")
        if effort is not None and (not isinstance(effort, str) or not effort):
            raise SessionStoreValidationError(
                f"session entry {key!r} has invalid effort: {effort!r}"
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


class SessionStore:
    """Process-local JSON store; one live instance must own each storage path."""

    def __init__(self, storage_path: Path):
        self._local_data: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._storage_path = secure_fs._absolute_path(Path(storage_path))
        self._initialized = False

    def validate_path(self) -> None:
        """Validate the configured storage path without creating runtime state."""
        secure_fs._validate_storage_directory(self._storage_path.parent)

    def initialize(self) -> None:
        """Create and load the session store at the explicit runtime boundary."""
        if self._initialized:
            return
        secure_fs._ensure_storage_directory(self._storage_path.parent)
        secure_fs._secure_existing_state_file(self._storage_path)
        secure_fs._secure_existing_state_file(self._backup_path)
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
        secure_fs._atomic_write_bytes(self._storage_path, payload)
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
                secure_fs._atomic_write_bytes(self._backup_path, previous_payload)
            secure_fs._atomic_write_bytes(self._storage_path, payload)
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
