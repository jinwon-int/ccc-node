"""Shared pytest setup for the bridge test suite.

Several test modules import the heavyweight ``telegram_bot.core.*`` stack, which
pulls in ``telegram_bot.utils.config`` (a pydantic ``Settings``) at import time
and reads ``PROJECT_ROOT`` from the environment. Historically each such test had
to inject fake ``telegram_bot.utils.*`` modules into ``sys.modules`` (or set the
env itself) just to make the import succeed — fragile boilerplate that also
leaks across tests in a collection-order-dependent way.

This conftest provides a minimal real environment so the *real* config validates
without a ``.env`` file, which means a test that just wants to import the real
modules no longer needs to fake anything. (Tests that still inject their own
fakes keep working — ``setdefault`` does not override an env a test sets first.)

It also restores the volatile ``telegram_bot.*`` ``sys.modules`` entries around
each test so a test that swaps a module in during its run can't leak that swap to
the next test.
"""

import os
import sys
from pathlib import Path

import pytest

import sys_modules_isolation

BRIDGE_DIR = Path(__file__).resolve().parents[1]

# Minimal env so the real pydantic config validates at import time.
os.environ.setdefault("PROJECT_ROOT", str(BRIDGE_DIR))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test")

# telegram_bot.* modules that individual tests are known to swap for fakes.
_VOLATILE_MODULES = (
    "telegram_bot.utils.config",
    "telegram_bot.utils.health",
    "telegram_bot.utils.chat_logger",
    "telegram_bot.core.project_chat",
)


@pytest.fixture(autouse=True, scope="module")
def _contain_registered_module_fakes(request):
    """Confine import-time fake sys.modules installations to their own module.

    Modules that install fakes at import (collection) time register the exact
    diff through ``sys_modules_isolation.ModuleFakesGuard`` and revert it right
    away, so collection stays pristine. This fixture reinstalls a module's
    registered fakes only while that module's own tests run and reverts them
    again at module teardown, keeping every other module's run unpolluted.
    """
    undo = sys_modules_isolation.activate(request.module.__name__)
    try:
        yield
    finally:
        sys_modules_isolation.deactivate(undo)


@pytest.fixture(autouse=True)
def _restore_volatile_modules():
    snapshot = {name: sys.modules.get(name) for name in _VOLATILE_MODULES}
    try:
        yield
    finally:
        for name, mod in snapshot.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
