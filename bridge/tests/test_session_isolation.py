import asyncio
import unittest

import anyio

from telegram_bot.core.session_isolation import apply_subprocess_session_isolation


class SubprocessSessionIsolationTests(unittest.TestCase):
    def setUp(self):
        self._orig = anyio.open_process

    def tearDown(self):
        anyio.open_process = self._orig

    def test_patch_injects_start_new_session_by_default(self):
        captured = {}

        async def fake_open_process(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return "proc"

        anyio.open_process = fake_open_process
        applied = apply_subprocess_session_isolation()
        self.assertTrue(applied)

        result = asyncio.run(anyio.open_process("cmd"))
        self.assertEqual(result, "proc")
        # The child gets its own session -> child signals cannot reach the bot.
        self.assertIs(captured["kwargs"].get("start_new_session"), True)

    def test_explicit_caller_choice_is_respected(self):
        captured = {}

        async def fake_open_process(*args, **kwargs):
            captured["kwargs"] = kwargs
            return "proc"

        anyio.open_process = fake_open_process
        apply_subprocess_session_isolation()

        asyncio.run(anyio.open_process("cmd", start_new_session=False))
        self.assertIs(captured["kwargs"].get("start_new_session"), False)

    def test_idempotent(self):
        async def fake_open_process(*args, **kwargs):
            return "proc"

        anyio.open_process = fake_open_process
        first = apply_subprocess_session_isolation()
        second = apply_subprocess_session_isolation()
        self.assertTrue(first)
        self.assertFalse(second)


if __name__ == "__main__":
    unittest.main()
