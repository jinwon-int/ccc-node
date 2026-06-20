import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from telegram.error import RetryAfter, TimedOut, NetworkError, BadRequest

from telegram_bot.utils.tg_robust import (
    send_with_retry,
    looks_like_connect_timeout,
    looks_like_pool_timeout,
)


def _run(coro):
    return asyncio.run(coro)


class TimeoutDiscriminationTest(unittest.TestCase):
    def test_connect_timeout_via_cause(self):
        class ConnectTimeout(Exception):
            pass

        err = TimedOut()
        err.__cause__ = ConnectTimeout("connection timed out")
        self.assertTrue(looks_like_connect_timeout(err))
        self.assertFalse(looks_like_pool_timeout(err))

    def test_pool_timeout_via_message(self):
        err = TimedOut(
            "Pool timeout: All connections in the connection pool are "
            "occupied. Request was not sent to Telegram."
        )
        self.assertTrue(looks_like_pool_timeout(err))

    def test_generic_timeout_is_neither(self):
        err = TimedOut()
        self.assertFalse(looks_like_connect_timeout(err))
        self.assertFalse(looks_like_pool_timeout(err))


class SendWithRetryTest(unittest.TestCase):
    def setUp(self):
        # Patch sleep so retries don't actually wait.
        self._sleep = patch("telegram_bot.utils.tg_robust.asyncio.sleep", AsyncMock())
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()

    def test_success_first_try(self):
        calls = {"n": 0}

        async def op():
            calls["n"] += 1
            return "ok"

        self.assertEqual(_run(send_with_retry(op)), "ok")
        self.assertEqual(calls["n"], 1)

    def test_retry_after_then_success(self):
        calls = {"n": 0}

        async def op():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RetryAfter(2)
            return "ok"

        self.assertEqual(_run(send_with_retry(op)), "ok")
        self.assertEqual(calls["n"], 2)

    def test_generic_timeout_not_retried(self):
        calls = {"n": 0}

        async def op():
            calls["n"] += 1
            raise TimedOut()

        with self.assertRaises(TimedOut):
            _run(send_with_retry(op))
        # Must NOT re-send — a generic timeout may have reached Telegram.
        self.assertEqual(calls["n"], 1)

    def test_pool_timeout_retried(self):
        calls = {"n": 0}

        async def op():
            calls["n"] += 1
            if calls["n"] < 2:
                raise TimedOut(
                    "Pool timeout: connection pool occupied. Not sent to Telegram."
                )
            return "ok"

        self.assertEqual(_run(send_with_retry(op)), "ok")
        self.assertEqual(calls["n"], 2)

    def test_bad_request_raises_immediately(self):
        calls = {"n": 0}

        async def op():
            calls["n"] += 1
            raise BadRequest("can't parse entities")

        with self.assertRaises(BadRequest):
            _run(send_with_retry(op))
        self.assertEqual(calls["n"], 1)

    def test_network_error_exhausts_attempts(self):
        calls = {"n": 0}

        async def op():
            calls["n"] += 1
            raise NetworkError("boom")

        with self.assertRaises(NetworkError):
            _run(send_with_retry(op, max_attempts=3))
        self.assertEqual(calls["n"], 3)


if __name__ == "__main__":
    unittest.main()
