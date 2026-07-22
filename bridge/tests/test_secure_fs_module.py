"""Architecture contracts for the shared secure filesystem core."""

from __future__ import annotations

import ast
from pathlib import Path

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


def test_secure_fs_owns_shared_storage_primitives() -> None:
    assert store.SessionStoreDurabilityError is secure_fs.SessionStoreDurabilityError
    assert _MOVED_HELPERS <= set(vars(secure_fs))

    store_tree = ast.parse((_BRIDGE_ROOT / "session" / "store.py").read_text())
    store_functions = {
        node.name for node in ast.walk(store_tree) if isinstance(node, ast.FunctionDef)
    }
    assert _MOVED_HELPERS.isdisjoint(store_functions)


def test_bridge_consumers_do_not_depend_on_session_store_internals() -> None:
    consumers = (
        _BRIDGE_ROOT / "core" / "task_ledger.py",
        _BRIDGE_ROOT / "memory" / "distill_journal.py",
        _BRIDGE_ROOT / "utils" / "logging_setup.py",
    )
    for consumer in consumers:
        source = consumer.read_text()
        assert "telegram_bot.session.store" not in source
        assert "telegram_bot.utils.secure_fs" in source
