"""Stable import path for the Codex provider adapter (#1756).

``CodexRuntime`` is the reference implementation of the
:class:`contracts.agent_runtime.AgentRuntime` seam, so callers that want *an
adapter that satisfies the contract* should be able to say so without reaching
into ``core``, where the adapter sits next to Telegram-facing orchestration.

This module is a pure re-export: the adapter's implementation and its module
home are unchanged, and ``contracts.codex_runtime.CodexRuntime`` is the same
class object as ``core.codex_runtime.CodexRuntime``. It is deliberately left
out of the ``contracts`` package facade — importing it pulls in the Codex
app-server stack, which contract-only consumers should not have to pay for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Same dual-name resolution as ``core/agent_runtime.py``: type checkers see the
# ``bridge/``-relative source root, the installed distribution sees the
# ``telegram_bot`` package. Both branches bind the identical class object.
if TYPE_CHECKING:
    from core.codex_runtime import CodexRuntime
else:
    from telegram_bot.core.codex_runtime import CodexRuntime

__all__ = ["CodexRuntime"]
