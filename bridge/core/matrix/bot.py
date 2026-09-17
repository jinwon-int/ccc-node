"""MatrixBot: the Matrix frontend for ``ProjectChatHandler`` (#1780 PR-2b).

The Telegram bot is a stack of mixins over python-telegram-bot ``Update``
objects. This module is the equivalent for Matrix but deliberately thin: it
implements the transport's ``TurnRunner`` contract (``run``/``cancel``) and
talks only to handler-facing and session-manager-facing APIs the Telegram
commands already use (``process_message``, ``get_usage``,
``list_runtime_models``, ``stop``, ``cancel_user_streaming``,
``invalidate_agent_approvals``, ``get_session``/``patch_session``…). No
Telegram type is imported here.

Identity: ``MatrixIdMap`` turns ``@user:server`` / ``!room:server`` into
stable positive ints; a direct room reports the sender's int as ``chat_id``
so ``session_scope.is_group_conversation`` stays false for DMs. The reverse
map is what unsolicited/async deliveries use to find the room again; the DM
room for a user is remembered separately (persisted, private file) because
the id map only knows the *user* for a direct conversation.

Transport contract (``telegram_bot.core.matrix.transport``, written in
parallel) is consumed through lazy imports and local ``Protocol`` mirrors so
this module and its tests do not depend on that file being present.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import time
import tomllib
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol

from telegram_bot.core import session_resume, tool_policy
from telegram_bot.core.agent_runtime import ApprovalDecision, ApprovalRequestEvent
from telegram_bot.core.matrix.render import chunk_text, render_matrix_message
from telegram_bot.core.matrix_ids import MatrixIdMap
from telegram_bot.core.memory_audience import resolve_memory_audience
from telegram_bot.core.project_chat_types import ChatResponse
from telegram_bot.core.session_scope import storage_key
from telegram_bot.core.usage import UsageSnapshot, render_usage
from telegram_bot.core.usage_meter import MODE_INTERACTIVE

logger = logging.getLogger(__name__)

IDS_FILENAME = "matrix-ids.json"
DIRECT_ROOMS_FILENAME = "matrix-direct-rooms.json"
SUPPORTED_COMMANDS = frozenset({"new", "model", "effort", "usage", "skills", "stop"})
_STATUS_HANDLE = 1
STATUS_MIN_INTERVAL_S = 60.0  # heartbeat notices become room messages on Matrix; throttle them
_RUNTIME_MODEL_PROVIDERS = frozenset({"codex", "piri", "crush", "danso"})
_EFFORT_PROVIDERS = frozenset({"codex", "piri", "danso"})
_CLAUDE_MODELS: tuple[tuple[str, str], ...] = (
    ("sonnet", "Claude Sonnet"),
    ("opus", "Claude Opus"),
    ("haiku", "Claude Haiku"),
)
_PROVIDER_LABELS = {"claude": "Claude Code", "codex": "Codex", "piri": "Piri"}
_SKILLS_PROMPT = (
    "List all installed skills, grouped by global and project.\n"
    "Output format requirements (strictly follow):\n"
    "- Group titles as a markdown heading line: ## Title\n"
    "- One skill per line, format: /skill_name description\n"
    "- Do NOT use HTML tags\n"
    "- Do NOT output any extra introductory text or status lines"
)


class MatrixConfigError(RuntimeError):
    """The Matrix frontend cannot start because its configuration is missing."""


# --- transport contract mirrors (see module docstring) -----------------------


class TurnSink(Protocol):
    async def typing(self) -> None: ...

    async def interim(self, text: str) -> None: ...

    async def approval(self, description: str, arguments: Any) -> bool: ...


class TransportPort(Protocol):
    async def open(self, initialize: bool = False) -> None: ...

    async def run(self) -> None: ...

    async def close(self) -> None: ...

    def enqueue_notice(self, room_id: str, text: str) -> None: ...

    def room_kind(self, room_id: str) -> str: ...


@dataclass(frozen=True)
class TurnResult:
    """Local mirror of ``transport.TurnResult`` (same field order/defaults)."""

    text: str
    session_id: str | None
    streamed: bool = False
    status: str = "complete"


def _turn_result(
    text: str, session_id: str | None, *, streamed: bool = False, status: str = "complete"
) -> Any:
    """Build the transport's ``TurnResult`` when importable, else the mirror."""

    try:
        from telegram_bot.core.matrix.transport import TurnResult as TransportTurnResult
    except ImportError:
        return TurnResult(text=text, session_id=session_id, streamed=streamed, status=status)
    return TransportTurnResult(text=text, session_id=session_id, streamed=streamed, status=status)


TransportFactory = Callable[[Mapping[str, Any], Any], Any]
AsyncCompletionSender = Callable[[int, int, str], Awaitable[bool]]


