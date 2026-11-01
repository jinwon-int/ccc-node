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

import asyncio
import logging
from pathlib import Path
import time
from typing import Any, Mapping

import secrets

from telegram_bot.contracts.agent_runtime import ApprovalRequestEvent, freeze_json
from telegram_bot.contracts.transport import TurnSink
from telegram_bot.core.approval_contract import build_approval_snapshot
from telegram_bot.core.matrix.bot import MatrixBot

from .state import HandeConfigError, load_config
from .steward import (
    STEWARD_SERVER,
    all_tool_names,
    build_steward_server,
    needs_approval,
    qualified,
)
from .wait_status import HandeWaitStatusMixin

logger = logging.getLogger(__name__)

_CLIENT_STARTUP_GRACE_S = 120.0


class HandeBot(HandeWaitStatusMixin, MatrixBot):
    """``TurnRunner`` over ``ProjectChatHandler`` for the 「한데」 relay bot.

    The external-wait status projection is the hande one (:mod:`.wait_status`,
    #2590): ``kind:"status"`` notices re-sent while a wait is watched, instead
    of the Matrix message that is edited, moved and redacted.
    """

    channel_name = "hande"
    # Re-declared for mypy: MatrixBot sets it in __init__, which mypy cannot
    # see through from a subclass method (has-type).
    _config: Mapping[str, Any] | None

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
        config = self._config
        if config is None:
            config = load_config(self.config_path())
            self._config = config
        return config

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
        # Self-jobs not started yet survive a restart here (#2561), like the
        # Matrix store's ``$self-`` rows.
        return HandeTransport(
            config,
            runner,
            socket_path=socket_path,
            self_jobs_path=self._data_dir() / "hande-self-jobs.json",
        )

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

    # -- steward room tools (#2380 PR-4) ---------------------------------------

    _steward_server: Any = None
    _steward_checked = False

    def _steward_enabled(self) -> bool:
        config = self.load_config()
        if not config.get("steward_tools", True):
            return False
        if self._active_provider() != "claude":
            if not self._steward_checked:
                logger.warning("hande steward tools need the claude provider; disabled")
            return False
        return True

    # The identity of the turn being served, for the steward approval audit
    # (MatrixBot keeps only the sink and the turn id).
    _steward_identity: tuple[int, int] | None = None

    async def run_turn(
        self, job: Mapping[str, Any], *, sink: TurnSink, session_id: str | None, room_kind: str
    ) -> Any:
        try:
            user_id, chat_id, _room_id = self._job_identity(job, room_kind)
            self._steward_identity = (user_id, chat_id)
        except Exception:
            self._steward_identity = None
        try:
            return await super().run_turn(
                job, sink=sink, session_id=session_id, room_kind=room_kind
            )
        finally:
            self._steward_identity = None

    def _steward_approve_all(self) -> bool:
        return bool(self.load_config().get("steward_tools_approve_all"))

    async def _steward_approval(
        self, sink: Any, name: str, arguments: Mapping[str, str]
    ) -> tuple[str, dict[str, Any] | None]:
        """Owner approval for one steward call through the hande ``approval`` frame.

        Same redacted snapshot + audit ledger as a provider approval (#1959);
        the model never sees the owner's reply, only the outcome.
        """

        event = ApprovalRequestEvent(
            request_id=f"steward-{secrets.token_urlsafe(8)}",
            action=qualified(name),
            arguments=freeze_json(dict(arguments)),
            description=f"steward {name}",
        )
        snapshot = build_approval_snapshot(event, reply_hint=None)
        identity = self._steward_identity or (0, 0)
        asked = self._record_approval_asked(snapshot, event, identity[0], identity[1], 0)
        # The generic tool_use snapshot says only "tool use"; the owner must see
        # which room operation and on what. Arguments are identifier-shaped
        # ([A-Za-z0-9_-]{1,64}, checked before this point), so they are safe
        # to show verbatim. The bot appends its own "승인 n / 거절 n" controls.
        shown = " ".join(f"{key}={value}" for key, value in arguments.items()) or "(없음)"
        prompt = f"⚠️ 한데 방 운영 도구 승인 요청\n도구: {name}\n인수: {shown}"
        try:
            outcome = await self._ask_approval(sink, prompt)
        except asyncio.CancelledError:
            self._record_approval_answered(asked, "cancelled", actor_user_id=None)
            raise
        except Exception:
            self._record_approval_answered(asked, "send_failure", actor_user_id=None)
            return "unavailable", None
        reason = {"allow": "owner_allow", "deny": "owner_deny", "timeout": "timeout"}.get(
            outcome, "send_failure"
        )
        self._record_approval_answered(
            asked, reason, actor_user_id=identity[0] if outcome in ("allow", "deny") else None
        )
        return outcome, None

    async def _steward_call(self, name: str, arguments: Mapping[str, str]) -> Mapping[str, Any]:
        """Forward one tool call to the turn being served, after the owner's approval (PR-5).

        Read-only tools go straight through (unless ``steward_tools_approve_all``);
        anything with a side effect asks the owner first through the same
        ``approval`` frame a provider approval uses, so the step does not
        depend on the node's SDK permission mode. The bot still enforces
        owner-only on every call.
        """

        sink = self._active_sink
        call = getattr(sink, "tool", None)
        if not callable(call):
            return {"ok": False, "error": "no_active_turn"}
        if needs_approval(name, self._steward_approve_all()):
            outcome, _ = await self._steward_approval(sink, name, arguments)
            if outcome != "allow":
                logger.info("hande steward %s not approved: %s", name, outcome)
                return {"ok": False, "error": f"approval_{outcome}"}
        return await call(name, arguments)

    # -- inbound attachments (#2606) -------------------------------------------

    async def _run_attachment(
        self, job: Mapping[str, Any], *, user_id: int, chat_id: int, room_id: str, sink: TurnSink
    ) -> Any:
        """Run a bot-decrypted 「한데」 attachment with the Telegram/Matrix prompt contract.

        ``job["attachment"]`` is the mapping :func:`.attachments.parse_frame_attachment`
        returned (the transport sets it only with ``attachment_frames`` on), never
        a Matrix ``file``. The spool file is taken over into the private
        ``matrix-media`` directory, verified there and deleted when the turn
        ends. A failure answers once with the Matrix wording and runs no model.
        The non-owner refusal already ran in ``MatrixBot.run_turn``.
        """

        from telegram_bot.core import media as core_media
        from telegram_bot.core.matrix import media as matrix_media
        from telegram_bot.core.matrix.bot import (
            ATTACHMENT_FAILED,
            ATTACHMENT_FAILED_DEFAULT,
            _turn_result,
        )

        from .attachments import adopt, matrix_shape

        attachment = job.get("attachment")
        spool_dir = self.load_config().get("attachment_spool_dir")
        if not isinstance(attachment, Mapping) or not spool_dir:
            return _turn_result(ATTACHMENT_FAILED["invalid"], None)
        directory = self._data_dir() / matrix_media.MEDIA_DIRNAME
        shape = matrix_shape(attachment)
        limit = matrix_media.size_limit(shape, self._settings)
        path: Path | None = None
        try:
            try:
                core_media.cleanup_stale_document_files(
                    directory, max_age_seconds=matrix_media.STALE_MEDIA_SECONDS
                )
                path = await asyncio.to_thread(
                    adopt, attachment, spool_dir=str(spool_dir), staging_dir=directory, limit=limit
                )
            except matrix_media.AttachmentError as exc:
                logger.warning(
                    "hande attachment unavailable reason=%s kind=%s", exc.reason, attachment.get("kind")
                )
                return _turn_result(ATTACHMENT_FAILED.get(exc.reason, ATTACHMENT_FAILED_DEFAULT), None)
            except OSError:
                logger.warning("hande attachment unavailable reason=storage")
                return _turn_result(ATTACHMENT_FAILED_DEFAULT, None)
            prompt = self._attachment_prompt(
                path, shape, str(attachment.get("caption") or ""), channel="Hande"
            )
            return await self._run_message(
                prompt, user_id=user_id, chat_id=chat_id, room_id=room_id, sink=sink,
                turn_marker=str(job.get("event_id") or ""),
            )
        finally:
            matrix_media.remove(path)

    def _turn_request_extras(self) -> dict[str, Any]:
        if not self._steward_enabled():
            return {}
        if not self._steward_checked:
            self._steward_checked = True
            self._steward_server = build_steward_server(self._steward_call)
        if self._steward_server is None:
            return {}
        # Every steward tool is pre-allowed at the SDK level: the bridge asks
        # the owner itself (``_steward_call``), so a node on ``approve-each``
        # never sees a second prompt and a node on ``auto-approve`` never
        # skips the first one.
        return {
            "mcp_servers": {STEWARD_SERVER: self._steward_server},
            "mcp_allowed_tools": all_tool_names(),
        }

    def startup_banner(self) -> str:
        # Posted as a ``notice`` frame when ``notice_frames`` is on; counted
        # as dropped otherwise.
        return "hande frontend started: " + super().startup_banner()
