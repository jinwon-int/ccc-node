"""Regression tests for atomic, corruption-recoverable SessionStore persistence."""

import asyncio
import errno
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
    SessionStoreValidationError,
)
from telegram_bot.utils.secure_fs import (  # noqa: E402
    _validate_existing_directory_components,
)


def run(awaitable):
    return asyncio.run(awaitable)


def initialized_store(path: Path) -> SessionStore:
    store = SessionStore(path)
    store.initialize()
    return store


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
    store = initialized_store(path)

    run(store.set(1, {"session_id": "one"}))

    assert read_json(path) == {"telegram_session:1": {"session_id": "one"}}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert list(path.parent.glob(f".{path.name}*.tmp-*")) == []


def test_list_sessions_returns_conversation_suffixes_and_deep_copies(tmp_path):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"session_id": "one", "nested": {"value": 1}}))
    run(store.set("11:1001", {"session_id": "group"}))

    listed = run(store.list_sessions())
    assert listed == {
        "1": {"session_id": "one", "nested": {"value": 1}},
        "11:1001": {"session_id": "group"},
    }
    listed["1"]["nested"]["value"] = 9
    assert run(store.get(1))["nested"]["value"] == 1


def test_backup_preserves_exact_previous_primary_bytes(tmp_path):
    path = tmp_path / "sessions.json"
    backup_path = path.with_name(f"{path.name}.bak")
    store = initialized_store(path)
    run(store.set("11:1001", {"version": 1, "label": "가나다"}))
    previous_primary = path.read_bytes()

    run(store.update("11:1001", {"version": 2}))

    assert backup_path.read_bytes() == previous_primary
    assert read_json(path)["telegram_session:11:1001"]["version"] == 2


def test_existing_state_files_are_tightened_to_0600(tmp_path):
    path = tmp_path / "sessions.json"
    backup_path = path.with_name(f"{path.name}.bak")
    payload = '{"telegram_session:1": {"version": 1}}\n'
    path.write_text(payload, encoding="utf-8")
    backup_path.write_text(payload, encoding="utf-8")
    path.chmod(0o644)
    backup_path.chmod(0o664)

    initialized_store(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600


def test_existing_safe_parent_permissions_are_not_overwritten(tmp_path):
    parent = tmp_path / "shared-state"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)

    initialized_store(parent / "sessions.json")

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755


def test_existing_group_writable_parent_fails_closed(tmp_path):
    parent = tmp_path / "unsafe-state"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)

    with pytest.raises(PermissionError, match="writable by group or others"):
        initialized_store(parent / "sessions.json")


def test_symlinked_storage_parent_fails_closed(tmp_path):
    real_parent = tmp_path / "real-state"
    real_parent.mkdir(mode=0o700)
    symlinked_parent = tmp_path / "linked-state"
    symlinked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(PermissionError, match="symlink"):
        initialized_store(symlinked_parent / "sessions.json")

    assert not (real_parent / "sessions.json").exists()


def test_symlinked_storage_ancestor_fails_closed(tmp_path):
    real_root = tmp_path / "real-root"
    state_parent = real_root / "state"
    state_parent.mkdir(parents=True, mode=0o700)
    symlinked_root = tmp_path / "linked-root"
    symlinked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(PermissionError, match="symlink"):
        initialized_store(symlinked_root / "state" / "sessions.json")


def test_group_writable_nonsticky_ancestor_fails_closed(tmp_path):
    unsafe_ancestor = tmp_path / "unsafe-ancestor"
    safe_parent = unsafe_ancestor / "private-state"
    safe_parent.mkdir(parents=True, mode=0o700)
    unsafe_ancestor.chmod(0o777)
    safe_parent.chmod(0o700)

    with pytest.raises(PermissionError, match="unsafe writable ancestor"):
        initialized_store(safe_parent / "sessions.json")


def _fake_foreign_owned_sticky_metadata(path: Path):
    sticky_ancestor = Path("/foreign-sticky")
    if path == sticky_ancestor:
        mode, user_id, group_id = 0o1777, 4000, 4000
    else:
        mode, user_id, group_id = 0o700, 2000, 2000
    return types.SimpleNamespace(
        st_mode=stat.S_IFDIR | mode,
        st_gid=group_id,
        st_uid=user_id,
    )