class _DirectRoomMap:
    """``@user -> !room`` memory for direct conversations (private, atomic)."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._rooms: dict[str, str] = {}
        if path is not None and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"matrix direct-room map unreadable: {path}") from exc
            rooms = raw.get("rooms") if isinstance(raw, dict) else None
            if not isinstance(rooms, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in rooms.items()
            ):
                raise ValueError(f"matrix direct-room map malformed: {path}")
            self._rooms = dict(rooms)

    def remember(self, user: str, room: str) -> None:
        if self._rooms.get(user) == room:
            return
        self._rooms[user] = room
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        payload = json.dumps({"version": 1, "rooms": self._rooms}, ensure_ascii=True, sort_keys=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, self._path)

    def room_for(self, user: str) -> str | None:
        return self._rooms.get(user)


class _NotificationRoute:
    """``notification_bot`` duck type: ``send_message(chat_id=, text=)``."""

    def __init__(self, deliver: Callable[[int, str], bool]) -> None:
        self._deliver = deliver

    async def send_message(self, chat_id: int, text: str, **_ignored: Any) -> None:
        if not self._deliver(int(chat_id), str(text)):
            raise RuntimeError(f"no Matrix route for chat_id {chat_id}")


class MatrixBot:
    """Matrix frontend: ``TurnRunner`` over ``ProjectChatHandler``."""

    def __init__(
        self,
        settings: Any,
        *,
        project_chat: Any,
        session_manager: Any,
        clock: Any = None,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self._settings = settings
        self._project_chat = project_chat
        self._session_manager = session_manager
        self._clock = clock or time
        self._transport_factory = transport_factory
        self._config: Mapping[str, Any] | None = None
        self._ids: MatrixIdMap | None = None
        self._direct_rooms: _DirectRoomMap | None = None
        self._transport: Any = None
        # Same role as TelegramBot._runtime_active_sessions: conversation keys
        # whose persisted session id this process has already resumed/created.
        self._runtime_active_sessions: set[Any] = set()

    # -- configuration -------------------------------------------------------

    def config_path(self) -> Path:
        raw = getattr(self._settings, "matrix_config_path", None)
        if not raw:
            raise MatrixConfigError(
                "Settings.matrix_config_path is not set: the Matrix frontend needs the "
                "private (0600) config file produced by the transport's login step"
            )
        return Path(str(raw)).expanduser()

    def load_config(self) -> Mapping[str, Any]:
        if self._config is None:
            from telegram_bot.core.matrix.state import load_config

            self._config = load_config(self.config_path())
        return self._config

    def _data_dir(self) -> Path:
        raw = getattr(self._settings, "bot_data_dir", None)
        if not raw:
            raise MatrixConfigError("Settings.bot_data_dir is not set")
        return Path(str(raw))

    @property
    def ids(self) -> MatrixIdMap:
        if self._ids is None:
            self._ids = MatrixIdMap(self._data_dir() / IDS_FILENAME)
        return self._ids

    def _direct_room_map(self) -> _DirectRoomMap:
        if self._direct_rooms is None:
            self._direct_rooms = _DirectRoomMap(self._data_dir() / DIRECT_ROOMS_FILENAME)
        return self._direct_rooms

    def validate_runtime_paths(self) -> None:
        """Fail fast on missing config or corrupt persisted maps (no writes)."""

        path = self.config_path()
        if not path.is_file():
            raise MatrixConfigError(f"Matrix config file not found: {path}")
        self._data_dir()
        self.ids
        self._direct_room_map()

    def allowed_user_ints(self) -> list[int]:
        """Ints for owner + family users, for extending the bridge allowlist."""

        config = self.load_config()
        users: list[str] = []
        owner = config.get("owner")
        if isinstance(owner, str) and owner:
            users.append(owner)
        for user in config.get("family_users") or ():
            if isinstance(user, str) and user and user not in users:
                users.append(user)
        return [self.ids.user_id(user) for user in users]

    def _owner_int(self) -> int | None:
        owner = self.load_config().get("owner")
        if not isinstance(owner, str) or not owner:
            return None
        return self.ids.user_id(owner)

    # -- lifecycle -----------------------------------------------------------

    @property
    def runner(self) -> MatrixTurnRunner:
        """The ``TurnRunner`` the transport drives (``run``/``cancel``)."""

        return MatrixTurnRunner(self)

    def _build_transport(self, config: Mapping[str, Any]) -> Any:
        runner = self.runner
        if self._transport_factory is not None:
            return self._transport_factory(config, runner)
        from telegram_bot.core.matrix.transport import MatrixTransport

        return MatrixTransport(dict(config), runner)

    async def serve(self) -> None:
        """Open the transport and serve until it returns; always closes it."""

        config = self.load_config()
        transport = self._build_transport(config)
        self._transport = transport
        self._project_chat.set_async_completion_sender(self.async_completion_sender)
        initialize = bool(getattr(self._settings, "matrix_initialize", False))
        try:
            # First run of a NEW bot device (CCC_MATRIX_INITIALIZE=1): create the
            # crypto store, upload keys, pin devices and gate rooms, then exit
            # without serving. The pilot's `--initialize` had the same contract;
            # a normal start refuses an empty store (explicit-new-device-
            # initialization-required) so a lost store is never recreated silently.
            await transport.open(initialize=initialize)
            if initialize:
                logger.info("Matrix frontend initialised a new bot device; start again without CCC_MATRIX_INITIALIZE")
                return
            self._post_startup_banner(config, transport)
            await transport.run()
        finally:
            self._transport = None
            await transport.close()

    def startup_banner(self) -> str:
        """One-line "frontend is up" notice: node · provider · model · effort · rev.

        Mirrors what the owner is used to seeing when the Telegram-side agent
        starts a session (owner request 2026-09-18). Everything is best-effort
        and read-only; unknown parts are simply omitted.
        """

        provider = str(getattr(self._settings, "agent_provider", "") or "")
        model, effort = self._configured_model_and_effort(provider)
        parts = [f"🟢 {platform.node()} ccc-node Matrix 프론트엔드 기동"]
        for value in (provider, model, effort, self._bridge_revision()):
            if value:
                parts.append(value)
        return " · ".join(parts)

    def _configured_model_and_effort(self, provider: str) -> tuple[str | None, str | None]:
        if provider == "codex":
            # Codex takes its default model/effort from ~/.codex/config.toml.
            home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
            try:
                with open(home / "config.toml", "rb") as fh:
                    data = tomllib.load(fh)
            except (OSError, ValueError):
                return None, None
            model = data.get("model")
            effort = data.get("model_reasoning_effort")
            return (str(model) if model else None, str(effort) if effort else None)
        model = getattr(self._settings, f"{provider}_model", None) if provider else None
        return (str(model) if model else None, None)

    @staticmethod
    def _bridge_revision() -> str | None:
        try:
            out = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(Path(__file__).resolve().parents[2]),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        rev = out.stdout.strip()
        return rev if out.returncode == 0 and rev else None

    def _post_startup_banner(self, config: Mapping[str, Any], transport: Any) -> None:
        if not getattr(self._settings, "matrix_startup_banner", True):
            return
        family = set(config.get("family_rooms") or ())
        direct_rooms = [room for room in (config.get("rooms") or ()) if room not in family]
        if not direct_rooms:
            return
        text = self.startup_banner()
        # Idempotent per hour and per text: a crash-looping unit (Restart=always)
        # must not queue one banner per restart (nine piled up on 2026-09-18).
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        key = f"startup-{digest}-{int(time.time() // 3600)}"
        for room in direct_rooms:
            try:
                try:
                    transport.enqueue_notice(room, text, key=key)
                except TypeError:  # transport without the key parameter
                    transport.enqueue_notice(room, text)
            except Exception:
                logger.warning("startup banner not queued for %s", room, exc_info=True)

    def run(self) -> None:
        """Blocking entry point with the same contract as ``TelegramBot.run()``.

        ``__main__.main()`` calls ``bot.run()`` synchronously, so this owns the
        event loop: access control and the session store are initialised the
        way the Telegram lifecycle does, SIGTERM/SIGINT cancel the serving task
        (the transport joins the running turn and marks it uncertain), and an
        orderly stop exits cleanly for systemd.
        """

        from telegram_bot.core.bot_shared import enforce_access_control

        enforce_access_control(self._settings)
        initialize = getattr(self._session_manager, "initialize", None)
        if callable(initialize):
            initialize()

        async def _main() -> None:
            loop = asyncio.get_running_loop()
            task = asyncio.current_task()
            assert task is not None
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, task.cancel)
                except (NotImplementedError, RuntimeError):  # pragma: no cover - non-POSIX loops
                    pass
            try:
                await self.serve()
            except asyncio.CancelledError:
                logger.info("Matrix frontend stopped")

        asyncio.run(_main())

    # -- outbound routing ----------------------------------------------------

    def room_for_chat(self, chat_id: int) -> str | None:
        """Reverse map a handler ``chat_id`` to a Matrix room, ``None`` if unknown."""

        matrix_id = self.ids.matrix_id(chat_id)
        if matrix_id is None:
            return None
        if matrix_id.startswith("!"):
            return matrix_id
        return self._direct_room_map().room_for(matrix_id)

    def _deliver_notice(self, chat_id: int, text: str) -> bool:
        transport = self._transport
        room = self.room_for_chat(chat_id)
        if transport is None or room is None:
            return False
        transport.enqueue_notice(room, text)
        return True

    async def async_completion_sender(self, user_id: int, chat_id: int, text: str) -> bool:
        """``ProjectChatHandler.set_async_completion_sender`` seam (#646)."""

        del user_id
        try:
            return self._deliver_notice(chat_id, text)
        except Exception:
            logger.exception("Matrix async completion delivery failed for chat %s", chat_id)
            return False

    def _notification_bot(self) -> _NotificationRoute:
        return _NotificationRoute(self._deliver_notice)

    def _supports_formatted(self) -> bool:
        return callable(getattr(self._transport, "send_formatted", None))

    async def _send_formatted(self, room_id: str, text: str) -> bool:
        """Deliver rendered HTML chunks; ``False`` leaves delivery to the plain path."""

        transport = self._transport
        if transport is None or not self._supports_formatted():
            return False
        try:
            for chunk in chunk_text(text):
                body, formatted = render_matrix_message(chunk)
                await transport.send_formatted(room_id, body, formatted)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Matrix formatted send failed; falling back to plain text")
            return False
        return True

    # -- callbacks handed to process_message ---------------------------------

    def _make_status_callback(
        self, sink: TurnSink
    ) -> Callable[[Optional[str], Optional[int]], Awaitable[Optional[int]]]:
        # Telegram edits one status bubble in place; Matrix has no edit path in
        # the durable outbox, so every status text would become a new room
        # message. Forward only when the text changed AND the interval passed,
        # so a long turn shows a few progress notices, not one every 4 s.
        last: dict[str, Any] = {"text": None, "at": 0.0}

        async def status_callback(
            text: Optional[str], message_id: Optional[int] = None
        ) -> Optional[int]:
            del message_id
            if text is None:
                return None  # delete: nothing to remove on Matrix
            now = time.monotonic()
            if text == last["text"] or now - last["at"] < STATUS_MIN_INTERVAL_S:
                return _STATUS_HANDLE
            last["text"], last["at"] = text, now
            try:
                await sink.interim(text)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Matrix status delivery failed", exc_info=True)
            return _STATUS_HANDLE

        return status_callback

    def _make_interim_callback(
        self, sink: TurnSink, room_id: str
    ) -> Callable[[str], Awaitable[None]]:
        async def interim(text: str) -> None:
            if await self._send_formatted(room_id, text):
                return
            await sink.interim(text)

        return interim

    def _bash_policy(self) -> str:
        profile = tool_policy.resolve_execution_profile(
            getattr(self._settings, "execution_profile", tool_policy.EXECUTION_STRICT_PROJECT),
            allowed_user_ids=getattr(self._settings, "allowed_user_ids", []),
            require_allowlist=getattr(self._settings, "require_allowlist", True),
        )
        return tool_policy.effective_bash_policy(
            tool_policy.resolve_bash_policy(getattr(self._settings, "bash_policy", None)),
            profile,
        )

    def _make_approval_callback(
        self, sink: TurnSink
    ) -> Callable[[int, int, ApprovalRequestEvent, int], Awaitable[ApprovalDecision]]:
        async def approval_callback(
            chat_id: int, user_id: int, event: ApprovalRequestEvent, generation: int
        ) -> ApprovalDecision:
            policy = self._bash_policy()
            if policy == tool_policy.BASH_AUTO_APPROVE:
                return ApprovalDecision.ALLOW
            if policy != tool_policy.BASH_APPROVE_EACH:
                return ApprovalDecision.DENY
            # Same gate as the Telegram route: only the owner may approve, and
            # only for a turn generation that is still active (fail-closed).
            if user_id != self._owner_int():
                return ApprovalDecision.DENY
            if not self._project_chat.is_agent_approval_active(user_id, chat_id, generation):
                return ApprovalDecision.DENY
            try:
                allowed = await sink.approval(event.description, event.arguments)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Matrix approval prompt failed; denying")
                return ApprovalDecision.DENY
            return ApprovalDecision.ALLOW if allowed is True else ApprovalDecision.DENY

        return approval_callback

    # -- TurnRunner ----------------------------------------------------------

    def _job_identity(self, job: Mapping[str, Any], room_kind: str) -> tuple[int, int, str]:
        sender = str(job["sender"])
        room_id = str(job["room_id"])
        direct = room_kind == "direct"
        user_id = self.ids.user_id(sender)
        chat_id = self.ids.chat_id(room_id, sender, direct=direct)
        if direct:
            self._direct_room_map().remember(sender, room_id)
        return user_id, chat_id, room_id

    async def run_turn(
        self, job: Mapping[str, Any], *, sink: TurnSink, session_id: str | None, room_kind: str
    ) -> Any:
        """``TurnRunner.run``: one Matrix message -> one reply."""

        del session_id  # the session manager is the resume authority (see report)
        user_id, chat_id, room_id = self._job_identity(job, room_kind)
        body = str(job.get("body") or "")
        command, args = self._parse_command(body)
        if command == "skills":
            return await self._cmd_skills(user_id=user_id, chat_id=chat_id, room_id=room_id, sink=sink)
        if command is not None:
            text = await self._run_command(command, args, user_id=user_id, chat_id=chat_id)
            return _turn_result(text, None)
        return await self._run_message(body, user_id=user_id, chat_id=chat_id, room_id=room_id, sink=sink)

    async def cancel(self, job: Mapping[str, Any]) -> bool:
        """``TurnRunner.cancel``: same handler path as ``/stop``."""

        room_id = str(job["room_id"])
        sender = str(job["sender"])
        transport = self._transport
        if transport is not None:
            kind = transport.room_kind(room_id)
        else:
            kind = "direct" if self._direct_room_map().room_for(sender) == room_id else "family"
        user_id, chat_id, _ = self._job_identity(job, kind)
        return await self._cancel_turn(user_id, chat_id)

    @staticmethod
    def _parse_command(body: str) -> tuple[str | None, list[str]]:
        stripped = body.strip()
        if not stripped.startswith("/"):
            return None, []
        parts = stripped.split()
        name = parts[0][1:].lower()
        if name not in SUPPORTED_COMMANDS:
            return None, []
        return name, parts[1:]

    async def _run_command(self, command: str, args: list[str], *, user_id: int, chat_id: int) -> str:
        if command == "new":
            return await self._cmd_new(user_id=user_id, chat_id=chat_id)
        if command == "model":
            return await self._cmd_model(args, user_id=user_id, chat_id=chat_id)
        if command == "effort":
            return await self._cmd_effort(args, user_id=user_id, chat_id=chat_id)
        if command == "usage":
            return await self._cmd_usage(user_id=user_id, chat_id=chat_id)
        if command == "stop":
            return await self._cmd_stop(user_id=user_id, chat_id=chat_id)
        raise ValueError(f"unsupported command: {command}")

    # -- conversation/session plumbing (mirrors bot.py) ----------------------

    def _conversation_key(self, user_id: int, chat_id: int) -> Any:
        scope = getattr(self._settings, "telegram_session_scope", "per-user-chat")
        return storage_key(scope, user_id, chat_id)

    def _active_provider(self) -> str:
        provider = str(getattr(self._settings, "agent_provider", "claude")).strip().lower()
        if provider not in {"claude", "codex", "crush", "piri", "danso"}:
            raise ValueError(f"Unsupported agent provider: {provider!r}")
        return provider

    def _now(self) -> datetime:
        return datetime.fromtimestamp(float(self._clock.time()), tz=timezone.utc)

    def _effective_session_id(self, key: Any, session: Mapping[str, Any]) -> str | None:
        session_id = session.get("session_id")
        if not session_id:
            return None
        provider = self._active_provider()
        if session.get("provider", "claude") != provider:
            return None
        if key in self._runtime_active_sessions:
            return str(session_id)
        if provider in {"codex", "piri", "danso"}:
            self._runtime_active_sessions.add(key)
            return str(session_id)
        if session_resume.resume_persisted_enabled() and session_resume.persisted_transcript_exists(
            getattr(self._project_chat, "conversations_dir", None), str(session_id)
        ):
            logger.info("Resuming persisted session for Matrix conversation %s after restart", key)
            self._runtime_active_sessions.add(key)
            return str(session_id)
        return None

    async def _resolve_turn_session(self, key: Any) -> tuple[dict[str, Any], str | None, bool]:
        """Return ``(session, session_id, new_session)`` the way the Telegram path does."""

        session, _switched = await self._session_manager.align_active_provider(key)
        new_session = False
        if session.get("new_session"):
            new_session = bool(
                await self._session_manager.patch_session_if(
                    key, expected={"new_session": True}, updates={"new_session": False}
                )
            )
            session["new_session"] = False
        now = self._now()
        if await self._session_manager.should_start_new_session(key, now=now):
            await self._session_manager.patch_session(
                key, updates={"session_id": None, "new_session": False}
            )
            session["session_id"] = None
            self._runtime_active_sessions.discard(key)
            new_session = True
        await self._session_manager.set_last_user_message_at(key, now)
        return session, self._effective_session_id(key, session), new_session

    async def _save_session_id(
        self, key: Any, response: ChatResponse, *, user_id: int, chat_id: int
    ) -> None:
        provider = self._active_provider()
        paused = provider == "danso" and getattr(response, "failure_class", None) == "danso_task_paused"
        if not ((getattr(response, "success", True) or paused) and response.session_id):
            return
        updates: dict[str, Any] = {"provider": provider, "session_id": response.session_id}
        remove: set[str] = set()
        audience = resolve_memory_audience(
            self._settings,
            user_id=user_id,
            chat_id=chat_id,
            route=getattr(self._project_chat, "_memory_route", "telegram"),
        )
        if audience is None:
            remove.update({"distill_memory_audience", "distill_memory_scope"})
        else:
            updates.update(
                {"distill_memory_audience": audience.kind, "distill_memory_scope": audience.scope}
            )
        await self._session_manager.patch_session(key, updates=updates, remove_fields=remove)
        self._runtime_active_sessions.add(key)

    async def _finish(self, response: ChatResponse, room_id: str) -> Any:
        status = "complete" if getattr(response, "success", True) else "error"
        streamed = bool(getattr(response, "streamed", False))
        content = str(response.content or "")
        if content.strip() and not streamed and await self._send_formatted(room_id, content):
            streamed = True
        return _turn_result(content, response.session_id, streamed=streamed, status=status)

    async def _run_message(
        self, body: str, *, user_id: int, chat_id: int, room_id: str, sink: TurnSink
    ) -> Any:
        key = self._conversation_key(user_id, chat_id)
        session, session_id, new_session = await self._resolve_turn_session(key)
        response = await self._project_chat.process_message(
            user_message=body,
            user_id=user_id,
            chat_id=chat_id,
            session_id=session_id,
            model=session.get("model"),
            effort=session.get("effort"),
            approval_policy=self._codex_approval_policy(),
            approvals_reviewer=self._codex_approvals_reviewer(),
            sandbox_policy=self._codex_sandbox_policy(),
            new_session=new_session,
            approval_callback=self._make_approval_callback(sink),
            typing_callback=sink.typing,
            status_callback=self._make_status_callback(sink),
            notification_bot=self._notification_bot(),
            interim_message_callback=self._make_interim_callback(sink, room_id),
            usage_mode=MODE_INTERACTIVE,
        )
        await self._save_session_id(key, response, user_id=user_id, chat_id=chat_id)
        return await self._finish(response, room_id)

    def _codex_approval_policy(self) -> str:
        policy = self._bash_policy()
        if policy == tool_policy.BASH_AUTO_APPROVE:
            return "never"
        if policy == tool_policy.BASH_AUTO_REVIEW:
            return "on-request"
        return "untrusted"

    def _codex_approvals_reviewer(self) -> str | None:
        return "auto_review" if self._bash_policy() == tool_policy.BASH_AUTO_REVIEW else None

    def _codex_sandbox_policy(self) -> dict[str, Any] | None:
        policy = self._bash_policy()
        if policy == tool_policy.BASH_AUTO_APPROVE:
            return {"type": "dangerFullAccess"}
        if policy == tool_policy.BASH_AUTO_REVIEW:
            return {"type": "workspaceWrite", "networkAccess": False}
        return None

    # -- commands ------------------------------------------------------------

    async def _cancel_user_streaming(self, user_id: int, chat_id: int) -> bool:
        try:
            return bool(await self._project_chat.cancel_user_streaming(user_id, chat_id))
        except Exception as exc:
            logger.error("Failed to cancel streaming for user %s: %s", user_id, exc)
            return False

    async def _cancel_turn(self, user_id: int, chat_id: int) -> bool:
        """The handler cancel path Telegram's ``/stop`` uses."""

        self._project_chat.invalidate_agent_approvals(user_id, chat_id)
        await self._cancel_user_streaming(user_id, chat_id)
        try:
            killed = await self._project_chat.stop(user_id, chat_id=chat_id)
        except TypeError:
            killed = await self._project_chat.stop(user_id)
        return bool(killed)

    async def _cmd_stop(self, *, user_id: int, chat_id: int) -> str:
        killed = await self._cancel_turn(user_id, chat_id)
        return "⏸️ Paused" if killed else "ℹ️ Nothing running"

    def _claude_settings_model(self, default: str | None) -> str | None:
        try:
            with open(getattr(self._settings, "claude_settings_path"), "r") as handle:
                value = json.load(handle).get("model", default)
        except Exception:
            return default
        return value if isinstance(value, str) else default

    def _get_real_model(self, session: Mapping[str, Any]) -> str:
        model = session.get("model")
        if isinstance(model, str) and model:
            return model
        return self._claude_settings_model("sonnet") or "sonnet"

    async def _cmd_new(self, *, user_id: int, chat_id: int) -> str:
        key = self._conversation_key(user_id, chat_id)
        # Telegram cancels the conversation's asyncio task here; the Matrix
        # transport owns its task, so interrupt through the handler instead.
        await self._cancel_turn(user_id, chat_id)
        session = await self._session_manager.get_session(key)
        provider = self._active_provider()
        provider_changed = session.get("provider") != provider
        updates: dict[str, Any] = {"provider": provider, "session_id": None, "new_session": True}
        if provider == "claude":
            settings_model = self._claude_settings_model(None)
            if session.get("model") != settings_model:
                updates["model"] = settings_model
        elif provider_changed:
            updates["model"] = None
        await self._session_manager.patch_session(
            key, updates=updates, remove_fields={"effort"} if provider_changed else ()
        )
        self._runtime_active_sessions.discard(key)
        label = _PROVIDER_LABELS.get(provider, provider)
        return (
            "🆕 Switched to new session mode. Your next message will start a new "
            f"{label} session."
        )

    async def _cmd_usage(self, *, user_id: int, chat_id: int) -> str:
        key = self._conversation_key(user_id, chat_id)
        provider = self._active_provider()
        session = await self._session_manager.get_session(key)
        session_id = session.get("session_id") if session.get("provider") == provider else None
        if not isinstance(session_id, str) or not session_id:
            session_id = None
        try:
            snapshot = await self._project_chat.get_usage(user_id, chat_id, session_id)
        except Exception:
            logger.warning("Provider usage read failed for %s", provider)
            snapshot = UsageSnapshot(provider=provider)
        reply = render_usage(snapshot)
        usage_meter = getattr(self._project_chat, "usage_meter", None)
        if usage_meter is not None:
            try:
                reply = f"{reply}\n\n{usage_meter.render_report(days=7)}"
            except Exception:
                logger.warning("Local usage meter report failed")
        cost_report = getattr(self._project_chat, "render_cost_report", None)
        if callable(cost_report):
            cost_text = cost_report(days=7)
            if cost_text:
                reply = f"{reply}\n\n{cost_text}"
        return reply

    async def _cmd_skills(self, *, user_id: int, chat_id: int, room_id: str, sink: TurnSink) -> Any:
        key = self._conversation_key(user_id, chat_id)
        response = await self._project_chat.process_message(
            user_message=_SKILLS_PROMPT,
            user_id=user_id,
            chat_id=chat_id,
            new_session=True,
            approval_policy=self._codex_approval_policy(),
            approvals_reviewer=self._codex_approvals_reviewer(),
            sandbox_policy=self._codex_sandbox_policy(),
            approval_callback=self._make_approval_callback(sink),
            typing_callback=sink.typing,
            status_callback=self._make_status_callback(sink),
            notification_bot=self._notification_bot(),
            interim_message_callback=self._make_interim_callback(sink, room_id),
            usage_mode=MODE_INTERACTIVE,
        )
        await self._save_session_id(key, response, user_id=user_id, chat_id=chat_id)
        return await self._finish(response, room_id)

    @staticmethod
    def _valid_piri_model_id(model_id: str) -> bool:
        return len(model_id) <= 256 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*", model_id) is not None

    async def _runtime_model_effort_reset_note(
        self, session: Mapping[str, Any], model_id: str
    ) -> str | None:
        current_effort = session.get("effort")
        if not current_effort:
            return None
        try:
            models = tuple(await self._project_chat.list_runtime_models())
        except Exception:
            logger.warning("Runtime model effort compatibility lookup failed", exc_info=True)
            return f"ℹ️ Reasoning effort {current_effort} could not be validated; reset to model default."
        model = next((item for item in models if item.id == model_id), None)
        if model is not None and current_effort in model.supported_reasoning_efforts:
            return None
        default_label = (
            model.default_reasoning_effort
            if model is not None and model.default_reasoning_effort
            else "provider default"
        )
        return (
            f"ℹ️ Reasoning effort {current_effort} is unsupported; "
            f"reset to model default ({default_label})."
        )

    async def _cmd_model(self, args: list[str], *, user_id: int, chat_id: int) -> str:
        key = self._conversation_key(user_id, chat_id)
        session = await self._session_manager.get_session(key)
        provider = self._active_provider()
        if args:
            return await self._select_model(key, session, args[0])
        if provider in _RUNTIME_MODEL_PROVIDERS:
            return await self._list_runtime_models_text(session)
        current = self._get_real_model(session)
        models = list(_CLAUDE_MODELS)
        if current not in dict(models):
            models.append((current, current))
        lines = ["🤖 Claude Code models (reply with /model <name>):"]
        for name, label in models:
            lines.append(f"• {name} — {label}" + (" (current)" if name == current else ""))
        return "\n".join(lines)

    async def _select_model(self, key: Any, session: Mapping[str, Any], name: str) -> str:
        provider = self._active_provider()
        if provider == "danso" and name != getattr(self._settings, "danso_model", None):
            return "❌ Danso uses the model configured by the operator. Use /model to view it."
        if provider == "piri" and not self._valid_piri_model_id(name):
            return "❌ Invalid Piri model id. Use a provider-qualified model id."
        updates: dict[str, Any] = {"provider": provider, "model": name}
        remove: set[str] = set()
        reset_note = None
        if session.get("provider") != provider:
            updates.update(session_id=None, new_session=True)
            remove.add("effort")
        elif provider in _RUNTIME_MODEL_PROVIDERS:
            reset_note = await self._runtime_model_effort_reset_note(session, name)
            if reset_note:
                remove.add("effort")
        await self._session_manager.patch_session(key, updates=updates, remove_fields=remove)
        label = dict(_CLAUDE_MODELS).get(name, name) if provider == "claude" else name
        logger.info("Matrix user: model set to %r via /model", name)
        reply = f"✅ Switched to {label}"
        return f"{reply}\n{reset_note}" if reset_note else reply

    async def _list_runtime_models_text(self, session: Mapping[str, Any]) -> str:
        provider = self._active_provider()
        label = provider.title()
        hint = "<codex-model>" if provider == "codex" else "<provider/model>"
        try:
            models = list(await self._project_chat.list_runtime_models())
        except Exception:
            logger.warning("%s model browsing failed", label)
            return f"⚠️ {label} model list is unavailable. Use /model {hint}."
        if not models:
            return f"📭 No {label} models are available. Use /model {hint}."
        current = session.get("model")
        lines = [f"🤖 {label} models (reply with /model <id>):"]
        for model in models:
            marks = [m for m, on in (("current", model.id == current), ("default", model.is_default)) if on]
            suffix = f" ({', '.join(marks)})" if marks else ""
            lines.append(f"• {model.id} — {model.display_name}{suffix}")
        return "\n".join(lines)

    @staticmethod
    def _selected_runtime_model(models: tuple[Any, ...], session: Mapping[str, Any]) -> Any:
        selected_id = session.get("model")
        if selected_id:
            return next((model for model in models if model.id == selected_id), None)
        return next((model for model in models if model.is_default), models[0] if models else None)

    async def _apply_runtime_effort_selection(self, key: Any, model: Any, requested: str) -> str:
        provider = self._active_provider()
        if requested == "default":
            await self._session_manager.patch_session(
                key, updates={"provider": provider}, remove_fields={"effort"}
            )
            default_label = model.default_reasoning_effort or "provider default"
            return f"✅ Reasoning effort reset to model default ({default_label}) for {model.display_name}"
        if requested not in model.supported_reasoning_efforts:
            supported = ", ".join(model.supported_reasoning_efforts)
            return f"❌ Unsupported effort for {model.display_name}: {requested}. Supported: {supported}, default"
        await self._session_manager.patch_session(
            key, updates={"provider": provider, "effort": requested}
        )
        return f"✅ Reasoning effort set to {requested} for {model.display_name}"

    async def _cmd_effort(self, args: list[str], *, user_id: int, chat_id: int) -> str:
        provider = self._active_provider()
        if provider not in _EFFORT_PROVIDERS:
            return "⚠️ /effort is available for Codex, Piri, or Danso."
        key = self._conversation_key(user_id, chat_id)
        session, _switched = await self._session_manager.align_active_provider(key)
        label = provider.title()
        try:
            models = tuple(await self._project_chat.list_runtime_models())
        except Exception:
            logger.warning("%s effort browsing failed", label, exc_info=True)
            return f"⚠️ {label} effort options are unavailable."
        model = self._selected_runtime_model(models, session)
        requested = args[0] if args else None
        no_options = f"📭 The selected {label} model does not advertise reasoning effort options."
        if model is None:
            if requested == "default":
                await self._session_manager.patch_session(
                    key, updates={"provider": provider}, remove_fields={"effort"}
                )
                return "✅ Reasoning effort reset to provider default"
            return no_options
        if not model.supported_reasoning_efforts and requested != "default":
            return no_options
        if requested is not None:
            return await self._apply_runtime_effort_selection(key, model, requested)
        current = session.get("effort")
        lines = [
            f"🧠 Reasoning effort for {model.display_name} (reply with /effort <level>):",
            f"Current: {current or 'model default'} · Model default: "
            f"{model.default_reasoning_effort or 'provider default'}",
        ]
        for effort in model.supported_reasoning_efforts:
            marks = [m for m, on in (("model default", effort == model.default_reasoning_effort), ("current", effort == current)) if on]
            lines.append(f"• {effort}" + (f" ({', '.join(marks)})" if marks else ""))
        lines.append("• default — use the model default")
        return "\n".join(lines)


class MatrixTurnRunner:
    """The ``TurnRunner`` object handed to the transport.

    ``MatrixBot.run()`` is the lifecycle coroutine, so the transport-facing
    ``run(job, ...)`` lives on this thin adapter instead of colliding with it.
    """

    def __init__(self, bot: MatrixBot) -> None:
        self._bot = bot

    async def run(
        self, job: Mapping[str, Any], *, sink: TurnSink, session_id: str | None, room_kind: str
    ) -> Any:
        return await self._bot.run_turn(job, sink=sink, session_id=session_id, room_kind=room_kind)

    async def cancel(self, job: Mapping[str, Any]) -> bool:
        return await self._bot.cancel(job)
