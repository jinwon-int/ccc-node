"""Channel-neutral memory writeback triggers and journal worker loops.

Both frontends supply sessions and the same budget-gated workers. The transport
owns start/stop; these loops never send chat messages or publish Wiki pages.
"""
import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict

from telegram_bot.core.project_chat_types import ChatResponse
from telegram_bot.core.bot_ports import ClockPort, ProjectChatPort, SessionManagerPort
from telegram_bot.memory.distill_types import DistillJob, DistillTrigger

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _DistillCheckpointProgress:
    thread_id: str
    started_at: float
    turns: int = 0
    byte_count: int = 0
    last_turn_marker: str | None = None
    pending_discriminator: str | None = None



def _memory_settings(frontend: Any) -> Any:
    # Matrix's _config contains transport JSON, not bridge Settings.
    return frontend._settings if hasattr(frontend, "_settings") else frontend._config


class MemoryDistillMixin:
    _active_provider: Callable[[], str]
    _project_chat: ProjectChatPort
    _session_manager: SessionManagerPort
    _clock: ClockPort
    _distill_journal: Any
    _distill_snapshot_worker: Any
    _distill_extraction_worker: Any
    _distill_local_sink_worker: Any
    _distill_wiki_sink_worker: Any
    _distill_checkpoint_progress: dict[Any, _DistillCheckpointProgress]
    _distill_checkpoint_locks: dict[Any, asyncio.Lock]
    _SHUTDOWN_DISTILL_MAX_SESSIONS = 128
    _SHUTDOWN_DISTILL_TIMEOUT_SECONDS = 2.0

    async def _enqueue_previous_codex_session(
        self,
        session: dict[str, Any],
        trigger: DistillTrigger,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        discriminator: str | None = None,
    ) -> DistillJob | None:
        if getattr(_memory_settings(self), "memory_distill_provider", "auto") == "off":
            return None
        from telegram_bot.memory.distill_guard import global_distill_disabled

        if global_distill_disabled():
            if not getattr(self, "_distill_global_disabled_warned", False):
                self._distill_global_disabled_warned = True
                logger.warning(
                    "Distill enqueue skipped: global disable marker is present"
                )
            return None
        provider = str(session.get("provider", "claude")).strip().lower()
        if self._active_provider() == "danso" and (
            provider != "danso" or getattr(_memory_settings(self), "bridge_memory_mode", "off") != "audience-scoped"
        ):
            return None
        thread_id = session.get("session_id")
        if provider not in {"claude", "codex", "piri", "danso"} or not isinstance(thread_id, str) or not thread_id:
            return None
        journal = getattr(self, "_distill_journal", None)
        if journal is None:
            return None
        memory_audience = None
        memory_scope = None
        if user_id is not None and chat_id is not None:
            from telegram_bot.core.memory_audience import resolve_memory_audience

            audience = resolve_memory_audience(
                _memory_settings(self),
                user_id=user_id,
                chat_id=chat_id,
                route=getattr(self._project_chat, "_memory_route", "telegram"),
            )
            if audience is not None:
                memory_audience = audience.kind
                memory_scope = audience.scope
        else:
            stored_audience = session.get("distill_memory_audience")
            stored_scope = session.get("distill_memory_scope")
            if isinstance(stored_audience, str) and isinstance(stored_scope, str):
                memory_audience = stored_audience
                memory_scope = stored_scope
        if memory_audience is None and not getattr(
            self, "_local_sink_unroutable_warned", False
        ):
            # The journal marks a routeless job UNROUTABLE with no error_code and
            # no log line, so a node whose memory mode yields no audience loses the
            # local lane (resume.md, memory facts) silently. The wiki sink is
            # unaffected. Warn once per process so the gap is observable.
            self._local_sink_unroutable_warned = True
            logger.warning(
                "distill local sink unroutable: bridge_memory_mode=%r resolves no memory "
                "audience, so resume.md and local memory facts will not be written "
                "(wiki sink unaffected)",
                getattr(_memory_settings(self), "bridge_memory_mode", None),
            )
        enqueue_kwargs = {
            "provider": provider,
            "thread_id": thread_id,
            "trigger": trigger,
            "memory_audience": memory_audience,
            "memory_scope": memory_scope,
        }
        if discriminator is not None:
            enqueue_kwargs["discriminator"] = discriminator
        return await asyncio.to_thread(
            journal.enqueue_once,
            **enqueue_kwargs,
        )

    def _distill_checkpoint_gates(self) -> tuple[int, int, int]:
        generic = (
            int(getattr(_memory_settings(self), "memory_distill_checkpoint_turns", 0) or 0),
            int(getattr(_memory_settings(self), "memory_distill_checkpoint_bytes", 0) or 0),
            int(getattr(_memory_settings(self), "memory_distill_checkpoint_age_seconds", 0) or 0),
        )
        if any(generic):
            return generic
        return (
            int(getattr(_memory_settings(self), "codex_distill_checkpoint_turns", 0) or 0),
            int(getattr(_memory_settings(self), "codex_distill_checkpoint_bytes", 0) or 0),
            int(getattr(_memory_settings(self), "codex_distill_checkpoint_age_seconds", 0) or 0),
        )

    @staticmethod
    def _distill_checkpoint_reached(
        progress: _DistillCheckpointProgress,
        gates: tuple[int, int, int],
        *,
        now: float,
    ) -> bool:
        turn_gate, byte_gate, age_gate = gates
        elapsed = max(0.0, now - progress.started_at)
        return (
            (turn_gate > 0 and progress.turns >= turn_gate)
            or (byte_gate > 0 and progress.byte_count >= byte_gate)
            or (age_gate > 0 and elapsed >= age_gate)
        )

    @staticmethod
    def _update_distill_checkpoint_progress(
        progress_by_key: Dict[Any, _DistillCheckpointProgress],
        session_key: Any,
        *,
        thread_id: str,
        marker_hash: str,
        turn_bytes: int,
        now: float,
    ) -> _DistillCheckpointProgress:
        progress = progress_by_key.get(session_key)
        if progress is None or progress.thread_id != thread_id:
            progress = _DistillCheckpointProgress(thread_id, now)
            progress_by_key[session_key] = progress
        if progress.last_turn_marker != marker_hash:
            progress.turns += 1
            progress.byte_count += turn_bytes
            progress.last_turn_marker = marker_hash
        return progress

    async def _record_codex_checkpoint(
        self,
        session_key: Any,
        response: ChatResponse,
        *,
        request_text: str,
        turn_marker: str | None,
        user_id: int | None,
        chat_id: int | None,
    ) -> None:
        """Count completed turns and durably enqueue the first reached gate."""
        active_provider = self._active_provider()
        if active_provider not in {"claude", "codex", "piri", "danso"}:
            return
        if getattr(self, "_distill_journal", None) is None:
            return
        gates = self._distill_checkpoint_gates()
        if all(gate <= 0 for gate in gates):
            return
        thread_id = response.session_id
        if not isinstance(thread_id, str) or not thread_id:
            return

        marker = turn_marker
        if not isinstance(marker, str) or not marker:
            content = response.content if isinstance(response.content, str) else ""
            marker = hashlib.sha256(
                f"{request_text}\0{content}".encode("utf-8")
            ).hexdigest()
        marker_hash = hashlib.sha256(marker.encode("utf-8")).hexdigest()

        progress_by_key = getattr(self, "_distill_checkpoint_progress", None)
        if progress_by_key is None:
            progress_by_key = self._distill_checkpoint_progress = {}
        locks = getattr(self, "_distill_checkpoint_locks", None)
        if locks is None:
            locks = self._distill_checkpoint_locks = {}
        lock = locks.setdefault(session_key, asyncio.Lock())

        async with lock:
            now = float(self._clock.time())
            response_text = (
                response.content if isinstance(response.content, str) else ""
            )
            progress = self._update_distill_checkpoint_progress(
                progress_by_key,
                session_key,
                thread_id=thread_id,
                marker_hash=marker_hash,
                turn_bytes=(
                    len(request_text.encode("utf-8"))
                    + len(response_text.encode("utf-8"))
                ),
                now=now,
            )

            if progress.pending_discriminator is None:
                if not self._distill_checkpoint_reached(
                    progress,
                    gates,
                    now=now,
                ):
                    return
                digest = hashlib.sha256(
                    f"{thread_id}\0{marker_hash}".encode("utf-8")
                ).hexdigest()
                progress.pending_discriminator = f"checkpoint-turn-v1-{digest}"

            try:
                session = await self._session_manager.get_session(session_key)
                if (
                    session.get("provider") != active_provider
                    or session.get("session_id") != thread_id
                ):
                    progress_by_key.pop(session_key, None)
                    return
                job = await self._enqueue_previous_codex_session(
                    session,
                    DistillTrigger.CHECKPOINT,
                    user_id=user_id,
                    chat_id=chat_id,
                    discriminator=progress.pending_discriminator,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "%s checkpoint journal enqueue failed error=%s",
                    active_provider.title(),
                    type(error).__name__,
                )
                return

            if job is not None:
                progress_by_key[session_key] = _DistillCheckpointProgress(
                    thread_id=thread_id,
                    started_at=now,
                    last_turn_marker=marker_hash,
                )

    @staticmethod
    def _shutdown_distill_discriminator(session: dict[str, Any]) -> str:
        marker = session.get("last_user_message_at")
        thread_id = session.get("session_id")
        digest = hashlib.sha256(
            f"{thread_id}\0{marker or 'unknown-turn'}".encode("utf-8")
        ).hexdigest()
        return f"shutdown-turn-v1-{digest}"

    async def _enqueue_shutdown_distills(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        """Bound shutdown work to durable journal writes; never call a provider."""
        if getattr(self, "_distill_journal", None) is None:
            return

        active_keys = sorted(
            tuple(getattr(self, "_runtime_active_sessions", ())),
            key=str,
        )
        limit = self._SHUTDOWN_DISTILL_MAX_SESSIONS
        selected_keys = active_keys[:limit]
        if len(active_keys) > limit:
            logger.warning(
                "Memory shutdown distill queue capped at %d active sessions",
                limit,
            )

        async def enqueue_selected() -> None:
            for session_key in selected_keys:
                try:
                    session = await self._session_manager.get_session(session_key)
                    if session.get("provider") != self._active_provider():
                        continue
                    await self._enqueue_previous_codex_session(
                        session,
                        DistillTrigger.SHUTDOWN,
                        discriminator=self._shutdown_distill_discriminator(session),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.warning(
                        "Memory shutdown distill queue entry failed error=%s",
                        type(error).__name__,
                    )

        timeout = (
            self._SHUTDOWN_DISTILL_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )
        try:
            await asyncio.wait_for(enqueue_selected(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Memory shutdown distill queue timed out after %.2fs",
                timeout,
            )

    async def _distill_sweep_jobs(
        self, interval: float, *, recover: bool = True
    ) -> tuple["DistillJob", ...]:
        """One shared journal scan per poll cluster for the pipeline loops.

        The six distill loops poll on the same interval and wake as a cluster,
        so each tick used to pay eleven full journal reads (five recoveries
        plus six listings), every record re-validated with its snapshot
        payload. Within a fraction of the interval one physical
        recover+prune+list result now serves all of them. Listings were
        always advisory — each worker's claim_* call is the serialization
        point and re-reads under the journal lock — so a shared snapshot
        changes no claim semantics. ``recover=False`` preserves the skill
        collector's read-only contract when it has to scan on a cache miss.
        """
        lock = getattr(self, "_distill_sweep_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._distill_sweep_lock = lock
        journal = self._distill_journal
        # Short enough that every real tick rescans (and per-loop tests with
        # millisecond intervals stay per-tick fresh), long enough to fold one
        # wake cluster into one scan.
        ttl = max(0.0, min(30.0, float(interval) * 0.5))
        async with lock:
            cache = getattr(self, "_distill_sweep_cache", None)
            now = time.monotonic()
            if (
                cache is not None
                and cache[0] is journal
                and now - cache[1] < ttl
            ):
                return cache[2]
            if recover:
                await asyncio.to_thread(journal.recover_stale_running)
            prune = getattr(journal, "prune_terminal_jobs", None)
            retention = float(
                getattr(
                    _memory_settings(self),
                    "distill_job_retention_seconds",
                    14 * 24 * 3600.0,
                )
                or 0.0
            )
            if recover and prune is not None and retention > 0:
                try:
                    pruned = await asyncio.to_thread(
                        prune, older_than_seconds=retention
                    )
                except Exception:
                    logger.warning(
                        "Distill journal prune failed; continuing", exc_info=True
                    )
                else:
                    if pruned:
                        logger.info(
                            "Distill journal pruned %d fully-terminal job(s)",
                            len(pruned),
                        )
            jobs = await asyncio.to_thread(journal.list_jobs)
            self._distill_sweep_cache = (journal, now, jobs)
            return jobs

    async def _distill_extraction_loop(self, stop_event: asyncio.Event) -> None:
        """Drive the budget-gated distill worker over ready snapshot jobs.

        This is the production scheduler for the retained worker (#388): each
        sweep runs every ready (snapshot_done) job through extract_once, whose
        prospective reservation gate defers capped work before any provider
        call. Trigger policy that *creates* jobs remains #465's phase, so on
        nodes without queued jobs each sweep is a no-op. Fail-open: sweep
        errors are logged and never end the loop or the bridge.
        """

        from telegram_bot.memory.distill_types import DistillJobStatus

        worker = self._distill_extraction_worker
        interval = float(
            getattr(_memory_settings(self), "distill_extraction_poll_interval", 300.0) or 300.0
        )
        max_jobs_per_sweep = max(
            1,
            int(
                getattr(
                    _memory_settings(self),
                    "memory_distill_max_jobs_per_sweep",
                    1,
                )
                or 1
            ),
        )
        while not stop_event.is_set():
            try:
                jobs = await self._distill_sweep_jobs(interval)
                attempted = 0
                for job in jobs:
                    if stop_event.is_set():
                        break
                    # Ready set: fresh snapshots AND transiently failed
                    # extractions — claim_extraction accepts both, and the
                    # worker's max-attempts gate bounds the retries.
                    if job.status not in (
                        DistillJobStatus.SNAPSHOT_DONE,
                        DistillJobStatus.EXTRACTION_RETRYABLE_FAILED,
                    ):
                        continue
                    retry_after_raw = getattr(job, "extraction_retry_after", None)
                    if retry_after_raw is not None:
                        retry_after = datetime.fromisoformat(
                            retry_after_raw.replace("Z", "+00:00")
                        )
                        if retry_after > datetime.now(timezone.utc):
                            continue
                    await worker.extract_once(job_id=job.job_id)
                    attempted += 1
                    if attempted >= max_jobs_per_sweep:
                        break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Distill extraction sweep failed; continuing", exc_info=True
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except (TimeoutError, asyncio.TimeoutError):
                continue

    async def _distill_snapshot_loop(self, stop_event: asyncio.Event) -> None:
        """Recover queued/stale snapshot jobs through their bound Codex route."""

        from telegram_bot.memory.distill_types import DistillJobStatus

        worker = self._distill_snapshot_worker
        interval = float(
            getattr(_memory_settings(self), "distill_extraction_poll_interval", 300.0) or 300.0
        )
        while not stop_event.is_set():
            try:
                jobs = await self._distill_sweep_jobs(interval)
                for job in jobs:
                    if stop_event.is_set():
                        break
                    if job.status not in (
                        DistillJobStatus.QUEUED,
                        DistillJobStatus.RETRYABLE_FAILED,
                    ):
                        continue
                    await worker.snapshot_once(job_id=job.job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Distill snapshot sweep failed; continuing", exc_info=True
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except (TimeoutError, asyncio.TimeoutError):
                continue

    async def _distill_local_sink_loop(self, stop_event: asyncio.Event) -> None:
        """Drive independently leased local write-back without re-extraction."""

        from telegram_bot.memory.distill_types import DistillLocalSinkStatus

        worker = self._distill_local_sink_worker
        interval = float(
            getattr(_memory_settings(self), "distill_extraction_poll_interval", 300.0) or 300.0
        )
        while not stop_event.is_set():
            try:
                jobs = await self._distill_sweep_jobs(interval)
                for job in jobs:
                    if stop_event.is_set():
                        break
                    if job.local_sink_status not in (
                        DistillLocalSinkStatus.PENDING,
                        DistillLocalSinkStatus.RETRYABLE_FAILED,
                    ):
                        continue
                    await worker.write_once(job_id=job.job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Distill local-sink sweep failed; continuing", exc_info=True
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except (TimeoutError, asyncio.TimeoutError):
                continue

    async def _distill_wiki_sink_loop(self, stop_event: asyncio.Event) -> None:
        """Drive the local human-review queue without any Wiki write or PR."""

        from telegram_bot.memory.distill_types import DistillWikiSinkStatus

        worker = self._distill_wiki_sink_worker
        interval = float(
            getattr(_memory_settings(self), "distill_extraction_poll_interval", 300.0) or 300.0
        )
        while not stop_event.is_set():
            try:
                jobs = await self._distill_sweep_jobs(interval)
                for job in jobs:
                    if stop_event.is_set():
                        break
                    if job.wiki_sink_status not in (
                        DistillWikiSinkStatus.PENDING,
                        DistillWikiSinkStatus.RETRYABLE_FAILED,
                    ):
                        continue
                    await worker.write_once(job_id=job.job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Distill Wiki-sink sweep failed; continuing", exc_info=True
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except (TimeoutError, asyncio.TimeoutError):
                continue

