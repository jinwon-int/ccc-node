"""Per-user bounded task queue for the Telegram bridge.

Extracted from the ``TelegramBot`` god object. Owns the three pieces of
per-user concurrency state that used to live as bare instance dicts on the bot:
a serializing lock, the set of in-flight run tasks, and the single currently
executing ("active") task used by the priority ``/stop`` and ``/revert`` paths.

Behavior is identical to the original inline methods:
- at most ``max_inflight`` concurrent run tasks per key (overflow is rejected),
- the active task is recorded for the duration of execution so it can be
  cancelled out of band,
- finished tasks are pruned lazily and on done-callback, and task exceptions are
  logged (CancelledError is swallowed).

Keys are opaque (a user id or a conversation key); the queue only uses them for
dict lookup, exactly as the bot did.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)


class UserTaskQueue:
    def __init__(self, max_inflight: int = 3) -> None:
        self._max_inflight = max_inflight
        self._locks: Dict[Any, asyncio.Lock] = {}
        self._run_tasks: Dict[Any, Set[asyncio.Task]] = {}
        # Currently executing task per key, for the priority stop/revert paths.
        self._active: Dict[Any, asyncio.Task] = {}

    def _lock(self, key: Any) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _prune(self, key: Any) -> Set[asyncio.Task]:
        tasks = self._run_tasks.get(key)
        if not tasks:
            tasks = set()
            self._run_tasks[key] = tasks
            return tasks
        done = {t for t in tasks if t.done()}
        tasks.difference_update(done)
        return tasks

    def _track(self, key: Any, task: asyncio.Task) -> None:
        tasks = self._prune(key)
        tasks.add(task)

        def _on_done(t: asyncio.Task):
            current = self._run_tasks.get(key)
            if current is not None:
                current.discard(t)
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Background task failed for user {key}: {e}", exc_info=True)

        task.add_done_callback(_on_done)

    def active(self, key: Any) -> Optional[asyncio.Task]:
        """The currently executing task for *key*, if any."""
        return self._active.get(key)

    def clear(self, key: Any) -> int:
        """Cancel and drop all in-flight tasks for *key*; return how many."""
        tasks = self._prune(key)
        cleared = len(tasks)
        for t in list(tasks):
            t.cancel()
        tasks.clear()
        return cleared

    async def enqueue(
        self,
        key: Any,
        run_task: Callable[[], Awaitable[None]],
        on_overflow: Callable[[], Awaitable[None]],
    ) -> bool:
        """Schedule *run_task* for *key* unless the per-key queue is full.

        Returns True if accepted (a task was created), False on overflow (in
        which case *on_overflow* is awaited). The active-task slot is set for
        the duration of execution and cleared in a finally block.
        """
        lock = self._lock(key)
        accepted_task: Optional[asyncio.Task] = None

        async with lock:
            tasks = self._prune(key)
            if len(tasks) >= self._max_inflight:
                accepted_task = None
            else:
                async def wrapped_task():
                    current_task = asyncio.current_task()
                    self._active[key] = current_task
                    try:
                        await run_task()
                    except asyncio.CancelledError:
                        # Re-raise to ensure cancellation propagates.
                        raise
                    finally:
                        # Identity-guarded clear: with up to max_inflight tasks
                        # per key running concurrently (the lock is released
                        # before the task body runs), a bare pop would let an
                        # earlier task delete a *later* task's active slot —
                        # leaving a still-running task invisible to the priority
                        # /stop and /revert paths. Only clear the slot when it
                        # still points at THIS task.
                        if self._active.get(key) is current_task:
                            self._active.pop(key, None)

                accepted_task = asyncio.create_task(wrapped_task())
                self._track(key, accepted_task)

        if not accepted_task:
            await on_overflow()
            return False
        return True
