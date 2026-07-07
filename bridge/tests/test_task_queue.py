"""Direct unit tests for the extracted per-user task queue (core/task_queue.py).

This concurrency-sensitive logic (bounded in-flight tasks, active-task tracking
for priority /stop, overflow rejection, lazy pruning) previously lived inline on
the TelegramBot god object and was only exercised indirectly. Testing the
UserTaskQueue directly pins the semantics under asyncio.
"""

import asyncio
import unittest

from telegram_bot.core.task_queue import UserTaskQueue


class UserTaskQueueTest(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_within_limit(self):
        q = UserTaskQueue(max_inflight=3)
        started = asyncio.Event()
        release = asyncio.Event()

        async def run():
            started.set()
            await release.wait()

        async def overflow():
            raise AssertionError("should not overflow")

        accepted = await q.enqueue("u", run, overflow)
        self.assertTrue(accepted)
        await asyncio.wait_for(started.wait(), 1)
        release.set()

    async def test_overflow_rejected_and_callback_runs(self):
        q = UserTaskQueue(max_inflight=2)
        release = asyncio.Event()
        overflowed = asyncio.Event()

        async def run():
            await release.wait()

        async def overflow():
            overflowed.set()

        self.assertTrue(await q.enqueue("u", run, overflow))
        self.assertTrue(await q.enqueue("u", run, overflow))
        # Third exceeds the limit of 2 -> rejected.
        accepted = await q.enqueue("u", run, overflow)
        self.assertFalse(accepted)
        self.assertTrue(overflowed.is_set())
        release.set()

    async def test_separate_keys_independent(self):
        q = UserTaskQueue(max_inflight=1)
        release = asyncio.Event()

        async def run():
            await release.wait()

        async def overflow():
            raise AssertionError("unexpected overflow")

        self.assertTrue(await q.enqueue("a", run, overflow))
        # Different key has its own budget.
        self.assertTrue(await q.enqueue("b", run, overflow))
        release.set()

    async def test_active_tracks_running_task(self):
        q = UserTaskQueue(max_inflight=1)
        started = asyncio.Event()
        release = asyncio.Event()

        async def run():
            started.set()
            await release.wait()

        async def overflow():
            raise AssertionError

        self.assertIsNone(q.active("u"))
        await q.enqueue("u", run, overflow)
        await asyncio.wait_for(started.wait(), 1)
        self.assertIsNotNone(q.active("u"))
        release.set()
        # Let the wrapped task finish and clear the active slot.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertIsNone(q.active("u"))

    async def test_slot_frees_after_completion(self):
        q = UserTaskQueue(max_inflight=1)

        async def quick():
            return None

        async def overflow():
            raise AssertionError("should not overflow after slot frees")

        self.assertTrue(await q.enqueue("u", quick, overflow))
        # Allow the first task to finish so the single slot is freed.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertTrue(await q.enqueue("u", quick, overflow))

    async def test_clear_cancels_inflight(self):
        q = UserTaskQueue(max_inflight=3)
        release = asyncio.Event()
        cancelled = asyncio.Event()

        async def run():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def overflow():
            raise AssertionError

        await q.enqueue("u", run, overflow)
        await asyncio.sleep(0)  # let it start
        cleared = q.clear("u")
        self.assertEqual(cleared, 1)
        await asyncio.sleep(0)
        self.assertTrue(cancelled.is_set())

    async def test_clear_unknown_key_is_zero(self):
        q = UserTaskQueue()
        self.assertEqual(q.clear("nobody"), 0)

    async def test_earlier_task_finishing_keeps_later_active_slot(self):
        # Regression: with two concurrent tasks under the same key, the first to
        # finish used to pop the shared _active slot unconditionally, erasing the
        # still-running later task so /stop could no longer cancel it.
        q = UserTaskQueue(max_inflight=3)
        a_started = asyncio.Event()
        a_release = asyncio.Event()
        b_started = asyncio.Event()
        b_release = asyncio.Event()

        async def run_a():
            a_started.set()
            await a_release.wait()

        async def run_b():
            b_started.set()
            await b_release.wait()

        async def overflow():
            raise AssertionError("unexpected overflow")

        await q.enqueue("u", run_a, overflow)
        await asyncio.wait_for(a_started.wait(), 1)
        await q.enqueue("u", run_b, overflow)
        await asyncio.wait_for(b_started.wait(), 1)

        # B started last, so it owns the active slot.
        b_task = q.active("u")
        self.assertIsNotNone(b_task)

        # A finishes first. Its finally must NOT clear B's slot.
        a_release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertIs(q.active("u"), b_task)

        # B is still cancellable via the active slot.
        q.active("u").cancel()
        b_release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertIsNone(q.active("u"))


if __name__ == "__main__":
    unittest.main()