def test_foreign_owned_sticky_writable_ancestor_fails_closed():
    target = Path("/foreign-sticky/private-state")

    with (
        patch.object(
            Path,
            "lstat",
            autospec=True,
            side_effect=_fake_foreign_owned_sticky_metadata,
        ),
        patch.object(os, "getuid", return_value=2000),
        patch.object(os, "getgid", return_value=2000),
        patch.object(os, "getgroups", return_value=[2000]),
        patch.dict(os.environ, {}, clear=True),
        pytest.raises(PermissionError, match="unsafe owner ancestor"),
    ):
        _validate_existing_directory_components(target)


def test_foreign_owned_owner_writable_ancestor_fails_closed():
    target = Path("/foreign-owned/private-state")

    def metadata(path: Path):
        if path == Path("/foreign-owned"):
            mode, user_id, group_id = 0o755, 4000, 4000
        elif path == Path("/"):
            mode, user_id, group_id = 0o755, 0, 0
        else:
            mode, user_id, group_id = 0o700, 2000, 2000
        return types.SimpleNamespace(
            st_mode=stat.S_IFDIR | mode,
            st_gid=group_id,
            st_uid=user_id,
        )

    with (
        patch.object(Path, "lstat", autospec=True, side_effect=metadata),
        patch.object(os, "getuid", return_value=2000),
        patch.object(os, "getgid", return_value=2000),
        patch.object(os, "getgroups", return_value=[2000]),
        patch.dict(os.environ, {}, clear=True),
        pytest.raises(PermissionError, match="unsafe owner ancestor"),
    ):
        _validate_existing_directory_components(target)


def _fake_directory_metadata(path: Path, *, group_id: int, user_id: int = 0):
    mode = 0o771 if path in {Path("/data"), Path("/data/data")} else 0o700
    return types.SimpleNamespace(
        st_mode=stat.S_IFDIR | mode,
        st_gid=group_id,
        st_uid=user_id,
    )


def test_android_platform_ancestor_is_allowed_when_process_is_not_in_its_group():
    target = Path("/data/data/com.termux/files/home/.telegram_bot")

    with (
        patch.object(
            Path,
            "lstat",
            autospec=True,
            side_effect=lambda path: _fake_directory_metadata(
                path,
                group_id=1000,
                user_id=(
                    2000
                    if path == Path("/data/data/com.termux/files/usr")
                    else 0
                ),
            ),
        ),
        patch.object(os, "getuid", return_value=2000),
        patch.object(os, "getgid", return_value=2000),
        patch.object(os, "getgroups", return_value=[2000, 3003]),
        patch.dict(
            os.environ,
            {"PREFIX": "/data/data/com.termux/files/usr"},
            clear=False,
        ),
    ):
        _validate_existing_directory_components(target)


def _fake_foreign_group_metadata(path: Path):
    mode = 0o771 if path == Path("/srv/shared") else 0o700
    return types.SimpleNamespace(
        st_mode=stat.S_IFDIR | mode,
        st_gid=4000 if path == Path("/srv/shared") else 2000,
        st_uid=0 if path == Path("/srv/shared") else 2000,
    )


def test_generic_foreign_group_writable_ancestor_fails_closed():
    target = Path("/srv/shared/private-state")

    with (
        patch.object(
            Path,
            "lstat",
            autospec=True,
            side_effect=_fake_foreign_group_metadata,
        ),
        patch.object(os, "getuid", return_value=2000),
        patch.object(os, "getgid", return_value=2000),
        patch.object(os, "getgroups", return_value=[2000, 3003]),
        patch.dict(os.environ, {}, clear=True),
        pytest.raises(PermissionError, match="unsafe writable ancestor"),
    ):
        _validate_existing_directory_components(target)


def _fake_termux_directory_metadata(path: Path):
    app_root = Path("/data/data/com.termux/files")
    if path == app_root:
        mode, user_id, group_id = 0o771, 2000, 2000
    elif app_root in path.parents:
        mode, user_id, group_id = 0o700, 2000, 2000
    else:
        mode, user_id, group_id = 0o771, 0, 1000
    return types.SimpleNamespace(
        st_mode=stat.S_IFDIR | mode,
        st_gid=group_id,
        st_uid=user_id,
    )


