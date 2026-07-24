import json
import os
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from telegram_bot.utils.config import config


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_reason(value: Optional[str]) -> str:
    if not value:
        return ""
    return " ".join(str(value).split())[:500]


def _pid_is_alive(pid_text: Optional[str]) -> bool:
    """True iff ``pid_text`` parses to a positive pid of a live process.

    Used to decide ownership of the shared pid / token-lock files without
    clobbering a concurrent surviving instance. A ``PermissionError`` from
    ``os.kill(pid, 0)`` means the pid exists but is owned by another user, which
    still counts as alive.
    """
    if not pid_text:
        return False
    try:
        pid = int(pid_text)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class RuntimeHealthReporter:
    SCHEMA_VERSION = 1

    def __init__(self, bot_data_dir: Path, agent_provider: str | None = None):
        self._lock = threading.Lock()
        self._bot_data_dir = bot_data_dir
        self._pid_file = bot_data_dir / "bot.pid"
        self._health_file = bot_data_dir / "health.json"
        self._started_at = _utc_now_iso()
        self._process_mode = "foreground"
        self._token_lock_file = ""
        self._owns_token_lock = False
        configured_provider = agent_provider or getattr(config, "agent_provider", "claude")
        self._agent_provider = (
            "codex" if str(configured_provider).strip().lower() == "codex" else "claude"
        )
        initial_agent_state = {
            "state": "degraded",
            "last_ok_at": None,
            "last_error_at": None,
            "last_error": "",
        }
        self._state: dict[str, Any] = {
            "schema_version": self.SCHEMA_VERSION,
            "updated_at": _utc_now_iso(),
            "process": {
                "pid": os.getpid(),
                "started_at": self._started_at,
                "mode": self._process_mode,
            },
            "service": {
                "state": "starting",
                "reason": "initializing bot",
            },
            "telegram": {
                "state": "degraded",
                "last_ok_at": None,
                "last_error_at": None,
                "last_error": "",
                "consecutive_failures": 0,
            },
            "agent": {
                "provider": self._agent_provider,
                **initial_agent_state,
            },
            # Backward-compatible alias for consumers of schema v1. It mirrors
            # the active agent even when the provider is Codex.
            "claude": dict(initial_agent_state),
            "workload": {
                "active_requests": 0,
                "oldest_request_age_seconds": 0,
            },
            "transport": {
                "reconnects": 0,
                "cancelled_by_transport": 0,
            },
            "recovery": {
                "quarantined_transcripts": 0,
                "hard_quarantined_transcripts": 0,
            },
            "requests": {
                "stalled": 0,
            },
        }

    @property
    def health_file(self) -> Path:
        return self._health_file

    @property
    def pid_file(self) -> Path:
        return self._pid_file

    def _ensure_runtime_dir(self) -> None:
        self._bot_data_dir.mkdir(parents=True, exist_ok=True)

    def _write_pid_locked(self) -> None:
        self._ensure_runtime_dir()
        # Do not clobber a pid file that still records a *different, live* bot
        # for this project (a concurrent instance / guard race). Overwriting it
        # would make THIS process the recorded owner; if this process is then
        # the one that loses the Telegram getUpdates conflict and exits,
        # cleanup_runtime_files() would unlink the file and orphan the survivor
        # — a live bot that `start.sh --status` reports as dead (observed:
        # jingun 2026-07-24, daegyo 2026-07-08). Claim the pid file only when it
        # is absent, empty, records a dead pid, or already records us.
        try:
            recorded = self._pid_file.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            recorded = ""
        if recorded and recorded != str(os.getpid()) and _pid_is_alive(recorded):
            return
        self._pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")

    def _write_health_locked(self) -> None:
        self._ensure_runtime_dir()
        self._state["updated_at"] = _utc_now_iso()
        temp_path = self._health_file.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(self._state, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, self._health_file)

    def _refresh_runtime_context_locked(self) -> None:
        self._process_mode = os.environ.get("BOT_PROCESS_MODE", "foreground")
        self._token_lock_file = os.environ.get("BOT_TOKEN_LOCK_FILE", "")
        self._owns_token_lock = os.environ.get("BOT_OWNS_TOKEN_LOCK") == "1"
        self._state["process"] = {
            "pid": os.getpid(),
            "started_at": self._started_at,
            "mode": self._process_mode,
        }

    def _recompute_service_locked(self) -> None:
        telegram_state = self._state["telegram"]["state"]
        agent = self._state["agent"]
        agent_state = agent["state"]
        if telegram_state == "healthy" and agent_state == "healthy":
            self._state["service"]["state"] = "available"
            self._state["service"]["reason"] = ""
            return

        reasons: list[str] = []
        if telegram_state != "healthy":
            detail = self._state["telegram"].get("last_error") or "telegram unavailable"
            reasons.append(f"Telegram: {detail}")
        if agent_state != "healthy":
            provider = str(agent.get("provider") or self._agent_provider).lower()
            label = "Codex" if provider == "codex" else "Claude"
            detail = agent.get("last_error") or f"{provider} unavailable"
            reasons.append(f"{label}: {detail}")

        self._state["service"]["state"] = "degraded"
        self._state["service"]["reason"] = "; ".join(reasons)

    def initialize_process(self) -> None:
        with self._lock:
            self._refresh_runtime_context_locked()
            self._write_pid_locked()
            self._write_health_locked()

    def mark_starting(self, reason: str) -> None:
        with self._lock:
            self._state["service"]["state"] = "starting"
            self._state["service"]["reason"] = _normalize_reason(reason)
            self._write_health_locked()

    def mark_unavailable(self, reason: str) -> None:
        with self._lock:
            # In launchd mode, process exit means restart window, not final unavailable
            if self._process_mode == "launchd":
                self._state["service"]["state"] = "starting"
                self._state["service"]["reason"] = f"waiting for launchd restart ({_normalize_reason(reason)})"
            else:
                self._state["service"]["state"] = "unavailable"
                self._state["service"]["reason"] = _normalize_reason(reason)
            self._write_health_locked()

    def record_telegram_ok(self) -> None:
        with self._lock:
            self._state["telegram"]["state"] = "healthy"
            self._state["telegram"]["last_ok_at"] = _utc_now_iso()
            self._state["telegram"]["consecutive_failures"] = 0
            self._recompute_service_locked()
            self._write_health_locked()

    def record_telegram_error(
        self, error: str, consecutive_failures: Optional[int] = None
    ) -> None:
        with self._lock:
            self._state["telegram"]["state"] = "degraded"
            self._state["telegram"]["last_error_at"] = _utc_now_iso()
            self._state["telegram"]["last_error"] = _normalize_reason(error)
            if consecutive_failures is None:
                consecutive_failures = (
                    int(self._state["telegram"]["consecutive_failures"]) + 1
                )
            self._state["telegram"]["consecutive_failures"] = consecutive_failures
            self._recompute_service_locked()
            self._write_health_locked()

    def _sync_legacy_agent_locked(self) -> None:
        agent = self._state["agent"]
        self._state["claude"] = {
            key: agent.get(key)
            for key in ("state", "last_ok_at", "last_error_at", "last_error")
        }

    def record_agent_ok(self) -> None:
        with self._lock:
            self._state["agent"]["state"] = "healthy"
            self._state["agent"]["last_ok_at"] = _utc_now_iso()
            self._state["agent"]["last_error"] = ""
            self._sync_legacy_agent_locked()
            self._recompute_service_locked()
            self._write_health_locked()

    def record_agent_error(self, error: str) -> None:
        with self._lock:
            self._state["agent"]["state"] = "degraded"
            self._state["agent"]["last_error_at"] = _utc_now_iso()
            self._state["agent"]["last_error"] = _normalize_reason(error)
            self._sync_legacy_agent_locked()
            self._recompute_service_locked()
            self._write_health_locked()

    # Compatibility names retained for shared runtime paths and external users.
    def record_claude_ok(self) -> None:
        self.record_agent_ok()

    def record_claude_error(self, error: str) -> None:
        self.record_agent_error(error)

    def _transport_locked(self) -> dict[str, Any]:
        return self._state.setdefault(
            "transport", {"reconnects": 0, "cancelled_by_transport": 0}
        )

    def record_transport_reconnect(self) -> None:
        """Count a successful transport-only polling reconnect (issue #411).

        A rising counter with ``cancelled_by_transport`` staying flat is the
        expected signature: the polling transport recovered without terminating
        any in-flight agent turn.
        """
        with self._lock:
            transport = self._transport_locked()
            transport["reconnects"] = int(transport.get("reconnects", 0)) + 1
            self._write_health_locked()

    def record_cancelled_by_transport(self, count: int = 1) -> None:
        """Count in-flight requests terminated by a transport-caused teardown."""
        with self._lock:
            transport = self._transport_locked()
            transport["cancelled_by_transport"] = int(
                transport.get("cancelled_by_transport", 0)
            ) + max(0, int(count))
            self._write_health_locked()

    def record_health_signals(
        self, signals: dict[str, Any], alerts_fired: int = 0
    ) -> None:
        """Publish one health-probe tick's structured signals (#389).

        ``signals`` carries only counts and ages — the probe never includes
        tokens, prompts, or filesystem paths.
        """
        with self._lock:
            section = self._state.setdefault("signals", {})
            section.update(signals)
            section["alerts_fired"] = int(section.get("alerts_fired", 0)) + max(
                0, int(alerts_fired)
            )
            section["updated_at"] = _utc_now_iso()
            self._write_health_locked()

    def record_stalled_request(self, count: int = 1) -> None:
        """Count requests released by the terminal-event stall guard (#411 C)."""
        with self._lock:
            requests = self._state.setdefault("requests", {"stalled": 0})
            requests["stalled"] = int(requests.get("stalled", 0)) + max(0, int(count))
            self._write_health_locked()

    def record_transcript_quarantined(self, count: int = 1) -> None:
        """Count dead-session transcripts newly quarantined as unrecoverable (#411)."""
        with self._lock:
            recovery = self._state.setdefault("recovery", {"quarantined_transcripts": 0})
            recovery["quarantined_transcripts"] = int(
                recovery.get("quarantined_transcripts", 0)
            ) + max(0, int(count))
            self._write_health_locked()

    def record_transcript_hard_quarantined(self, count: int = 1) -> None:
        """Count conversations that hit the consecutive-reject bound and were
        hard-quarantined (the give-up transition that stops the churn loop; #423).
        Separate from ``quarantined_transcripts`` so the every-event metric stays
        continuous."""
        with self._lock:
            recovery = self._state.setdefault("recovery", {"quarantined_transcripts": 0})
            recovery["hard_quarantined_transcripts"] = int(
                recovery.get("hard_quarantined_transcripts", 0)
            ) + max(0, int(count))
            self._write_health_locked()

    def record_workload(
        self, active_requests: int, oldest_request_age_seconds: float
    ) -> None:
        """Publish the current in-flight request count for idle-gated restarts.

        External supervisors (e.g. the self-update procedure) read this from
        ``health.json`` and defer a restart while the bridge is busy, so an
        in-flight ``claude`` child is not SIGTERM-killed mid-task.
        """
        with self._lock:
            self._state["workload"] = {
                "active_requests": max(0, int(active_requests)),
                "oldest_request_age_seconds": max(
                    0, int(oldest_request_age_seconds)
                ),
            }
            self._write_health_locked()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state)

    def cleanup_runtime_files(self) -> None:
        with self._lock:
            self._refresh_runtime_context_locked()
            # Only remove the pid file if it still records THIS process.
            # Concurrent instances share one pid file: the newer instance
            # overwrites it in initialize_process(), and when either of them
            # exits (e.g. Telegram getUpdates conflict kills the loser), an
            # unconditional unlink here would delete the survivor's pid file —
            # leaving a live bot that `start.sh --status` reports as dead.
            try:
                recorded = self._pid_file.read_text(encoding="utf-8").strip()
            except (FileNotFoundError, OSError):
                recorded = ""
            if recorded == str(os.getpid()):
                try:
                    self._pid_file.unlink()
                except FileNotFoundError:
                    pass
            if self._owns_token_lock and self._token_lock_file:
                # Same survivor-safety as the pid file: only remove the token
                # lock if it still records THIS process (or a now-dead pid). A
                # losing instance that set BOT_OWNS_TOKEN_LOCK=1 must not delete
                # a lock the survivor has since overwritten with its own pid.
                lock_path = Path(self._token_lock_file)
                try:
                    lock_recorded = lock_path.read_text(encoding="utf-8").strip()
                except (FileNotFoundError, OSError):
                    lock_recorded = ""
                if lock_recorded == str(os.getpid()) or not _pid_is_alive(
                    lock_recorded
                ):
                    try:
                        lock_path.unlink()
                    except FileNotFoundError:
                        pass


class DeferredHealthReporter:
    """Import-safe proxy bound to the validated runtime directory at composition."""

    def __init__(self) -> None:
        self._reporter: RuntimeHealthReporter | None = None
        self._lock = threading.RLock()

    def bind(self, bot_data_dir: Path, agent_provider: str | None = None) -> None:
        with self._lock:
            self._reporter = RuntimeHealthReporter(Path(bot_data_dir), agent_provider)

    def _get(self) -> RuntimeHealthReporter:
        with self._lock:
            if self._reporter is None:
                self._reporter = RuntimeHealthReporter(Path(config.bot_data_dir))
            return self._reporter

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)


health_reporter = DeferredHealthReporter()
