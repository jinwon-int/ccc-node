import asyncio
import copy
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any
from telegram_bot.utils.config import config

logger = logging.getLogger(__name__)


class SessionStoreCorruptionError(RuntimeError):
    """Raised when session state exists but no valid copy can be loaded."""


def _fsync_directory(path: Path) -> None:
    """Durably record a rename when the filesystem supports directory fsync."""
    fd = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(path, flags)
        os.fsync(fd)
    except OSError as error:
        # The file itself was already fsynced and atomically replaced. Some
        # overlay/network filesystems reject directory fsync; treating that as
        # a failed commit would roll memory back after disk already changed.
        logger.warning("Directory fsync unavailable for %s: %s", path, error)
    finally:
        if fd is not None:
            os.close(fd)


def _atomic_write_bytes(destination: Path, payload: bytes) -> None:
    """Write *payload* via a private same-directory temp file and replace."""
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        _fsync_directory(destination.parent)
    except Exception:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)
        raise


class SessionStore:
    def __init__(self, storage_path: Optional[Path] = None):
        self._local_data: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._storage_path = Path(storage_path or config.session_store_path)
        self._storage_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._storage_path.parent, 0o700)
        self._load_local_data()
        logger.info(f"Using local JSON storage at {self._storage_path}")

    @property
    def _backup_path(self) -> Path:
        return self._storage_path.with_name(f"{self._storage_path.name}.bak")

    @staticmethod
    def _read_json_object(path: Path) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError(f"session store root must be an object, got {type(data).__name__}")
        return data

    def _load_local_data(self):
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
            except Exception as primary_error:
                logger.error(f"Failed to load local session data: {primary_error}")

        try:
            recovered = self._read_json_object(self._backup_path)
        except Exception as backup_error:
            raise SessionStoreCorruptionError(
                f"Session store {self._storage_path} is corrupt and has no valid backup"
            ) from backup_error

        payload = (
            json.dumps(recovered, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        _atomic_write_bytes(self._storage_path, payload)
        self._local_data = recovered
        logger.warning(
            "Recovered local session data from previous-good backup %s",
            self._backup_path,
        )

    def _save_local_data(self):
        try:
            payload = (
                json.dumps(self._local_data, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            if self._storage_path.exists():
                previous_payload = self._storage_path.read_bytes()
                # Never promote a malformed primary to the recovery slot.
                self._read_json_object(self._storage_path)
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
        except Exception:
            self._local_data = previous
            raise

    def _key(self, user_id: int) -> str:
        return f"telegram_session:{user_id}"

    async def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        # Return a deep copy, never the live stored dict. Callers throughout the
        # bot mutate the returned session in place and only persist via
        # update_session(); handing out the live reference let those mutations
        # (and, under concurrent same-user handlers, each other's edits) leak
        # into the store before — or without — an explicit commit.
        key = self._key(user_id)
        async with self._lock:
            value = self._local_data.get(key)
            return copy.deepcopy(value) if value is not None else None

    async def set(
        self, user_id: int, data: Dict[str, Any], ttl: Optional[int] = None
    ) -> None:
        del ttl  # kept for API compatibility; local JSON storage has no TTL
        key = self._key(user_id)
        value = copy.deepcopy(data)
        async with self._lock:
            self._commit(lambda: self._local_data.__setitem__(key, value))

    async def delete(self, user_id: int) -> None:
        key = self._key(user_id)
        async with self._lock:
            if key in self._local_data:
                self._commit(lambda: self._local_data.pop(key))

    async def update(self, user_id: int, updates: Dict[str, Any]) -> None:
        # Build the new state from a private copy of the stored dict so an
        # earlier get()'s returned object can't alias the base being updated.
        key = self._key(user_id)
        async with self._lock:
            data = copy.deepcopy(self._local_data.get(key, {}))
            data.update(copy.deepcopy(updates))
            self._commit(lambda: self._local_data.__setitem__(key, data))


session_store = SessionStore()