def test_termux_app_private_root_is_allowed_when_owned_by_the_process():
    target = Path("/data/data/com.termux/files/home/.telegram_bot")

    with (
        patch.object(
            Path,
            "lstat",
            autospec=True,
            side_effect=_fake_termux_directory_metadata,
        ),
        patch.object(os, "getuid", return_value=2000),
        patch.object(os, "getgid", return_value=2000),
        patch.object(os, "getgroups", return_value=[2000, 3003]),
        patch.dict(
            os.environ,
            {"PREFIX": "/data/data/com.termux/files/usr"},
            clear=False,
        ),
    ):
        _validate_existing_directory_components(target)


def _fake_termux_foreign_group_descendant(path: Path):
    prefix = Path("/data/data/com.termux/files/usr")
    app_root = Path("/data/data/com.termux/files")
    foreign_descendant = app_root / "home"
    if path == prefix:
        mode, user_id, group_id = 0o700, 2000, 2000
    elif path == app_root:
        mode, user_id, group_id = 0o771, 2000, 2000
    elif path == foreign_descendant:
        mode, user_id, group_id = 0o770, 2000, 4000
    elif app_root in path.parents:
        mode, user_id, group_id = 0o700, 2000, 2000
    else:
        mode, user_id, group_id = 0o771, 0, 1000
    return types.SimpleNamespace(
        st_mode=stat.S_IFDIR | mode,
        st_gid=group_id,
        st_uid=user_id,
    )


def test_termux_foreign_group_writable_descendant_fails_closed():
    target = Path("/data/data/com.termux/files/home/.telegram_bot")

    with (
        patch.object(
            Path,
            "lstat",
            autospec=True,
            side_effect=_fake_termux_foreign_group_descendant,
        ),
        patch.object(os, "getuid", return_value=2000),
        patch.object(os, "getgid", return_value=2000),
        patch.object(os, "getgroups", return_value=[2000, 3003]),
        patch.dict(
            os.environ,
            {"PREFIX": "/data/data/com.termux/files/usr"},
            clear=False,
        ),
        pytest.raises(PermissionError, match="unsafe writable ancestor"),
    ):
        _validate_existing_directory_components(target)


def _fake_missing_termux_prefix(path: Path):
    if path == Path("/data/data/com.termux/files/usr"):
        raise FileNotFoundError(path)
    return _fake_termux_directory_metadata(path)


def test_canonical_termux_prefix_must_exist_and_be_owned():
    target = Path("/data/data/com.termux/files/home/.telegram_bot")

    with (
        patch.object(
            Path,
            "lstat",
            autospec=True,
            side_effect=_fake_missing_termux_prefix,
        ),
        patch.object(os, "getuid", return_value=2000),
        patch.object(os, "getgid", return_value=2000),
        patch.object(os, "getgroups", return_value=[2000, 3003]),
        patch.dict(
            os.environ,
            {"PREFIX": "/data/data/com.termux/files/usr"},
            clear=False,
        ),
        pytest.raises(PermissionError, match="unsafe writable ancestor"),
    ):
        _validate_existing_directory_components(target)


def _fake_termux_alias_metadata(path: Path):
    app_root = Path("/data/user/0/com.termux/files")
    if path == Path("/data/data/com.termux/files/usr"):
        mode, user_id, group_id = 0o700, 2000, 2000
    elif path == app_root:
        mode, user_id, group_id = 0o771, 2000, 2000
    elif app_root in path.parents or path == app_root.parent:
        mode, user_id, group_id = 0o700, 2000, 2000
    else:
        mode, user_id, group_id = 0o771, 0, 1000
    return types.SimpleNamespace(
        st_mode=stat.S_IFDIR | mode,
        st_gid=group_id,
        st_uid=user_id,
    )


