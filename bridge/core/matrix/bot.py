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
import functools
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import re
import secrets
import signal
import subprocess
import time
import tomllib
from typing import Any, Awaitable, Callable, Coroutine, Mapping, Optional, Protocol, Sequence

from telegram_bot.core import restart_handoff, session_resume, tool_policy
from telegram_bot.core.memory_distill import MemoryDistillMixin
from telegram_bot.memory.distill_types import DistillTrigger
from telegram_bot.core.agent_runtime import ApprovalDecision, ApprovalRequestEvent
from telegram_bot.core.approval_audit import ApprovalAuditLedger, ApprovalAuditRecord
from telegram_bot.core.approval_contract import (
    ApprovalDisplaySnapshot,
    build_approval_snapshot,
    opaque_ref,
)
from telegram_bot.core.bot_danso_recovery import (
    OFFER,
    RECOVERY_TEXT_ACTIONS,
    DansoRecoveryMixin,
)
from telegram_bot.core.continuation import STATE_RUNNING, ContinuationQueue
from telegram_bot.core.continuation import default_queue_path as continuation_queue_path
from telegram_bot.core.continuation_monitor import ContinuationMonitor
from telegram_bot.core.dead_session_recovery import (
    recover_dead_session_notifications,
    run_periodic_dead_session_recovery,
)
from telegram_bot.core.external_wait import (
    ExternalWaitRegistry,
    default_active_turns_path,
    default_registry_path,
    render_waits,
)
from telegram_bot.core.external_wait_monitor import ExternalWaitMonitor, GhCliTransport
from telegram_bot.core.lifecycle_loops import (
    run_health_alerts_probe,
    run_session_resource_guard,
    run_skill_candidate_collector,
)
from telegram_bot.core.matrix import lifecycle as matrix_lifecycle
from telegram_bot.core.matrix.render import chunk_text, render_matrix_message
from telegram_bot.core.matrix_ids import MatrixIdMap
from telegram_bot.core.memory_audience import resolve_memory_audience
from telegram_bot.core.project_chat_types import ChatResponse
from telegram_bot.core.push_notifier import (
    _DEDUP_WINDOW_SECONDS,
    _SENT_RETENTION_SECONDS,
    PushNotifier,
    fan_out_pending,
    mirror_dirs_from,
)
from telegram_bot.core.session_scope import storage_key
from telegram_bot.core.turn_notices import session_start_notice_text, session_start_reason
from telegram_bot.core.turn_watchdog import DEFAULT_NOTIFY_MINUTES, TurnAgeWatchdog
from telegram_bot.core.usage import UsageSnapshot, render_usage
from telegram_bot.core.usage_meter import MODE_AUTONOMOUS, MODE_INTERACTIVE
from telegram_bot.utils.health import health_reporter
from telegram_bot.utils.secure_fs import _fsync_directory
from telegram_bot.utils.orphan_reaper import (
    run_periodic_reaper,
    sweep_orphaned_claude_processes,
)

logger = logging.getLogger(__name__)

# serve()'s task-group legs: zero-argument coroutine factories (#1825).
_LegFactory = Callable[[], Coroutine[Any, Any, Any]]
IDS_FILENAME = "matrix-ids.json"
DIRECT_ROOMS_FILENAME = "matrix-direct-rooms.json"
SUPPORTED_COMMANDS = frozenset(
    {
        "new", "distill", "model", "effort", "usage", "skills", "stop", "continue",
        "task_pause", "task_resume", "task_recover", "history", "resume", "restart",
        "waits", "cancelwait", "memory_promote",
    }
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
# #1825: a registered next bundle (continuation_cli) started as a room turn.
SELF_JOB_CONTINUATION = "continuation"
# Slack on top of the turn ceiling for a continuation self-job that is still
# queued behind the room's earlier jobs when the monitor starts waiting on it.
_CONTINUATION_QUEUE_SLACK_S = 1800.0
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
    {
        "resume", "history", "model", "effort", "distill", "usage", "continue",
        "task_pause", "task_resume", "task_recover", "restart", "waits", "cancelwait",
        "memory_promote",
    }
)
# #2001: files an answer names are sent after it, at most this many per turn.
MAX_FILES_PER_TURN = 10
FILES_DIRECT_ONLY = "📎 답변에 파일 {count}개가 있지만, 파일은 개인 대화방에서만 보냅니다."
FILES_SKIPPED = "📎 파일 {count}개는 보내지 않았습니다(프로젝트 폴더 밖이거나 한 번에 보낼 수 있는 {limit}개 초과).".replace("{limit}", str(MAX_FILES_PER_TURN))
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
# #1959: room-sink approval outcome -> Telegram's audit reason / decision.
_APPROVAL_AUDIT_REASONS = {"allow": "owner_allow", "deny": "owner_deny", "timeout": "timeout"}
_APPROVAL_AUDIT_DECISIONS = {
    "owner_allow": "allow",
    "owner_deny": "deny",
    "timeout": "timeout",
}
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


_TRANSPORT_LOG_WITHHELD = "Matrix transport diagnostic (details withheld)"


