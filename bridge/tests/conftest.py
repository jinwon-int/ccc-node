"""Shared pytest setup for the bridge test suite.

Several test modules import the heavyweight ``telegram_bot.core.*`` stack, which
pulls in ``telegram_bot.utils.config`` (a pydantic ``Settings``) at import time
and reads ``PROJECT_ROOT`` from the environment. Historically each such test had
to inject fake ``telegram_bot.utils.*`` modules into ``sys.modules`` (or set the
env itself) just to make the import succeed — fragile boilerplate that also
leaks across tests in a collection-order-dependent way.

This conftest provides a minimal real environment so the *real* config validates
without a ``.env`` file, which means a test that just wants to import the real
modules no longer needs to fake anything.

Live bridge processes export their settings before launching descendants. Tests
must not inherit those values: ``_env_file=None`` disables dotenv loading but
does not disable pydantic-settings' ``EnvSettingsSource``. Bridge settings are
therefore removed before collection and around every test. Tests that exercise
environment parsing remain free to inject an explicit value after fixture setup.

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

# All aliases without ``CCC_`` currently accepted by ``Config``, plus bridge
# settings read directly outside pydantic. Keep exact names here so unrelated
# provider/tool environment remains available to tests that need it.
_NON_PREFIXED_BRIDGE_ENV = frozenset(
    {
        "ALLOWED_USER_IDS",
        "AUTO_NEW_SESSION_AFTER_HOURS",
        "BOT_DATA_DIR",
        "CLAUDE_AUTH_STATUS_TIMEOUT",
        "CLAUDE_CLI_PATH",
        "CLAUDE_PROCESS_TIMEOUT",
        "CLAUDE_SETTINGS_PATH",
        "CODEX_AUTH_STATUS_TIMEOUT",
        "DRAFT_UPDATE_INTERVAL",
        "DRAFT_UPDATE_MIN_CHARS",
        "ENABLE_STREAMING_TOOL_CALLS",
        "FFMPEG_PATH",
        "LOG_FORMAT",
        "LOG_LEVEL",
        "LOGS_DIR",
        "MAX_VOICE_DURATION",
        "NETWORK_RETRY_ATTEMPTS",
        "NETWORK_RETRY_DELAY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "POLLING_TIMEOUT",
        "PROJECT_ROOT",
        "SESSION_STORE_PATH",
        "TELEGRAM_BOT_TOKEN",
        "TRANSCRIPTION_PROVIDER",
        "VOICE_REPLY_PERSONA",
        "VOLCENGINE_ACCESS_KEY",
        "VOLCENGINE_APP_ID",
        "VOLCENGINE_CLUSTER",
        "VOLCENGINE_INITIAL_BACKOFF",
        "VOLCENGINE_MAX_POLL_SECONDS",
        "VOLCENGINE_MAX_RETRIES",
        "VOLCENGINE_MODEL_NAME",
        "VOLCENGINE_POLL_INTERVAL_SECONDS",
        "VOLCENGINE_QUERY_ENDPOINT",
        "VOLCENGINE_RESOURCE_ID",
        "VOLCENGINE_SECRET_ACCESS_KEY",
        "VOLCENGINE_SUBMIT_ENDPOINT",
        "VOLCENGINE_TIMEOUT_SECONDS",
        "VOLCENGINE_TOKEN",
        "VOLCENGINE_TOS_BUCKET_NAME",
        "VOLCENGINE_TOS_ENDPOINT",
        "VOLCENGINE_TOS_REGION",
        "VOLCENGINE_TOS_SIGNED_URL_TTL_SECONDS",
        "WHISPER_MODEL",
    }
)
_TEST_BOT_TOKEN = "123456:test"


def _reset_bridge_environment() -> None:
    """Remove inherited bridge settings without retaining or printing values."""
    for name in tuple(os.environ):
        if name.startswith("CCC_") or name in _NON_PREFIXED_BRIDGE_ENV:
            os.environ.pop(name, None)

    # Minimal, fixed test env so the real pydantic config validates on import.
    os.environ["PROJECT_ROOT"] = str(BRIDGE_DIR)
    os.environ["TELEGRAM_BOT_TOKEN"] = _TEST_BOT_TOKEN


# conftest is imported before test modules, so collection-time Config imports
# cannot capture live-node settings either.
_reset_bridge_environment()

# telegram_bot.* modules that individual tests are known to swap for fakes.
_VOLATILE_MODULES = (
    "telegram_bot.utils.config",
    "telegram_bot.utils.health",
    "telegram_bot.utils.chat_logger",
    "telegram_bot.core.project_chat",
)


@pytest.fixture(autouse=True)
def _isolate_bridge_environment():
    """Keep inherited and leaked bridge settings out of every test."""
    _reset_bridge_environment()
    try:
        yield
    finally:
        _reset_bridge_environment()


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