def test_legacy_prefix_allows_android_user_zero_alias():
    target = Path("/data/user/0/com.termux/files/home/.telegram_bot")

    with (
        patch.object(
            Path,
            "lstat",
            autospec=True,
            side_effect=_fake_termux_alias_metadata,
        ),
        patch.object(os, "getuid", return_value=2000),
        patch.object(os, "getgid", return_value=2000),
        patch.object(os, "getgroups", return_value=[2000, 3003]),
        patch.dict(
            os.environ,
            {"PREFIX": "/data/data/com.termux/files/usr"},
            clear=False,
        ),
    ):
        _validate_existing_directory_components(target)


def _fake_unicode_termux_metadata(path: Path):
    app_root = Path("/data/user/١٠/com.termux/files")
    prefix = app_root / "usr"
    if path == prefix:
        mode, user_id, group_id = 0o700, 2000, 2000
    elif path == app_root:
        mode, user_id, group_id = 0o771, 2000, 2000
    elif app_root in path.parents or path == app_root.parent:
        mode, user_id, group_id = 0o700, 2000, 2000
    else:
        mode, user_id, group_id = 0o771, 0, 1000
    return types.SimpleNamespace(
        st_mode=stat.S_IFDIR | mode,
        st_gid=group_id,
        st_uid=user_id,
    )


def test_non_ascii_android_user_id_fails_closed():
    prefix = "/data/user/١٠/com.termux/files/usr"
    target = Path("/data/user/١٠/com.termux/files/home/.telegram_bot")

    with (
        patch.object(
            Path,
            "lstat",
            autospec=True,
            side_effect=_fake_unicode_termux_metadata,
        ),
        patch.object(os, "getuid", return_value=2000),
        patch.object(os, "getgid", return_value=2000),
        patch.object(os, "getgroups", return_value=[2000, 3003]),
        patch.dict(os.environ, {"PREFIX": prefix}, clear=False),
        pytest.raises(PermissionError, match="unsafe writable ancestor"),
    ):
        _validate_existing_directory_components(target)


def _fake_spoofed_termux_metadata(path: Path):
    app_root = Path("/tmp/com.termux/files")
    if path == Path("/tmp"):
        mode, user_id, group_id = 0o1777, 0, 0
    elif path == app_root:
        mode, user_id, group_id = 0o771, 2000, 2000
    elif app_root in path.parents:
        mode, user_id, group_id = 0o700, 2000, 2000
    else:
        mode, user_id, group_id = 0o755, 0, 0
    return types.SimpleNamespace(
        st_mode=stat.S_IFDIR | mode,
        st_gid=group_id,
        st_uid=user_id,
    )


def test_spoofed_termux_prefix_outside_android_data_root_fails_closed():
    target = Path("/tmp/com.termux/files/home/.telegram_bot")

    with (
        patch.object(
            Path,
            "lstat",
            autospec=True,
            side_effect=_fake_spoofed_termux_metadata,
        ),
        patch.object(os, "getuid", return_value=2000),
        patch.object(os, "getgid", return_value=2000),
        patch.object(os, "getgroups", return_value=[2000, 3003]),
        patch.dict(
            os.environ,
            {"PREFIX": "/tmp/com.termux/files/usr"},
            clear=False,
        ),
        pytest.raises(PermissionError, match="unsafe writable ancestor"),
    ):
        _validate_existing_directory_components(target)


def test_group_writable_ancestor_still_fails_when_process_is_in_its_group():
    target = Path("/data/data/com.termux/files/home/.telegram_bot")

    with (
        patch.object(
            Path,
            "lstat",
            autospec=True,
            side_effect=lambda path: _fake_directory_metadata(path, group_id=1000),
        ),
        patch.object(os, "getgid", return_value=1000),
        patch.object(os, "getgroups", return_value=[1000, 3003]),
        pytest.raises(PermissionError, match="unsafe writable ancestor"),
    ):
        _validate_existing_directory_components(target)


def test_missing_directory_components_are_created_private(tmp_path):
    level_one = tmp_path / "level-one"
    level_two = level_one / "level-two"

    initialized_store(level_two / "sessions.json")

    assert stat.S_IMODE(level_one.stat().st_mode) == 0o700
    assert stat.S_IMODE(level_two.stat().st_mode) == 0o700
    assert not level_one.is_symlink()
    assert not level_two.is_symlink()


