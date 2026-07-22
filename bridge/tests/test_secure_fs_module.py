"""Architecture contracts for the shared secure filesystem core."""

from __future__ import annotations

import ast
import errno
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from telegram_bot.session import store
from telegram_bot.utils import secure_fs


_BRIDGE_ROOT = Path(__file__).resolve().parents[1]
_MOVED_HELPERS = {
    "_absolute_path",
    "_termux_app_roots",
    "_is_owned_termux_private_ancestor",
    "_is_trusted_android_platform_ancestor",
    "_validate_existing_directory_components",
    "_create_missing_directory_components",
    "_validate_storage_directory",
    "_ensure_storage_directory",
    "ensure_private_directory",
    "_secure_existing_state_file",
    "_fsync_directory",
    "_atomic_write_bytes",
}
_DESCRIPTOR_HELPERS = {
    "atomic_write_bytes_at",
    "fsync_directory_fd",
    "owner_only_regular_violation",
}


def test_secure_fs_owns_shared_storage_primitives() -> None:
    assert store.SessionStoreDurabilityError is secure_fs.SessionStoreDurabilityError
    assert _MOVED_HELPERS <= set(vars(secure_fs))

    store_tree = ast.parse((_BRIDGE_ROOT / "session" / "store.py").read_text())
    store_functions = {
        node.name for node in ast.walk(store_tree) if isinstance(node, ast.FunctionDef)
    }
    assert _MOVED_HELPERS.isdisjoint(store_functions)


def test_secure_fs_exposes_descriptor_relative_atomic_write_primitives() -> None:
    assert _DESCRIPTOR_HELPERS <= set(vars(secure_fs))


def test_owner_only_regular_violation_classifies_each_invariant(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}", encoding="utf-8")
    metadata = target.stat()

    assert (
        secure_fs.owner_only_regular_violation(metadata, owner_id=os.getuid())
        is None
    )
    assert (
        secure_fs.owner_only_regular_violation(
            SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700,
                st_nlink=1,
                st_uid=os.getuid(),
            ),
            owner_id=os.getuid(),
        )
        == "not_regular"
    )
    assert (
        secure_fs.owner_only_regular_violation(
            SimpleNamespace(
                st_mode=stat.S_IFREG | 0o600,
                st_nlink=2,
                st_uid=os.getuid(),
            ),
            owner_id=os.getuid(),
        )
        == "multiple_links"
    )
    assert (
        secure_fs.owner_only_regular_violation(
            SimpleNamespace(
                st_mode=stat.S_IFREG | 0o600,
                st_nlink=1,
                st_uid=os.getuid() + 1,
            ),
            owner_id=os.getuid(),
        )
        == "wrong_owner"
    )
    assert (
        secure_fs.owner_only_regular_violation(
            SimpleNamespace(
                st_mode=stat.S_IFREG | 0o620,
                st_nlink=1,
                st_uid=os.getuid(),
            ),
            owner_id=os.getuid(),
        )
        == "unsafe_mode"
    )


def test_descriptor_relative_atomic_write_is_private_and_complete(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        assert secure_fs.atomic_write_bytes_at(dir_fd, "snapshot.json", b'{"ok":true}\n')
    finally:
        os.close(dir_fd)

    target = directory / "snapshot.json"
    assert target.read_bytes() == b'{"ok":true}\n'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(directory.glob(".snapshot.json.tmp.*")) == []


def test_descriptor_directory_fsync_distinguishes_unsupported_and_io_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unsupported(_fd: int) -> None:
        raise OSError(errno.EINVAL, "unsupported")

    def failed(_fd: int) -> None:
        raise OSError(errno.EIO, "failed")

    monkeypatch.setattr(secure_fs.os, "fsync", unsupported)
    assert secure_fs.fsync_directory_fd(123) is False

    monkeypatch.setattr(secure_fs.os, "fsync", failed)
    with pytest.raises(OSError) as caught:
        secure_fs.fsync_directory_fd(123)
    assert caught.value.errno == errno.EIO


def test_bridge_consumers_do_not_depend_on_session_store_internals() -> None:
    consumers = (
        _BRIDGE_ROOT / "core" / "task_ledger.py",
        _BRIDGE_ROOT / "memory" / "distill_journal.py",
        _BRIDGE_ROOT / "memory" / "distill_local_sink.py",
        _BRIDGE_ROOT / "utils" / "logging_setup.py",
    )
    for consumer in consumers:
        source = consumer.read_text()
        assert "telegram_bot.session.store" not in source
        assert "telegram_bot.utils.secure_fs" in source
