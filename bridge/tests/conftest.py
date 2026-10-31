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

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from skill_lookup_fixtures import build_lookup_world

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

# #2600: PROJECT_ROOT is the bridge checkout itself, so with BOT_DATA_DIR unset
# the global health reporter and task ledger wrote health.json / tasks.json /
# tasks.json.bak into <checkout>/bridge/.telegram_bot — on a node whose live
# bridge runs from that checkout, a test run overwrote the live files. Each
# test process (each pytest-xdist worker included) gets its own private data
# dir instead; tests that exercise BOT_DATA_DIR parsing still set or delete it
# themselves after fixture setup.
_TEST_BOT_DATA_DIR = Path(tempfile.mkdtemp(prefix="ccc-test-bot-data-"))
atexit.register(shutil.rmtree, _TEST_BOT_DATA_DIR, ignore_errors=True)


def _reset_bridge_environment() -> None:
    """Remove inherited bridge settings without retaining or printing values."""
    for name in tuple(os.environ):
        if name.startswith("CCC_") or name in _NON_PREFIXED_BRIDGE_ENV:
            os.environ.pop(name, None)

    # Minimal, fixed test env so the real pydantic config validates on import.
    os.environ["PROJECT_ROOT"] = str(BRIDGE_DIR)
    os.environ["TELEGRAM_BOT_TOKEN"] = _TEST_BOT_TOKEN
    os.environ["BOT_DATA_DIR"] = str(_TEST_BOT_DATA_DIR)
    # MatrixBot.serve() runs the marker-scoped orphan reaper, which reads the
    # real /proc and signals matching PIDs. A test run on a node that hosts a
    # live bridge must never do that; reaper tests opt back in explicitly.
    os.environ["CCC_MATRIX_ORPHAN_REAPER"] = "0"


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
def _restore_turn_liveness_registry():
    """Keep the process-global turn-liveness registry per-test (#2598).

    Runtimes register themselves in ``core.turn_stall`` on construction
    (grok, danso, piri, crush, claude), so a test that builds a runtime and
    never closes it leaks a binding into every later test in the same
    process. Serial runs hid this only by file order (an adapter suite's
    ``unregister_turn_liveness("grok")`` happened to run just before the
    ``registered_turn_liveness_sources() == ()`` assertion); under
    pytest-xdist the order is not guaranteed. Snapshot and restore it.
    """
    from telegram_bot.core import turn_stall

    with turn_stall._TURN_LIVENESS_LOCK:
        snapshot = dict(turn_stall._TURN_LIVENESS_SOURCES)
    try:
        yield
    finally:
        with turn_stall._TURN_LIVENESS_LOCK:
            turn_stall._TURN_LIVENESS_SOURCES.clear()
            turn_stall._TURN_LIVENESS_SOURCES.update(snapshot)


_UNIX_SOCKET_PATH_MAX = 107  # sun_path is 108 bytes including the NUL


@pytest.fixture
def short_tmp_path(tmp_path_factory):
    """A private temp dir short enough to hold AF_UNIX socket paths (#2598).

    ``tmp_path`` nests under ``$TMPDIR/pytest-of-<user>/pytest-<n>/`` and,
    under pytest-xdist, an extra ``popen-gw<k>/`` level; with CI's
    ``TMPDIR=$RUNNER_TEMP/ccc-tmp`` that pushed ``…/responder.sock`` past the
    108-byte ``sun_path`` limit (``OSError: AF_UNIX path too long``). Socket
    suites override ``tmp_path`` with this fixture (built on
    ``tmp_path_factory`` so the override cannot recurse): a pytest temp dir
    when the base is short enough, otherwise a fresh 0700 ``mkdtemp`` under
    ``/tmp`` that is removed afterwards.
    """
    import shutil
    import tempfile

    if len(str(tmp_path_factory.getbasetemp())) <= _UNIX_SOCKET_PATH_MAX - 48:
        yield tmp_path_factory.mktemp("sock", numbered=True)
        return
    base = "/tmp" if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK) else None
    short = Path(tempfile.mkdtemp(prefix="ccc-sock-", dir=base))
    try:
        yield short
    finally:
        shutil.rmtree(short, ignore_errors=True)


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


# ---------------------------------------------------------------------------
# Skill lookup fixtures (#1678) — shared by test_skill_lookup.py and
# test_family_skills_server.py.  The synthetic repo/installed roots give the
# lookup a hermetic world: ambient CCC_* bridge variables are already stripped
# around every test by this conftest, and the fixture pins only what the
# lookup itself reads.
# ---------------------------------------------------------------------------

@pytest.fixture
def lookup_env(tmp_path: Path, monkeypatch) -> dict[str, str]:
    """Synthetic repo (registry + twin/retired sources) plus installed roots."""

    env = build_lookup_world(tmp_path)
    monkeypatch.setattr(os, "environ", {**os.environ, **env})
    return env