def test_serialization_failure_preserves_disk_and_memory(tmp_path):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"session_id": "stable"}))
    before = path.read_bytes()

    with pytest.raises(SessionStoreValidationError):
        run(store.set(2, {"not_json": object()}))

    assert path.read_bytes() == before
    assert run(store.get(2)) is None
    assert run(store.get(1)) == {"session_id": "stable"}


@pytest.mark.parametrize(
    "invalid_value",
    [
        {"nested": {1: "integer-key"}},
        {"tuple_value": (1, 2)},
        {"collision": {1: "integer", "1": "string"}},
        {"number": float("nan")},
        {"number": float("inf")},
        {"number": float("-inf")},
    ],
)
def test_noncanonical_nested_values_are_rejected_without_state_change(
    tmp_path, invalid_value
):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"version": 1}))
    before = path.read_bytes()

    with pytest.raises(SessionStoreValidationError):
        run(store.set(2, invalid_value))

    assert path.read_bytes() == before
    assert run(store.get(1)) == {"version": 1}
    assert run(store.get(2)) is None


def test_cyclic_nested_value_is_rejected_without_state_change(tmp_path):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"version": 1}))
    before = path.read_bytes()
    cyclic = {}
    cyclic["self"] = cyclic

    with pytest.raises(SessionStoreValidationError, match="cyclic"):
        run(store.set(2, cyclic))

    assert path.read_bytes() == before
    assert run(store.get(2)) is None


def test_canonical_nested_value_round_trips_without_type_drift(tmp_path):
    path = tmp_path / "sessions.json"
    value = {
        "nested": {
            "items": [1, 2.5, True, None, {"name": "stable"}],
            "empty": {},
        }
    }
    store = initialized_store(path)

    run(store.set("11:1001", value))

    assert run(store.get("11:1001")) == value
    assert initialized_store(path)._local_data == {
        "telegram_session:11:1001": value
    }


def test_nonfinite_number_on_disk_is_confirmed_corruption(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(
        '{"telegram_session:1": {"number": NaN}}\n', encoding="utf-8"
    )

    with pytest.raises(SessionStoreCorruptionError):
        initialized_store(path)


def test_duplicate_nested_json_key_on_disk_is_confirmed_corruption(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(
        '{"telegram_session:1": {"value": 1, "value": 2}}\n',
        encoding="utf-8",
    )

    with pytest.raises(SessionStoreCorruptionError):
        initialized_store(path)


@pytest.mark.parametrize("operation", ["write", "flush"])
def test_temp_write_failure_preserves_disk_and_memory(tmp_path, operation):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"version": 1}))
    before = path.read_bytes()
    real_fdopen = os.fdopen

    def failing_fdopen(*args, **kwargs):
        return FailingStream(real_fdopen(*args, **kwargs), operation)

    with patch("telegram_bot.utils.secure_fs.os.fdopen", side_effect=failing_fdopen):
        with pytest.raises(OSError, match=f"{operation} failed"):
            run(store.update(1, {"version": 2}))

    assert path.read_bytes() == before
    assert run(store.get(1)) == {"version": 1}
    assert list(path.parent.glob(f".{path.name}*.tmp-*")) == []


@pytest.mark.parametrize("operation", ["write", "flush"])
def test_primary_temp_io_failure_after_backup_preserves_state(tmp_path, operation):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"version": 1}))
    before = path.read_bytes()
    real_fdopen = os.fdopen
    open_calls = 0

    def fail_primary_fdopen(*args, **kwargs):
        nonlocal open_calls
        open_calls += 1
        stream = real_fdopen(*args, **kwargs)
        if open_calls == 2:
            return FailingStream(stream, operation)
        return stream

    with patch("telegram_bot.utils.secure_fs.os.fdopen", side_effect=fail_primary_fdopen):
        with pytest.raises(OSError, match=f"{operation} failed"):
            run(store.update(1, {"version": 2}))

    assert path.read_bytes() == before
    assert run(store.get(1)) == {"version": 1}
    assert list(path.parent.glob(f".{path.name}*.tmp-*")) == []


