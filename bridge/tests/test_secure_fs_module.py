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
    # Pin owner-only perms explicitly so the baseline is umask-independent
    # (#779): the contract fail-closes on group/other-writable files.
    target.chmod(0o600)
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


# --- #1484: shared script helpers (clock, bounded env int, owner-only read,
# JSONL, atomic replace, flock) -----------------------------------------------

_SCRIPT_HELPERS = {
    "SecureFsError",
    "utc_now_iso",
    "bounded_int_env",
    "read_owner_only_bytes",
    "parse_jsonl_rows",
    "read_jsonl_rows",
    "json_line",
    "append_jsonl_line",
    "atomic_write_bytes",
    "atomic_write_text",
    "open_lock_descriptor",
    "acquire_flock",
    "flock_guard",
}


def test_secure_fs_exposes_shared_script_helpers() -> None:
    assert _SCRIPT_HELPERS <= set(vars(secure_fs))
    adapter = Path(__file__).resolve().parents[2] / "scripts" / "ccc_secure_fs.py"
    source = adapter.read_text(encoding="utf-8")
    for name in _SCRIPT_HELPERS | _DESCRIPTOR_HELPERS:
        assert f"{name} = _MODULE.{name}" in source, name


def test_utc_now_iso_formats() -> None:
    seconds = secure_fs.utc_now_iso()
    assert len(seconds) == len("2026-01-01T00:00:00Z") and seconds.endswith("Z")
    assert "+00:00" not in seconds
    auto = secure_fs.utc_now_iso(timespec="auto")
    assert auto.endswith("Z") and auto.startswith(seconds[:13])


def test_bounded_int_env_default_vs_clamp() -> None:
    env = {"N": "7", "BIG": "99", "BAD": "x", "EMPTY": ""}
    assert secure_fs.bounded_int_env(env, "N", 1, 1, 10) == 7
    assert secure_fs.bounded_int_env(env, "MISSING", 3, 1, 10) == 3
    assert secure_fs.bounded_int_env(env, "BAD", 3, 1, 10) == 3
    assert secure_fs.bounded_int_env(env, "EMPTY", 3, 1, 10) == 3
    assert secure_fs.bounded_int_env(env, "BIG", 3, 1, 10) == 3
    assert secure_fs.bounded_int_env(env, "BIG", 3, 1, 10, clamp=True) == 10
    assert secure_fs.bounded_int_env({"N": "-5"}, "N", 3, 1, 10, clamp=True) == 1


def _private(path: Path, payload: bytes, mode: int = 0o600) -> Path:
    path.write_bytes(payload)
    path.chmod(mode)
    return path


def test_read_owner_only_bytes_accepts_private_regular_file(tmp_path: Path) -> None:
    target = _private(tmp_path / "state.json", b'{"ok":true}')
    payload, metadata = secure_fs.read_owner_only_bytes(target, max_bytes=64)
    assert payload == b'{"ok":true}'
    assert metadata.st_size == len(payload)
    # exact_mode / owner_id are honoured when they match.
    assert secure_fs.read_owner_only_bytes(
        target, max_bytes=64, exact_mode=0o600, owner_id=os.getuid()
    )[0] == payload


@pytest.mark.parametrize(
    ("setup", "kwargs", "reason"),
    [
        ("symlink", {}, "unsafe"),
        ("mode", {}, "unsafe"),
        ("exact", {"exact_mode": 0o600}, "unsafe"),
        ("owner", {"owner_id": os.getuid() + 1}, "unsafe"),
        ("empty", {"require_nonempty": True}, "unsafe"),
        ("large", {}, "too_large"),
    ],
)
def test_read_owner_only_bytes_rejects_each_invariant(
    tmp_path: Path, setup: str, kwargs: dict[str, object], reason: str
) -> None:
    target = tmp_path / "state.json"
    if setup == "symlink":
        _private(tmp_path / "real.json", b"{}")
        target.symlink_to(tmp_path / "real.json")
    elif setup == "mode":
        _private(target, b"{}", 0o622)
    elif setup == "exact":
        _private(target, b"{}", 0o640)
        kwargs = {**kwargs, "unsafe_mode_mask": 0}
    elif setup == "empty":
        _private(target, b"")
    elif setup == "large":
        _private(target, b"x" * 9)
    else:
        _private(target, b"{}")
    with pytest.raises(secure_fs.SecureFsError) as caught:
        secure_fs.read_owner_only_bytes(target, max_bytes=8, **kwargs)  # type: ignore[arg-type]
    assert caught.value.reason == reason


