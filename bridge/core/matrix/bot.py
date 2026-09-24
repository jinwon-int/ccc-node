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
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol, Sequence

from telegram_bot.core import session_resume, tool_policy
from telegram_bot.core.memory_distill import MemoryDistillMixin
from telegram_bot.memory.distill_types import DistillTrigger
from telegram_bot.core.agent_runtime import ApprovalDecision, ApprovalRequestEvent
from telegram_bot.core.bot_danso_recovery import (
    OFFER,
    RECOVERY_TEXT_ACTIONS,
    DansoRecoveryMixin,
)
from telegram_bot.core.external_wait import ExternalWaitRegistry, default_registry_path
from telegram_bot.core.external_wait_monitor import ExternalWaitMonitor, GhCliTransport
from telegram_bot.core.matrix.render import chunk_text, render_matrix_message
from telegram_bot.core.matrix_ids import MatrixIdMap
from telegram_bot.core.memory_audience import resolve_memory_audience
from telegram_bot.core.project_chat_types import ChatResponse
from telegram_bot.core.push_notifier import (
    _DEDUP_WINDOW_SECONDS,
    _SENT_RETENTION_SECONDS,
    PushNotifier,
)
from telegram_bot.core.session_scope import storage_key
from telegram_bot.core.turn_notices import session_start_notice_text, session_start_reason
from telegram_bot.core.turn_watchdog import DEFAULT_NOTIFY_MINUTES, TurnAgeWatchdog
from telegram_bot.core.usage import UsageSnapshot, render_usage
from telegram_bot.core.usage_meter import MODE_INTERACTIVE
from telegram_bot.utils.health import health_reporter

logger = logging.getLogger(__name__)

IDS_FILENAME = "matrix-ids.json"
DIRECT_ROOMS_FILENAME = "matrix-direct-rooms.json"
SUPPORTED_COMMANDS = frozenset(
    {"new", "distill", "model", "effort", "usage", "skills", "stop", "task_pause", "task_resume", "task_recover", "history", "resume"}
)
_STATUS_HANDLE = 1
SELF_JOB_PREFIX = "$self-"

# #1795: one room answer per attachment that could not be staged (no agent run).
ATTACHMENT_FAILED_DEFAULT = "❌ 첨부를 열 수 없었습니다. 잠시 후 다시 보내 주세요."
ATTACHMENT_FAILED = {
    "oversize": "❌ 첨부가 허용 크기를 넘어 열지 않았습니다. 더 작은 파일(또는 해상도를 낮춘 사진)로 보내 주세요.",
    "integrity": "❌ 첨부의 무결성 확인에 실패해 열지 않았습니다. 다시 보내 주세요.",
    "download": "❌ 첨부를 내려받지 못했습니다. 잠시 후 다시 보내 주세요.",
    "invalid": "❌ 첨부 형식을 확인할 수 없어 열지 않았습니다.",
}
SELF_JOB_DANSO_AUTO_RESUME = "danso-auto-resume"
SELF_JOB_EXTERNAL_WAIT_RESUME = "external-wait-resume"
# #1955: visible answers when a non-owner turn is refused (fail-closed).
NON_OWNER_TURN_REFUSED = (
    "🔒 This agent runs with the owner's host access on this node, so it only "
    "takes turns from the owner."
)
EXTERNAL_WAIT_RESUME_REFUSED = (
    "🔒 A queued follow-up was not run: it could not be matched to the person who asked for it."
)
OWNER_ONLY_COMMAND = "🔒 Only the owner may use this command here."
# #1955: commands that read or switch sessions, change model/effort, touch
# memory, account usage or stored tasks. Non-owners keep /new, /stop and
# /skills (an agent run, gated like any other turn).
_OWNER_ONLY_COMMANDS = frozenset(
    {"resume", "history", "model", "effort", "distill", "usage", "task_pause", "task_resume", "task_recover"}
)
STATUS_MIN_INTERVAL_S = 15.0  # match Telegram CCC_HEARTBEAT_* defaults
_HEALTH_INTERVAL_S = 10.0  # match bot_lifecycle._WORKLOAD_INTERVAL
# #1820: a /sync long-poll returns within ~25s (40s HTTP ceiling), so a commit
# older than this means the receive leg is stuck; the same budget covers the
# first sync after start. An outbox head undelivered this long is stuck too.
_SYNC_STALE_S = 90.0
_SYNC_STARTUP_GRACE_S = 90.0
_OUTBOX_STUCK_S = 120.0
_DELIVERY_REJECTIONS_DEGRADED = 3
_AGENT_ERROR_LABEL_MAX = 160
# Turn outcomes that are not agent failures (drain, input validation, a turn
# folded into another, a paused Danso task, an expired recovery choice).
_NON_AGENT_ERRORS = frozenset(
    {"bridge_draining", "danso_input", "danso_task_resume_unavailable", "coalesced_turn"}
)
_NON_AGENT_FAILURE_CLASSES = frozenset({"danso_task_paused", "coalesced-turn"})
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

    async def status(self, text: Optional[str]) -> None:
        """Progress bubble: created once per turn, edited in place, redacted when done."""
        ...

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


class MatrixSpoolNotifier:
    """Polls the channel-neutral push spool and delivers records to the owner room.

    Record handling mirrors core.push_notifier (Telegram) byte for byte — same
    spool dir default, sent/ archive, dedup window and rate limit, and the
    same record format via ``PushNotifier._format``. The enabling flag is
    per-service (``CCC_PUSH_ENABLED``): on a node running both frontends
    exactly one process may consume the spool, or every notice is delivered
    twice. Records have no Matrix room of their own, so they land in the
    owner's direct room, falling back to the family room.
    """

    def __init__(self, settings: Any, transport: Any) -> None:
        # ``transport`` is a MatrixTransport; imported lazily in _build_transport
        # (bot/transport import cycle), so the annotation stays Any here.
        self._transport = transport
        self.enabled: bool = bool(getattr(settings, "push_enabled", False))
        self.spool_dir = Path(
            getattr(settings, "push_spool_dir", None)
            or (Path.home() / ".claude" / "state" / "telegram-spool")
        )
        self.interval: float = float(getattr(settings, "push_poll_interval", 3.0))
        self.max_per_minute: int = int(getattr(settings, "push_max_per_minute", 10))
        self._recent: dict[str, float] = {}
        self._sent_times: list[float] = []

    def _owner_room(self) -> Optional[str]:
        rooms = self._transport.policy.rooms
        direct = [r for r, mode in rooms.items() if mode == "direct"]
        if direct:
            return direct[0]
        family = [r for r, mode in rooms.items() if mode == "mention"]
        return family[0] if family else None

    async def run(self) -> None:
        if not self.enabled:
            logger.info("Matrix spool notifier disabled (push_enabled is false)")
            return
        room = self._owner_room()
        if room is None:
            logger.warning(
                "Matrix spool notifier enabled but no direct/family room is configured; not sending"
            )
            return
        sent_dir = self.spool_dir / "sent"
        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            sent_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Matrix spool notifier cannot create spool dir %s: %s", self.spool_dir, e)
            return
        self._prune_sent(sent_dir)
        logger.info("Matrix spool notifier active → room %s, spool %s", room, self.spool_dir)
        while True:
            try:
                await self._drain(room, sent_dir)
            except Exception:
                logger.warning("Matrix spool drain error (continuing)", exc_info=True)
            await asyncio.sleep(self.interval)

    async def _drain(self, room: str, sent_dir: Path) -> None:
        for p in sorted(self.spool_dir.glob("*.json")):
            if not p.is_file():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._archive(p, sent_dir)  # malformed → don't retry forever
                continue
            text = (data.get("text") or "").strip()
            if not text:
                self._archive(p, sent_dir)
                continue
            now = time.time()
            key = data.get("dedup") or text
            if key in self._recent and now - self._recent[key] < _DEDUP_WINDOW_SECONDS:
                self._archive(p, sent_dir)
                continue
            self._sent_times = [t for t in self._sent_times if now - t < 60]
            self._recent = {
                k: t for k, t in self._recent.items() if now - t < _DEDUP_WINDOW_SECONDS
            }
            if len(self._sent_times) >= self.max_per_minute:
                logger.warning("Matrix spool rate limit reached (%d/min); deferring", self.max_per_minute)
                return
            try:
                self._transport.enqueue_notice(room, PushNotifier._format(data))
            except ValueError as e:
                # A room this process may never write (not-allowed/too long)
                # stays failing forever — archive instead of looping on it.
                logger.warning("Matrix spool record undeliverable, archived: %s", e)
                self._archive(p, sent_dir)
                continue
            except Exception:
                logger.warning("Matrix spool send failed (will retry next cycle)", exc_info=True)
                return  # keep file; stop this cycle to preserve order
            self._recent[key] = now
            self._sent_times.append(now)
            self._archive(p, sent_dir)

    @staticmethod
    def _archive(p: Path, sent_dir: Path) -> None:
        try:
            p.rename(sent_dir / p.name)
        except OSError:
            try:
                p.unlink()
            except OSError:
                pass

    @staticmethod
    def _prune_sent(sent_dir: Path) -> None:
        cutoff = time.time() - _SENT_RETENTION_SECONDS
        try:
            for p in sent_dir.glob("*.json"):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except OSError:
                    pass
        except OSError:
            pass