def test_primary_file_fsync_failure_after_backup_preserves_state(tmp_path):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"version": 1}))
    before = path.read_bytes()
    real_fsync = os.fsync
    regular_calls = 0

    def fail_second_regular_file(fd):
        nonlocal regular_calls
        if stat.S_ISREG(os.fstat(fd).st_mode):
            regular_calls += 1
            if regular_calls == 2:
                raise OSError(errno.EIO, "primary file fsync failed")
        return real_fsync(fd)

    with patch("telegram_bot.utils.secure_fs.os.fsync", side_effect=fail_second_regular_file):
        with pytest.raises(OSError, match="primary file fsync failed"):
            run(store.update(1, {"version": 2}))

    assert path.read_bytes() == before
    assert run(store.get(1)) == {"version": 1}
    assert list(path.parent.glob(f".{path.name}*.tmp-*")) == []


def test_backup_replace_failure_preserves_primary_and_memory(tmp_path):
    path = tmp_path / "sessions.json"
    backup_path = path.with_name(f"{path.name}.bak")
    store = initialized_store(path)
    run(store.set(1, {"version": 1}))
    before = path.read_bytes()
    real_replace = os.replace

    def fail_backup_replace(source, destination):
        if Path(destination) == backup_path:
            raise OSError("backup replace failed")
        return real_replace(source, destination)

    with patch("telegram_bot.utils.secure_fs.os.replace", side_effect=fail_backup_replace):
        with pytest.raises(OSError, match="backup replace failed"):
            run(store.update(1, {"version": 2}))

    assert path.read_bytes() == before
    assert run(store.get(1)) == {"version": 1}
    assert list(path.parent.glob(f".{path.name}*.tmp-*")) == []


def test_delete_replace_failure_rolls_memory_back(tmp_path):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"version": 1}))
    real_replace = os.replace

    def fail_primary_replace(source, destination):
        if Path(destination) == path:
            raise OSError("replace failed")
        return real_replace(source, destination)

    with patch("telegram_bot.utils.secure_fs.os.replace", side_effect=fail_primary_replace):
        with pytest.raises(OSError, match="replace failed"):
            run(store.delete(1))

    assert run(store.get(1)) == {"version": 1}
    assert read_json(path) == {"telegram_session:1": {"version": 1}}


def test_file_fsync_failure_preserves_disk_and_memory(tmp_path):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"version": 1}))
    before = path.read_bytes()

    with patch("telegram_bot.utils.secure_fs.os.fsync", side_effect=OSError("fsync failed")):
        with pytest.raises(OSError, match="fsync failed"):
            run(store.update(1, {"version": 2}))

    assert path.read_bytes() == before
    assert run(store.get(1)) == {"version": 1}
    assert list(path.parent.glob(f".{path.name}*.tmp-*")) == []


def test_unsupported_directory_fsync_keeps_committed_state(tmp_path, caplog):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"version": 1}))
    real_fsync = os.fsync

    def fail_for_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "directory fsync unsupported")
        return real_fsync(fd)

    with patch("telegram_bot.utils.secure_fs.os.fsync", side_effect=fail_for_directory):
        run(store.update(1, {"version": 2}))

    assert read_json(path) == {"telegram_session:1": {"version": 2}}
    assert run(store.get(1)) == {"version": 2}
    assert "Directory fsync unavailable" in caplog.text


def test_backup_directory_fsync_io_error_rolls_back_before_primary_replace(tmp_path):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"version": 1}))
    before = path.read_bytes()
    real_fsync = os.fsync

    def fail_first_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory fsync failed")
        return real_fsync(fd)

    with patch("telegram_bot.utils.secure_fs.os.fsync", side_effect=fail_first_directory):
        with pytest.raises(OSError, match="directory fsync failed"):
            run(store.update(1, {"version": 2}))

    assert path.read_bytes() == before
    assert run(store.get(1)) == {"version": 1}


def test_primary_directory_fsync_io_error_keeps_committed_disk_and_memory(tmp_path):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"version": 1}))
    real_fsync = os.fsync
    directory_calls = 0

    def fail_second_directory(fd):
        nonlocal directory_calls
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_calls += 1
            if directory_calls == 2:
                raise OSError(errno.EIO, "directory fsync failed")
        return real_fsync(fd)

    with patch("telegram_bot.utils.secure_fs.os.fsync", side_effect=fail_second_directory):
        with pytest.raises(OSError, match="directory fsync failed"):
            run(store.update(1, {"version": 2}))

    assert read_json(path) == {"telegram_session:1": {"version": 2}}
    assert run(store.get(1)) == {"version": 2}


