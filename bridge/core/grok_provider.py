"""Explicit existing-Bot attachment; no filesystem or network work at composition."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .grok_journal import GrokBinding, GrokJournal
from .grok_protocol import ProtocolError
from .grok_runtime import GrokRuntime
from .grok_ssh import GrokLocalTransport, GrokSshTransport


@dataclass(frozen=True)
class GrokRoute:
    owner_id: int
    telegram_bot_id: int
    journal: GrokJournal


def configured_route(settings: Any) -> GrokRoute:
    """Bind local owner/Telegram Bot identity into the immutable journal label.

    Token identity is checked without returning its secret. getMe is still
    required before opening the journal or accepting any Telegram update.
    """
    owner = getattr(settings, "grok_owner_id", None)
    bot = getattr(settings, "grok_telegram_bot_id", None)
    destination = getattr(settings, "grok_ssh_destination", None)
    agent = getattr(settings, "grok_bot_id", None)
    state = getattr(settings, "grok_journal_path", None)
    if (settings.agent_provider != "grok" or type(owner) is not int or owner <= 0
            or type(bot) is not int or bot <= 0
            or settings.require_allowlist is not True
            or list(settings.allowed_user_ids) != [owner]
            or not isinstance(destination, str) or not isinstance(agent, str)
            or not isinstance(state, (str, Path)) or not Path(state).is_absolute()):
        raise ProtocolError("grok_explicit_owner_route_required")
    token = settings.telegram_bot_token
    if not isinstance(token, str) or token.partition(":")[0] != str(bot):
        raise ProtocolError("grok_telegram_identity_mismatch")
    binding = GrokBinding(destination, agent, f"telegram-{bot}-owner-{owner}", str(settings.project_root))
    return GrokRoute(owner, bot, GrokJournal(Path(state), binding))


def build_grok_runtime(settings: Any) -> GrokRuntime:
    route = configured_route(settings)
    binding = route.journal.binding
    mode = str(os.environ.get("CCC_GROK_TRANSPORT") or "").strip().lower()
    transport = GrokLocalTransport if mode == "local" else GrokSshTransport
    return GrokRuntime(route.journal, transport(binding.destination, binding.agent_id))
