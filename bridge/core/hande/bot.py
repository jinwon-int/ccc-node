"""「한데」 frontend: ``MatrixBot`` behind the native relay bot (#2380 PR-2).

Everything a turn gets on Matrix — commands, the redacted approval snapshot
and audit ledger, continuation, waits, Danso recovery, session continuity,
health — comes from ``MatrixBot`` unchanged. This subclass swaps the two
Matrix-specific pieces: the private config (``CCC_HANDE_CONFIG_PATH``,
:mod:`.state`) and the transport (:class:`.transport.HandeTransport`, the
FLEET-WORKER port server), and words the transport health verdict for a
socket instead of a sync loop.
"""

from __future__ import annotations

import logging
from pathlib import Path
import time
from typing import Any, Mapping

from telegram_bot.core.matrix.bot import MatrixBot

from .state import HandeConfigError, load_config

logger = logging.getLogger(__name__)

_CLIENT_STARTUP_GRACE_S = 120.0


class HandeBot(MatrixBot):
    """``TurnRunner`` over ``ProjectChatHandler`` for the 「한데」 relay bot."""

    channel_name = "hande"

    # -- configuration -------------------------------------------------------

    def config_path(self) -> Path:
        raw = getattr(self._settings, "hande_config_path", None)
        if not raw:
            raise HandeConfigError(
                "Settings.hande_config_path is not set: the hande frontend needs the "
                "private (0600) config file (CCC_HANDE_CONFIG_PATH)"
            )
        return Path(str(raw)).expanduser()

    def load_config(self) -> Mapping[str, Any]:
        if self._config is None:
            self._config = load_config(self.config_path())
        return self._config

    def validate_runtime_paths(self) -> None:
        """Fail fast on a missing/invalid config or corrupt persisted maps (no writes)."""

        self.load_config()
        self._data_dir()
        self.ids
        self._direct_room_map()

    # -- transport -----------------------------------------------------------

    def _build_transport(self, config: Mapping[str, Any]) -> Any:
        runner = self.runner
        if self._transport_factory is not None:
            return self._transport_factory(config, runner)
        from .transport import HandeTransport

        socket_path = getattr(self._settings, "hande_socket_path", None) or config.get(
            "socket_path"
        )
        return HandeTransport(config, runner, socket_path=socket_path)

    def _transport_verdict(self) -> tuple[str, str, int]:
        """``ok`` while the bot is connected; ``error`` once it has been gone past the grace."""

        signals_fn = getattr(self._transport, "health_signals", None)
        if not callable(signals_fn):
            return "wait", "", 0
        signals = signals_fn(time.time())
        if signals.get("connected"):
            return "ok", "", 0
        if time.monotonic() - self._health_started <= _CLIENT_STARTUP_GRACE_S:
            return "wait", "", 0
        seen = int(signals.get("clients_seen") or 0)
        return "error", "hande responder socket: no bot connected", max(1, 1 if seen else 2)

    def startup_banner(self) -> str:
        # The native bot does not speak the ``notice`` frame yet, so a banner
        # would only be counted as dropped; keep the text for when it does.
        return "hande frontend started: " + super().startup_banner()