def test_directory_close_error_after_primary_replace_is_nonfatal(tmp_path, caplog):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"version": 1}))
    real_close = os.close
    directory_calls = 0

    def fail_second_directory_close(fd):
        nonlocal directory_calls
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        real_close(fd)
        if is_directory:
            directory_calls += 1
            if directory_calls == 2:
                raise OSError(errno.EIO, "directory close failed")

    with patch(
        "telegram_bot.utils.secure_fs.os.close", side_effect=fail_second_directory_close
    ):
        run(store.update(1, {"version": 2}))

    assert read_json(path) == {"telegram_session:1": {"version": 2}}
    assert run(store.get(1)) == {"version": 2}
    assert "Directory close failed" in caplog.text


def test_primary_replace_failure_preserves_disk_and_memory(tmp_path):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"version": 1}))
    before = path.read_bytes()
    real_replace = os.replace

    def fail_primary_replace(source, destination):
        if Path(destination) == path:
            raise OSError("replace failed")
        real_replace(source, destination)

    with patch("telegram_bot.utils.secure_fs.os.replace", side_effect=fail_primary_replace):
        with pytest.raises(OSError, match="replace failed"):
            run(store.update(1, {"version": 2}))

    assert path.read_bytes() == before
    assert run(store.get(1)) == {"version": 1}
    assert list(path.parent.glob(f".{path.name}*.tmp-*")) == []


def test_corrupt_primary_recovers_previous_good_backup(tmp_path, caplog):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"version": 1}))
    run(store.update(1, {"version": 2}))
    backup_path = path.with_name(f"{path.name}.bak")
    assert backup_path.exists()
    assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600

    path.write_text("{ truncated", encoding="utf-8")
    recovered = initialized_store(path)

    assert run(recovered.get(1)) == {"version": 1}
    assert read_json(path) == {"telegram_session:1": {"version": 1}}
    assert "Recovered local session data" in caplog.text


def test_corrupt_primary_without_backup_fails_closed(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("{ truncated", encoding="utf-8")

    with pytest.raises(SessionStoreCorruptionError, match="no valid backup"):
        initialized_store(path)


def test_missing_primary_with_malformed_backup_fails_closed(tmp_path):
    path = tmp_path / "sessions.json"
    backup_path = path.with_name(f"{path.name}.bak")
    backup_path.write_text("{ truncated", encoding="utf-8")

    with pytest.raises(SessionStoreCorruptionError, match="no valid backup"):
        initialized_store(path)


def test_recovery_rewrite_failure_leaves_corrupt_primary_and_backup_intact(tmp_path):
    path = tmp_path / "sessions.json"
    backup_path = path.with_name(f"{path.name}.bak")
    corrupt_primary = b"{ truncated"
    valid_backup = b'{"telegram_session:1": {"version": 1}}\n'
    path.write_bytes(corrupt_primary)
    backup_path.write_bytes(valid_backup)
    real_replace = os.replace

    def fail_primary_replace(source, destination):
        if Path(destination) == path:
            raise OSError("recovery replace failed")
        return real_replace(source, destination)

    with patch("telegram_bot.utils.secure_fs.os.replace", side_effect=fail_primary_replace):
        with pytest.raises(OSError, match="recovery replace failed"):
            initialized_store(path)

    assert path.read_bytes() == corrupt_primary
    assert backup_path.read_bytes() == valid_backup
    assert list(path.parent.glob(f".{path.name}*.tmp-*")) == []


def test_existing_symlink_state_file_fails_closed(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    path = tmp_path / "sessions.json"
    path.symlink_to(target)

    with pytest.raises(PermissionError, match="regular file"):
        initialized_store(path)


def test_existing_hardlinked_state_file_fails_closed(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    path = tmp_path / "sessions.json"
    os.link(target, path)

    with pytest.raises(PermissionError, match="multiple hard links"):
        initialized_store(path)


def test_runtime_wrong_shaped_session_is_rejected_without_mutation(tmp_path):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"version": 1}))
    before = path.read_bytes()

    with pytest.raises(ValueError, match="must be an object"):
        run(store.set(2, []))  # type: ignore[arg-type]

    assert path.read_bytes() == before
    assert run(store.get(2)) is None
    assert run(store.get(1)) == {"version": 1}