def test_read_owner_only_bytes_missing_file_raises_plain_oserror(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        secure_fs.read_owner_only_bytes(tmp_path / "missing", max_bytes=8)


def test_read_owner_only_bytes_detects_change_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _private(tmp_path / "state.json", b"abc")
    real_read = os.read

    def grow_then_read(descriptor: int, size: int) -> bytes:
        with open(target, "ab") as stream:
            stream.write(b"defghijklmnop")
        monkeypatch.setattr(secure_fs.os, "read", real_read)
        return real_read(descriptor, size)

    monkeypatch.setattr(secure_fs.os, "read", grow_then_read)
    with pytest.raises(secure_fs.SecureFsError) as caught:
        secure_fs.read_owner_only_bytes(target, max_bytes=8)
    assert caught.value.reason == "changed"


def test_parse_jsonl_rows_filters_and_reports_invalid() -> None:
    text = '{"a":1}\nnot json\n[1,2]\n\n{"b":2}\n'
    assert secure_fs.parse_jsonl_rows(text) == [{"a": 1}, {"b": 2}]
    import json

    with pytest.raises(json.JSONDecodeError):
        secure_fs.parse_jsonl_rows(text, on_invalid="raise")


def test_read_jsonl_rows_reads_owner_only_file(tmp_path: Path) -> None:
    target = _private(tmp_path / "ledger.jsonl", b'{"a":1}\nbad\n{"b":2}\n')
    assert secure_fs.read_jsonl_rows(target, max_bytes=64, exact_mode=0o600) == [
        {"a": 1},
        {"b": 2},
    ]
    target.chmod(0o622)
    with pytest.raises(secure_fs.SecureFsError):
        secure_fs.read_jsonl_rows(target, max_bytes=64)


def test_append_jsonl_line_creates_private_file_and_serializes_canonically(
    tmp_path: Path,
) -> None:
    target = tmp_path / "ledger.jsonl"
    secure_fs.append_jsonl_line(target, {"z": 1, "a": "한글"})
    secure_fs.append_jsonl_line(target, {"n": None}, fsync=False)
    assert target.read_bytes() == b'{"a":"\xed\x95\x9c\xea\xb8\x80","z":1}\n{"n":null}\n'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert secure_fs.json_line({"b": [1, 2], "a": 1}) == '{"a":1,"b":[1,2]}'


def test_append_jsonl_line_refuses_symlink_and_wrong_mode(tmp_path: Path) -> None:
    real = _private(tmp_path / "real.jsonl", b"")
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    with pytest.raises(secure_fs.SecureFsError):
        secure_fs.append_jsonl_line(link, {"a": 1})
    assert real.read_bytes() == b""
    loose = _private(tmp_path / "loose.jsonl", b"", 0o640)
    with pytest.raises(secure_fs.SecureFsError):
        secure_fs.append_jsonl_line(loose, {"a": 1})
    assert loose.read_bytes() == b""
    with pytest.raises(FileNotFoundError):
        secure_fs.append_jsonl_line(tmp_path / "nodir" / "x.jsonl", {"a": 1})


def test_atomic_write_bytes_preserves_existing_mode_and_cleans_temp(tmp_path: Path) -> None:
    target = _private(tmp_path / "settings.json", b"old", 0o644)
    assert secure_fs.atomic_write_bytes(target, b"new") is True
    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    fresh = tmp_path / "fresh.json"
    assert secure_fs.atomic_write_text(fresh, "text\n", durable=False) is False
    assert fresh.read_text(encoding="utf-8") == "text\n"
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o600
    secure_fs.atomic_write_bytes(target, b"explicit", mode=0o600)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".*.tmp")) == []


def test_atomic_write_bytes_symlink_destination_policy(tmp_path: Path) -> None:
    real = _private(tmp_path / "real.json", b"real", 0o640)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    secure_fs.atomic_write_bytes(link, b"through", resolve_symlink=True)
    assert link.is_symlink() and real.read_bytes() == b"through"
    assert stat.S_IMODE(real.stat().st_mode) == 0o640
    secure_fs.atomic_write_bytes(link, b"replaced")
    assert not link.is_symlink() and link.read_bytes() == b"replaced"
    assert real.read_bytes() == b"through"


def test_atomic_write_bytes_failure_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _private(tmp_path / "state.json", b"old")

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EIO, "replace failed")

    monkeypatch.setattr(secure_fs.os, "replace", fail_replace)
    with pytest.raises(OSError):
        secure_fs.atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"old"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_flock_guard_non_blocking_reports_contention(tmp_path: Path) -> None:
    lock = tmp_path / "state.lock"
    with secure_fs.flock_guard(lock) as acquired:
        assert acquired is True
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
        with secure_fs.flock_guard(lock) as nested:
            assert nested is False
        with secure_fs.flock_guard(lock, timeout=0.05) as polled:
            assert polled is False
    with secure_fs.flock_guard(lock, blocking=True) as again:
        assert again is True
    with secure_fs.flock_guard(lock, timeout=0.05) as after:
        assert after is True


def test_open_lock_descriptor_validates_and_refuses_symlink(tmp_path: Path) -> None:
    real = _private(tmp_path / "real.lock", b"")
    link = tmp_path / "link.lock"
    link.symlink_to(real)
    with pytest.raises(secure_fs.SecureFsError):
        secure_fs.open_lock_descriptor(link)
    loose = _private(tmp_path / "loose.lock", b"", 0o640)
    with pytest.raises(secure_fs.SecureFsError):
        secure_fs.open_lock_descriptor(loose, exact_mode=0o600, unsafe_mode_mask=0)
    writable = _private(tmp_path / "writable.lock", b"", 0o620)
    with pytest.raises(secure_fs.SecureFsError):
        secure_fs.open_lock_descriptor(writable)
    with pytest.raises(FileNotFoundError):
        secure_fs.open_lock_descriptor(tmp_path / "nodir" / "x.lock")
    dir_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        descriptor = secure_fs.open_lock_descriptor("rel.lock", dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    try:
        assert secure_fs.acquire_flock(descriptor) is True
        assert secure_fs.acquire_flock(descriptor, blocking=True) is True
    finally:
        os.close(descriptor)
    assert stat.S_IMODE((tmp_path / "rel.lock").stat().st_mode) == 0o600