class _TransportLogFilter(logging.Filter):
    """Matrix/HTTP client records may carry access tokens or bodies; withhold them (#1959).

    Same contract as the Grok frontends' filter: the record still flows (level
    and logger name stay visible), only its message, args and traceback go.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.split(".")[0] in {"nio", "aiohttp", "httpx", "httpcore"}:
            record.msg = _TRANSPORT_LOG_WITHHELD
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def _install_transport_log_filter() -> None:
    """Attach the filter to every root handler once (idempotent)."""

    for handler in logging.getLogger().handlers:
        if not any(isinstance(existing, _TransportLogFilter) for existing in handler.filters):
            handler.addFilter(_TransportLogFilter())


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

    def enqueue_notice(self, room_id: str, text: str, *, key: str | None = None) -> None: ...

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
            os.fsync(fd)  # #1959: durable before the rename publishes it
        finally:
            os.close(fd)
        os.replace(tmp, self._path)
        _fsync_directory(self._path.parent)

    def room_for(self, user: str) -> str | None:
        return self._rooms.get(user)


def spool_write_dir(settings: Any) -> Path:
    """The push spool this process's writers use (``CCC_PUSH_SPOOL`` or the default)."""

    return Path(
        getattr(settings, "push_spool_dir", None)
        or (Path.home() / ".claude" / "state" / "telegram-spool")
    )


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
    owner's direct room, falling back to the family room. To reach Telegram
    as well, set ``CCC_PUSH_MIRROR_DIRS`` here and point the telegram unit's
    ``CCC_PUSH_CONSUME_SPOOL`` (not ``CCC_PUSH_SPOOL``, which its own writers
    keep using) at that mirror dir (see ``push_notifier.fan_out_pending``).
    """

    def __init__(self, settings: Any, transport: Any) -> None:
        # ``transport`` is a MatrixTransport; imported lazily in _build_transport
        # (bot/transport import cycle), so the annotation stays Any here.
        self._transport = transport
        self.enabled: bool = bool(getattr(settings, "push_enabled", False))
        write_dir = spool_write_dir(settings)
        consume = getattr(settings, "push_consume_spool_dir", None)
        self.spool_dir = Path(consume).expanduser() if consume else write_dir
        # Where this process's own writers (health alerts) queue records.
        self.write_spool_dir = write_dir
        self.interval: float = float(getattr(settings, "push_poll_interval", 3.0))
        self.max_per_minute: int = int(getattr(settings, "push_max_per_minute", 10))
        self.mirror_dirs: list[Path] = mirror_dirs_from(settings, self.spool_dir, write_dir)
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
        logger.info(
            "Matrix spool notifier active → room %s, spool %s, fan-out %s",
            room,
            self.spool_dir,
            [str(d) for d in self.mirror_dirs] or "none",
        )
        while True:
            try:
                await self._drain(room, sent_dir)
            except Exception:
                logger.warning("Matrix spool drain error (continuing)", exc_info=True)
            await asyncio.sleep(self.interval)

    async def _drain(self, room: str, sent_dir: Path) -> None:
        ready = fan_out_pending(self.spool_dir, self.mirror_dirs) if self.mirror_dirs else None
        for p in sorted(self.spool_dir.glob("*.json")):
            if not p.is_file():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._archive(p, sent_dir)  # malformed → don't retry forever
                continue
            if not isinstance(data, dict):
                self._archive(p, sent_dir)  # #1959: valid JSON, not a record
                continue
            text = (data.get("text") or "").strip()
            if not text:
                self._archive(p, sent_dir)
                continue
            if ready is not None and p.name not in ready:
                return  # not mirrored yet; keep file, preserve order
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
        skill_candidate_collector_worker: Any = None,
        memory_promoter: Any = None,
        clock: Any = None,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self._skill_candidate_collector_worker = skill_candidate_collector_worker
        # #2004: explicit private -> shared fact promotion (audience-scoped only).
        self._memory_promoter = memory_promoter
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
        # #2001: event id of the turn being served, keying its outbound files.
        self._active_turn_id = ""
        self._config: Mapping[str, Any] | None = None
        self._ids: MatrixIdMap | None = None
        self._direct_rooms: _DirectRoomMap | None = None
        self._transport: Any = None
        # Same role as TelegramBot._runtime_active_sessions: conversation keys
        # whose persisted session id this process has already resumed/created.
        self._runtime_active_sessions: set[Any] = set()
        # #1825: continuation_id -> outcome of its self-job turn, awaited by the
        # continuation monitor's runner while this process serves.
        self._continuation_waiters: dict[str, asyncio.Future[bool]] = {}
        self._approval_audit_ledger: ApprovalAuditLedger | None = None

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
            await self._startup_lifecycle_sweeps()
            await self._startup_danso_recovery_scan()
            await self._startup_dead_session_recovery()
            notifier = MatrixSpoolNotifier(self._settings, transport)
            # Same TaskGroup semantics as transport.run(): a leg that dies
            # stops the service so systemd restarts it whole. Legs are built
            # before the listener binds, so a failing builder cannot leave the
            # socket open outside the ``finally`` that closes it.
            stop = asyncio.Event()
            cancel_on_stop, stop_driven = self._background_legs(stop, notifier)
            nudge_server = self._build_webhook_nudge_server()
            try:
                if nudge_server is not None:
                    # Bind failures log and degrade to polling; never fatal.
                    await nudge_server.start()
                async with asyncio.TaskGroup() as group:
                    # The watchdog/health loops run until the stop event is set,
                    # and a TaskGroup only cancels siblings when a leg raises — a
                    # transport that returns *cleanly* would otherwise leave the
                    # group waiting forever. Setting the event from the transport
                    # leg's finally keeps shutdown finite on both paths.
                    memory_tasks: list[asyncio.Task[Any]] = [
                        group.create_task(start(), name=name) for name, start in cancel_on_stop
                    ]
                    group.create_task(self._run_until_stop(transport.run(), stop, memory_tasks))
                    for name, start in stop_driven:
                        group.create_task(start(), name=name)
            except BaseExceptionGroup as failure:
                # A single failing leg (normally the transport) surfaces as itself,
                # as it did when the transport was awaited directly.
                if len(failure.exceptions) == 1:
                    raise failure.exceptions[0] from None
                raise
            finally:
                if nudge_server is not None:
                    await nudge_server.close()
        finally:
            if not initialize:
                await self._enqueue_shutdown_distills()
            self._transport = None
            self._stop_health_reporting()
            await transport.close()

    def _background_legs(
        self, stop: asyncio.Event, notifier: "MatrixSpoolNotifier"
    ) -> tuple[list[tuple[str, _LegFactory]], list[tuple[str, _LegFactory]]]:
        """``(cancel_on_stop, stop_driven)`` legs for ``serve()``'s task group.

        *Stop-driven* legs watch ``stop`` and return on their own. Legs that
        may be parked where the event is not seen — a distill or collector
        sweep awaiting a provider call, the continuation runner awaiting a
        self-job turn shutdown will never finish, the reaper's sleep — are
        cancelled when the transport leg ends instead. Each entry is
        ``(task name, zero-argument coroutine factory)``.
        """

        cancel_on_stop: list[tuple[str, _LegFactory]] = []
        stop_driven: list[tuple[str, _LegFactory]] = []
        if self._distill_journal is not None:
            for stage in ("snapshot", "extraction", "local_sink", "wiki_sink"):
                if getattr(self, f"_distill_{stage}_worker") is not None:
                    loop = getattr(self, f"_distill_{stage}_loop")
                    cancel_on_stop.append((f"matrix-distill-{stage}", functools.partial(loop, stop)))
            collector = self._skill_candidate_collector_worker
            if collector is not None:
                cancel_on_stop.append(
                    (
                        "matrix-skill-candidate-collector",
                        lambda: run_skill_candidate_collector(collector, self._distill_sweep_jobs, self._settings, stop),
                    )
                )
        continuation = self._build_continuation_monitor()
        if continuation is not None:
            # The durable self-job records its own outcome after a restart (#1825).
            cancel_on_stop.append(("matrix-continuation-monitor", lambda: continuation.run(stop)))
        if self._orphan_reaper_enabled():
            cancel_on_stop.append(("matrix-orphan-reaper", lambda: self._periodic_reaper()))
        if notifier.enabled:
            # ``MatrixSpoolNotifier.run`` polls forever without watching
            # ``stop``; as a stop-driven leg it held the group open after a
            # clean transport return.
            cancel_on_stop.append(("matrix-spool-notifier", notifier.run))

        stop_driven.append(("matrix-health-reporter", lambda: self._health_reporter_loop(stop)))
        watchdog = self._build_turn_age_watchdog()
        if watchdog is not None:
            stop_driven.append(("matrix-turn-age-watchdog", lambda: watchdog.run(stop)))
        external_wait = self._build_external_wait_monitor()
        if external_wait is not None:
            stop_driven.append(("matrix-external-wait-monitor", lambda: external_wait.run(stop)))
        stop_driven.append(("matrix-dead-session-recovery", lambda: self._periodic_dead_session_recovery(stop)))
        stop_driven.append(
            (
                "matrix-health-alerts",
                lambda: run_health_alerts_probe(
                    self._settings,
                    self._project_chat,
                    stop,
                    spool_dir=notifier.spool_dir,
                    write_spool_dir=notifier.write_spool_dir,
                ),
            )
        )
        if getattr(self._settings, "session_guard_enabled", False):
            stop_driven.append(
                (
                    "matrix-session-resource-guard",
                    lambda: run_session_resource_guard(self._settings, self._project_chat, stop),
                )
            )
        stall_probe = self._build_turn_stall_probe()
        if stall_probe is not None:
            stop_driven.append(("matrix-turn-stall-probe", lambda: stall_probe.run(stop)))
        if str(getattr(self._settings, "restart_handoff", "off")) == "systemd":
            stop_driven.append(("matrix-restart-receipt", lambda: self._restart_receipt_loop(stop)))
        return cancel_on_stop, stop_driven

    async def _restart_receipt_loop(self, stop: asyncio.Event) -> None:
        """Deliver a terminal restart receipt to the owner's direct room (#2003).

        Mirrors ``bot_lifecycle._restart_receipt_loop``: poll ``read_receipt``
        every 2s; on a terminal state send ``✅``/``❌`` to the chat that asked
        for the restart and archive the receipt so the next request can start.
        A room the id map cannot reverse (or a delivery error) keeps the
        receipt pending for the next poll; the ``request_id`` enqueue key
        keeps a retry from queueing the notice twice.
        """

        while not stop.is_set():
            try:
                receipt = await asyncio.to_thread(restart_handoff.read_receipt, self._data_dir())
                if receipt and receipt.get("state") in restart_handoff.TERMINAL_STATES:
                    request_id = str(receipt.get("request_id", ""))
                    if receipt["state"] == "completed":
                        text = (
                            f"✅ Bridge restart completed ({request_id[:8]}). "
                            f"New PID: {receipt.get('new_pid', 'unknown')}."
                        )
                    else:
                        text = (
                            f"❌ Bridge restart failed ({request_id[:8]}): "
                            f"{receipt.get('reason_code', 'worker_error')}."
                        )
                    delivered = self._deliver_notice(
                        int(receipt["chat_id"]), text, key=f"restart-receipt-{request_id}"
                    )
                    if delivered:
                        await asyncio.to_thread(
                            restart_handoff.archive_receipt, self._data_dir(), request_id
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Restart receipt delivery deferred: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass

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

        _install_transport_log_filter()
        # The transport refuses a crypto store with group/other-readable files
        # (unsafe-crypto-store); nio creates its SQLite store with the process
        # umask, which systemd leaves at 022. The pilot set this in main().
        os.umask(0o077)
        # Account before the start-up checks, so a unit that dies in them is
        # backed off and alerted like one that dies while serving.
        budget = self._crash_budget()
        decision = budget.begin() if budget is not None else None
        try:
            enforce_access_control(self._settings)
            initialize = getattr(self._session_manager, "initialize", None)
            if callable(initialize):
                initialize()
        except BaseException as error:
            if budget is not None:
                budget.record_error(error)
            raise

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
                await self._crash_backoff(decision)
                await self.serve()
            except asyncio.CancelledError:
                logger.info("Matrix frontend stopped")
            except BaseException as error:
                if budget is not None:
                    budget.record_error(error)
                raise
            # Reached only on an orderly stop (signal) or a clean return.
            if budget is not None:
                budget.mark_clean()

        asyncio.run(_main())

    def _crash_budget(self) -> "matrix_lifecycle.CrashBudget | None":
        try:
            return matrix_lifecycle.CrashBudget(self._data_dir() / matrix_lifecycle.CRASH_BUDGET_FILENAME)
        except Exception as error:
            logger.warning("Matrix crash budget unavailable: %s", type(error).__name__)
            return None

    async def _crash_backoff(self, decision: "matrix_lifecycle.CrashDecision | None") -> None:
        """Delay this start after a streak of rapid unclean exits (Matrix rapid-crash guard).

        systemd restarts the unit every ``RestartSec`` forever; this is the
        back-off the Telegram ``start.sh`` supervisor provides, with the same
        ``crash-policy.env`` numbers. One owner alert per streak rides the push
        spool (delivered once the spool consumer is up; ``CCC_PUSH_ENABLED``).
        """

        if decision is None:
            return
        if decision.streak:
            logger.warning(
                "Matrix frontend: %d rapid unclean exit(s) in a row (last: %s); delaying start %ds",
                decision.streak,
                decision.last_error or "unknown",
                int(decision.delay_seconds),
            )
        if decision.alert:
            alert = matrix_lifecycle.crash_loop_alert(decision)
            logger.error("Health alert [%s]: %s", alert.code, alert.message)
            if getattr(self._settings, "push_enabled", False):
                from telegram_bot.utils.health_alerts import write_alert_spool

                write_alert_spool(spool_write_dir(self._settings), alert)
        if decision.delay_seconds > 0:
            await asyncio.sleep(decision.delay_seconds)

    # -- outbound routing ----------------------------------------------------

    def room_for_chat(self, chat_id: int) -> str | None:
        """Reverse map a handler ``chat_id`` to a Matrix room, ``None`` if unknown."""

        matrix_id = self.ids.matrix_id(chat_id)
        if matrix_id is None:
            return None
        if matrix_id.startswith("!"):
            return matrix_id
        return self._direct_room_map().room_for(matrix_id)

    def _deliver_notice(self, chat_id: int, text: str, *, key: str | None = None) -> bool:
        transport = self._transport
        room = self.room_for_chat(chat_id)
        if transport is None or room is None:
            return False
        if key is None:
            transport.enqueue_notice(room, text)
        else:
            try:
                transport.enqueue_notice(room, text, key=key)
            except TypeError:  # transport without the key parameter
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
        registry = self._external_wait_registry()
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

    # -- yield-and-continue (#1113 / #1825) -----------------------------------

    def _continuation_queue(self) -> ContinuationQueue:
        """This frontend's continuation queue.

        The agent-side ``continuation_cli`` resolves its home as the sibling of
        ``CCC_EXTERNAL_WAIT_HOME`` (``bot_data_dir/external-wait``), so a Matrix
        registration lands in ``bot_data_dir/continuation`` -- the directory
        this monitor reads.
        """

        return ContinuationQueue(continuation_queue_path(self._data_dir() / "continuation"))

    def _build_continuation_monitor(self) -> ContinuationMonitor | None:
        """Yield-and-continue loop for the Matrix frontend (#1825); ``None`` when opted out.

        Telegram builds this in ``BotLifecycleMixin``, which ``MatrixBot`` does
        not inherit: a Matrix ``continuation_cli register`` answered ``ok`` and
        wrote the record while nothing ever read it, so the promised next
        bundle silently never started. Same contract and flags as Telegram
        (``CCC_CONTINUATION_ENABLED`` default on, ``CCC_CONTINUATION_DAILY_CAP``
        default 20, three consecutive failures park the chain); the bundle
        runs as a **self-job** turn in the conversation's room, like the
        external-wait resume (#1934).
        """

        if not ExternalWaitMonitor.env_flag("CCC_CONTINUATION_ENABLED", default=True):
            logger.info("Matrix continuation monitor disabled (CCC_CONTINUATION_ENABLED=0)")
            return None
        return ContinuationMonitor(
            self._continuation_queue(),
            runner=self._run_continuation,
            notifier=self._notify_chat,
            active_turns_path=default_active_turns_path(self._data_dir() / "external-wait"),
            daily_cap=ExternalWaitMonitor.env_int("CCC_CONTINUATION_DAILY_CAP", default=20),
        )

    def _continuation_wait_seconds(self) -> float:
        """Upper bound the runner waits for its self-job turn to finish."""

        try:
            minutes = float(self.load_config().get("turn_timeout_minutes", 360))
        except Exception:
            minutes = 360.0
        return max(5.0, min(minutes, 360.0)) * 60.0 + _CONTINUATION_QUEUE_SLACK_S

    async def _run_continuation(self, record: Mapping[str, Any], prompt: str) -> bool:
        """``ContinuationMonitor`` runner: enqueue the bundle, await its turn outcome.

        Telegram's runner awaits ``process_message`` inline, so a failed turn
        counts toward the consecutive-failure guard. The Matrix turn runs as a
        durable self-job instead (room serialization, sink, outbox), so the
        runner waits on a future the self-job resolves when the turn ends.
        ``False`` (not enqueued, refused, failed or not finished within the
        turn ceiling) is a failed bundle, exactly as on Telegram.
        """

        cid = str(record.get("continuation_id") or "")
        transport = self._transport
        room = self.room_for_chat(int(record.get("chat_id") or 0))
        enqueue = getattr(transport, "enqueue_self_job", None)
        if not cid or transport is None or room is None or not callable(enqueue):
            logger.info(
                "Matrix continuation deferred: no transport/room for chat %s", record.get("chat_id")
            )
            return False
        # #1955: the bundle runs as the person whose turn registered it, never
        # silently as the owner; a no-longer-admitted requester is refused.
        raw_user = record.get("user_id")
        user_int = raw_user if isinstance(raw_user, int) and not isinstance(raw_user, bool) else None
        sender = self.ids.matrix_id(user_int) if user_int is not None else None
        if user_int is None or sender is None or not self._is_admitted_sender(sender):
            logger.info(
                "Matrix continuation refused: requester not admitted for chat %s", record.get("chat_id")
            )
            return False
        body = json.dumps(
            {
                "kind": SELF_JOB_CONTINUATION,
                "v": 1,
                "continuation_id": cid,
                "user_id": user_int,
                "prompt": prompt,
            }
        )
        extra: dict[str, Any] = {} if sender == self.load_config().get("owner") else {"sender": sender}
        waiter: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._continuation_waiters[cid] = waiter
        try:
            try:
                enqueue(room, body, key=f"{SELF_JOB_CONTINUATION}:{cid}", **extra)
            except Exception as error:
                logger.warning("Matrix continuation self-job enqueue failed: %s", type(error).__name__)
                return False
            try:
                return bool(
                    await asyncio.wait_for(asyncio.shield(waiter), timeout=self._continuation_wait_seconds())
                )
            except asyncio.TimeoutError:
                logger.warning("Matrix continuation turn did not finish within its ceiling: %s", cid)
                return False
        finally:
            self._continuation_waiters.pop(cid, None)

    def _settle_continuation(self, cid: str, ok: bool) -> None:
        """Hand a continuation turn's outcome to its waiting runner.

        With no runner waiting (the process restarted after the enqueue, or
        the runner gave up) the self-job records the outcome itself; both
        transitions are no-ops unless the record is still ``running`` (a
        ``/stop`` has already cancelled it, or the runner already marked it).
        """

        if not cid:
            return
        waiter = self._continuation_waiters.get(cid)
        if waiter is not None and not waiter.done():
            waiter.set_result(bool(ok))
            return
        try:
            queue = self._continuation_queue()
            if ok:
                queue.mark_done(cid)
            else:
                queue.mark_failed(cid, "turn_failed")
        except Exception:
            logger.warning("Matrix continuation outcome not recorded: %s", cid, exc_info=True)

    async def _run_continuation_job(
        self,
        payload: Mapping[str, Any],
        *,
        user_id: int,
        chat_id: int,
        room_id: str,
        sink: TurnSink,
        turn_marker: str | None,
    ) -> Any:
        """The continuation self-job: the bundle prompt as an autonomous room turn."""

        cid = str(payload.get("continuation_id") or "")
        prompt = str(payload.get("prompt") or "")
        ok = False
        try:
            record = self._continuation_queue().get(cid) if cid else None
            if record is None or record.get("state") != STATE_RUNNING:
                # /stop (or a newer registration) cancelled the bundle while its
                # self-job was still queued behind other room jobs.
                logger.info("Matrix continuation self-job skipped: %s is no longer running", cid)
                return _turn_result("", None, streamed=True)
            if not prompt.strip():
                logger.warning("Matrix continuation self-job has no prompt; ignored")
                return _turn_result("", None, streamed=True)
            outcome: list[Any] = []
            result = await self._run_message(
                prompt,
                user_id=user_id,
                chat_id=chat_id,
                room_id=room_id,
                sink=sink,
                turn_marker=turn_marker,
                usage_mode=MODE_AUTONOMOUS,
                responses=outcome,
            )
            ok = bool(outcome) and bool(getattr(outcome[-1], "success", False))
            return result
        finally:
            # Cancellation (/stop, turn timeout, shutdown) ends here too.
            self._settle_continuation(cid, ok)

    async def _cmd_continue(self, *, user_id: int, chat_id: int) -> str:
        """Owner confirmation to keep auto-continuing past the daily tripwire."""

        repended = self._continuation_queue().repend_cap_holds(user_id, chat_id)
        if repended:
            return f"▶️ Resumed {len(repended)} queued continuation(s) — auto-continue re-armed for today."
        return "ℹ️ No continuations waiting on the daily cap."

    # -- /waits and /cancelwait (#2004) -----------------------------------------

    def _external_wait_registry(self) -> ExternalWaitRegistry:
        """The registry the Matrix monitor polls (``bot_data_dir/external-wait``, #1934)."""

        return ExternalWaitRegistry(default_registry_path(self._data_dir() / "external-wait"))

    @staticmethod
    def _wait_is_requesters(record: Mapping[str, Any], user_id: int) -> bool:
        # Same visibility as Telegram's /waits; legacy records carry no user_id.
        return record.get("user_id") in (None, user_id)

    async def _cmd_waits(self, *, user_id: int) -> str:
        """List this requester's active and recent external waits, read-only."""

        records = [rec for rec in self._external_wait_registry().records() if self._wait_is_requesters(rec, user_id)]
        return render_waits(records)

    async def _cmd_cancelwait(self, args: list[str], *, user_id: int) -> str:
        """Cancel exactly one of this requester's external waits by ``wait_id``.

        Tighter than Telegram's form (any id): a wait registered by someone
        else is reported as not found rather than cancelled.
        """

        wait_id = args[0].strip() if args else ""
        if not wait_id:
            return "Usage: /cancelwait <wait_id> — see /waits"
        registry = self._external_wait_registry()
        record = registry.get(wait_id)
        if record is None or not self._wait_is_requesters(record, user_id) or not registry.cancel(wait_id):
            return f"No active external wait with id `{wait_id}`."
        return f"Cancelled external wait `{wait_id}`."

    # -- /memory_promote (#2004) -----------------------------------------------

    async def _cmd_memory_promote(self, args: list[str], *, user_id: int, chat_id: int) -> str:
        """Promote one validated private fact to shared memory (port of Telegram's).

        Same contract as ``BotCommandMixin._cmd_memory_promote``: audience-scoped
        mode with a promoter and local sink wired, the owner's private DM only
        (the private scope is this frontend's own ``matrix`` route), one exact
        ``distill-<12 hex>`` id, and body-free logs.
        """

        if (
            getattr(self._settings, "bridge_memory_mode", "off") != "audience-scoped"
            or self._memory_promoter is None
            or self._distill_local_sink_worker is None
        ):
            return "ℹ️ Explicit memory promotion is unavailable on this bridge."
        audience = resolve_memory_audience(
            self._settings, user_id=user_id, chat_id=chat_id, route=self._memory_route()
        )
        if audience is None or audience.kind != "private":
            return "❌ Memory promotion is allowed only from your private DM."
        if len(args) != 1 or re.fullmatch(r"distill-[0-9a-f]{12}", args[0]) is None:
            return "Usage: /memory_promote distill-<12 lowercase hex>"

        fact_id = args[0]
        try:
            result = await asyncio.to_thread(
                self._memory_promoter.promote,
                source_scope=audience.scope,
                fact_id=fact_id,
            )
            await self._distill_local_sink_worker.refresh_route(audience="shared", scope="shared")
        except LookupError:
            return "ℹ️ That fact was not found in your private memory."
        except ValueError:
            logger.warning("Private memory promotion rejected by validation")
            return "⚠️ That private fact is not eligible for promotion."
        except Exception:
            logger.warning("Private memory promotion or shared index refresh failed")
            return "⚠️ Memory promotion could not be completed. You can retry safely."

        if result.promoted:
            return f"✅ Promoted {fact_id} to shared memory as {result.destination_fact_id}."
        return (
            f"✅ {fact_id} was already promoted as "
            f"{result.destination_fact_id}; shared memory was refreshed."
        )

    # -- dead-session recovery (#1825) ------------------------------------------

    def _dead_session_recovery_args(self) -> tuple[Any, ...]:
        # Delivery rides the durable outbox through the same bot-shaped port
        # the Danso recovery offers use; the marker is written only after the
        # notice was accepted into the outbox (at-least-once, as on Telegram).
        return (
            _NoticeBotPort(self),
            self._session_manager,
            self._project_chat,
            getattr(self._project_chat, "conversations_dir", None),
        )

    async def _startup_dead_session_recovery(self) -> None:
        """Deliver terminal Claude task notices a dead session left behind (#1825).

        Same scanner as the Telegram startup pass. The opt-in dead-session
        *wakeup* is not run by this frontend, so recovery never defers to it
        (``wakeup_defer=None``): notices are always delivered raw.
        """

        try:
            stats = await recover_dead_session_notifications(
                *self._dead_session_recovery_args(),
                max_delivery_attempts_per_scan=3,
                send_timeout=5.0,
            )
        except Exception as error:
            logger.warning("Matrix dead-session recovery failed at startup: %s", type(error).__name__)
            return
        self._record_recovery_stats(stats)
        if stats.delivered or stats.failed or stats.rejected:
            logger.info(
                "Matrix dead-session recovery: scanned=%d delivered=%d duplicate=%d failed=%d "
                "rejected=%d quarantined=%d locked=%d",
                stats.scanned,
                stats.delivered,
                stats.duplicate,
                stats.failed,
                stats.rejected,
                stats.quarantined,
                stats.skipped_locked,
            )

    async def _periodic_dead_session_recovery(self, stop: asyncio.Event) -> None:
        """Periodic scan (``CCC_DEAD_SESSION_RECOVERY_INTERVAL_SECONDS``) until stop."""

        await run_periodic_dead_session_recovery(
            *self._dead_session_recovery_args(),
            stop,
            on_stats=self._record_recovery_stats,
        )

    def _record_recovery_stats(self, stats: Any) -> None:
        """Surface quarantine counters in health.json (fail-open), as Telegram does."""

        if not self._health_active:
            return
        try:
            if getattr(stats, "quarantined", 0):
                health_reporter.record_transcript_quarantined(stats.quarantined)
            if getattr(stats, "hard_quarantined", 0):
                health_reporter.record_transcript_hard_quarantined(stats.hard_quarantined)
        except Exception as error:
            logger.debug("Matrix recovery stats health recording failed: %s", type(error).__name__)

    # -- Matrix-native background services (#1825, rest of #1998) -------------

    # Seams: the marker-scoped reaper reads /proc and signals real PIDs.
    _orphan_sweep = staticmethod(sweep_orphaned_claude_processes)
    _periodic_reaper = staticmethod(run_periodic_reaper)

    @staticmethod
    def _orphan_reaper_enabled() -> bool:
        """``CCC_MATRIX_ORPHAN_REAPER`` (default on) — a per-unit kill switch."""

        return ExternalWaitMonitor.env_flag("CCC_MATRIX_ORPHAN_REAPER", default=True)

    async def _startup_lifecycle_sweeps(self) -> None:
        """One-shot startup cleanups of what a previous process left behind.

        * task ledger: non-terminal records died with the previous process
          (the transport has already told their rooms; see ``lifecycle``);
        * orphan reaper: bridge-marked ``node claude`` children reparented to
          PID 1 by a previous run. Both are best-effort and never fatal.
        """

        try:
            interrupted = await asyncio.to_thread(
                matrix_lifecycle.reconcile_task_ledger, self._settings
            )
            if interrupted:
                logger.info(
                    "Matrix task ledger reconciliation: %d task(s) from a previous run marked interrupted",
                    interrupted,
                )
        except Exception as error:
            logger.warning("Matrix task ledger reconciliation failed: %s", type(error).__name__)
        if not self._orphan_reaper_enabled():
            return
        try:
            killed = await asyncio.to_thread(self._orphan_sweep)
            if killed:
                logger.info(
                    "Matrix startup orphan sweep: signalled %d orphan node-claude process(es) — PIDs %s",
                    len(killed),
                    killed,
                )
        except Exception as error:
            logger.warning("Matrix startup orphan sweep failed: %s", type(error).__name__)

    async def _recover_for_stall_probe(self) -> None:
        await recover_dead_session_notifications(*self._dead_session_recovery_args())

    def _build_turn_stall_probe(self) -> Any:
        """Silent-death stall probe (#1112); ``None`` unless ``CCC_TURN_STALL_PROBE_MIN`` > 0."""

        return matrix_lifecycle.build_turn_stall_probe(
            self._project_chat,
            notifier=self._notify_chat,
            recover=self._recover_for_stall_probe,
        )

    def _build_webhook_nudge_server(self) -> Any:
        """External-wait nudge listener on this frontend's own port; ``None`` when off."""

        try:
            return matrix_lifecycle.build_webhook_nudge_server(self._data_dir())
        except Exception as error:
            logger.warning("Matrix webhook nudge not started: %s", type(error).__name__)
            return None

    def _notification_bot(self) -> _NotificationRoute:
        return _NotificationRoute(self._deliver_notice)

    def _supports_formatted(self) -> bool:
        """Whether the transport offers a direct ``send_formatted``.

        The production :class:`MatrixTransport` deliberately does not (#1957),
        so this is ``False`` in production and replies, interims and recovery
        texts all go through the durable outbox, which already renders
        markdown to ``formatted_body`` at send time. A direct send would
        bypass what the outbox guarantees: crash-safe idempotent delivery by
        part, the encrypted-event size budget (#1956) and the quarantine of
        parts the homeserver rejects. Only test fakes implement it; removing
        this seam (and its three call sites) is a follow-up.
        """
        return callable(getattr(self._transport, "send_formatted", None))

    async def _send_formatted(self, room_id: str, text: str) -> bool:
        """Deliver rendered HTML chunks; ``False`` leaves delivery to the plain path.

        Always ``False`` against the real transport (see
        :meth:`_supports_formatted`): the "plain path" is the outbox, which
        formats too.
        """

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

    def _family_room_ids(self) -> set[str]:
        try:
            return {str(room) for room in (self.load_config().get("family_rooms") or ())}
        except Exception:  # noqa: BLE001 - unknown config: treat every room as shared
            return {"*"}

    def _progress_callbacks(self, sink: TurnSink, room_id: str) -> dict[str, Any]:
        """Status, interim and (opt-in) live-preview callbacks for one ``process_message``.

        With ``CCC_MATRIX_STREAMING`` on (#1796, default off like Telegram's
        ``CCC_TELEGRAM_STREAMING``), the answer grows in the turn's progress
        bubble; heartbeat texts are held back while the preview shows, and
        completed intermediate messages go out through the same interim path.
        """

        interim = self._make_interim_callback(sink, room_id)
        # Direct rooms only: in a family room, other people's messages bury the
        # bubble, and every repost (redact + new message) notifies the family.
        family = self._family_room_ids()
        direct = bool(room_id) and "*" not in family and room_id not in family
        if not direct or not ExternalWaitMonitor.env_flag("CCC_MATRIX_STREAMING", default=False):
            return {"status_callback": self._make_status_callback(sink), "interim_message_callback": interim}
        from telegram_bot.core.matrix.streaming import MatrixAnswerPreview, interval_from

        preview = MatrixAnswerPreview(
            sink,
            interim,
            interval_s=interval_from(os.environ.get("CCC_MATRIX_DRAFT_EDIT_INTERVAL_S", "")),
        )
        return {
            "status_callback": self._make_status_callback(sink, preview=preview),
            "interim_message_callback": interim,
            "streaming_sink": preview,
        }

    def _make_status_callback(
        self, sink: TurnSink, *, preview: Any = None
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
            if preview is not None and preview.holds_heartbeat():
                # The bubble shows a live answer preview (#1796); a heartbeat
                # would overwrite it. A preview that stopped moving yields the
                # bubble so elapsed time / stall warnings still show. The
                # handle keeps the cleanup path armed.
                return _STATUS_HANDLE
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
            # #1959: the room sees only the provider-neutral redacted snapshot
            # (as Telegram), never the raw provider arguments.
            snapshot = build_approval_snapshot(event, reply_hint=None)
            asked = self._record_approval_asked(snapshot, event, user_id, chat_id, generation)
            try:
                outcome = await self._ask_approval(sink, snapshot.prompt_text)
            except asyncio.CancelledError:
                self._record_approval_answered(asked, "cancelled", actor_user_id=None)
                raise
            except Exception:
                logger.exception("Matrix approval prompt failed; denying")
                self._record_approval_answered(asked, "send_failure", actor_user_id=None)
                return ApprovalDecision.DENY
            reason = _APPROVAL_AUDIT_REASONS.get(outcome, "send_failure")
            self._record_approval_answered(
                asked, reason, actor_user_id=user_id if outcome in ("allow", "deny") else None
            )
            return ApprovalDecision.ALLOW if outcome == "allow" else ApprovalDecision.DENY

        return approval_callback

    @staticmethod
    async def _ask_approval(sink: TurnSink, text: str) -> str:
        """``allow``/``deny``/``timeout``/``unavailable`` from the room sink."""

        ask = getattr(sink, "approval_outcome", None)
        if callable(ask):
            return str(await ask(text))
        return "allow" if await sink.approval(text, None) is True else "deny"

    # -- approval audit (#1959) ------------------------------------------------

    def _approval_ledger(self) -> ApprovalAuditLedger | None:
        if self._approval_audit_ledger is None:
            try:
                self._approval_audit_ledger = ApprovalAuditLedger(self._data_dir() / "approval-audit")
            except Exception as error:
                logger.warning("Matrix approval audit unavailable: %s", type(error).__name__)
                return None
        return self._approval_audit_ledger

    @staticmethod
    def _audit_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _record_approval_asked(
        self,
        snapshot: ApprovalDisplaySnapshot,
        event: ApprovalRequestEvent,
        user_id: int,
        chat_id: int,
        generation: int,
    ) -> dict[str, Any]:
        """Body-free ``asked`` record, same schema as Telegram; fail-open."""

        key = self._conversation_key(user_id, chat_id)
        asked: dict[str, Any] = {
            "snapshot": snapshot,
            "approval_ref": opaque_ref("approval", secrets.token_urlsafe(16)),
            "session_ref": opaque_ref("approval-session", key),
            "turn_ref": opaque_ref("approval-turn", key, generation),
            "request_ref": opaque_ref("approval-request", event.request_id),
            "asked_at": self._audit_now(),
            "asked_monotonic": time.monotonic(),
        }
        self._write_approval_audit(asked, event="asked")
        return asked

    def _record_approval_answered(
        self, asked: Mapping[str, Any], reason: str, *, actor_user_id: int | None
    ) -> None:
        decision = _APPROVAL_AUDIT_DECISIONS.get(reason, "invalidated")
        self._write_approval_audit(
            asked,
            event="answered",
            answered_at=self._audit_now(),
            decision=decision,
            reason=reason,
            latency_ms=max(0, round((time.monotonic() - float(asked["asked_monotonic"])) * 1000)),
            actor_ref=(
                opaque_ref("approval-actor", actor_user_id) if actor_user_id is not None else None
            ),
        )

    def _write_approval_audit(self, asked: Mapping[str, Any], *, event: str, **terminal: Any) -> None:
        ledger = self._approval_ledger()
        if ledger is None:
            return
        snapshot: ApprovalDisplaySnapshot = asked["snapshot"]
        try:
            ledger.record(
                ApprovalAuditRecord(
                    event=event,
                    approval_ref=asked["approval_ref"],
                    provider=snapshot.provider,
                    action=snapshot.action,
                    target_shape=snapshot.target_shape,
                    session_ref=asked["session_ref"],
                    turn_ref=asked["turn_ref"],
                    request_ref=asked["request_ref"],
                    actor_ref=terminal.pop("actor_ref", None),
                    request_fingerprint=snapshot.request_fingerprint,
                    display_fingerprint=snapshot.display_fingerprint,
                    asked_at=asked["asked_at"],
                    redaction_flags=snapshot.redaction_flags,
                    displayed_fields=snapshot.displayed_fields,
                    **terminal,
                )
            )
        except Exception as error:
            logger.warning("Matrix approval audit %s record failed (continuing): %s", event, type(error).__name__)

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
        self._active_turn_id = str(job.get("event_id") or "")
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
            if command == "restart":
                return _turn_result(
                    await self._cmd_restart(user_id=user_id, chat_id=chat_id, room_kind=room_kind),
                    None,
                )
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
            self._active_turn_id = ""

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

    def _control_identity(self, job: Mapping[str, Any]) -> tuple[int, int]:
        room_id = str(job["room_id"])
        sender = str(job["sender"])
        transport = self._transport
        if transport is not None:
            kind = transport.room_kind(room_id)
        else:
            kind = "direct" if self._direct_room_map().room_for(sender) == room_id else "family"
        user_id, chat_id, _ = self._job_identity(job, kind)
        return user_id, chat_id

    async def stop_idle(self, job: Mapping[str, Any]) -> bool:
        """``/stop`` with no turn running in the sender's scope (#1825).

        Cancels that conversation's queued continuations; ``False`` (nothing
        queued) leaves the transport's ordinary invalid-control answer.
        """

        user_id, chat_id = self._control_identity(job)
        return bool(self._cancel_continuations(user_id, chat_id))

    async def cancel(self, job: Mapping[str, Any]) -> bool:
        """``TurnRunner.cancel``: same handler path as ``/stop``."""

        user_id, chat_id = self._control_identity(job)
        # Only a user control (/stop, /cancel <turn>) reaches this runner seam;
        # the turn timeout cancels the task directly.
        self._cancel_continuations(user_id, chat_id)
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
        if command == "continue":
            return await self._cmd_continue(user_id=user_id, chat_id=chat_id)
        if command == "waits":
            return await self._cmd_waits(user_id=user_id)
        if command == "cancelwait":
            return await self._cmd_cancelwait(args, user_id=user_id)
        if command == "memory_promote":
            return await self._cmd_memory_promote(args, user_id=user_id, chat_id=chat_id)
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
        self._enqueue_deliverables(room_id, content)
        return _turn_result(content, response.session_id, streamed=streamed, status=status)

    def _enqueue_deliverables(self, room_id: str, content: str) -> None:
        """Queue the files an answer names for the room, after the answer (#2001).

        Same rule as Telegram (``core/deliverables.py``): a real file with a
        deliverable extension, under the size cap, inside ``PROJECT_ROOT``.
        Files go to *direct* rooms only — a family room gets one notice
        instead, so an owner-host file never lands where others read. Files
        outside the project root are not sent (Telegram asks with a button;
        Matrix has none) and the room is told how many. Never raises.
        ``CCC_MATRIX_SEND_FILES=0`` turns it off.
        """

        transport = self._transport
        enqueue = getattr(transport, "enqueue_file", None)
        if not content.strip() or transport is None or not callable(enqueue):
            return
        if not ExternalWaitMonitor.env_flag("CCC_MATRIX_SEND_FILES", default=True):
            return
        try:
            from telegram_bot.core import deliverables
            from telegram_bot.core import paths as path_scope
            from telegram_bot.core.matrix.transport import MAX_OUTBOUND_FILE_BYTES

            root = Path(str(getattr(self._settings, "project_root", "") or ".")).resolve()
            found = deliverables.resolve_deliverable_paths(content, root, max_bytes=MAX_OUTBOUND_FILE_BYTES)
            if not found:
                return
            inside, outside = path_scope.split_paths_by_scope(found, root)
            turn = self._active_turn_id or hashlib.sha256(content.encode()).hexdigest()[:24]
            if transport.room_kind(room_id) != "direct":
                self._deliverable_notice(transport, room_id, FILES_DIRECT_ONLY.format(count=len(found)), f"files-family-{turn}")
                return
            parent = self._active_turn_id or None
            for index, path in enumerate(inside[:MAX_FILES_PER_TURN]):
                enqueue(room_id, str(path), key=f"deliverable-{turn}-{index}", after=parent, root=str(root))
            skipped = len(outside) + max(0, len(inside) - MAX_FILES_PER_TURN)
            if skipped:
                self._deliverable_notice(transport, room_id, FILES_SKIPPED.format(count=skipped), f"files-skipped-{turn}")
        except Exception:
            logger.warning("Matrix deliverable files not queued", exc_info=True)

    @staticmethod
    def _deliverable_notice(transport: Any, room_id: str, text: str, key: str) -> None:
        # Versioned key: notice rows are permanent (see transport._file_unsent_notice).
        transport.enqueue_notice(room_id, text, key=f"{key}-{hashlib.sha256(text.encode()).hexdigest()[:12]}")

    async def _run_message(
        self,
        body: str,
        *,
        user_id: int,
        chat_id: int,
        room_id: str,
        sink: TurnSink,
        turn_marker: str | None = None,
        usage_mode: str = MODE_INTERACTIVE,
        responses: list[Any] | None = None,
    ) -> Any:
        """One agent turn; ``responses`` (optional) receives the ``ChatResponse``."""

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
            usage_mode=usage_mode,
        )
        if responses is not None:
            responses.append(response)
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
        usage_mode: str = MODE_INTERACTIVE,
        room_id: str | None = None,
    ) -> ChatResponse:
        """One ``process_message`` call with this room's callbacks; persists the session."""

        sink = sink or self._active_sink or _NullSink()
        # The job's own room when the caller has it (#1959: /skills), else the
        # reverse map — the same room for every turn run_turn has admitted.
        room_id = room_id or self.room_for_chat(chat_id) or ""
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
            notification_bot=self._notification_bot(),
            **self._progress_callbacks(sink, room_id),
            usage_mode=usage_mode,
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
        if kind == SELF_JOB_CONTINUATION:
            # #1825: same sender binding as the external-wait resume (#1955).
            if payload.get("user_id") is None or not self._external_wait_job_admitted(
                payload, user_id=user_id, room_kind=room_kind
            ):
                self._settle_continuation(str(payload.get("continuation_id") or ""), False)
                return _turn_result(EXTERNAL_WAIT_RESUME_REFUSED, None)
            return await self._run_continuation_job(
                payload, user_id=user_id, chat_id=chat_id, room_id=room_id, sink=sink, turn_marker=turn_marker
            )
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

    async def _cmd_restart(self, *, user_id: int, chat_id: int, room_kind: str) -> str:
        """``/restart``: owner-only safe restart via the systemd handoff (#2003).

        Mirrors ``bot_commands._cmd_restart``: the owner's direct chat plus the
        ``restart_handoff == "systemd"`` opt-in schedules a ``systemd-run``
        worker that replaces this process; the receipt loop reports the
        outcome once the replacement bridge is healthy.
        """

        if room_kind != "direct" or str(getattr(self._settings, "restart_handoff", "off")) != "systemd":
            return (
                "⛔ Safe restart is unavailable. It requires systemd opt-in and "
                "a private chat with the sole allowlisted owner."
            )
        try:
            scheduled = await asyncio.to_thread(
                restart_handoff.schedule_restart,
                data_dir=self._data_dir(),
                chat_id=chat_id,
                unit=str(getattr(self._settings, "restart_service_unit", "") or ""),
                delay_seconds=int(getattr(self._settings, "restart_delay_seconds", 10)),
            )
        except restart_handoff.RestartHandoffError as exc:
            return f"❌ Restart was not scheduled ({exc.code}). The bridge is still running."
        return (
            f"♻️ Restart scheduled ({scheduled.request_id[:8]}). "
            "I will report when the replacement bridge is healthy."
        )

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

    def _cancel_continuations(self, user_id: int, chat_id: int) -> list[str]:
        """User intent wins over queued autonomous continuations (#1113/#1825).

        Drops the pending bundle and marks the in-flight one cancelled *before*
        the turn is cancelled, so a stopped continuation turn is never counted
        as a failed bundle.
        """

        try:
            cancelled = self._continuation_queue().cancel_for(user_id, chat_id, include_running=True)
        except Exception:
            logger.warning("Matrix continuation cancel on /stop failed", exc_info=True)
            return []
        if cancelled:
            logger.info("Cancelled %s queued continuation(s) on /stop", len(cancelled))
        return cancelled

    async def _cmd_stop(self, *, user_id: int, chat_id: int) -> str:
        cancelled = self._cancel_continuations(user_id, chat_id)
        killed = await self._cancel_turn(user_id, chat_id)
        if killed:
            return "⏸️ Paused"
        if cancelled:
            return f"⏹️ Cancelled {len(cancelled)} queued continuation(s)"
        return "ℹ️ Nothing running"

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
        # #1959: one dispatch path. ``session={}`` keeps the listing on the
        # provider defaults (no stored model/effort), exactly as before and as
        # Telegram's /skills; it always starts a fresh session.
        response = await self._dispatch_turn(
            _SKILLS_PROMPT, key=key, user_id=user_id, chat_id=chat_id, session={},
            session_id=None, new_session=True, sink=sink, turn_marker=turn_marker, room_id=room_id,
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

    async def stop_idle(self, job: Mapping[str, Any]) -> bool:
        return await self._bot.stop_idle(job)
