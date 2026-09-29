"""Matrix-native background services (#1825, the part #1998 left open).

``BotLifecycleMixin`` owns the Telegram frontend's background services. The
Matrix frontend does not inherit it, and several of those services are
shaped around Telegram primitives (a ``start.sh`` supervisor, editable
"⏳ Working" messages, one webhook port per node). This module rebuilds the
remaining ones for Matrix rather than porting them line by line:

* **Rapid-crash budget** — Telegram has two layers (in-process polling
  rebuild + the ``start.sh`` supervisor). The Matrix unit runs directly under
  systemd with ``Restart=always`` / ``RestartSec=5``, and systemd's default
  start limit (5 in 10 s) can never trip at that spacing, so a frontend that
  dies on start restarted every 5 s forever with no signal. :class:`CrashBudget`
  keeps a tiny durable record in the frontend's data dir, counts consecutive
  *rapid unclean* exits with the shared ``crash-policy.env`` numbers, delays
  the next start exponentially, and raises one owner health alert per streak.
* **Task-ledger reconciliation** — the shared request lifecycle writes
  ``tasks.json`` in this data dir; nothing reconciled it, so a turn killed by
  a restart stayed ``working`` forever. The room already hears about the
  interrupted turn from the transport (``NOTICE_RESTARTED``) and the status
  bubble belongs to the transport's durable outbox, so the terminal op is
  resolved without any message edit — the Telegram edit/delete has no
  Matrix equivalent to perform.
* **Turn-stall probe** — the shared :class:`StallProbeMonitor` with the Matrix
  notice seam and the outbox-backed dead-session recovery (#1998). On by
  default at 20 min, exactly as on Telegram (``CCC_TURN_STALL_PROBE_MIN``;
  ``0`` opts out, #1741).
* **Webhook nudge** — opt-in, and only on an *explicit Matrix port*
  (``CCC_MATRIX_WEBHOOK_NUDGE_PORT``). Both frontends restart together
  under self-update; sharing ``CCC_WEBHOOK_NUDGE_PORT`` would make them race
  for the socket, and the loser's waits would silently lose their nudges.
* **Orphan reaper** — the shared marker-scoped reaper (only processes a
  bridge started, ``CCC_BRIDGE_CLAUDE_CHILD``), at startup and periodically.

Health alerts, the session resource guard and the skill-candidate collector
reuse the channel-neutral loops in ``core.lifecycle_loops``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Awaitable, Callable, Mapping, Optional

from telegram_bot.core import crash_policy

logger = logging.getLogger(__name__)

CRASH_BUDGET_FILENAME = "crash-budget.json"
CRASH_LOOP_ALERT_CODE = "matrix_crash_loop"
MATRIX_NUDGE_PORT_ENV = "CCC_MATRIX_WEBHOOK_NUDGE_PORT"


# -- rapid-crash budget ---------------------------------------------------------


@dataclass(frozen=True)
class CrashDecision:
    """What this start should do about the previous run's exit."""

    streak: int  # consecutive rapid unclean exits, this start's predecessors
    delay_seconds: float  # back-off before serving
    alert: bool  # True once per streak, when it reaches the strike count
    last_error: Optional[str]  # exception class name of the last unclean exit