class _NullSink:
    """Sink for dispatches that happen outside a served turn (no room bubble)."""

    async def typing(self) -> None:
        return None

    async def interim(self, text: str) -> None:
        del text

    async def status(self, text: Optional[str]) -> None:
        del text

    async def approval(self, description: str, arguments: Any) -> bool:
        del description, arguments
        return False


class _NoticeBotPort:
    """``application.bot.send_message`` shape the recovery mixin expects, over the outbox."""

    def __init__(self, bot: "MatrixBot") -> None:
        self._bot = bot

    async def send_message(self, *, chat_id: int, text: str, reply_markup: Any = None) -> None:
        del reply_markup  # Matrix offers are numbered text menus, never keyboards
        if not self._bot._deliver_notice(int(chat_id), str(text)):
            raise RuntimeError("no Matrix room is known for this chat")


class _NoticeApp:
    def __init__(self, bot: "MatrixBot") -> None:
        self.bot = _NoticeBotPort(bot)


class MatrixBot(MemoryDistillMixin, DansoRecoveryMixin):
    """Matrix frontend: ``TurnRunner`` over ``ProjectChatHandler``."""

    # True only while serve() owns the bound health reporter (#1820): per-turn
    # agent marks must never touch the default Telegram health.json in tests.
    _health_active = False
    _health_started = 0.0

    def __init__(
        self,
        settings: Any,
        *,
        project_chat: Any,
        session_manager: Any,
        distill_journal: Any = None,
        distill_snapshot_worker: Any = None,
        distill_extraction_worker: Any = None,
        distill_local_sink_worker: Any = None,
        distill_wiki_sink_worker: Any = None,
        clock: Any = None,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self._distill_journal = distill_journal
        self._distill_snapshot_worker = distill_snapshot_worker
        self._distill_extraction_worker = distill_extraction_worker
        self._distill_local_sink_worker = distill_local_sink_worker
        self._distill_wiki_sink_worker = distill_wiki_sink_worker
        self._settings = settings
        self._project_chat = project_chat
        self._session_manager = session_manager
        self._clock = clock or time
        self._transport_factory = transport_factory
        # Danso recovery (#1895 PR-A): per-conversation resume epoch (mirrors
        # bot.py) and the sink of the turn currently being served, so a typed
        # recovery choice can dispatch with the same room callbacks.
        self._task_resume_generations: dict[Any, int] = {}
        self._active_sink: Any = None
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
            if self._distill_journal is not None:
                self._distill_journal.validate_path()
                self._distill_journal.initialize()
            self._post_startup_banner(config, transport)
            self._start_health_reporting()
            await self._startup_danso_recovery_scan()
            notifier = MatrixSpoolNotifier(self._settings, transport)
            watchdog = self._build_turn_age_watchdog()
            external_wait = self._build_external_wait_monitor()
            # Same TaskGroup semantics as transport.run(): a leg that dies
            # stops the service so systemd restarts it whole.
            stop = asyncio.Event()
            try:
                async with asyncio.TaskGroup() as group:
                    # The watchdog/health loops run until the stop event is set,
                    # and a TaskGroup only cancels siblings when a leg raises — a
                    # transport that returns *cleanly* would otherwise leave the
                    # group waiting forever. Setting the event from the transport
                    # leg's finally keeps shutdown finite on both paths.
                    memory_tasks = []
                    if self._distill_journal is not None:
                        for stage in ("snapshot", "extraction", "local_sink", "wiki_sink"):
                            if getattr(self, f"_distill_{stage}_worker") is not None:
                                loop = getattr(self, f"_distill_{stage}_loop")
                                memory_tasks.append(group.create_task(loop(stop), name=f"matrix-distill-{stage}"))
                    group.create_task(self._run_until_stop(transport.run(), stop, memory_tasks))
                    group.create_task(self._health_reporter_loop(stop), name="matrix-health-reporter")
                    if notifier.enabled:
                        group.create_task(notifier.run())
                    if watchdog is not None:
                        group.create_task(watchdog.run(stop), name="matrix-turn-age-watchdog")
                    if external_wait is not None:
                        group.create_task(external_wait.run(stop), name="matrix-external-wait-monitor")
            except BaseExceptionGroup as failure:
                # A single failing leg (normally the transport) surfaces as itself,
                # as it did when the transport was awaited directly.
                if len(failure.exceptions) == 1:
                    raise failure.exceptions[0] from None
                raise
        finally:
            if not initialize:
                await self._enqueue_shutdown_distills()
            self._transport = None
            self._stop_health_reporting()
            await transport.close()

    @staticmethod
    async def _run_until_stop(
        leg: Awaitable[None], stop: asyncio.Event, memory_tasks: Sequence[asyncio.Task] = (),
    ) -> None:
        """Await the serving leg, then release every stop-event-driven sibling."""

        try:
            await leg
        finally:
            stop.set()
            for task in memory_tasks:
                task.cancel()

    # -- health.json (#1895 follow-up) ----------------------------------------
    #
    # The Telegram bridge writes ``<bot_data_dir>/health.json`` from its
    # lifecycle (startup marks, a 10s workload tick, per-turn agent marks). The
    # shared handler only records *turn* events, so an idle Matrix frontend left
    # ``health.json`` frozen at its last turn (observed 2026-09-21: two nodes
    # stale since 09-19) and fleet freshness checks could not tell "idle" from
    # "dead". Bind the reporter to *this* frontend's data dir and run the same
    # tick. In a Matrix data dir the ``telegram`` block means the Matrix sync
    # transport; ``bot.pid`` is this process.

    def _start_health_reporting(self) -> None:
        try:
            health_reporter.bind(self._data_dir(), agent_provider=self._active_provider())
            health_reporter.initialize_process()
            health_reporter.mark_starting("matrix frontend syncing")
            # Telegram marks the agent healthy once its provider probe passes at
            # startup; the Matrix frontend shares that runtime and start-up gate.
            health_reporter.record_agent_ok()
            self._health_started = time.monotonic()
            self._health_active = True
        except Exception:
            logger.warning("Matrix health reporter start failed", exc_info=True)

    def _stop_health_reporting(self) -> None:
        self._health_active = False
        try:
            health_reporter.mark_unavailable("matrix frontend stopped")
        except Exception:
            logger.debug("Matrix health reporter stop mark failed", exc_info=True)

    def _workload_snapshot(self, now: float) -> tuple[int, float, int]:
        snapshot = getattr(self._project_chat, "workload_snapshot", None)
        if callable(snapshot):
            count, oldest = snapshot(now)
        else:
            count, oldest = (1, 0.0) if getattr(self._transport, "active", None) else (0, 0.0)
        waiting = getattr(self._project_chat, "waiting_for_turn_snapshot", None)
        return int(count), float(oldest), int(waiting()) if callable(waiting) else 0

    def _transport_verdict(self) -> tuple[str, str, int]:
        """``("ok"|"wait"|"error", reason, consecutive failures)`` from real transport signals (#1820).

        A transport without ``health_signals`` gets no verdict ("wait"): the
        old blind tick reported the sync leg healthy while it was retrying.
        """

        signals_fn = getattr(self._transport, "health_signals", None)
        if not callable(signals_fn):
            return "wait", "", 0
        signals = signals_fn(time.time())
        receive_failures = int(signals.get("receive_failures") or 0)
        if receive_failures:
            error = signals.get("receive_error") or "network"
            return "error", f"matrix sync retrying ({error})", receive_failures
        sync_age = signals.get("sync_age_s")
        if sync_age is None:
            if time.monotonic() - self._health_started <= _SYNC_STARTUP_GRACE_S:
                return "wait", "", 0
            return "error", "matrix sync not completed since start", 1
        if sync_age > _SYNC_STALE_S:
            return "error", f"matrix sync stale for {int(sync_age)}s", 1
        # #1965 contract: parts the homeserver refused with a 4xx are
        # quarantined (skipped) and counted until a part is sent again, so a
        # systematic 4xx that silently drops every reply never shows green.
        streak = int(getattr(self._transport, "delivery_rejections_streak", 0) or 0)
        if streak >= _DELIVERY_REJECTIONS_DEGRADED:
            return "error", "outbox-rejections", streak
        head_age = signals.get("outbox_head_age_s")
        if head_age is not None and head_age > _OUTBOX_STUCK_S:
            send_failures = int(signals.get("send_failures") or 0)
            reason = f"matrix outbox stuck for {int(head_age)}s ({int(signals.get('outbox_pending') or 0)} pending)"
            if send_failures:
                reason += f"; send retrying ({signals.get('send_error') or 'network'})"
            return "error", reason, max(send_failures, 1)
        return "ok", "", 0

    def _record_transport_health(self) -> None:
        try:
            verdict, reason, failures = self._transport_verdict()
            if verdict == "ok":
                health_reporter.record_telegram_ok()
            elif verdict == "error":
                health_reporter.record_telegram_error(reason, consecutive_failures=failures)
        except Exception as exc:
            logger.debug("Matrix transport health check failed: %s", type(exc).__name__)

    async def _health_reporter_loop(self, stop: asyncio.Event) -> None:
        """Publish transport liveness and in-flight workload every ``_HEALTH_INTERVAL_S``."""

        while not stop.is_set():
            try:
                count, oldest, waiting = self._workload_snapshot(asyncio.get_running_loop().time())
                self._record_transport_health()
                health_reporter.record_workload(count, oldest, waiting_for_turn=waiting)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Matrix health tick failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=_HEALTH_INTERVAL_S)
            except asyncio.TimeoutError:
                continue

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

        # The transport refuses a crypto store with group/other-readable files
        # (unsafe-crypto-store); nio creates its SQLite store with the process
        # umask, which systemd leaves at 022. The pilot set this in main().
        os.umask(0o077)
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

    async def _notify_chat(self, chat_id: int, text: str) -> bool:
        """``(chat_id, text) -> delivered`` seam the background monitors need (#1825).

        Every monitor in ``core/`` takes exactly this callable, and
        ``async_completion_sender`` already is one modulo the unused ``user_id``.
        Going through it rather than ``_deliver_notice`` matters: the raw
        enqueue raises on an unknown room or oversized text, and a monitor
        expects ``False``, not an exception.
        """

        return await self.async_completion_sender(0, chat_id, text)

    def _build_turn_age_watchdog(self) -> TurnAgeWatchdog | None:
        """Notify-only turn-age dashboard (#1111) for the Matrix frontend (#1825).

        Telegram gets this from ``BotLifecycleMixin``, which ``MatrixBot`` does
        not inherit, so a Matrix turn that lost its terminal frame produced no
        signal at all — the operator had to ask whether anything was running.
        The watchdog never interrupts, pauses, or reroutes a turn; it only
        reports age. ``None`` when explicitly disabled or when the handler
        exposes no session registry to read ages from.
        """

        threshold_min = ExternalWaitMonitor.env_int(
            "CCC_TURN_AGE_NOTIFY_MIN", default=DEFAULT_NOTIFY_MINUTES
        )
        if threshold_min <= 0:
            logger.info("Matrix turn-age watchdog disabled (CCC_TURN_AGE_NOTIFY_MIN=0)")
            return None
        registry = getattr(self._project_chat, "_agent_session_registry", None)
        if registry is None:
            logger.warning(
                "Matrix turn-age watchdog unavailable: no session registry on project chat"
            )
            return None
        renotify_min = ExternalWaitMonitor.env_int("CCC_TURN_AGE_RENOTIFY_MIN", default=30)

        def turns_provider() -> list[tuple[int, int, float]]:
            return [
                (int(key[0]), int(key[1]), float(started))
                for key, started in registry.active_turn_ages()
                if len(key) >= 2
            ]

        return TurnAgeWatchdog(
            turns_provider=turns_provider,
            notifier=self._notify_chat,
            threshold_seconds=threshold_min * 60.0,
            renotify_seconds=renotify_min * 60.0,
        )

    def _build_external_wait_monitor(self) -> ExternalWaitMonitor | None:
        """Durable external-wait watch loop for the Matrix frontend (#1934).

        Telegram builds this in ``BotLifecycleMixin``, which ``MatrixBot``
        does not inherit — so a Matrix ``gh-ci-wait`` registration answered
        ``ok`` and wrote the record while nothing ever polled it: the CI
        result and the promised continuation never arrived (#1934). Same
        contract as Telegram: the shared ``ExternalWaitMonitor`` polls this
        frontend's own registry (``bot_data_dir/external-wait`` — the home
        the agent-side CLI resolves through ``CCC_EXTERNAL_WAIT_HOME``),
        notifications ride :meth:`_notify_chat`, and the continuation is a
        **self-job** turn in the waiting room (the #1895 mechanism), never a
        detached process call. ``None`` when explicitly disabled.
        """

        if not ExternalWaitMonitor.env_flag("CCC_EXTERNAL_WAIT_ENABLED", default=True):
            logger.info("Matrix external-wait monitor disabled (CCC_EXTERNAL_WAIT_ENABLED=0)")
            return None
        registry = ExternalWaitRegistry(default_registry_path(self._data_dir() / "external-wait"))
        return ExternalWaitMonitor(
            registry,
            transport=GhCliTransport(),
            notifier=self._notify_chat,
            resumer=self._enqueue_external_wait_resume,
            session_lookup=self._external_wait_session_lookup,
            resume_enabled=ExternalWaitMonitor.env_flag(
                "CCC_EXTERNAL_WAIT_RESUME", default=True
            ),
            resume_daily_cap=ExternalWaitMonitor.env_int(
                "CCC_EXTERNAL_WAIT_RESUME_DAILY_CAP", default=10
            ),
        )

    async def _external_wait_session_lookup(self, user_id: int, chat_id: int) -> str | None:
        """Current canonical session id for the conversation, or ``None``.

        The monitor skips a registered continuation whose session has moved
        on (``/new``, provider switch) so a stale promise is never injected
        into a new session (#740); Matrix resolves through the same session
        manager and conversation key the ordinary turn path uses.
        """

        try:
            session = await self._session_manager.get_session(
                self._conversation_key(int(user_id), int(chat_id))
            )
            return (session or {}).get("session_id")
        except Exception:
            return None

    async def _enqueue_external_wait_resume(self, record: Mapping[str, Any], prompt: str) -> bool:
        """Resumer seam: hand the continuation to the room as a self-job (#1934).

        Enqueuing is the resume: the durable ``$self-…`` job runs the prompt
        as an ordinary turn (claim, room sink, finish) and is idempotent per
        ``wait_id``, so a restart drains it instead of re-deciding it.
        """

        transport = self._transport
        room = self.room_for_chat(int(record.get("chat_id") or 0))
        enqueue = getattr(transport, "enqueue_self_job", None)
        if transport is None or room is None or not callable(enqueue):
            logger.info(
                "Matrix external-wait resume deferred: no transport/room for chat %s",
                record.get("chat_id"),
            )
            return False
        # #1955: the continuation runs as the person who registered the wait,
        # never silently as the owner. Unknown or no-longer-admitted users are
        # refused (``False`` -> the monitor posts its resume-failed notice).
        raw_user = record.get("user_id")
        user_int = raw_user if isinstance(raw_user, int) and not isinstance(raw_user, bool) else None
        sender = self.ids.matrix_id(user_int) if user_int is not None else None
        if user_int is None or sender is None or not self._is_admitted_sender(sender):
            logger.info(
                "Matrix external-wait resume refused: requester not admitted for chat %s",
                record.get("chat_id"),
            )
            return False
        body = json.dumps(
            {
                "kind": SELF_JOB_EXTERNAL_WAIT_RESUME,
                "v": 1,
                "wait_id": str(record.get("wait_id") or ""),
                "user_id": user_int,
                "prompt": prompt,
            }
        )
        extra: dict[str, Any] = {} if sender == self.load_config().get("owner") else {"sender": sender}
        try:
            enqueue(room, body, key=f"{SELF_JOB_EXTERNAL_WAIT_RESUME}:{record.get('wait_id') or 'none'}", **extra)
        except Exception as error:
            logger.warning("Matrix external-wait self-job enqueue failed: %s", type(error).__name__)
            return False
        return True

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
        # Telegram edits one status bubble in place; Matrix now matches via
        # sink.status (create once, then m.replace edits, redact on None).
        # Forward only when the text changed AND the interval passed, so a
        # long turn refreshes one bubble, not a stream of room messages.
        last: dict[str, Any] = {"text": None, "at": 0.0}

        async def status_callback(
            text: Optional[str], message_id: Optional[int] = None
        ) -> Optional[int]:
            del message_id
            if text is None:
                try:
                    await sink.status(None)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Matrix status redact failed", exc_info=True)
                return None
            now = time.monotonic()
            if text == last["text"] or now - last["at"] < STATUS_MIN_INTERVAL_S:
                return _STATUS_HANDLE
            last["text"], last["at"] = text, now
            try:
                await sink.status(text)
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

    def _execution_profile(self) -> str:
        return tool_policy.resolve_execution_profile(
            getattr(self._settings, "execution_profile", tool_policy.EXECUTION_STRICT_PROJECT),
            allowed_user_ids=getattr(self._settings, "allowed_user_ids", []),
            require_allowlist=getattr(self._settings, "require_allowlist", True),
        )

    def _bash_policy(self) -> str:
        return tool_policy.effective_bash_policy(
            tool_policy.resolve_bash_policy(getattr(self._settings, "bash_policy", None)),
            self._execution_profile(),
        )

    def _refuses_non_owner_turn(self, user_id: int) -> bool:
        """Fail-closed gate: no non-owner turn on ``owner-operator`` (#1955).

        ``owner-operator`` binds host-capable execution to exactly one owner,
        but Matrix also admits ``family_users``. No provider can confine a
        single turn to what a non-owner may see: Claude (including the
        unrestricted path, whose auto-approve allowlist never consults the
        approval callback), Danso, Piri and Crush run under the process-wide
        execution profile, and Codex's ``workspaceWrite`` sandbox limits
        writes but not reads (host secrets, owner transcripts). So on
        ``owner-operator`` every non-owner turn — messages, attachments,
        commands, resumes — is refused before any work. ``True`` means refuse.
        """

        if self._check_user_access(user_id):
            return False
        if self._execution_profile() != tool_policy.EXECUTION_OWNER_OPERATOR:
            return False
        logger.info(
            "Matrix non-owner turn refused: provider=%s profile=%s",
            self._active_provider(),
            tool_policy.EXECUTION_OWNER_OPERATOR,
        )
        return True

    def _is_admitted_sender(self, matrix_user: str) -> bool:
        """Owner or a configured ``family_users`` member (#1955)."""

        config = self.load_config()
        if matrix_user == config.get("owner"):
            return True
        return matrix_user in (config.get("family_users") or ())

    def _make_approval_callback(
        self, sink: TurnSink
    ) -> Callable[[int, int, ApprovalRequestEvent, int], Awaitable[ApprovalDecision]]:
        async def approval_callback(
            chat_id: int, user_id: int, event: ApprovalRequestEvent, generation: int
        ) -> ApprovalDecision:
            # #1955: sender first. Only the owner's turns may be approved —
            # auto-approve included — so a family member never inherits the
            # owner-operator grant (fail-closed).
            if not self._check_user_access(user_id):
                return ApprovalDecision.DENY
            policy = self._bash_policy()
            if policy == tool_policy.BASH_AUTO_APPROVE:
                return ApprovalDecision.ALLOW
            if policy != tool_policy.BASH_APPROVE_EACH:
                return ApprovalDecision.DENY
            # Same gate as the Telegram route: only for a turn generation that
            # is still active (fail-closed).
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
        self._active_sink = sink
        try:
            # #1955: before attachment staging, command parsing or self-jobs.
            if self._refuses_non_owner_turn(user_id):
                return _turn_result(NON_OWNER_TURN_REFUSED, None)
            if job.get("attachment"):
                # #1795: a photo/file never goes through command parsing.
                return await self._run_attachment(
                    job, user_id=user_id, chat_id=chat_id, room_id=room_id, sink=sink
                )
            if str(job.get("event_id") or "").startswith(SELF_JOB_PREFIX):
                return await self._run_self_job(
                    body,
                    user_id=user_id,
                    chat_id=chat_id,
                    sink=sink,
                    room_id=room_id,
                    turn_marker=str(job.get("event_id") or ""),
                    room_kind=room_kind,
                )
            command, args = self._parse_command(body)
            if command in _OWNER_ONLY_COMMANDS and not self._check_user_access(user_id):
                return _turn_result(OWNER_ONLY_COMMAND, None)
            if command == "skills":
                return await self._cmd_skills(user_id=user_id, chat_id=chat_id, room_id=room_id, sink=sink, turn_marker=str(job.get("event_id") or ""))
            if command == "task_resume":
                return await self._cmd_task_resume(user_id=user_id, chat_id=chat_id, room_id=room_id, sink=sink)
            if command is not None:
                text = await self._run_command(command, args, user_id=user_id, chat_id=chat_id)
                return _turn_result(text, None)
            if await self._answer_danso_recovery_choice(body, user_id=user_id, chat_id=chat_id):
                return _turn_result("", None, streamed=True)
            chosen = await self._select_resume_choice(body, user_id=user_id, chat_id=chat_id)
            if chosen is not None:
                return _turn_result(chosen, None)
            return await self._run_message(body, user_id=user_id, chat_id=chat_id, room_id=room_id, sink=sink, turn_marker=str(job.get("event_id") or ""))
        finally:
            self._active_sink = None

    async def _run_attachment(
        self, job: Mapping[str, Any], *, user_id: int, chat_id: int, room_id: str, sink: TurnSink
    ) -> Any:
        """Stage a Matrix photo/file and run it with the Telegram prompt contract (#1795).

        The decrypted file lives under ``<bot_data_dir>/matrix-media`` (0600)
        only for this turn. A failure answers the room once and does not run
        the agent; it never raises into the transport.
        """
        from telegram_bot.core import media as core_media
        from telegram_bot.core.matrix import media as matrix_media
        from telegram_bot.core.matrix.attachments import decode_attachment

        attachment = decode_attachment(job.get("attachment"))
        if attachment is None:
            logger.warning("Matrix attachment unavailable reason=invalid")
            return _turn_result(ATTACHMENT_FAILED.get("invalid", ATTACHMENT_FAILED_DEFAULT), None)
        directory = self._data_dir() / matrix_media.MEDIA_DIRNAME
        path: Path | None = None
        try:
            try:
                path = await matrix_media.stage(self._transport, attachment, directory, self._settings)
            except matrix_media.AttachmentError as exc:
                logger.warning("Matrix attachment unavailable reason=%s kind=%s", exc.reason, attachment.get("kind"))
                return _turn_result(ATTACHMENT_FAILED.get(exc.reason, ATTACHMENT_FAILED_DEFAULT), None)
            # The caption is the job body (``(attachment)`` when there is none).
            caption = str(job.get("body") or "") if attachment.get("captioned") else ""
            if attachment.get("kind") == "image":
                prompt = core_media.build_image_prompt(path, caption, channel="Matrix")
            else:
                prompt = core_media.build_document_prompt(
                    path,
                    display_name=str(attachment.get("name") or ""),
                    mime_type=str(attachment.get("mimetype") or "") or None,
                    size_bytes=path.stat().st_size,
                    caption=caption,
                    channel="Matrix",
                )
            logger.info("Matrix attachment staged kind=%s bytes=%d", attachment.get("kind"), path.stat().st_size)
            return await self._run_message(
                prompt,
                user_id=user_id,
                chat_id=chat_id,
                room_id=room_id,
                sink=sink,
                turn_marker=str(job.get("event_id") or ""),
            )
        finally:
            matrix_media.remove(path)

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
        if command == "distill":
            return await self._cmd_distill(user_id=user_id, chat_id=chat_id)
        if command == "model":
            return await self._cmd_model(args, user_id=user_id, chat_id=chat_id)
        if command == "effort":
            return await self._cmd_effort(args, user_id=user_id, chat_id=chat_id)
        if command == "usage":
            return await self._cmd_usage(user_id=user_id, chat_id=chat_id)
        if command == "stop":
            return await self._cmd_stop(user_id=user_id, chat_id=chat_id)
        if command == "task_pause":
            return await self._cmd_task_pause(args, user_id=user_id, chat_id=chat_id)
        if command == "task_recover":
            return await self._cmd_task_recover(user_id=user_id, chat_id=chat_id)
        if command == "history":
            return await self._cmd_history(user_id=user_id, chat_id=chat_id)
        if command == "resume":
            return await self._cmd_resume(args, user_id=user_id, chat_id=chat_id)
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

    async def _resolve_turn_session(
        self, key: Any, *, user_id: int, chat_id: int
    ) -> tuple[dict[str, Any], str | None, bool, str | None, bool]:
        """Return ``(session, session_id, new_session, stale_session_id, auto_new_session)``.

        Same decisions as the Telegram path (``bot.py``). ``stale_session_id`` is
        the persisted id *before* an automatic reset clears it, so the
        session-start banner can name the session that was not resumed.
        """

        previous = await self._session_manager.get_session(key)
        if previous.get("provider", "claude") != self._active_provider():
            await self._enqueue_previous_codex_session(
                previous, DistillTrigger.PROVIDER_SWITCH, user_id=user_id, chat_id=chat_id,
            )
        session, _switched = await self._session_manager.align_active_provider(key)
        stale_session_id = session.get("session_id") or None
        new_session = False
        if session.get("new_session"):
            new_session = bool(
                await self._session_manager.patch_session_if(
                    key, expected={"new_session": True}, updates={"new_session": False}
                )
            )
            session["new_session"] = False
        now = self._now()
        auto_new_session = bool(await self._session_manager.should_start_new_session(key, now=now))
        if auto_new_session:
            await self._enqueue_previous_codex_session(
                session, DistillTrigger.AUTO_NEW, user_id=user_id, chat_id=chat_id,
            )
            await self._session_manager.patch_session(
                key, updates={"session_id": None, "new_session": False}
            )
            session["session_id"] = None
            self._runtime_active_sessions.discard(key)
            new_session = True
        await self._session_manager.set_last_user_message_at(key, now)
        return (
            session,
            self._effective_session_id(key, session),
            new_session,
            stale_session_id,
            auto_new_session,
        )

    async def _post_session_start_notice(
        self,
        *,
        session: Mapping[str, Any],
        new_session: bool,
        auto_new_session: bool,
        stale_session_id: str | None,
        room_id: str,
        sink: TurnSink,
    ) -> None:
        """Post the same session-start banner the Telegram bridge sends.

        Telegram replies with ``session_start_notice_text`` whenever a turn
        starts on a fresh provider stream (bridge restart without a resumable
        transcript, ``/new``, automatic reset). The Matrix frontend never did,
        so a room could not tell a resumed conversation from a fresh one. Best
        effort: a delivery failure is logged and the turn still runs.
        """

        provider = str(session.get("provider") or self._active_provider())
        notice = session_start_notice_text(
            reason=session_start_reason(
                new_session=new_session,
                auto_new_session=auto_new_session,
                stale_session_id=stale_session_id,
            ),
            model=session.get("model"),
            provider=provider,
            previous_session_id=stale_session_id,
        )
        try:
            await self._make_interim_callback(sink, room_id)(notice)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Matrix session-start notice delivery failed", exc_info=True)

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
            route=self._memory_route(),
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
        self, body: str, *, user_id: int, chat_id: int, room_id: str, sink: TurnSink, turn_marker: str | None = None
    ) -> Any:
        if self._refuses_non_owner_turn(user_id):
            return _turn_result(NON_OWNER_TURN_REFUSED, None)
        key = self._conversation_key(user_id, chat_id)
        session, session_id, new_session, stale_session_id, auto_new_session = (
            await self._resolve_turn_session(key, user_id=user_id, chat_id=chat_id)
        )
        if session_id is None:
            await self._post_session_start_notice(
                session=session,
                new_session=new_session,
                auto_new_session=auto_new_session,
                stale_session_id=stale_session_id,
                room_id=room_id,
                sink=sink,
            )
        response = await self._dispatch_turn(
            body, key=key, user_id=user_id, chat_id=chat_id, session=session,
            session_id=session_id, new_session=new_session, sink=sink, turn_marker=turn_marker,
        )
        # #1895: like the Telegram path, a failed Danso turn is followed by the
        # recovery menu — after the failure text, never instead of it.
        result = await self._finish(response, room_id)
        await self._offer_danso_recovery_if_failed(response, key, user_id, chat_id)
        return result

    async def _dispatch_turn(
        self,
        body: str,
        *,
        key: Any,
        user_id: int,
        chat_id: int,
        session: Mapping[str, Any],
        session_id: str | None,
        new_session: bool,
        sink: TurnSink | None = None,
        resume_task: bool = False,
        turn_marker: str | None = None,
        dispatch_guard: Callable[[], bool] | None = None,
    ) -> ChatResponse:
        """One ``process_message`` call with this room's callbacks; persists the session."""

        sink = sink or self._active_sink or _NullSink()
        room_id = self.room_for_chat(chat_id) or ""
        extra: dict[str, Any] = {}
        if resume_task:
            extra["resume_task"] = True
        if dispatch_guard is not None:
            extra["dispatch_guard"] = dispatch_guard
        response = await self._record_turn_health(self._project_chat.process_message(
            user_message=body,
            user_id=user_id,
            chat_id=chat_id,
            session_id=session_id,
            model=session.get("model"),
            effort=session.get("effort"),
            approval_policy=self._codex_approval_policy(user_id),
            approvals_reviewer=self._codex_approvals_reviewer(user_id),
            sandbox_policy=self._codex_sandbox_policy(user_id),
            new_session=new_session,
            approval_callback=self._make_approval_callback(sink),
            typing_callback=sink.typing,
            status_callback=self._make_status_callback(sink),
            notification_bot=self._notification_bot(),
            interim_message_callback=self._make_interim_callback(sink, room_id),
            usage_mode=MODE_INTERACTIVE,
            **extra,
        ))
        await self._save_session_id(key, response, user_id=user_id, chat_id=chat_id)
        if getattr(response, "success", True):
            await self._record_codex_checkpoint(
                key, response, request_text=body, turn_marker=turn_marker,
                user_id=user_id, chat_id=chat_id,
            )
        return response

    async def _record_turn_health(self, turn: Awaitable[ChatResponse]) -> ChatResponse:
        """Await one agent turn and mark ``agent`` in health.json by its outcome (#1820).

        Before this, ``agent.last_ok_at`` stayed at process start. Outcomes
        that are not agent failures leave the agent state untouched; a raised
        exception is recorded by type name only and re-raised; cancellation
        is recorded only when the transport's turn timeout caused it.
        """

        try:
            response = await turn
        except asyncio.CancelledError:
            # The transport cancels the runner both for /stop or shutdown and
            # when ``turn_timeout`` expires; only the last is an agent failure
            # (a provider hanging until the cap must not stay "healthy").
            if getattr(self._transport, "turn_timed_out", False) is True:
                self._mark_agent_health("turn-timeout", prefix="")
            raise
        except Exception as exc:
            self._mark_agent_health(type(exc).__name__)
            raise
        if getattr(response, "success", True):
            self._mark_agent_health(None)
            return response
        error = getattr(response, "error", None)
        failure_class = getattr(response, "failure_class", None)
        if error in _NON_AGENT_ERRORS or failure_class in _NON_AGENT_FAILURE_CLASSES:
            return response
        failure_code = getattr(response, "failure_code", None)
        if not (error or failure_class or failure_code):
            return response  # e.g. an expired recovery selection
        label = " / ".join(str(part) for part in (failure_class, failure_code, error) if part)
        self._mark_agent_health(label)
        return response

    def _mark_agent_health(self, error: str | None, *, prefix: str = "agent turn failed: ") -> None:
        if not self._health_active:
            return
        try:
            if error is None:
                health_reporter.record_agent_ok()
            else:
                label = " ".join(str(error).split())[:_AGENT_ERROR_LABEL_MAX]
                health_reporter.record_agent_error(prefix + label)
        except Exception:
            logger.debug("Matrix agent health mark failed", exc_info=True)

    # -- Danso long-task commands and recovery (#1895 PR-A) ------------------
    #
    # ``DansoRecoveryMixin`` is the Telegram implementation of the recovery
    # offer/choice discipline (one-shot claim, binding guard, evidence-first
    # continue). Matrix reuses it verbatim and supplies the few channel ports
    # it needs. Differences that are deliberate:
    # * offers are always the numbered text menu (Matrix has no inline
    #   keyboards) and only the owner may see or answer them;
    # * ``self._config`` is the Matrix JSON here, so every mixin method that
    #   reads bridge settings through it is overridden to use ``self._settings``;
    # * the restart scan never dispatches by itself: an eligible automatic
    #   resume becomes a transport *self-job* (PR-A2) that runs as a normal
    #   turn, with this room's sink, under the single-turn discipline.

    def _danso_recovery_enabled(self) -> bool:
        return self._active_provider() == "danso" and bool(
            getattr(self._settings, "danso_long_task_enabled", False)
        )

    def _danso_recovery_text_mode(self) -> bool:
        return True

    def _danso_recovery_auto_resume(self) -> bool:
        return bool(getattr(self._settings, "danso_recovery_auto_resume", False))

    def _danso_recovery_auto_resume_retry_delay(self) -> int:
        try:
            delay = int(getattr(self._settings, "danso_recovery_auto_resume_retry_delay_seconds", 10))
        except (TypeError, ValueError):
            return 0
        return min(max(delay, 0), 60)

    async def _auto_resume_danso_recovery(
        self, key: Any, user_id: int, chat_id: int, token: str, epoch: int, route: Any, snapshot: Any, offer: Any
    ) -> bool:
        """Outside a served turn (restart scan) hand the resume to the transport as a
        self-job; inside one (that self-job running) do the mixin's automatic resume
        with this room's sink. (#1895 PR-A2)"""

        if self._active_sink is not None:
            return await super()._auto_resume_danso_recovery(key, user_id, chat_id, token, epoch, route, snapshot, offer)
        transport = self._transport
        room = self.room_for_chat(int(chat_id))
        enqueue = getattr(transport, "enqueue_self_job", None)
        if transport is None or room is None or not callable(enqueue):
            logger.info("Matrix auto-resume deferred: no transport/room for chat %s", chat_id)
            return False
        body = json.dumps({"kind": SELF_JOB_DANSO_AUTO_RESUME, "v": 1})
        try:
            enqueue(room, body, key=f"{SELF_JOB_DANSO_AUTO_RESUME}:{snapshot.fingerprint[:16]}")
        except Exception as error:
            logger.warning("Matrix auto-resume self-job enqueue failed: %s", type(error).__name__)
            return False
        # The offer stays pending (no NOTIFIED mark): the self-job re-inspects
        # the journal inside its turn and only then claims it.
        return True

    async def _run_self_job(
        self,
        body: str,
        *,
        user_id: int,
        chat_id: int,
        sink: TurnSink,
        room_id: str,
        turn_marker: str | None = None,
        room_kind: str | None = None,
    ) -> Any:
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        kind = payload.get("kind") if isinstance(payload, dict) else None
        if kind == SELF_JOB_EXTERNAL_WAIT_RESUME and not self._external_wait_job_admitted(
            payload, user_id=user_id, room_kind=room_kind
        ):
            return _turn_result(EXTERNAL_WAIT_RESUME_REFUSED, None)
        if kind not in (SELF_JOB_DANSO_AUTO_RESUME, SELF_JOB_EXTERNAL_WAIT_RESUME) or (
            kind == SELF_JOB_DANSO_AUTO_RESUME and not self._check_user_access(user_id)
        ):
            logger.warning("Matrix self-job ignored: kind=%s", kind)
            return _turn_result("", None, streamed=True)
        if kind == SELF_JOB_EXTERNAL_WAIT_RESUME:
            # External-wait continuation (#1934): the resume prompt runs as an
            # ordinary turn in the waiting room — session resolution, room
            # sink, finish — so the promised follow-up reads like the answer
            # it replaced, not like a detached system notice.
            prompt = str(payload.get("prompt") or "")
            if not prompt.strip():
                logger.warning("Matrix external-wait self-job has no prompt; ignored")
                return _turn_result("", None, streamed=True)
            return await self._run_message(
                prompt, user_id=user_id, chat_id=chat_id, room_id=room_id, sink=sink, turn_marker=turn_marker
            )
        key = self._conversation_key(user_id, chat_id)
        # Re-runs the full eligibility check; with the sink set the mixin's
        # automatic path claims the offer and dispatches. Anything no longer
        # eligible falls back to the ordinary menu.
        await self._offer_danso_recovery(key, user_id, chat_id, auto=True)
        return _turn_result("", None, streamed=True)

    def _external_wait_job_admitted(
        self, payload: Mapping[str, Any], *, user_id: int, room_kind: str | None
    ) -> bool:
        """Whether an external-wait self-job may run as its job sender (#1955).

        The body's ``user_id`` must name the job sender and that sender must
        still be admitted (owner or ``family_users``). A legacy body without
        ``user_id`` predates the sender binding and runs only for the owner
        in a direct room.
        """

        requested = payload.get("user_id")
        if requested is None:
            admitted = self._check_user_access(user_id) and room_kind == "direct"
        else:
            sender = self.ids.matrix_id(int(user_id))
            admitted = (
                isinstance(requested, int)
                and not isinstance(requested, bool)
                and requested == int(user_id)
                and sender is not None
                and self._is_admitted_sender(sender)
            )
        if not admitted:
            logger.info("Matrix external-wait self-job refused: requester not matched or not admitted")
        return admitted

    def _danso_recovery_route(self, user_id: int, chat_id: int) -> Any:
        audience = resolve_memory_audience(
            self._settings, user_id=user_id, chat_id=chat_id, route=self._memory_route()
        )
        return None if audience is None else [audience.kind, audience.scope]

    def _memory_route(self) -> str:
        return str(getattr(self._project_chat, "_memory_route", "matrix") or "matrix")

    def _check_user_access(self, user_id: int) -> bool:
        owner = self._owner_int()
        return owner is not None and int(user_id) == owner

    def _task_resume_generation(self, session_key: Any) -> int:
        return self._task_resume_generations.get(session_key, 0)

    def _bump_task_resume_generation(self, session_key: Any) -> int:
        generation = self._task_resume_generations.get(session_key, 0) + 1
        self._task_resume_generations[session_key] = generation
        return generation

    async def _enqueue_user_task(self, user_id: Any, run_task: Any, on_overflow: Any) -> bool:
        # The transport already serialises one turn per room; a typed choice
        # is answered inside that turn, so there is no second queue to join.
        del user_id, on_overflow
        await run_task()
        return True

    def _require_application(self) -> Any:
        return _NoticeApp(self)

    async def _send_smart(self, chat_id: int, text: str) -> None:
        text = str(text or "")
        if not text.strip():
            return
        room = self.room_for_chat(int(chat_id))
        if room is not None and await self._send_formatted(room, text):
            return
        if not self._deliver_notice(int(chat_id), text):
            logger.warning("Matrix recovery reply undeliverable for chat %s (no room)", chat_id)

    async def _continue_danso_recovery(
        self, key: Any, user_id: int, chat_id: int, epoch: int, route: Any,
        current: Mapping[str, Any], snapshot: Any, *, auto: bool = False,
    ) -> None:
        from telegram_bot.core.danso_worker import TASK_RESUME_CONTROL

        safe_resume = bool(snapshot.resume_allowed)
        prompt = TASK_RESUME_CONTROL if safe_resume else snapshot.continuation()

        def guard() -> bool:
            return self._danso_recovery_guard(key, user_id, chat_id, epoch, route)

        async def dispatch() -> ChatResponse:
            return await self._dispatch_turn(
                prompt, key=key, user_id=user_id, chat_id=chat_id, session=current,
                session_id=current["session_id"] if safe_resume else None,
                new_session=not safe_resume, resume_task=safe_resume, dispatch_guard=guard,
            )

        response = await dispatch()
        if auto and safe_resume and await self._auto_resume_retry_allowed(
                key, user_id, chat_id, epoch, route, current, snapshot, response):
            response = await dispatch()  # #1888 stale-lock retry, same rules as Telegram
        await self._send_smart(chat_id, response.content)
        if not response.success:
            await self._offer_danso_recovery(key, user_id, chat_id, force=True)

    async def _answer_danso_recovery_choice(self, body: str, *, user_id: int, chat_id: int) -> bool:
        """A typed ``1``/``2``/``3`` from the owner answers a pending offer (Telegram #1718)."""

        action = RECOVERY_TEXT_ACTIONS.get(body.strip())
        if action is None or not self._danso_recovery_enabled() or not self._check_user_access(user_id):
            return False
        key = self._conversation_key(user_id, chat_id)
        current = await self._session_manager.get_session(key)
        offer = current.get(OFFER)
        if not isinstance(offer, dict) or not offer.get("token"):
            return False
        epoch, route = self._task_resume_generation(key), self._danso_recovery_route(user_id, chat_id)

        async def report(text: str, keyboard_token: Any = None) -> None:
            del keyboard_token  # text menus only
            await self._send_smart(chat_id, text)

        await self._apply_danso_recovery_choice(key, user_id, chat_id, offer["token"], action, epoch, route, report)
        return True

    async def _startup_danso_recovery_scan(self) -> None:
        """Offer recovery for stored Danso tasks after a restart; eligible automatic
        resumes are queued as transport self-jobs, never dispatched here."""

        if not self._danso_recovery_enabled():
            return
        try:
            await self._recover_danso_tasks(None)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Matrix startup Danso recovery scan failed; /task_recover remains available")

    # -- /history and /resume (#1895 PR-B) ------------------------------------
    #
    # Ports of bot_commands._cmd_history / _cmd_resume and the digit reply in
    # bot_delivery: same provider rules (Claude browsing is locked under
    # audience-scoped memory; Piri/Danso resume by exact id only; Codex/Crush
    # browse runtime threads), plain-text output, no Telegram markup.

    def _claude_scoped_transcript_controls_disabled(self) -> bool:
        return (
            self._active_provider() == "claude"
            and getattr(self._settings, "bridge_memory_mode", "off") == "audience-scoped"
        )

    async def _cmd_history(self, *, user_id: int, chat_id: int) -> str:
        key = self._conversation_key(user_id, chat_id)
        session = await self._session_manager.get_session(key)
        session_id = self._effective_session_id(key, session)
        provider = str(session.get("provider") or self._active_provider())
        if not session_id:
            return "📭 No active session. Start a conversation first."
        if provider in {"piri", "danso"}:
            return (
                f"ℹ️ {provider.title()} does not expose bounded transcript history. "
                "The current session still resumes by its exact id."
            )
        if provider in {"codex", "crush"}:
            try:
                history = await self._project_chat.read_runtime_session(session_id, limit=5)
            except Exception:
                logger.warning("%s history browsing failed", provider.title())
                return f"⚠️ {provider.title()} history is unavailable for this session."
            messages = [
                {"role": item.role, "content": item.content, "timestamp": item.timestamp or ""}
                for item in history.messages
            ]
        else:
            messages = await asyncio.to_thread(self._project_chat.get_recent_messages, session_id, limit=5)
        if not messages:
            return "📭 No history available for this session."
        lines = ["📜 Recent History (last 5 messages)", f"Provider: {provider}", ""]
        for msg in messages:
            role, content, timestamp = str(msg["role"]), str(msg["content"]), str(msg.get("timestamp") or "")
            emoji, label = ("🧑", "User") if role == "user" else ("🤖", "Assistant")
            try:
                ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                ts = timestamp[:19]
            if len(content) > 500:
                content = content[:500] + "..."
            lines.extend([f"{emoji} {label} [{ts}]", content, ""])
        reply = "\n".join(lines).strip()
        return reply if len(reply) <= 4000 else reply[:3997] + "..."

    async def _cmd_resume(self, args: list[str], *, user_id: int, chat_id: int) -> str:
        key = self._conversation_key(user_id, chat_id)
        provider = self._active_provider()
        if self._claude_scoped_transcript_controls_disabled():
            await self._session_manager.patch_session(key, remove_fields={"resume_list"})
            return (
                "🔒 Claude session browsing is disabled while private memory is audience-scoped. "
                "Use /new to start a fresh session."
            )
        session = await self._session_manager.get_session(key)
        stored = str(session.get("provider") or provider)
        if stored != provider:
            return f"❌ Provider mismatch: this session is {stored}, but the active provider is {provider}. Use /new first."
        if provider == "danso":
            current = session.get("session_id")
            if args and (len(args) != 1 or args[0] != current):
                return "❌ Danso can resume only this conversation's current session. Use /new for a fresh session."
            return (f"ℹ️ Current Danso session auto-resumes: {current}" if current
                    else "📭 No Danso session yet. Send a message to start one.")
        if provider == "piri":
            return await self._resume_piri(args, key=key, session=session, user_id=user_id, chat_id=chat_id)
        if provider in {"codex", "crush"}:
            return await self._resume_runtime_list(key=key, provider=provider)
        sessions = await asyncio.to_thread(self._project_chat.list_sessions, limit=10)
        if not sessions:
            return "📭 No session history found."
        await self._session_manager.patch_session(
            key, updates={"resume_list": [[sid, msg, "claude"] for sid, msg, _ in sessions]}
        )
        lines = ["📋 Session History", ""]
        for index, (sid, msg, mtime) in enumerate(sessions, 1):
            text = re.sub(r"https?://\S+", "", str(msg).replace("\n", " ")).strip()
            lines.append(f"{index}. {text} [claude]")
            lines.append(self._relative_time(float(mtime)))
            lines.append("")
        lines.append("Reply with a number to switch to that session:")
        return "\n".join(lines).strip()

    async def _resume_piri(self, args: list[str], *, key: Any, session: Mapping[str, Any], user_id: int, chat_id: int) -> str:
        if len(args) > 1:
            return "Usage: /resume <piri-session-id>"
        if args:
            requested = args[0].strip()
            if len(requested) > 128 or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", requested) is None:
                return "❌ Invalid Piri session id."
            if requested != session.get("session_id"):
                await self._enqueue_previous_codex_session(
                    dict(session), DistillTrigger.EXPLICIT, user_id=user_id, chat_id=chat_id,
                    discriminator=self._shutdown_distill_discriminator(dict(session)),
                )
            await self._session_manager.patch_session(
                key, updates={"provider": "piri", "session_id": requested, "new_session": False},
                remove_fields={"resume_list"},
            )
            self._runtime_active_sessions.add(key)
            return f"✅ Piri session selected: {requested}"
        current = session.get("session_id")
        if isinstance(current, str) and current:
            return f"ℹ️ Current Piri session auto-resumes: {current}\nTo select another session, use /resume <piri-session-id>."
        return "Usage: /resume <piri-session-id>"

    async def _resume_runtime_list(self, *, key: Any, provider: str) -> str:
        label = provider.title()
        try:
            items = await self._project_chat.list_runtime_sessions(limit=10)
        except Exception:
            logger.warning("%s session browsing failed", label)
            return f"⚠️ {label} session history is unavailable."
        if not items:
            return f"📭 No {label} session history found."
        resume_list: list[list[str]] = []
        lines = ["📋 Session History", ""]
        for index, item in enumerate(items, 1):
            title = item.title or item.preview or item.id
            title = " ".join(re.sub(r"https?://\S+", "", str(title)).split())[:120] or item.id
            resume_list.append([item.id, title, provider])
            details = " · ".join(" ".join(str(v).split())[:80] for v in (item.model, item.cwd) if v)
            lines.append(f"{index}. {title} [{provider + ' · ' + details if details else provider}]")
        lines.append("")
        lines.append("Reply with a number to switch to that session:")
        await self._session_manager.patch_session(key, updates={"resume_list": resume_list})
        return "\n".join(lines)

    def _relative_time(self, mtime: float) -> str:
        delta = int(float(self._clock.time()) - mtime)
        if delta < 60:
            return f"{delta} seconds ago"
        if delta < 3600:
            return f"{delta // 60} minutes ago"
        if delta < 86400:
            return f"{delta // 3600} hours ago"
        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")

    async def _select_resume_choice(self, body: str, *, user_id: int, chat_id: int) -> str | None:
        """A digit reply after ``/resume`` switches sessions; anything else is untouched."""

        if not self._check_user_access(user_id):
            return None  # #1955: /resume is owner-only, so is its selection
        key = self._conversation_key(user_id, chat_id)
        session = await self._session_manager.get_session(key)
        resume_list = session.get("resume_list")
        if resume_list and self._claude_scoped_transcript_controls_disabled():
            await self._session_manager.patch_session(key, remove_fields={"resume_list"})
            return None
        if not resume_list:
            return None
        text = body.strip()
        if not text.isdigit():
            await self._session_manager.patch_session(key, remove_fields={"resume_list"})
            return None
        index = int(text) - 1
        if not 0 <= index < len(resume_list):
            return "❌ Invalid number, please try again."
        entry = resume_list[index]
        sid, label = str(entry[0]), str(entry[1])
        provider = str(entry[2]) if len(entry) > 2 else "claude"
        active = self._active_provider()
        if provider != active:
            return f"❌ Provider mismatch: selected session is {provider}, but the active provider is {active}."
        if sid != session.get("session_id"):
            await self._enqueue_previous_codex_session(
                session, DistillTrigger.EXPLICIT, user_id=user_id, chat_id=chat_id,
                discriminator=self._shutdown_distill_discriminator(session),
            )
        await self._session_manager.patch_session(
            key, updates={"provider": provider, "session_id": sid, "new_session": False},
            remove_fields={"resume_list"},
        )
        self._runtime_active_sessions.add(key)
        reply = f"✅ Switched to session: {label}"
        if provider == "claude":
            reader = getattr(self._project_chat, "get_session_last_assistant_message", None)
            last = await asyncio.to_thread(reader, sid) if callable(reader) else None
            if last:
                reply += f"\n\n📋 {last}"
        return reply

    async def _cmd_task_pause(self, args: list[str], *, user_id: int, chat_id: int) -> str:
        if args:
            return "Usage: /task_pause"
        if not self._danso_recovery_enabled():
            return "❌ Danso long-task mode is disabled."
        request_pause = getattr(self._project_chat, "request_danso_task_pause", None)
        status = await request_pause(user_id, chat_id) if callable(request_pause) else "unsupported"
        return {
            "requested": (
                "⏸ Graceful pause requested. Wait for the saved-checkpoint result, then use /task_resume."
            ),
            "not_active": "ℹ️ No active Danso long task is running.",
            "not_ready": (
                "⏳ The native task is still starting or finishing; retry /task_pause after its checkpoint heartbeat."
            ),
            "unsupported": "❌ Explicit Danso long-task pause is unavailable.",
        }.get(status, "❌ Explicit Danso long-task pause is unavailable.")

    async def _cmd_task_recover(self, *, user_id: int, chat_id: int) -> str:
        if not self._danso_recovery_enabled():
            return "❌ Danso long-task mode is disabled."
        shown = await self._offer_danso_recovery(self._conversation_key(user_id, chat_id), user_id, chat_id, force=True)
        if shown:
            return ""
        return "현재 복구할 작업이 없거나 실행 중이라 기록을 읽을 수 없습니다. 잠시 후 다시 확인해 주세요."

    async def _cmd_task_resume(self, *, user_id: int, chat_id: int, room_id: str, sink: TurnSink) -> Any:
        """``/task_resume``: explicit no-prompt resume of the stored Danso journal."""

        from telegram_bot.core.danso_worker import TASK_RESUME_CONTROL

        if not self._danso_recovery_enabled():
            return _turn_result("❌ Danso long-task mode is disabled.", None)
        if not self._check_user_access(user_id):
            return _turn_result("❌ Only the owner may resume a stored task.", None)
        key = self._conversation_key(user_id, chat_id)
        generation = self._task_resume_generation(key)
        session, session_id, new_session, _stale, _auto = await self._resolve_turn_session(
            key, user_id=user_id, chat_id=chat_id
        )
        if new_session or not session_id or session.get("provider", "claude") != "danso":
            return _turn_result(
                "📭 No paused Danso long task is stored for this conversation. "
                "Start one first, or use /new for a fresh session.",
                None,
            )
        route = self._danso_recovery_route(user_id, chat_id)

        def guard() -> bool:
            return (
                self._task_resume_generation(key) == generation
                and self._danso_recovery_route(user_id, chat_id) == route
            )

        response = await self._dispatch_turn(
            TASK_RESUME_CONTROL, key=key, user_id=user_id, chat_id=chat_id, session=session,
            session_id=session_id, new_session=False, resume_task=True, dispatch_guard=guard, sink=sink,
        )
        result = await self._finish(response, room_id)
        await self._offer_danso_recovery_if_failed(response, key, user_id, chat_id)
        return result

    # #1955: ``user_id`` reduces a non-owner Codex turn — "untrusted" approvals
    # (the sender-first callback then denies), no auto reviewer, and a
    # workspace sandbox without network. Reachable only off ``owner-operator``
    # (that profile refuses non-owner turns outright), where it keeps a family
    # turn off ``never + dangerFullAccess``. It limits writes and network, NOT
    # reads: this is not a confidentiality boundary (read scoping is #1960).
    # ``None`` keeps the owner-only callers (Danso recovery) on the configured
    # policy.
    def _narrow_for(self, user_id: int | None) -> bool:
        return user_id is not None and not self._check_user_access(user_id)

    def _codex_approval_policy(self, user_id: int | None = None) -> str:
        if self._narrow_for(user_id):
            return "untrusted"
        policy = self._bash_policy()
        if policy == tool_policy.BASH_AUTO_APPROVE:
            return "never"
        if policy == tool_policy.BASH_AUTO_REVIEW:
            return "on-request"
        return "untrusted"

    def _codex_approvals_reviewer(self, user_id: int | None = None) -> str | None:
        if self._narrow_for(user_id):
            return None
        return "auto_review" if self._bash_policy() == tool_policy.BASH_AUTO_REVIEW else None

    def _codex_sandbox_policy(self, user_id: int | None = None) -> dict[str, Any] | None:
        if self._narrow_for(user_id):
            return {"type": "workspaceWrite", "networkAccess": False}
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

    async def _cmd_distill(self, *, user_id: int, chat_id: int) -> str:
        key = self._conversation_key(user_id, chat_id)
        session = await self._session_manager.get_session(key)
        if session.get("provider") != self._active_provider() or not session.get("session_id"):
            return "ℹ️ No active session to save."
        # Same turn-local idempotence as shutdown; explicit is a separate trigger.
        job = await self._enqueue_previous_codex_session(
            session, DistillTrigger.EXPLICIT, user_id=user_id, chat_id=chat_id,
            discriminator=self._shutdown_distill_discriminator(session),
        )
        return ("✅ Memory save queued. Processing follows the configured memory policy and budget."
                if job is not None else "ℹ️ Memory saving is disabled or unavailable.")

    async def _cmd_new(self, *, user_id: int, chat_id: int) -> str:
        key = self._conversation_key(user_id, chat_id)
        # Telegram cancels the conversation's asyncio task here; the Matrix
        # transport owns its task, so interrupt through the handler instead.
        await self._cancel_turn(user_id, chat_id)
        session = await self._session_manager.get_session(key)
        await self._enqueue_previous_codex_session(
            session, DistillTrigger.NEW_COMMAND, user_id=user_id, chat_id=chat_id,
        )
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

    async def _cmd_skills(self, *, user_id: int, chat_id: int, room_id: str, sink: TurnSink, turn_marker: str | None = None) -> Any:
        if self._refuses_non_owner_turn(user_id):
            return _turn_result(NON_OWNER_TURN_REFUSED, None)
        key = self._conversation_key(user_id, chat_id)
        session = await self._session_manager.get_session(key)
        await self._enqueue_previous_codex_session(
            session, DistillTrigger.NEW_COMMAND, user_id=user_id, chat_id=chat_id,
        )
        response = await self._record_turn_health(self._project_chat.process_message(
            user_message=_SKILLS_PROMPT,
            user_id=user_id,
            chat_id=chat_id,
            new_session=True,
            approval_policy=self._codex_approval_policy(user_id),
            approvals_reviewer=self._codex_approvals_reviewer(user_id),
            sandbox_policy=self._codex_sandbox_policy(user_id),
            approval_callback=self._make_approval_callback(sink),
            typing_callback=sink.typing,
            status_callback=self._make_status_callback(sink),
            notification_bot=self._notification_bot(),
            interim_message_callback=self._make_interim_callback(sink, room_id),
            usage_mode=MODE_INTERACTIVE,
        ))
        await self._save_session_id(key, response, user_id=user_id, chat_id=chat_id)
        if getattr(response, "success", True):
            await self._record_codex_checkpoint(
                key, response, request_text=_SKILLS_PROMPT, turn_marker=turn_marker,
                user_id=user_id, chat_id=chat_id,
            )
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
            return await self._select_model(key, session, args[0], user_id=user_id, chat_id=chat_id)
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

    async def _select_model(self, key: Any, session: Mapping[str, Any], name: str, *, user_id: int, chat_id: int) -> str:
        provider = self._active_provider()
        if provider == "danso" and name != getattr(self._settings, "danso_model", None):
            return "❌ Danso uses the model configured by the operator. Use /model to view it."
        if provider == "piri" and not self._valid_piri_model_id(name):
            return "❌ Invalid Piri model id. Use a provider-qualified model id."
        updates: dict[str, Any] = {"provider": provider, "model": name}
        remove: set[str] = set()
        reset_note = None
        if session.get("provider") != provider:
            await self._enqueue_previous_codex_session(
                dict(session), DistillTrigger.PROVIDER_SWITCH, user_id=user_id, chat_id=chat_id,
            )
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
        previous = await self._session_manager.get_session(key)
        if previous.get("provider", "claude") != self._active_provider():
            await self._enqueue_previous_codex_session(
                previous, DistillTrigger.PROVIDER_SWITCH, user_id=user_id, chat_id=chat_id,
            )
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
