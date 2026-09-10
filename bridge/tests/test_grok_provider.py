"""Real composition + Telegram Update objects + GrokRuntime, generated RPC only."""
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telegram import Chat, Message, Update, User
from telegram.ext import Application
from telegram.request import BaseRequest

from telegram_bot.__main__ import build_context, create_app
from telegram_bot.core.grok_bot import GrokTelegramBot, _TransportLogFilter
from telegram_bot.core.grok_protocol import ProtocolError
from telegram_bot.core.grok_provider import configured_route
from telegram_bot.core.grok_runtime import GrokRuntime
from telegram_bot.utils.config import Settings
from test_grok_runtime import AGENT, FakeGrokHost


def configuration(root, **overrides):
    env = {"HOME": str(root), "TELEGRAM_BOT_TOKEN": "123456:synthetic-token",
           "CCC_AGENT_PROVIDER": "grok", "ALLOWED_USER_IDS": "[42]",
           "CCC_GROK_OWNER_ID": "42", "CCC_GROK_TELEGRAM_BOT_ID": "123456",
           "CCC_GROK_BOT_ID": AGENT, "CCC_GROK_SSH_DESTINATION": "box@fixture.invalid",
           "CCC_GROK_JOURNAL_PATH": str(root / "journal"), **overrides}
    return Settings.load(project_root=root, environ=env, bot_env_file=root / "absent.env")


def update(number, text="generated input", *, user=42, chat=42, kind="private", **kwargs):
    message = Message(number, datetime.now(timezone.utc), Chat(chat, kind),
                      from_user=User(user, "synthetic", False), text=text, **kwargs)
    return Update(number, message=message)


def terminate_on_send(host):
    original = host.call
    async def delayed(operation, arguments=None):
        result = await original(operation, arguments)
        if operation == "send":
            asyncio.get_running_loop().call_soon(os.kill, os.getpid(), signal.SIGTERM)
            await asyncio.Future()
        return result
    return delayed


class GrokCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_actual_factory_is_pure_and_no_generic_workers(self):
        settings = configuration(self.root)
        before = list(self.root.iterdir())
        context = build_context(settings)
        bot = create_app(context)
        bot.validate_runtime_paths()
        self.assertIsInstance(bot, GrokTelegramBot)
        self.assertIsInstance(context.agent_runtime, GrokRuntime)
        self.assertIsNone(context.session_manager)
        self.assertIsNone(context.project_chat)
        self.assertIsNone(context.distill_journal)
        self.assertEqual(list(self.root.iterdir()), before)

    def test_incomplete_or_wrong_owner_route_never_falls_back(self):
        cases = [{"grok_owner_id": None}, {"grok_telegram_bot_id": 654321},
                 {"grok_ssh_destination": "-F /private"}, {"grok_bot_id": "invalid"},
                 {"grok_journal_path": Path("relative")}, {"allowed_user_ids": []},
                 {"allowed_user_ids": [42, 43]}, {"require_allowlist": False}]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ProtocolError):
                build_context(configuration(self.root).model_copy(update=values))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_generic_reset_and_project_frontends_explicitly_deny_grok(self):
        from telegram_bot.core.project_chat import ProjectChatHandler
        from telegram_bot.session.manager import SessionManager
        settings = configuration(self.root)
        with self.assertRaises(ValueError):
            SessionManager(None, settings)
        with self.assertRaises(ValueError):
            ProjectChatHandler(settings=settings)

    def test_management_process_attach_inspect_and_existing_state_retained(self):
        settings = configuration(self.root)
        envfile = self.root / ".telegram_bot"
        envfile.mkdir(mode=0o700)
        values = {"TELEGRAM_BOT_TOKEN": settings.telegram_bot_token, "CCC_AGENT_PROVIDER": "grok",
                  "ALLOWED_USER_IDS": "[42]", "CCC_GROK_OWNER_ID": "42", "CCC_GROK_TELEGRAM_BOT_ID": "123456",
                  "CCC_GROK_BOT_ID": AGENT, "CCC_GROK_SSH_DESTINATION": "box@fixture.invalid",
                  "CCC_GROK_JOURNAL_PATH": str(self.root / "journal")}
        path = envfile / ".env"
        path.write_text("\n".join(k + "=" + v for k, v in values.items()))
        path.chmod(0o600)
        repo = Path(__file__).resolve().parents[2]
        env = {"PATH": os.environ["PATH"], "HOME": str(self.root), "PYTHONPATH": str(repo / ".github/pythonpath")}
        def run(*args):
            return subprocess.run([sys.executable, "-m", "telegram_bot.core.grok_manage", "--path", str(self.root), *args],
                                  env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(run("attach-existing").returncode, 0)
        self.assertFalse((self.root / "journal").exists())
        first = run("attach-existing", "--acknowledge-existing-context")
        self.assertEqual(first.returncode, 0, first.stdout)
        journal = self.root / "journal"
        before = {p.name: p.read_bytes() for p in journal.iterdir()}
        self.assertEqual(json.loads(run("inspect").stdout)["revisions"], 1)
        self.assertNotEqual(run("attach-existing", "--acknowledge-existing-context").returncode, 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in journal.iterdir()})

    def test_transport_logs_drop_bodies_exception_and_urls(self):
        record = logging.LogRecord("telegram.ext.Updater", logging.ERROR, "", 1,
                                   "SECRET URL %s", ("PRIVATE",), (ValueError, ValueError("TOKEN"), None))
        record.exc_text = "PRIVATE"
        record.stack_info = "PRIVATE"
        _TransportLogFilter().filter(record)
        rendered = logging.Formatter().format(record)
        self.assertNotIn("SECRET", rendered)
        self.assertNotIn("PRIVATE", rendered)
        self.assertNotIn("TOKEN", rendered)

    def polling_fixture(self, *, terminate=False):
        settings = configuration(self.root)
        route = configured_route(settings)
        route.journal.create()
        host = FakeGrokHost(route.journal.binding)
        if terminate:
            host.call = terminate_on_send(host)
        runtime = GrokRuntime(route.journal, host)
        sent, rpc, delivered = [], [], False
        bot = None

        class TelegramFixture(BaseRequest):
            @property
            def read_timeout(self):
                return 1

            async def initialize(self):
                asyncio.get_running_loop().call_later(8, bot.request_shutdown)

            async def shutdown(self):
                pass

            async def do_request(self, url, method, request_data=None, **kwargs):
                nonlocal delivered
                name = url.rsplit("/", 1)[-1]
                rpc.append(name)
                if name == "getMe":
                    value = {"id": 123456, "is_bot": True, "first_name": "fixture", "username": "fixture_bot"}
                elif name == "getWebhookInfo":
                    value = {"url": "", "has_custom_certificate": False, "pending_update_count": 0}
                elif name == "deleteWebhook":
                    value = True
                elif name == "getUpdates":
                    if not delivered:
                        delivered = True
                        value = [json.loads(update(1).to_json())]
                    else:
                        await asyncio.sleep(0.05)
                        value = []
                elif name == "sendMessage":
                    sent.append(request_data.parameters)
                    value = json.loads(update(2, "generated reply").message.to_json())
                    asyncio.get_running_loop().call_soon(bot.request_shutdown)
                else:
                    raise AssertionError("unexpected Telegram API")
                return 200, json.dumps({"ok": True, "result": value}).encode()

        def builder():
            return Application.builder().request(TelegramFixture()).get_updates_request(TelegramFixture())
        bot = create_app(build_context(settings, agent_runtime=runtime, telegram_port=builder))
        bot.run()  # owns its event loop, including on Python 3.14
        self.assertEqual(len(host.sends), 1)
        if terminate:
            self.assertEqual(sent, [])
            with route.journal.claim() as claim:
                self.assertEqual(claim.load()[0]["stage"], "attempted")
        else:
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["text"], "generated reply generated input")
            self.assertEqual(sent[0]["chat_id"], 42)
        self.assertLess(rpc.index("getWebhookInfo"), rpc.index("getUpdates"))
        self.assertFalse(bot.ready)
        self.assertIsNone(bot._poller)

    def test_real_telegram_polling_dispatch_with_generated_api(self):
        self.polling_fixture()

    def test_real_process_sigterm_during_accepted_send_no_late_telegram_reply(self):
        # Signal only our owned disposable child, never the test runner/node.
        repo = Path(__file__).resolve().parents[2]
        script = """from test_grok_provider import GrokCompositionTests
c=GrokCompositionTests(); c.setUp()
try: c.polling_fixture(terminate=True)
finally: c.doCleanups()
print('sigterm-retained-no-delivery')
"""
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(repo / ".github/pythonpath"), str(Path(__file__).parent)))}
        child = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), "sigterm-retained-no-delivery")


class GrokTelegramTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = configuration(self.root)
        self.route = configured_route(self.settings)
        self.route.journal.create()
        self.host = FakeGrokHost(self.route.journal.binding)
        runtime = GrokRuntime(self.route.journal, self.host)
        self.bot = create_app(build_context(self.settings, agent_runtime=runtime))
        self.port = SimpleNamespace(id=123456, get_me=AsyncMock(return_value=User(123456, "fixture", True)),
                                    get_webhook_info=AsyncMock(return_value=SimpleNamespace(url="")),
                                    send_message=AsyncMock())
        self.app = SimpleNamespace(bot=self.port)
        self.context = SimpleNamespace(bot=self.port)
        await self.bot.initialize(self.app)
        self.addAsyncCleanup(self.bot.shutdown, self.app)

    def state(self):
        return {p.name: p.read_bytes() for p in self.route.journal.root.iterdir()}

    def fresh_bot(self):
        bot = create_app(build_context(self.settings, agent_runtime=GrokRuntime(self.route.journal, self.host)))
        self.addAsyncCleanup(bot.shutdown, self.app)
        return bot

    async def test_owner_dm_actual_runtime_and_persisted_result_before_reply(self):
        async def reply(**kwargs):
            with self.route.journal.claim() as claim:
                self.assertEqual(claim.load()[0]["stage"], "complete")
            self.assertEqual(kwargs["chat_id"], 42)
            self.assertEqual(kwargs["text"], "generated reply generated input")
            self.assertIsNone(kwargs["parse_mode"])
        self.port.send_message.side_effect = reply
        await self.bot.handle(update(1), self.context)
        self.assertEqual(len(self.host.sends), 1)
        self.port.send_message.assert_awaited_once()

    async def test_wrong_user_group_topic_forward_edit_callback_file_and_commands(self):
        before, calls = self.state(), len(self.host.calls)
        blocked = [update(1, user=43), update(2, chat=-1, kind="supergroup"),
                   update(3, message_thread_id=7, is_topic_message=True),
                   update(4, sender_chat=Chat(-1, "channel")),
                   Update(5, edited_message=update(5).message), Update(6)]
        for item in blocked:
            await self.bot.handle(item, self.context)
        self.port.send_message.assert_not_awaited()
        for number, text in enumerate([None, "/new", "/model other", "/resume foreign", "/command rm", "/continue", "/distill"], 10):
            await self.bot.handle(update(number, text), self.context)
        self.assertEqual(self.state(), before)
        self.assertEqual(len(self.host.calls), calls)

    async def test_boot_identity_before_any_journal_or_bot_access(self):
        await self.bot.shutdown(self.app)
        self.bot = self.fresh_bot()
        before, calls = self.state(), len(self.host.calls)
        self.port.get_me.return_value = User(999, "wrong", True)
        with patch.object(self.route.journal, "claim", side_effect=AssertionError("state opened")):
            with self.assertRaises(ProtocolError):
                await self.bot.initialize(self.app)
        self.assertEqual(before, self.state())
        self.assertEqual(calls, len(self.host.calls))
        self.assertFalse(self.bot.ready)

    async def test_missing_or_corrupt_journal_denies_start_without_reset(self):
        await self.bot.shutdown(self.app)
        self.bot = self.fresh_bot()
        foreign = configuration(self.root, CCC_GROK_JOURNAL_PATH=str(self.root / "missing"))
        bot = create_app(build_context(foreign))
        with self.assertRaises(Exception):
            await bot.initialize(self.app)
        self.assertFalse((self.root / "missing").exists())
        (self.route.journal.root / "unknown").write_bytes(b"generated")
        before = self.state()
        with self.assertRaises(Exception):
            await self.bot.initialize(self.app)
        self.assertEqual(before, self.state())

    async def test_local_second_poller_denied_even_separate_journal(self):
        other = configuration(self.root, CCC_GROK_JOURNAL_PATH=str(self.root / "other"))
        bot = create_app(build_context(other))
        with self.assertRaisesRegex(ProtocolError, "grok_local_poller_busy"):
            await bot.initialize(self.app)
        self.assertFalse((self.root / "other").exists())

    async def test_existing_webhook_denied_before_journal_open(self):
        await self.bot.shutdown(self.app)
        self.bot = self.fresh_bot()
        self.port.get_webhook_info.return_value = SimpleNamespace(url="https://fixture.invalid")
        with patch.object(self.bot.runtime.journal, "claim", side_effect=AssertionError("state opened")):
            with self.assertRaisesRegex(ProtocolError, "grok_existing_webhook_denied"):
                await self.bot.initialize(self.app)

    async def test_late_boot_after_shutdown_never_reopens_state_or_poller(self):
        await self.bot.shutdown(self.app)
        bot = self.fresh_bot()
        before, calls = self.state(), len(self.host.calls)
        entered, release = asyncio.Event(), asyncio.Event()
        async def late():
            entered.set()
            await release.wait()
            return User(123456, "fixture", True)
        self.port.get_me.side_effect = late
        boot = asyncio.create_task(bot.initialize(self.app))
        await entered.wait()
        await bot.shutdown(self.app)
        release.set()
        with self.assertRaisesRegex(ProtocolError, "grok_startup_retired"):
            await boot
        self.assertFalse(bot.ready)
        self.assertIsNone(bot._poller)
        self.assertEqual(before, self.state())
        self.assertEqual(calls, len(self.host.calls))
        with self.assertRaises(ProtocolError):
            await bot.initialize(self.app)

    async def test_shutdown_fence_cancels_active_before_framework_drain(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.host.call
        async def delayed(operation, args=None):
            result = await original(operation, args)
            if operation == "send":
                entered.set()
                await release.wait()
            return result
        self.host.call = delayed
        turn = asyncio.create_task(self.bot.handle(update(1), self.context))
        await entered.wait()
        self.bot.request_shutdown()  # synchronous signal boundary
        self.assertFalse(self.bot.ready)
        release.set()
        await self.bot.shutdown(self.app)
        await asyncio.gather(turn, return_exceptions=True)
        self.port.send_message.assert_not_awaited()
        with self.route.journal.claim() as claim:
            self.assertEqual(claim.load()[0]["stage"], "attempted")

    async def test_process_update_replay_and_restart_cached_exact_input(self):
        await self.bot.handle(update(1), self.context)
        before = self.state()
        await self.bot.handle(update(1), self.context)
        self.assertEqual(self.port.send_message.await_count, 1)
        await self.bot.shutdown(self.app)
        reopened = create_app(build_context(self.settings, agent_runtime=GrokRuntime(self.route.journal, self.host)))
        await reopened.initialize(self.app)
        try:
            await reopened.handle(update(2), self.context)
        finally:
            await reopened.shutdown(self.app)
        self.assertEqual(before, self.state())
        self.assertEqual(len(self.host.sends), 1)
        self.assertEqual(self.port.send_message.await_count, 2)

    async def test_lost_accepted_send_response_reconciles_same_nonce(self):
        self.host.fail_after_send = True
        await self.bot.handle(update(1), self.context)
        self.host.fail_after_send = False
        await self.bot.handle(update(2), self.context)
        self.assertEqual(len(self.host.sends), 1)
        self.assertEqual(self.port.send_message.call_args.kwargs["text"], "generated reply generated input")

    async def test_stop_busy_late_reply_and_explicit_reopen(self):
        original = self.host.call
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(op, args=None):
            result = await original(op, args)
            if op == "send":
                entered.set()
                await release.wait()
            return result
        self.host.call = delayed
        turn = asyncio.create_task(self.bot.handle(update(1), self.context))
        await asyncio.wait_for(entered.wait(), 2)
        await self.bot.handle(update(2, "other"), self.context)
        self.assertEqual(len(self.host.sends), 1)
        await self.bot.handle(update(3, "/stop"), self.context)
        await asyncio.gather(turn, return_exceptions=True)
        release.set()
        self.assertFalse(any("generated reply" in c.kwargs["text"] for c in self.port.send_message.call_args_list))
        await self.bot.handle(update(4), self.context)
        self.assertEqual(len(self.host.sends), 1)
        self.assertIn("generated reply", self.port.send_message.call_args.kwargs["text"])

    async def test_oversize_and_wrong_context_bot_never_touch_runtime(self):
        before, calls = self.state(), len(self.host.calls)
        await self.bot.handle(update(1, "x" * 32769), self.context)
        await self.bot.handle(update(2), SimpleNamespace(bot=SimpleNamespace(id=999)))
        self.assertEqual(before, self.state())
        self.assertEqual(calls, len(self.host.calls))


if __name__ == "__main__":
    unittest.main()
