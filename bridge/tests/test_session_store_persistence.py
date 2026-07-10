"""Regression tests for atomic, corruption-recoverable SessionStore persistence."""

import asyncio
import json
import os
import stat
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

BRIDGE_DIR = Path(__file__).resolve().parents[1]
if "telegram_bot" not in sys.modules:
    package = types.ModuleType("telegram_bot")
    package.__path__ = [str(BRIDGE_DIR)]
    sys.modules["telegram_bot"] = package

from telegram_bot.session.store import (  # noqa: E402
    SessionStore,
    SessionStoreCorruptionError,
)


def run(awaitable):
    return asyncio.run(awaitable)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class FailingStream:
    def __init__(self, stream, operation: str):
        self._stream = stream
        self._operation = operation

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, *args):
        return self._stream.__exit__(*args)

    def write(self, payload):
        if self._operation == "write":
            raise OSError("write failed")
        return self._stream.write(payload)

    def flush(self):
        if self._operation == "flush":
            raise OSError("flush failed")
        return self._stream.flush()

    def fileno(self):
        return self._stream.fileno()


def test_first_save_is_atomic_and_private(tmp_path):
    path = tmp_path / "state" / "sessions.json"
    store = SessionStore(path)

    run(store.set(1, {"session_id": "one"}))

    assert read_json(path) == {"telegram_session:1": {"session_id": "one"}}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert list(path.parent.glob(f".{path.name}.tmp-*")) == []


def test_serialization_failure_preserves_disk_and_memory(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    run(store.set(1, {"session_id": "stable"}))
    before = path.read_bytes()

    with pytest.raises(TypeError):
        run(store.set(2, {"not_json": object()}))

    assert path.read_bytes() == before
    assert run(store.get(2)) is None
    assert run(store.get(1)) == {"session_id": "stable"}


@pytest.mark.parametrize("operation", ["write", "flush"])
def test_temp_write_failure_preserves_disk_and_memory(tmp_path, operation):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    run(store.set(1, {"version": 1}))
    before = path.read_bytes()
    real_fdopen = os.fdopen

    def failing_fdopen(*args, **kwargs):
        return FailingStream(real_fdopen(*args, **kwargs), operation)

    with patch("telegram_bot.session.store.os.fdopen", side_effect=failing_fdopen):
        with pytest.raises(OSError, match=f"{operation} failed"):
            run(store.update(1, {"version": 2}))

    assert path.read_bytes() == before
    assert run(store.get(1)) == {"version": 1}
    assert list(path.parent.glob(f".{path.name}.tmp-*")) == []


def test_file_fsync_failure_preserves_disk_and_memory(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    run(store.set(1, {"version": 1}))
    before = path.read_bytes()

    with patch("telegram_bot.session.store.os.fsync", side_effect=OSError("fsync failed")):
        with pytest.raises(OSError, match="fsync failed"):
            run(store.update(1, {"version": 2}))

    assert path.read_bytes() == before
    assert run(store.get(1)) == {"version": 1}
    assert list(path.parent.glob(f".{path.name}.tmp-*")) == []


def test_unsupported_directory_fsync_keeps_committed_state(tmp_path, caplog):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    run(store.set(1, {"version": 1}))
    real_fsync = os.fsync

    def fail_for_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory fsync unsupported")
        return real_fsync(fd)

    with patch("telegram_bot.session.store.os.fsync", side_effect=fail_for_directory):
        run(store.update(1, {"version": 2}))

    assert read_json(path) == {"telegram_session:1": {"version": 2}}
    assert run(store.get(1)) == {"version": 2}
    assert "Directory fsync unavailable" in caplog.text


def test_primary_replace_failure_preserves_disk_and_memory(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    run(store.set(1, {"version": 1}))
    before = path.read_bytes()
    real_replace = os.replace

    def fail_primary_replace(source, destination):
        if Path(destination) == path:
            raise OSError("replace failed")
        real_replace(source, destination)

    with patch("telegram_bot.session.store.os.replace", side_effect=fail_primary_replace):
        with pytest.raises(OSError, match="replace failed"):
            run(store.update(1, {"version": 2}))

    assert path.read_bytes() == before
    assert run(store.get(1)) == {"version": 1}
    assert list(path.parent.glob(f".{path.name}.tmp-*")) == []


def test_corrupt_primary_recovers_previous_good_backup(tmp_path, caplog):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    run(store.set(1, {"version": 1}))
    run(store.update(1, {"version": 2}))
    backup_path = path.with_name(f"{path.name}.bak")
    assert backup_path.exists()
    assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600

    path.write_text("{ truncated", encoding="utf-8")
    recovered = SessionStore(path)

    assert run(recovered.get(1)) == {"version": 1}
    assert read_json(path) == {"telegram_session:1": {"version": 1}}
    assert "Recovered local session data" in caplog.text


def test_corrupt_primary_without_backup_fails_closed(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("{ truncated", encoding="utf-8")

    with pytest.raises(SessionStoreCorruptionError, match="no valid backup"):
        SessionStore(path)


def test_missing_primary_recovers_previous_good_backup(tmp_path, caplog):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    run(store.set(1, {"version": 1}))
    run(store.update(1, {"version": 2}))
    path.unlink()

    recovered = SessionStore(path)

    assert run(recovered.get(1)) == {"version": 1}
    assert read_json(path) == {"telegram_session:1": {"version": 1}}
    assert "Recovered local session data" in caplog.text


def test_restart_and_concurrent_updates_persist_valid_json(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)

    async def mutate():
        await store.set(1, {})
        await asyncio.gather(
            *(store.update(1, {f"key_{index}": index}) for index in range(20))
        )

    run(mutate())
    reloaded = SessionStore(path)
    expected = {f"key_{index}": index for index in range(20)}

    assert run(reloaded.get(1)) == expected
    assert read_json(path) == {"telegram_session:1": expected}
    assert list(path.parent.glob(f".{path.name}.tmp-*")) == []


def test_concurrent_set_update_delete_remains_consistent(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)

    async def mutate():
        await asyncio.gather(
            *(store.set(user_id, {"value": user_id}) for user_id in range(20))
        )
        await asyncio.gather(
            *(
                store.delete(user_id)
                if user_id % 2 == 0
                else store.update(user_id, {"updated": True})
                for user_id in range(20)
            )
        )

    run(mutate())
    reloaded = SessionStore(path)
    expected = {
        f"telegram_session:{user_id}": {"value": user_id, "updated": True}
        for user_id in range(1, 20, 2)
    }

    assert read_json(path) == expected
    for user_id in range(20):
        value = run(reloaded.get(user_id))
        if user_id % 2 == 0:
            assert value is None
        else:
            assert value == {"value": user_id, "updated": True}
