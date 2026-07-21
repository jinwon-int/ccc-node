import ast
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHONPATH_SHIM = REPO_ROOT / ".github" / "pythonpath"


def _run_probe(script: str, *, probe_root: Path) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(PYTHONPATH_SHIM),
        "PROBE_ROOT": str(probe_root),
    }
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=probe_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _bot_settings(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        bot_data_dir=root / "bot-data",
        ffmpeg_path=None,
        execution_profile="strict-project",
        allowed_user_ids=[1],
        bash_policy="disabled",
        require_allowlist=True,
    )


def test_bot_run_initializes_session_store_after_access_guard(tmp_path):
    result = _run_probe(
        """
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

root = Path(os.environ["PROBE_ROOT"])
os.environ["PROJECT_ROOT"] = str(root / "project")
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:test"

from telegram_bot.core import bot_lifecycle
from telegram_bot.core.bot import TelegramBot

settings = SimpleNamespace(
    bot_data_dir=root / "bot-data",
    ffmpeg_path=None,
    execution_profile="strict-project",
    allowed_user_ids=[1],
    bash_policy="disabled",
    require_allowlist=True,
)
events = []
manager = SimpleNamespace(initialize=lambda: events.append("manager"))
project_chat = SimpleNamespace()
bot = TelegramBot(
    settings=settings,
    session_manager=manager,
    project_chat=project_chat,
)

async def fake_run_async():
    events.append("async")

bot._run_async = fake_run_async
bot_lifecycle.enforce_access_control = lambda value: (
    events.append("guard") if value is settings else None
)
bot_lifecycle.health_reporter.initialize_process = lambda: events.append("health")
bot_lifecycle.health_reporter.mark_starting = lambda _reason: None
bot_lifecycle.health_reporter.mark_unavailable = lambda _reason: None
bot_lifecycle.health_reporter.cleanup_runtime_files = lambda: None
bot.run()
assert events[:3] == ["guard", "manager", "health"]
assert events[-1] == "async"
print("STARTUP_ORDER_OK")
""",
        probe_root=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "STARTUP_ORDER_OK"


def test_main_rejects_unsafe_session_store_before_logging(tmp_path):
    result = _run_probe(
        """
import os
import sys
from pathlib import Path

root = Path(os.environ["PROBE_ROOT"])
project = root / "project"
unsafe = root / "unsafe"
logs = root / "logs"
project.mkdir()
unsafe.mkdir()
unsafe.chmod(0o777)

os.environ.update(
    {
        "PROJECT_ROOT": str(project),
        "TELEGRAM_BOT_TOKEN": "123456:test",
        "ALLOWED_USER_IDS": "[7]",
        "SESSION_STORE_PATH": str(unsafe / "sessions.json"),
        "LOGS_DIR": str(logs),
    }
)
sys.argv = ["telegram_bot"]

from telegram_bot.__main__ import main

try:
    main()
except (OSError, RuntimeError, ValueError) as error:
    message = str(error)
    assert "session store" in message and "writable" in message, message
else:
    raise AssertionError("unsafe session store unexpectedly accepted")

assert not logs.exists(), list(logs.glob("*")) if logs.exists() else []
print("SESSION_REJECTED_BEFORE_LOGGING")
""",
        probe_root=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SESSION_REJECTED_BEFORE_LOGGING"


def test_create_bot_composes_runtime_without_initializing_filesystem(tmp_path):
    result = _run_probe(
        """
import os
from pathlib import Path
from types import SimpleNamespace

from telegram_bot.__main__ import build_context, create_app
from telegram_bot.utils.config import Settings

root = Path(os.environ["PROBE_ROOT"])
os.environ["PROJECT_ROOT"] = str(root / "project")
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:test"
settings = Settings.load(
    project_root=root / "project",
    environ={
        "HOME": str(root / "home"),
        "TELEGRAM_BOT_TOKEN": "123456:test",
    },
    bot_env_file=root / "missing.env",
)
sdk_factory = object()
telegram_port = lambda: None
clock = SimpleNamespace(time=lambda: 1.0, monotonic=lambda: 2.0)
context = build_context(
    settings,
    sdk_factory=sdk_factory,
    telegram_port=telegram_port,
    clock=clock,
)
bot = create_app(context)
assert context.settings is settings
assert context.sdk_factory is sdk_factory
assert context.telegram_port is telegram_port
assert context.clock is clock
# Default Claude provider (#584): a ClaudeRuntime adapter is composed for
# ProjectChat and receives the injected SDK client factory. Its constructor
# performs no filesystem initialization, preserving this probe's
# deferred-initialization invariant.
from telegram_bot.core.claude_runtime import ClaudeRuntime

assert isinstance(context.agent_runtime, ClaudeRuntime)
assert context.agent_runtime._sdk_client_factory is sdk_factory
assert bot._project_chat._agent_runtime is context.agent_runtime
assert bot._config is settings
assert bot._session_manager.settings is settings
assert bot._project_chat._config is settings
assert bot._project_chat._clock is clock
assert bot._application_builder_factory is telegram_port
assert bot._clock is clock
assert bot._project_chat.project_root == settings.project_root
assert bot._push_notifier._config is settings
assert bot._session_manager.store._storage_path == settings.session_store_path
assert context.distill_journal.root == settings.bot_data_dir / "distill-journal"
assert bot._distill_journal is context.distill_journal
assert not settings.session_store_path.parent.exists()
assert not context.distill_journal.root.exists()
print("RUNTIME_COMPOSED")
""",
        probe_root=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "RUNTIME_COMPOSED"


def test_telegram_bot_requires_injected_runtime_dependencies(tmp_path):
    from telegram_bot.core import bot as bot_module

    settings = _bot_settings(tmp_path)
    manager = SimpleNamespace()
    project_chat = SimpleNamespace()
    telegram_bot = bot_module.TelegramBot(
        settings=settings,
        session_manager=manager,
        project_chat=project_chat,
    )

    assert telegram_bot._config is settings
    assert telegram_bot._session_manager is manager
    assert telegram_bot._project_chat is project_chat
    assert not hasattr(bot_module, "bot")
    with pytest.raises(TypeError):
        bot_module.TelegramBot()


def test_session_manager_initializes_its_injected_store():
    from telegram_bot.session.manager import SessionManager

    class FakeStore:
        initialized = False

        def initialize(self):
            self.initialized = True

    store = FakeStore()
    manager = SessionManager(
        store=store,
        settings=SimpleNamespace(auto_new_session_after_hours=24.0),
    )

    manager.initialize()

    assert store.initialized is True


def test_session_store_constructor_defers_filesystem_initialization(tmp_path):
    from telegram_bot.session.store import SessionStore

    storage_path = tmp_path / "state" / "sessions.json"

    store = SessionStore(storage_path)

    assert not storage_path.parent.exists()
    store.initialize()
    assert storage_path.parent.is_dir()


def test_session_store_rejects_every_operation_before_initialization(tmp_path):
    from telegram_bot.session.store import SessionStore

    storage_path = tmp_path / "state" / "sessions.json"
    store = SessionStore(storage_path)
    operations = (
        lambda: store.list_sessions(),
        lambda: store.get(1),
        lambda: store.set(1, {"value": 1}),
        lambda: store.update(1, {"value": 2}),
        lambda: store.delete(1),
    )

    for operation in operations:
        with pytest.raises(RuntimeError, match="not initialized"):
            asyncio.run(operation())

    assert not storage_path.parent.exists()


def test_uninitialized_second_store_cannot_overwrite_existing_state(tmp_path):
    from telegram_bot.session.store import SessionStore

    storage_path = tmp_path / "state" / "sessions.json"
    owner = SessionStore(storage_path)
    owner.initialize()
    asyncio.run(owner.set(1, {"approval": "unused"}))
    before = storage_path.read_bytes()

    uninitialized = SessionStore(storage_path)
    with pytest.raises(RuntimeError, match="not initialized"):
        asyncio.run(uninitialized.get(1))
    with pytest.raises(RuntimeError, match="not initialized"):
        asyncio.run(uninitialized.set(2, {"approval": "replayed"}))

    assert storage_path.read_bytes() == before


def test_stale_reply_mode_normalization_cannot_rearm_consumed_approval(tmp_path):
    from telegram_bot.session.manager import SessionManager
    from telegram_bot.session.store import SessionStore

    class InterleavingStore(SessionStore):
        release_before_return = False

        async def get(self, user_id):
            snapshot = await super().get(user_id)
            if self.release_before_return:
                self.release_before_return = False
                await super().patch(
                    user_id,
                    remove_fields={"bash_approved_once"},
                )
            return snapshot

    store = InterleavingStore(tmp_path / "state" / "sessions.json")
    manager = SessionManager(
        store=store,
        settings=SimpleNamespace(auto_new_session_after_hours=24.0),
    )
    manager.initialize()
    asyncio.run(
        store.set(
            7,
            {"reply_mode": "invalid", "bash_approved_once": True},
        )
    )
    store.release_before_return = True

    normalized = asyncio.run(manager.get_session(7))
    persisted = asyncio.run(store.get(7))

    assert normalized["reply_mode"] == "text"
    assert "bash_approved_once" not in persisted


def test_session_id_writer_cannot_rearm_consumed_approval(tmp_path):
    result = _run_probe(
        """
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

root = Path(os.environ["PROBE_ROOT"])
os.environ["PROJECT_ROOT"] = str(root / "project")
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:test"

from telegram_bot.core.bot import TelegramBot

class InterleavingManager:
    def __init__(self):
        self.persisted = {}

    async def get_session(self, key):
        del key
        return {"bash_approved_once": True}

    async def update_session(self, key, data):
        del key
        self.persisted.update(data)

    async def patch_session(self, key, *, updates=None, remove_fields=()):
        del key
        self.persisted.update(dict(updates or {}))
        for field in remove_fields:
            self.persisted.pop(field, None)

subject = object.__new__(TelegramBot)
subject._session_manager = InterleavingManager()
subject._runtime_active_sessions = set()
subject._config = SimpleNamespace(agent_provider="claude")

asyncio.run(
    subject._save_session_id(
        "7:99",
        SimpleNamespace(session_id="session-new"),
    )
)

assert subject._session_manager.persisted == {
    "provider": "claude",
    "session_id": "session-new",
}
print("SESSION_ID_PATCH_OK")
""",
        probe_root=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SESSION_ID_PATCH_OK"


def test_consumed_bash_approval_cannot_be_rearmed_without_fresh_request(tmp_path):
    from telegram_bot.core.bot_access import BotAccessMixin
    from telegram_bot.session.manager import SessionManager
    from telegram_bot.session.store import SessionStore

    class Subject(BotAccessMixin):
        _ALLOW_OUTSIDE_ONCE_TOKEN = "ALLOW_OUTSIDE_ONCE"
        _DENY_OUTSIDE_TOKEN = "DENY_OUTSIDE"

        def __init__(self, manager):
            self._session_manager = manager

        @staticmethod
        def _conversation_key(user_id, chat_id=None):
            return f"{user_id}:{chat_id}" if chat_id is not None else str(user_id)

        @staticmethod
        def _bash_policy():
            return "approve-each"

    store = SessionStore(tmp_path / "state" / "sessions.json")
    manager = SessionManager(
        store=store,
        settings=SimpleNamespace(auto_new_session_after_hours=24.0),
    )
    manager.initialize()
    subject = Subject(manager)
    request = {"command": "pwd"}

    first = asyncio.run(subject._permission_callback(10, 20, "Bash", request))
    assert first.behavior == "deny"
    asyncio.run(subject._maybe_capture_outside_approval(20, "ALLOW_OUTSIDE_ONCE", 10))
    consumed = asyncio.run(subject._permission_callback(10, 20, "Bash", request))
    assert consumed.behavior == "allow"

    asyncio.run(subject._maybe_capture_outside_approval(20, "ALLOW_OUTSIDE_ONCE", 10))
    persisted = asyncio.run(manager.get_session("20:10"))
    assert persisted.get("bash_approved_once") is False
    replay = asyncio.run(subject._permission_callback(10, 20, "Bash", request))
    assert replay.behavior == "deny"


def test_production_constructs_session_store_only_at_composition_root():
    production_calls = []
    bridge_dir = REPO_ROOT / "bridge"
    for path in bridge_dir.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            direct_call = isinstance(function, ast.Name) and function.id == "SessionStore"
            qualified_call = (
                isinstance(function, ast.Attribute) and function.attr == "SessionStore"
            )
            if direct_call or qualified_call:
                production_calls.append((path.relative_to(REPO_ROOT), node.lineno))

    assert len(production_calls) == 1
    assert production_calls[0][0] == Path("bridge/__main__.py")


def test_deletion_bearing_handlers_persist_removals_and_reject_stale_callbacks(tmp_path):
    result = _run_probe(
        """
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

root = Path(os.environ["PROBE_ROOT"])
project = root / "project"
os.environ["PROJECT_ROOT"] = str(project)
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:test"

from telegram_bot.__main__ import create_bot
from telegram_bot.core import bot_commands, bot_delivery
from telegram_bot.utils.config import Settings

settings = Settings.load(
    project_root=project,
    environ={"HOME": str(root / "home"), "TELEGRAM_BOT_TOKEN": "123456:test"},
    bot_env_file=root / "missing.env",
)
bot = create_bot(settings)
bot._session_manager.initialize()
telegram_port = SimpleNamespace(send_message=AsyncMock())
bot.application = SimpleNamespace(bot=telegram_port)

async def allowed(_update):
    return True

async def no_queue(_key, _run_task, _overflow):
    return None

bot._check_access = allowed
bot._enqueue_user_task = no_queue
bot._send_file_paths = AsyncMock()
bot_delivery.build_reply_context_prefix = lambda *args, **kwargs: ""
bot_delivery.project_chat_handler = SimpleNamespace(
    get_session_last_assistant_message=lambda _sid: None,
)

async def exercise():
    user = SimpleNamespace(id=7)
    chat = SimpleNamespace(id=9)
    query = SimpleNamespace(
        data="extsend:old-token:allow",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    callback_update = SimpleNamespace(
        effective_user=user,
        effective_chat=chat,
        callback_query=query,
        message=None,
    )
    external = root / "external.txt"
    external.write_text("private", encoding="utf-8")
    bot_delivery.secrets.token_urlsafe = lambda _length: "request-token"
    await bot._prompt_outside_file_confirmation(9, 7, [external])
    key = bot._conversation_key(7, 9)
    persisted = await bot._session_manager.get_session(key)
    assert persisted["pending_external_files"] == [str(external)]
    assert persisted["pending_external_files_token"] == "request-token"
    keyboard = telegram_port.send_message.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == "extsend:request-token:allow"
    assert keyboard.inline_keyboard[1][0].callback_data == "extsend:request-token:deny"

    await bot._handle_callback(callback_update, None)
    persisted = await bot._session_manager.get_session(key)
    assert persisted["pending_external_files_token"] == "request-token"
    bot._send_file_paths.assert_not_awaited()

    query.data = "extsend:request-token:deny"
    await bot._handle_callback(callback_update, None)
    persisted = await bot._session_manager.get_session(key)
    assert "pending_external_files" not in persisted
    assert "pending_external_files_token" not in persisted

    query.data = "extsend:request-token:allow"
    await bot._handle_callback(callback_update, None)
    bot._send_file_paths.assert_not_awaited()

    key = bot._conversation_key(7, 9)
    message = SimpleNamespace(text="1", reply_text=AsyncMock(), reply_to_message=None)
    text_update = SimpleNamespace(
        effective_user=user,
        effective_chat=chat,
        callback_query=None,
        message=message,
    )
    await bot._session_manager.update_session(key, {"resume_list": [["sid-1", "summary"]]})
    await bot._handle_text_message(text_update, None)
    assert "resume_list" not in await bot._session_manager.get_session(key)

    message.text = "cancel"
    await bot._session_manager.update_session(key, {"resume_list": [["sid-2", "other"]]})
    await bot._handle_text_message(text_update, None)
    assert "resume_list" not in await bot._session_manager.get_session(key)

    await bot._session_manager.update_session(7, {"approve_all_outside_access": True})
    bot_commands.project_chat_handler = SimpleNamespace(
        clear_user_stream=AsyncMock(),
        clear_pending_permissions=lambda _user_id: None,
    )
    await bot._clear_user_state(7)
    assert "approve_all_outside_access" not in await bot._session_manager.get_session(7)

asyncio.run(exercise())
print("DELETION_SEMANTICS_OK")
""",
        probe_root=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "DELETION_SEMANTICS_OK"


def test_setup_logging_rejects_symlinked_runtime_directory_before_writing(tmp_path):
    result = _run_probe(
        """
import os
from pathlib import Path

root = Path(os.environ["PROBE_ROOT"])
project = root / "project"
target = root / "redirected"
project.mkdir(mode=0o700)
target.mkdir(mode=0o700)
(project / ".telegram_bot").symlink_to(target, target_is_directory=True)
os.environ["PROJECT_ROOT"] = str(project)
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:test"

from telegram_bot.utils.config import Settings
from telegram_bot.utils.logging_setup import setup_logging

settings = Settings.load(
    project_root=project,
    environ={
        "HOME": str(root / "home"),
        "TELEGRAM_BOT_TOKEN": "123456:test",
    },
    bot_env_file=root / "missing.env",
)
try:
    setup_logging(settings)
except PermissionError:
    pass
else:
    raise AssertionError("symlinked runtime directory was accepted")
assert list(target.rglob("*")) == []
print("UNSAFE_LOG_PATH_REJECTED")
""",
        probe_root=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "UNSAFE_LOG_PATH_REJECTED"


@pytest.mark.parametrize("log_kind", ["bot", "error"])
def test_setup_logging_rejects_final_log_symlink(tmp_path, log_kind):
    script = """
import os
from datetime import datetime
from pathlib import Path

root = Path(os.environ["PROBE_ROOT"])
project = root / "project"
logs = project / ".telegram_bot" / "logs"
logs.mkdir(parents=True, mode=0o700)
target = root / "redirected.log"
target.write_text("SAFE", encoding="utf-8")
log_name = "bot.log" if __LOG_KIND__ == "bot" else f"error_{datetime.now():%Y-%m-%d}.log"
(logs / log_name).symlink_to(target)
os.environ["PROJECT_ROOT"] = str(project)
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:test"

from telegram_bot.utils.config import Settings
from telegram_bot.utils.logging_setup import setup_logging

settings = Settings.load(
    project_root=project,
    environ={"HOME": str(root / "home"), "TELEGRAM_BOT_TOKEN": "123456:test"},
    bot_env_file=root / "missing.env",
)
try:
    setup_logging(settings)
except PermissionError:
    pass
else:
    raise AssertionError("final log symlink was accepted")
assert target.read_text(encoding="utf-8") == "SAFE"
print("FINAL_LOG_SYMLINK_REJECTED")
""".replace("__LOG_KIND__", repr(log_kind))
    result = _run_probe(script, probe_root=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "FINAL_LOG_SYMLINK_REJECTED"


@pytest.mark.parametrize("unsafe_kind", ["hardlink", "fifo"])
def test_setup_logging_rejects_non_private_final_log_file(tmp_path, unsafe_kind):
    script = """
import os
from pathlib import Path

root = Path(os.environ["PROBE_ROOT"])
project = root / "project"
logs = project / ".telegram_bot" / "logs"
logs.mkdir(parents=True, mode=0o700)
log_path = logs / "bot.log"
target = root / "hardlink-target.log"
if __UNSAFE_KIND__ == "hardlink":
    target.write_text("SAFE", encoding="utf-8")
    os.link(target, log_path)
else:
    os.mkfifo(log_path, mode=0o600)
os.environ["PROJECT_ROOT"] = str(project)
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:test"

from telegram_bot.utils.config import Settings
from telegram_bot.utils.logging_setup import setup_logging

settings = Settings.load(
    project_root=project,
    environ={"HOME": str(root / "home"), "TELEGRAM_BOT_TOKEN": "123456:test"},
    bot_env_file=root / "missing.env",
)
try:
    setup_logging(settings)
except PermissionError:
    pass
else:
    raise AssertionError("unsafe final log file was accepted")
if target.exists():
    assert target.read_text(encoding="utf-8") == "SAFE"
print("UNSAFE_FINAL_LOG_REJECTED")
""".replace("__UNSAFE_KIND__", repr(unsafe_kind))
    result = _run_probe(script, probe_root=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "UNSAFE_FINAL_LOG_REJECTED"


def test_private_log_handler_transaction_rolls_back_second_open_failure(tmp_path):
    result = _run_probe(
        """
import os
from pathlib import Path
from unittest.mock import patch

from telegram_bot.utils import logging_setup as config_module

logs = Path(os.environ["PROBE_ROOT"]) / "logs"
logs.mkdir(mode=0o700)
before_fds = len(list(Path("/proc/self/fd").iterdir()))
original = config_module._private_log_handler_at
calls = 0

def fail_second(directory_fd, name, display_path):
    global calls
    calls += 1
    if calls == 2:
        raise OSError("second handler failed")
    return original(directory_fd, name, display_path)

with patch.object(config_module, "_private_log_handler_at", side_effect=fail_second):
    try:
        config_module._private_log_handlers(logs, ["bot.log", "error.log"])
    except OSError as exc:
        assert str(exc) == "second handler failed"
    else:
        raise AssertionError("second handler failure was not propagated")

assert not (logs / "bot.log").exists()
assert not (logs / "error.log").exists()
assert len(list(Path("/proc/self/fd").iterdir())) == before_fds
print("LOG_TRANSACTION_ROLLBACK_OK")
""",
        probe_root=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "LOG_TRANSACTION_ROLLBACK_OK"


def test_setup_logging_creates_owner_only_log_files_in_legacy_directory(tmp_path):
    result = _run_probe(
        """
import os
import stat
from pathlib import Path

root = Path(os.environ["PROBE_ROOT"])
project = root / "project"
logs = project / ".telegram_bot" / "logs"
logs.mkdir(parents=True, mode=0o755)
project.chmod(0o700)
(project / ".telegram_bot").chmod(0o700)
logs.chmod(0o755)
os.environ["PROJECT_ROOT"] = str(project)
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:test"

from telegram_bot.utils.config import Settings
from telegram_bot.utils.logging_setup import setup_logging

settings = Settings.load(
    project_root=project,
    environ={"HOME": str(root / "home"), "TELEGRAM_BOT_TOKEN": "123456:test"},
    bot_env_file=root / "missing.env",
)
setup_logging(settings)
created = list(logs.glob("*.log"))
assert len(created) == 2
assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in created)
print("PRIVATE_LOG_FILES")
""",
        probe_root=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "PRIVATE_LOG_FILES"


def test_runtime_modules_import_without_runtime_state(tmp_path):
    result = _run_probe(
        """
import os
from pathlib import Path

root = Path(os.environ["PROBE_ROOT"])
os.environ.pop("PROJECT_ROOT", None)
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
before_env = dict(os.environ)
before_entries = set(root.rglob("*"))

import telegram_bot.__main__ as main_module
import telegram_bot.core.bot as bot_module
import telegram_bot.core.project_chat as project_chat_module
import telegram_bot.session.manager as manager_module
import telegram_bot.session.store as store_module
import telegram_bot.utils.config as config_module

assert dict(os.environ) == before_env
assert set(root.rglob("*")) == before_entries
assert not hasattr(store_module, "session_store")
assert not hasattr(manager_module, "session_manager")
assert not hasattr(project_chat_module, "project_chat_handler")
assert not hasattr(bot_module, "bot")
assert callable(main_module.build_context)
assert callable(config_module.Settings.load)
print("RUNTIME_IMPORT_PURE")
""",
        probe_root=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "RUNTIME_IMPORT_PURE"


def test_pinned_sdk_public_options_and_client_construction_smoke(tmp_path):
    result = _run_probe(
        """
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

options = ClaudeAgentOptions(cwd="/tmp", cli_path="/bin/true")
client = ClaudeSDKClient(options=options)
assert options.cli_path == "/bin/true"
assert client is not None
print("PINNED_SDK_PUBLIC_API_OK")
""",
        probe_root=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "PINNED_SDK_PUBLIC_API_OK"


def test_build_context_composes_a_budget_gated_distill_worker(tmp_path):
    # Production-composition regression (#388): the real composition root
    # must construct the distill extraction worker through the handler
    # factory so its autonomous spend shares the node usage meter, and the
    # worker must not be constructible without an explicit gate decision.
    result = _run_probe(
        """
import inspect
import os
from pathlib import Path

root = Path(os.environ["PROBE_ROOT"])
(root / "project").mkdir(parents=True, exist_ok=True)
os.environ["PROJECT_ROOT"] = str(root / "project")
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:test"
os.environ["ALLOWED_USER_IDS"] = "1"

from telegram_bot.__main__ import build_context, load_runtime_settings
from telegram_bot.memory.distill_worker import CodexDistillExtractionWorker

settings = load_runtime_settings()
context = build_context(settings)
worker = context.distill_extraction_worker
assert isinstance(worker, CodexDistillExtractionWorker), type(worker)
meter = context.project_chat.usage_meter
assert meter is not None, "the production meter must exist by default"
assert worker._usage_meter is meter, "the worker must share the handler meter"

parameter = inspect.signature(CodexDistillExtractionWorker.__init__).parameters[
    "usage_meter"
]
assert parameter.default is inspect.Parameter.empty, (
    "usage_meter must stay an explicit constructor decision"
)

from telegram_bot.__main__ import create_app

bot = create_app(context)
assert bot._distill_extraction_worker is worker, (
    "the running application must retain the gated worker"
)
print("COMPOSED-GATED-WORKER-OK")
""",
        probe_root=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "COMPOSED-GATED-WORKER-OK" in result.stdout
