"""Isolate Claude CLI subprocesses (and their descendants) into a new session.

Why this exists
---------------
The bridge can be asked — via a Telegram message — to run heavy work inside its OWN
repository (for example ``python3 -m pytest -q`` under ``/opt/ccc-node/bridge``). That
work runs as a process chain::

    bot (telegram_bot)  ->  claude_agent_sdk  ->  claude CLI  ->  bash  ->  pytest

``claude_agent_sdk`` spawns the CLI via ``anyio.open_process(...)`` *without* passing
``start_new_session`` (see ``claude_agent_sdk/_internal/transport/subprocess_cli.py``),
so the CLI and every descendant inherit the bot's session and process group. When the
child tree is signalled — e.g. a Bash tool kills a slow ``pytest`` with ``killpg`` — the
SIGTERM/SIGINT reaches the bot's own ``signal`` handler (``core/bot.py`` registers
``SIGINT``/``SIGTERM`` -> ``stop_event.set``) and the bot shuts down with "Bot stopped".
Observed on soonwook/vps6: asking the bot to test its own bridge repeatedly killed it,
even under setsid/systemd, because the children are created *inside* the bot's session.

The fix
-------
Wrap ``anyio.open_process`` so child processes default to ``start_new_session=True``.
This makes the spawned ``claude`` CLI a new session leader (its own process group), so
signals delivered to the child tree can never propagate back to the bot. Idempotent and
fail-open: any error leaves the original behaviour untouched.
"""

from __future__ import annotations

import functools
import logging

logger = logging.getLogger(__name__)


def apply_subprocess_session_isolation() -> bool:
    """Patch ``anyio.open_process`` to default ``start_new_session=True``.

    Returns ``True`` if the patch was applied, ``False`` if it was already applied or
    could not be applied (fail-open — never raises).
    """
    try:
        import anyio
    except Exception:  # pragma: no cover - anyio is a hard dependency of the SDK
        logger.warning("session isolation skipped: anyio unavailable")
        return False

    original = getattr(anyio, "open_process", None)
    if original is None:
        logger.warning("session isolation skipped: anyio.open_process not found")
        return False
    if getattr(original, "_ccc_session_isolated", False):
        return False  # already patched

    @functools.wraps(original)
    async def open_process(*args, **kwargs):
        # Only set a default; never override an explicit caller choice.
        kwargs.setdefault("start_new_session", True)
        return await original(*args, **kwargs)

    open_process._ccc_session_isolated = True  # type: ignore[attr-defined]
    open_process._ccc_original = original  # type: ignore[attr-defined]
    anyio.open_process = open_process
    logger.info(
        "Applied subprocess session isolation "
        "(child processes spawned with start_new_session=True)"
    )
    return True
