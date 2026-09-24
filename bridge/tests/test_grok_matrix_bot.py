"""Grok restricted frontend over the E2EE Matrix transport: owner direct room only.

Real composition (``build_context``/``create_app``) and real ``GrokRuntime`` +
journal with the generated host fake; the Matrix transport is faked at the
``TurnRunner`` seam like ``test_matrix_bot``.
"""
import asyncio
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from telegram_bot.__main__ import build_context, create_app
from telegram_bot.core.grok_bot import GrokTelegramBot
from telegram_bot.core.grok_matrix_bot import (
    BANNER_KEY,
    BUSY,
    GrokMatrixBot,
    NOT_ADMITTED,
    STATUS_TEXT,
    TEXT_ONLY,
    TOO_LARGE,
    UNCERTAIN,
)
from telegram_bot.core.grok_protocol import MAX_PROMPT, ProtocolError
from telegram_bot.core.grok_provider import configured_route
from telegram_bot.core.grok_runtime import GrokRuntime
from test_grok_provider import configuration
from test_grok_runtime import FakeGrokHost

OWNER = "@owner:example.org"
KID = "@kid:example.org"
DM_ROOM = "!dm:example.org"
FAMILY_ROOM = "!family:example.org"


def matrix_configuration(root, **overrides):
    (root / "matrix.json").touch(mode=0o600)
    return configuration(root, CCC_CHANNEL="matrix", CCC_MATRIX_CONFIG_PATH=str(root / "matrix.json"), **overrides)


def matrix_config(**overrides):
    config = {
        "homeserver": "https://matrix.example.org",
        "account": "@grok:example.org",
        "device_id": "DEV",
        "owner": OWNER,
        "rooms": [DM_ROOM],
        "not_before_ms": 0,
    }
    config.update(overrides)
    return config


def stub_state(config):
    """Replace ``matrix.state`` so ``load_config`` returns the generated config."""
    module = types.ModuleType("telegram_bot.core.matrix.state")
    module.load_config = lambda path: dict(config)  # type: ignore[attr-defined]
    return patch.dict(sys.modules, {"telegram_bot.core.matrix.state": module})


def job(body, *, sender=OWNER, room=DM_ROOM, event_id="$e1"):
    return {"event_id": event_id, "room_id": room, "sender": sender, "scope": "ab" * 32, "body": body}


class FakeTransport:
    def __init__(self, config, runner, *, script=None):
        self.config = config
        self.runner = runner
        self.script = script
        self.events = []
        self.notices = []

    async def open(self, initialize=False):
        self.events.append(("open", initialize))

    async def run(self):
        self.events.append(("run", None))
        if self.script is not None:
            await self.script(self)

    async def close(self):
        self.events.append(("close", None))

    def enqueue_notice(self, room_id, text, *, key=None):
        self.notices.append((room_id, text, key))

    def room_kind(self, room_id):
        return "family" if room_id == FAMILY_ROOM else "direct"


class GrokMatrixCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_factory_selects_restricted_matrix_frontend_without_generic_workers(self):
        settings = matrix_configuration(self.root)
        before = sorted(self.root.iterdir())
        context = build_context(settings)
        bot = create_app(context)
        bot.validate_runtime_paths()
        self.assertIsInstance(bot, GrokMatrixBot)
        self.assertIsInstance(context.agent_runtime, GrokRuntime)
        self.assertIsNone(context.session_manager)
        self.assertIsNone(context.project_chat)
        self.assertIsNone(context.distill_journal)
        self.assertEqual(sorted(self.root.iterdir()), before)

    def test_telegram_channel_still_selects_telegram_frontend(self):
        bot = create_app(build_context(configuration(self.root)))
        self.assertIsInstance(bot, GrokTelegramBot)

    def test_missing_config_path_or_file_denies_startup(self):
        settings = matrix_configuration(self.root)
        # Settings itself refuses CCC_CHANNEL=matrix without a config path; the
        # frontend still fails closed when handed such an object directly.
        without = settings.model_copy(update={"matrix_config_path": None})
        with self.assertRaisesRegex(ProtocolError, "grok_matrix_config_required"):
            create_app(build_context(without)).validate_runtime_paths()
        (self.root / "matrix.json").unlink()
        with self.assertRaisesRegex(ProtocolError, "grok_matrix_config_missing"):
            create_app(build_context(settings)).validate_runtime_paths()

    def test_foreign_runtime_route_is_rejected(self):
        settings = matrix_configuration(self.root)
        other = configured_route(configuration(self.root, CCC_GROK_JOURNAL_PATH=str(self.root / "other")))
        with self.assertRaisesRegex(ProtocolError, "grok_runtime_route_mismatch"):
            GrokMatrixBot(settings, GrokRuntime(other.journal, FakeGrokHost(other.journal.binding)))


class GrokMatrixLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = matrix_configuration(self.root)
        self.route = configured_route(self.settings)
        self.route.journal.create()
        self.host = FakeGrokHost(self.route.journal.binding)
        self.transport = None

    def state(self):
        return {p.name: p.read_bytes() for p in self.route.journal.root.iterdir()}

    def bot(self, settings=None, script=None):
        settings = settings or self.settings
        runtime = GrokRuntime(configured_route(settings).journal, self.host)
        bot = create_app(build_context(settings, agent_runtime=runtime))
        self.assertIsInstance(bot, GrokMatrixBot)

        def factory(config, runner):
            self.transport = FakeTransport(config, runner, script=script)
            return self.transport

        bot._transport_factory = factory
        return bot

    async def turn(self, bot, body, **kwargs):
        room_kind = self.transport.room_kind(kwargs.get("room", DM_ROOM))
        return await bot.runner.run(job(body, **kwargs), sink=None, session_id=None, room_kind=room_kind)

    async def test_family_rooms_denied_before_transport_opens(self):
        with stub_state(matrix_config(family_rooms=[FAMILY_ROOM], family_users=[KID])):
            bot = self.bot()
            with self.assertRaisesRegex(ProtocolError, "grok_matrix_direct_room_only"):
                await bot.serve()
        self.assertIsNone(self.transport)
        self.assertEqual(self.host.calls, [])

    async def test_family_rooms_admit_allowlisted_senders_when_opted_in(self):
        seen = {}
        settings = matrix_configuration(self.root, CCC_GROK_MATRIX_FAMILY_ROOMS="1")

        async def script(transport):
            bot = transport.runner._bot
            before, calls = self.state(), len(self.host.calls)
            seen["stranger"] = (await self.turn(bot, "hi", sender="@x:example.org", room=FAMILY_ROOM)).text
            seen["kid_dm"] = (await self.turn(bot, "hi", sender=KID)).text
            seen["unlisted"] = (await self.turn(bot, "hi", sender=KID, room="!other:example.org")).text
            seen["denied_untouched"] = (self.state() == before, len(self.host.calls) == calls)
            seen["kid"] = (await self.turn(bot, "kid input", sender=KID, room=FAMILY_ROOM, event_id="$k1")).text
            seen["owner_family"] = (await self.turn(bot, "owner input", room=FAMILY_ROOM, event_id="$o1")).text
            seen["owner_dm"] = (await self.turn(bot, "dm input", event_id="$d1")).text
            with self.route.journal.claim() as claim:
                seen["stage"] = claim.load()[0]["stage"]

        # Production shape: family_rooms must be a subset of rooms (family_config).
        with stub_state(matrix_config(rooms=[DM_ROOM, FAMILY_ROOM], family_rooms=[FAMILY_ROOM], family_users=[KID])):
            bot = self.bot(settings=settings, script=script)
            await bot.serve()
        self.assertEqual(seen["stranger"], NOT_ADMITTED)
        self.assertEqual(seen["kid_dm"], NOT_ADMITTED)
        self.assertEqual(seen["unlisted"], NOT_ADMITTED)
        self.assertEqual(seen["denied_untouched"], (True, True))
        self.assertEqual(seen["kid"], "generated reply kid input")
        self.assertEqual(seen["owner_family"], "generated reply owner input")
        self.assertEqual(seen["owner_dm"], "generated reply dm input")
        self.assertEqual(seen["stage"], "complete")
        # One owner conversation: every admitted prompt is sent to the same Bot.
        self.assertEqual([s["prompt"] for s in self.host.sends], ["kid input", "owner input", "dm input"])
        # The startup banner still goes to the owner's direct rooms only.
        self.assertEqual([n[0] for n in self.transport.notices], [DM_ROOM])

    async def test_family_flag_without_family_config_stays_direct_only(self):
        seen = {}
        settings = matrix_configuration(self.root, CCC_GROK_MATRIX_FAMILY_ROOMS="1")

        async def script(transport):
            bot = transport.runner._bot
            seen["family"] = (await self.turn(bot, "hi", room=FAMILY_ROOM)).text
            seen["owner_dm"] = (await self.turn(bot, "dm input")).text

        with stub_state(matrix_config()):
            await self.bot(settings=settings, script=script).serve()
        self.assertEqual(seen["family"], NOT_ADMITTED)
        self.assertEqual(seen["owner_dm"], "generated reply dm input")
        self.assertEqual(len(self.host.sends), 1)

    async def test_attach_after_open_then_owner_turn_commits_before_reply(self):
        seen = {}

        async def script(transport):
            bot = transport.runner._bot
            seen["ready"] = bot.ready
            seen["gates"] = list(self.host.calls)
            result = await self.turn(bot, "generated input")
            with self.route.journal.claim() as claim:
                seen["stage"] = claim.load()[0]["stage"]
            seen["reply"] = result.text
            seen["status"] = result.status

        with stub_state(matrix_config()):
            bot = self.bot(script=script)
            await bot.serve()
        self.assertTrue(seen["ready"])
        self.assertEqual(seen["gates"], ["status", "health"])
        self.assertEqual(seen["reply"], "generated reply generated input")
        self.assertEqual(seen["status"], "complete")
        self.assertEqual(seen["stage"], "complete")
        self.assertEqual(len(self.host.sends), 1)
        self.assertEqual([e[0] for e in self.transport.events], ["open", "run", "close"])
        self.assertEqual(self.transport.notices[0][0], DM_ROOM)
        self.assertEqual(self.transport.notices[0][2], BANNER_KEY)
        self.assertFalse(bot.ready)
        self.assertIsNone(bot._poller)

    async def test_other_senders_rooms_commands_and_limits_never_reach_journal_or_host(self):
        seen = {}

        async def script(transport):
            bot = transport.runner._bot
            before, calls = self.state(), len(self.host.calls)
            seen["kid"] = (await self.turn(bot, "hi", sender=KID)).text
            seen["family"] = (await self.turn(bot, "hi", room=FAMILY_ROOM)).text
            seen["foreign_room"] = (await self.turn(bot, "hi", room="!other:example.org")).text
            seen["self_job"] = (await self.turn(bot, "hi", event_id="$self-1")).text
            seen["status"] = (await self.turn(bot, "/status")).text
            seen["commands"] = {await self.turn(bot, c) for c in ("/new", "/model other", "/resume x", "/command rm", "")}
            seen["large"] = (await self.turn(bot, "x" * (MAX_PROMPT + 1))).text
            # #1795: a Matrix photo/file job (caption or placeholder body) stays text-only for Grok.
            photo = {**job("이 사진 설명해줘"), "attachment": '{"kind":"image"}'}
            seen["attachment"] = (await bot.runner.run(photo, sink=None, session_id=None, room_kind="direct")).text
            seen["untouched"] = (self.state() == before, len(self.host.calls) == calls)

        with stub_state(matrix_config()):
            await self.bot(script=script).serve()
        self.assertEqual(seen["kid"], NOT_ADMITTED)
        self.assertEqual(seen["family"], NOT_ADMITTED)
        self.assertEqual(seen["foreign_room"], NOT_ADMITTED)
        self.assertEqual(seen["self_job"], NOT_ADMITTED)
        self.assertEqual(seen["status"], STATUS_TEXT)
        self.assertEqual({r.text for r in seen["commands"]}, {TEXT_ONLY})
        self.assertEqual(seen["large"], TOO_LARGE)
        self.assertEqual(seen["attachment"], TEXT_ONLY)
        self.assertEqual(seen["untouched"], (True, True))
        self.assertEqual(self.host.sends, [])

    async def test_initialize_flag_provisions_device_without_journal_or_poller(self):
        settings = matrix_configuration(self.root, CCC_MATRIX_INITIALIZE="1")
        before = self.state()
        with stub_state(matrix_config()):
            bot = self.bot(settings=settings)
            await bot.serve()
        self.assertEqual([e for e in self.transport.events], [("open", True), ("close", None)])
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.state(), before)
        self.assertFalse(bot.ready)
        self.assertEqual(self.transport.notices, [])

    async def test_second_local_frontend_denied_even_with_separate_journal(self):
        seen = {}

        async def script(transport):
            other = configuration(self.root, CCC_CHANNEL="matrix",
                                  CCC_MATRIX_CONFIG_PATH=str(self.root / "matrix.json"),
                                  CCC_GROK_JOURNAL_PATH=str(self.root / "other"))
            second = create_app(build_context(other))
            second._transport_factory = lambda config, runner: FakeTransport(config, runner)
            with self.assertRaisesRegex(ProtocolError, "grok_local_poller_busy"):
                await second.serve()
            seen["other_journal"] = (self.root / "other").exists()

        with stub_state(matrix_config()):
            await self.bot(script=script).serve()
        self.assertFalse(seen["other_journal"])

    async def test_host_failure_after_send_reports_uncertain_and_retains_state(self):
        seen = {}
        self.host.fail_after_send = True

        async def script(transport):
            bot = transport.runner._bot
            seen["reply"] = (await self.turn(bot, "generated input")).text
            with self.route.journal.claim() as claim:
                seen["stage"] = claim.load()[0]["stage"]
            seen["active"] = bot.active

        with stub_state(matrix_config()):
            await self.bot(script=script).serve()
        self.assertEqual(seen["reply"], UNCERTAIN)
        self.assertIn(seen["stage"], {"attempted", "accepted"})
        self.assertIsNone(seen["active"])

    async def test_concurrent_input_is_rejected_not_queued(self):
        seen = {}
        release = asyncio.Event()

        async def script(transport):
            bot = transport.runner._bot
            self.host.before_send = lambda: None
            original = self.host.call

            async def slow(operation, arguments=None):
                if operation == "send":
                    await release.wait()
                return await original(operation, arguments)

            self.host.call = slow
            first = asyncio.create_task(self.turn(bot, "generated input"))
            await asyncio.sleep(0)
            while bot.active is None:
                await asyncio.sleep(0)
            seen["second"] = (await self.turn(bot, "another", event_id="$e2")).text
            release.set()
            seen["first"] = (await first).text

        with stub_state(matrix_config()):
            await self.bot(script=script).serve()
        self.assertEqual(seen["second"], BUSY)
        self.assertEqual(seen["first"], "generated reply generated input")
        self.assertEqual(len(self.host.sends), 1)

    async def test_gate_failure_releases_poller_and_closes_transport(self):
        self.host.busy = True
        with stub_state(matrix_config()):
            bot = self.bot()
            with self.assertRaisesRegex(ProtocolError, "host_not_idle_for_target"):
                await bot.serve()
        self.assertEqual([e[0] for e in self.transport.events], ["open", "close"])
        self.assertIsNone(bot._poller)
        self.assertFalse(bot.ready)
        self.assertEqual(self.transport.notices, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