def test_missing_primary_recovers_previous_good_backup(tmp_path, caplog):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"version": 1}))
    run(store.update(1, {"version": 2}))
    path.unlink()

    recovered = initialized_store(path)

    assert run(recovered.get(1)) == {"version": 1}
    assert read_json(path) == {"telegram_session:1": {"version": 1}}
    assert "Recovered local session data" in caplog.text


def test_primary_permission_error_does_not_promote_stale_backup(tmp_path):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)
    run(store.set(1, {"version": 1}))
    run(store.update(1, {"version": 2}))
    before = path.read_bytes()
    calls = []
    real_read = SessionStore._read_json_object

    def fail_primary(candidate):
        calls.append(Path(candidate))
        if Path(candidate) == path:
            raise PermissionError("primary unreadable")
        return real_read(candidate)

    with patch.object(SessionStore, "_read_json_object", side_effect=fail_primary):
        with pytest.raises(PermissionError, match="primary unreadable"):
            initialized_store(path)

    assert calls == [path]
    assert path.read_bytes() == before
    assert read_json(path) == {"telegram_session:1": {"version": 2}}


def test_backup_permission_error_does_not_replace_corrupt_primary(tmp_path):
    path = tmp_path / "sessions.json"
    backup_path = path.with_name(f"{path.name}.bak")
    path.write_text("{ truncated", encoding="utf-8")
    backup_path.write_text(
        '{"telegram_session:1": {"version": 1}}\n', encoding="utf-8"
    )
    primary_before = path.read_bytes()
    real_read = SessionStore._read_json_object

    def fail_backup(candidate):
        if Path(candidate) == backup_path:
            raise PermissionError("backup unreadable")
        return real_read(candidate)

    with patch.object(SessionStore, "_read_json_object", side_effect=fail_backup):
        with pytest.raises(PermissionError, match="backup unreadable"):
            initialized_store(path)

    assert path.read_bytes() == primary_before


@pytest.mark.parametrize(
    "invalid_primary",
    [
        "[]\n",
        '{"unexpected:1": {"version": 2}}\n',
        '{"telegram_session:1": []}\n',
    ],
)
def test_wrong_shaped_primary_recovers_valid_backup(tmp_path, invalid_primary):
    path = tmp_path / "sessions.json"
    backup_path = path.with_name(f"{path.name}.bak")
    path.write_text(invalid_primary, encoding="utf-8")
    backup_path.write_text(
        '{"telegram_session:1": {"version": 1}}\n', encoding="utf-8"
    )

    recovered = initialized_store(path)

    assert run(recovered.get(1)) == {"version": 1}
    assert read_json(path) == {"telegram_session:1": {"version": 1}}


def test_wrong_shaped_primary_and_backup_fail_closed(tmp_path):
    path = tmp_path / "sessions.json"
    backup_path = path.with_name(f"{path.name}.bak")
    path.write_text('{"telegram_session:1": []}\n', encoding="utf-8")
    backup_path.write_text('{"unexpected:1": {}}\n', encoding="utf-8")

    with pytest.raises(SessionStoreCorruptionError, match="no valid backup"):
        initialized_store(path)


def test_restart_and_concurrent_updates_persist_valid_json(tmp_path):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)

    async def mutate():
        await store.set(1, {})
        await asyncio.gather(
            *(store.update(1, {f"key_{index}": index}) for index in range(20))
        )

    run(mutate())
    reloaded = initialized_store(path)
    expected = {f"key_{index}": index for index in range(20)}

    assert run(reloaded.get(1)) == expected
    assert read_json(path) == {"telegram_session:1": expected}
    assert list(path.parent.glob(f".{path.name}*.tmp-*")) == []


def test_concurrent_set_update_delete_remains_consistent(tmp_path):
    path = tmp_path / "sessions.json"
    store = initialized_store(path)

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
    reloaded = initialized_store(path)
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
