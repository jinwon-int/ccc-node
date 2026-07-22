"""Fail-closed router for audience-bound Codex app-server runtimes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from .agent_runtime import AgentRuntime, AgentSession, ModelInfo, SessionRequest
from .usage import UsageSnapshot
from telegram_bot.memory.distill_types import (
    CodexTranscriptSnapshot,
    TranscriptBounds,
)


class _UsageRuntime(AgentRuntime, Protocol):
    async def get_usage(self, thread_id: str | None) -> UsageSnapshot: ...

    async def read_session_snapshot(
        self, session_id: str, *, bounds: TranscriptBounds
    ) -> CodexTranscriptSnapshot: ...

    async def close(self) -> None: ...


RuntimeFactory = Callable[[Mapping[str, str]], _UsageRuntime]
RouteEnvironmentFactory = Callable[[str, str], Mapping[str, str]]
EnvironmentKey = tuple[tuple[str, str], ...]


class CodexRuntimePool:
    """Own exactly one Codex runtime per complete audience environment.

    Codex reads ``CODEX_HOME`` when the app-server process starts, so changing
    environment variables between requests cannot isolate one shared process.
    This router keeps the process boundary aligned with the opaque audience and
    remembers which runtime owns each returned thread id.
    """

    supports_session_browsing = False

    def __init__(
        self,
        *,
        shared_environment: Mapping[str, str],
        runtime_factory: RuntimeFactory,
        route_environment_factory: RouteEnvironmentFactory | None = None,
    ) -> None:
        self._shared_environment = dict(shared_environment)
        self._environment_key(self._shared_environment)
        self._runtime_factory = runtime_factory
        self._route_environment_factory = route_environment_factory
        self._runtimes: dict[EnvironmentKey, _UsageRuntime] = {}
        self._thread_runtimes: dict[str, _UsageRuntime] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _environment_key(environment: Mapping[str, str]) -> EnvironmentKey:
        if not environment:
            raise ValueError("Codex audience environment must not be empty")
        items: list[tuple[str, str]] = []
        for name, value in environment.items():
            if not isinstance(name, str) or not name or "\x00" in name:
                raise ValueError("Codex audience environment name is invalid")
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError("Codex audience environment value is invalid")
            items.append((name, value))
        return tuple(sorted(items))

    async def _runtime_for(self, environment: Mapping[str, str]) -> _UsageRuntime:
        key = self._environment_key(environment)
        async with self._lock:
            if self._closed:
                raise RuntimeError("Codex runtime pool is closed")
            runtime = self._runtimes.get(key)
            if runtime is None:
                runtime = self._runtime_factory(dict(environment))
                self._runtimes[key] = runtime
            return runtime

    async def start_or_resume(self, request: SessionRequest) -> AgentSession:
        environment = request.memory_environment
        if environment is None:
            raise RuntimeError("Codex request has no audience environment")
        runtime = await self._runtime_for(environment)
        reserved_thread_id: str | None = None
        if request.session_id is not None:
            async with self._lock:
                owner = self._thread_runtimes.get(request.session_id)
                if owner is not None and owner is not runtime:
                    raise RuntimeError("Codex thread belongs to another audience")
                if owner is None:
                    self._thread_runtimes[request.session_id] = runtime
                    reserved_thread_id = request.session_id
        try:
            session = await runtime.start_or_resume(request)
        except BaseException:
            if reserved_thread_id is not None:
                async with self._lock:
                    if self._thread_runtimes.get(reserved_thread_id) is runtime:
                        self._thread_runtimes.pop(reserved_thread_id, None)
            raise
        async with self._lock:
            owner = self._thread_runtimes.get(session.session_id)
            if owner is not None and owner is not runtime:
                if reserved_thread_id is not None:
                    self._thread_runtimes.pop(reserved_thread_id, None)
                raise RuntimeError("Codex thread belongs to another audience")
            self._thread_runtimes[session.session_id] = runtime
            self._thread_runtimes = dict(tuple(self._thread_runtimes.items())[-2048:])
        return session

    async def list_models(self) -> Sequence[ModelInfo]:
        runtime = await self._runtime_for(self._shared_environment)
        return await runtime.list_models()

    async def get_usage(self, thread_id: str | None) -> UsageSnapshot:
        if thread_id is not None:
            runtime = self._thread_runtimes.get(thread_id)
            if runtime is None:
                return UsageSnapshot(provider="codex")
        else:
            runtime = await self._runtime_for(self._shared_environment)
        return await runtime.get_usage(thread_id)

    async def read_session_snapshot(
        self,
        session_id: str,
        *,
        bounds: TranscriptBounds,
        memory_audience: str | None = None,
        memory_scope: str | None = None,
    ) -> CodexTranscriptSnapshot:
        """Read through the runtime reconstructed from one journal-bound route."""

        if not session_id:
            raise ValueError("session id must not be empty")
        if memory_audience is None or memory_scope is None:
            raise ValueError("Codex snapshot requires an audience route")
        route_environment_factory = self._route_environment_factory
        if route_environment_factory is None:
            raise RuntimeError("Codex snapshot route reconstruction is unavailable")
        environment = route_environment_factory(memory_audience, memory_scope)
        runtime = await self._runtime_for(environment)
        async with self._lock:
            owner = self._thread_runtimes.get(session_id)
            if owner is not None and owner is not runtime:
                raise RuntimeError("Codex thread belongs to another audience")
            self._thread_runtimes[session_id] = runtime
            self._thread_runtimes = dict(tuple(self._thread_runtimes.items())[-2048:])
        return await runtime.read_session_snapshot(session_id, bounds=bounds)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            runtimes = tuple(self._runtimes.values())
            self._runtimes.clear()
            self._thread_runtimes.clear()
        await asyncio.gather(*(runtime.close() for runtime in runtimes))