class CrashBudget:
    """Durable rapid-crash accounting for a systemd-supervised frontend.

    The record lives next to the frontend's other state (``BOT_DATA_DIR``),
    is body-free (timestamps, a counter and an exception *class* name) and
    is written atomically with mode 0600. A run is *unclean* when it never
    reached :meth:`mark_clean`; it is *rapid* when the next start comes
    within ``window_seconds`` of the unclean run's own start. Only a streak
    of rapid unclean exits delays the next start, so a single crash after a
    long healthy run restarts at systemd's normal pace.
    """

    def __init__(
        self,
        path: Path,
        *,
        window_seconds: float = crash_policy.PROCESS_CRASH_WINDOW_SECONDS,
        max_rapid: int = crash_policy.MAX_RAPID_CRASHES,
        base_delay_seconds: float = crash_policy.RESTART_DELAY_BASE_SECONDS,
        max_delay_seconds: float = crash_policy.RESTART_DELAY_MAX_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path)
        self._window = max(0.0, float(window_seconds))
        self._max_rapid = max(1, int(max_rapid))
        self._base = max(0.0, float(base_delay_seconds))
        self._max_delay = max(self._base, float(max_delay_seconds))
        self._clock = clock

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, record: Mapping[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(dict(record), handle)
            os.replace(tmp, self._path)
        except OSError as error:
            # Accounting must never keep the frontend from starting.
            logger.warning("Matrix crash budget not persisted: %s", type(error).__name__)

    def delay_for(self, streak: int) -> float:
        if streak <= 0:
            return 0.0
        return float(min(self._base * (2 ** (streak - 1)), self._max_delay))

    def begin(self) -> CrashDecision:
        """Account for the previous run and mark this one as running."""

        previous = self._read()
        now = float(self._clock())
        streak = 0
        alerted = False
        last_error: Optional[str] = None
        if previous.get("running") is True:
            started = previous.get("started_at")
            # ``started_at`` is when serving began (after any back-off), so a
            # restart before it means the run died during its own back-off —
            # rapid too. A far-negative delta is clock skew, not a crash loop.
            elapsed = now - float(started) if isinstance(started, (int, float)) else None
            rapid = elapsed is not None and -(self._max_delay + self._window) < elapsed < self._window
            if rapid:
                streak = int(previous.get("streak") or 0) + 1
                alerted = bool(previous.get("alerted"))
            last_error = previous.get("last_error") if isinstance(previous.get("last_error"), str) else None
        alert = streak >= self._max_rapid and not alerted
        delay = self.delay_for(streak)
        self._write(
            {
                "v": 1,
                "running": True,
                # The run starts serving only after the back-off sleep. Stamping
                # the pre-sleep time would count the delay itself toward the
                # rapid window: at a 24-30 s delay a steady crash loop read as
                # non-rapid, reset its own streak and never alerted (review of
                # #1825). The supervisor's start.sh likewise measures uptime.
                "started_at": now + delay,
                "streak": streak,
                "alerted": alerted or alert,
                "last_error": last_error if streak else None,
            }
        )
        return CrashDecision(
            streak=streak,
            delay_seconds=delay,
            alert=alert,
            last_error=last_error,
        )

    def record_error(self, error: BaseException) -> None:
        """Remember the class name of what ended this run (never the message)."""

        record = self._read()
        record["last_error"] = type(error).__name__
        self._write(record)

    def mark_clean(self) -> None:
        """This run ended in an orderly way: the streak is over."""

        self._write({"v": 1, "running": False, "started_at": self._read().get("started_at"), "streak": 0, "alerted": False, "last_error": None})


def crash_loop_alert(decision: CrashDecision) -> Any:
    """Owner alert for a rapid-crash streak (constant template + numbers only)."""

    from telegram_bot.utils.health_alerts import Alert

    cause = f" (last exit: {decision.last_error})" if decision.last_error else ""
    return Alert(
        code=CRASH_LOOP_ALERT_CODE,
        message=(
            f"Matrix frontend exited uncleanly {decision.streak} times in a row, each "
            f"within {crash_policy.PROCESS_CRASH_WINDOW_SECONDS}s of starting{cause}. "
            f"Restarts are now delayed {int(decision.delay_seconds)}s."
        ),
    )


# -- task ledger ------------------------------------------------------------------


def reconcile_task_ledger(settings: Any) -> int:
    """Close ledger records a previous process left non-terminal; return how many.

    The interrupted turn was already announced in its room by the transport
    and its status bubble is transport-owned, so every terminal op is resolved
    immediately — there is no Telegram message to edit or delete here.
    """

    from telegram_bot.core.task_ledger import TaskLedger, ledger_path_for

    data_dir = getattr(settings, "bot_data_dir", None)
    path = ledger_path_for(data_dir, getattr(settings, "task_ledger_path", None))
    if path is None or data_dir is None:
        return 0
    # Every non-terminal record is removed, so the file must be this
    # frontend's own. A unit without BOT_DATA_DIR falls back to the Telegram
    # data dir, and a shared CCC_TASK_LEDGER_PATH points outside it: either
    # way a Matrix start would erase the Telegram bridge's in-flight records.
    data_dir = Path(data_dir)
    if data_dir.name == ".telegram_bot" or Path(path).parent != data_dir:
        logger.warning(
            "Matrix task ledger reconciliation skipped: %s is not a Matrix-only data dir "
            "(set BOT_DATA_DIR for the Matrix unit)",
            Path(path).parent,
        )
        return 0
    ledger = TaskLedger(path)
    interrupted = ledger.reconcile_interrupted(op_kind="notice")
    for task_id, _op in ledger.pending_terminal_ops():
        ledger.resolve_terminal_op(task_id, success=True)
    return int(interrupted)


# -- turn-stall probe ---------------------------------------------------------------


def build_turn_stall_probe(
    project_chat: Any,
    *,
    notifier: Callable[[int, str], Awaitable[bool]],
    recover: Callable[[], Awaitable[Any]],
) -> Any:
    """Silent-death stall probe (#1112) for Matrix; on by default at 20 min (#1741).

    ``CCC_TURN_STALL_PROBE_MIN=0`` is the explicit opt-out (returns ``None``).

    Recovers ONLY on a confirmed-dead engine (spawned process exited); a
    quiet-but-alive turn is never touched and an ambiguous liveness verdict
    only logs. ``recover`` is the frontend's outbox-backed dead-session scan.
    """

    from telegram_bot.core.codex_app_server import live_app_server_clients
    from telegram_bot.core.external_wait_monitor import ExternalWaitMonitor
    from telegram_bot.core.turn_stall import (
        DEFAULT_STALL_PROBE_MINUTES,
        STALL_PROBE_ENV,
        StallProbeMonitor,
    )

    probe_min = ExternalWaitMonitor.env_int(STALL_PROBE_ENV, default=DEFAULT_STALL_PROBE_MINUTES)
    if probe_min <= 0:
        logger.info("Matrix turn-stall probe disabled (CCC_TURN_STALL_PROBE_MIN=0)")
        return None
    registry = getattr(project_chat, "_agent_session_registry", None)
    if registry is None:
        logger.warning("Matrix turn-stall probe unavailable: no session registry on project chat")
        return None

    def turns_provider() -> list[tuple[int, int, Any, float]]:
        turns = []
        for handle in registry.active_handles_snapshot():
            key = handle.token.key
            if len(key) < 2:
                continue
            session = handle.session
            thread_id = getattr(session, "id", None) or getattr(session, "session_id", None)
            turns.append((int(key[0]), int(key[1]), thread_id, 0.0))
        return turns

    def liveness() -> str:
        clients = live_app_server_clients()
        running = sum(1 for client in clients if client.process_exited() is False)
        exited = sum(1 for client in clients if client.process_exited() is True)
        if exited and not running:
            return "dead"
        if running:
            return "alive"
        return "unknown"

    codex_home = os.environ.get("CODEX_HOME", "").strip() or str(Path.home() / ".codex")
    return StallProbeMonitor(
        turns_provider=turns_provider,
        liveness_probe=liveness,
        recover=recover,
        notifier=notifier,
        sessions_roots=[Path(codex_home)],
        stall_seconds=probe_min * 60.0,
    )


# -- webhook nudge --------------------------------------------------------------------


def build_webhook_nudge_server(
    data_dir: Path, *, environ: Optional[Mapping[str, str]] = None
) -> Any:
    """GitHub webhook nudge listener for this frontend's external waits (#1222).

    ``None`` unless ``CCC_WEBHOOK_NUDGE_ENABLED`` is on **and**
    ``CCC_MATRIX_WEBHOOK_NUDGE_PORT`` names a port different from the
    Telegram listener's. The Telegram frontend keeps ``CCC_WEBHOOK_NUDGE_PORT``;
    the same secret, host and body cap apply. A verified delivery only pulls
    a matching wait's next poll forward — the monitor still reads GitHub
    through its authenticated transport.
    """

    from telegram_bot.core.external_wait import ExternalWaitRegistry, default_registry_path
    from telegram_bot.core.webhook_nudge import DEFAULT_PORT, build_from_env

    env = dict(os.environ if environ is None else environ)
    if (env.get("CCC_WEBHOOK_NUDGE_ENABLED") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    raw_port = (env.get(MATRIX_NUDGE_PORT_ENV) or "").strip()
    if not raw_port:
        logger.info(
            "Matrix webhook nudge not started: set %s to a port of its own "
            "(the Telegram listener keeps CCC_WEBHOOK_NUDGE_PORT)",
            MATRIX_NUDGE_PORT_ENV,
        )
        return None
    try:
        port = int(raw_port)
    except ValueError:
        logger.error("Matrix webhook nudge not started: %s is not a port number", MATRIX_NUDGE_PORT_ENV)
        return None
    try:
        telegram_port = int((env.get("CCC_WEBHOOK_NUDGE_PORT") or "").strip() or DEFAULT_PORT)
    except ValueError:
        telegram_port = DEFAULT_PORT  # the Telegram listener falls back the same way
    if not 0 < port < 65536:
        logger.error("Matrix webhook nudge not started: %s is out of range", MATRIX_NUDGE_PORT_ENV)
        return None
    if port == telegram_port:
        logger.error(
            "Matrix webhook nudge not started: %s equals the Telegram listener port %d "
            "(the two frontends would race for the socket)",
            MATRIX_NUDGE_PORT_ENV,
            telegram_port,
        )
        return None
    env["CCC_WEBHOOK_NUDGE_PORT"] = str(port)
    home = Path(data_dir) / "external-wait"
    return build_from_env(lambda: ExternalWaitRegistry(default_registry_path(home)), environ=env)


__all__ = [
    "CRASH_BUDGET_FILENAME",
    "CRASH_LOOP_ALERT_CODE",
    "CrashBudget",
    "CrashDecision",
    "MATRIX_NUDGE_PORT_ENV",
    "build_turn_stall_probe",
    "build_webhook_nudge_server",
    "crash_loop_alert",
    "reconcile_task_ledger",
]
