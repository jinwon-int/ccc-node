"""Audience isolation tests for the Codex runtime router."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib

import pytest

from telegram_bot.core.agent_runtime import ModelInfo, SessionRequest
from telegram_bot.core.codex_runtime_pool import CodexRuntimePool
from telegram_bot.core.usage import UsageSnapshot
from telegram_bot.memory.distill_types import (
    CodexTranscriptSnapshot,
    TranscriptBounds,
)


class _Session:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class _Runtime:
    def __init__(self, environment: Mapping[str, str]) -> None:
        self.environment = dict(environment)
        self.requests: list[SessionRequest] = []
        self.snapshot_requests: list[str] = []
        self.closed = False

    async def start_or_resume(self, request: SessionRequest) -> _Session:
        self.requests.append(request)
        return _Session(request.session_id or f"new-{len(self.requests)}")

    async def list_models(self) -> Sequence[ModelInfo]:
        return (ModelInfo("codex-test", "Codex Test"),)

    async def get_usage(self, thread_id: str | None) -> UsageSnapshot:
        return UsageSnapshot(provider="codex", service=thread_id or self.environment["CODEX_HOME"])

    async def read_session_snapshot(
        self, session_id: str, *, bounds: TranscriptBounds
    ) -> CodexTranscriptSnapshot:
        self.snapshot_requests.append(session_id)
        return CodexTranscriptSnapshot(
            thread_hash=hashlib.sha256(session_id.encode()).hexdigest(),
            last_turn_id=None,
            messages=(),
            byte_count=0,
            truncated=False,
            captured_at=datetime.now(timezone.utc).isoformat(),
        )

    async def close(self) -> None:
        self.closed = True


def _environment(scope: str) -> dict[str, str]:
    return {
        "CODEX_HOME": f"/memory/{scope}/codex",
        "CODEX_SQLITE_HOME": f"/memory/{scope}/codex",
        "CCC_MEMORY_SCOPE": scope,
    }


@pytest.mark.anyio
async def test_pool_reuses_one_runtime_per_environment_and_routes_usage() -> None:
    runtimes: list[_Runtime] = []

    def factory(environment: Mapping[str, str]) -> _Runtime:
        runtime = _Runtime(environment)
        runtimes.append(runtime)
        return runtime

    pool = CodexRuntimePool(
        shared_environment=_environment("shared"),
        runtime_factory=factory,
    )
    private = _environment("private-opaque")
    first = await pool.start_or_resume(
        SessionRequest(
            working_directory="/workspace",
            session_id="thread-private",
            memory_environment=private,
        )
    )
    again = await pool.start_or_resume(
        SessionRequest(
            working_directory="/workspace",
            session_id="thread-private-2",
            memory_environment=dict(private),
        )
    )
    shared = await pool.start_or_resume(
        SessionRequest(
            working_directory="/workspace",
            session_id="thread-shared",
            memory_environment=_environment("shared"),
        )
    )

    assert first.session_id == "thread-private"
    assert again.session_id == "thread-private-2"
    assert shared.session_id == "thread-shared"
    assert len(runtimes) == 2
    assert len(runtimes[0].requests) == 2
    assert (await pool.get_usage("thread-private")).service == "thread-private"
    assert (await pool.list_models())[0].id == "codex-test"

    await pool.close()
    assert all(runtime.closed for runtime in runtimes)


@pytest.mark.anyio
async def test_pool_fails_closed_without_an_audience_or_on_thread_collision() -> None:
    pool = CodexRuntimePool(
        shared_environment=_environment("shared"),
        runtime_factory=_Runtime,
    )
    with pytest.raises(RuntimeError, match="audience environment"):
        await pool.start_or_resume(SessionRequest(working_directory="/workspace"))

    for scope in ("private-a", "private-b"):
        request = SessionRequest(
            working_directory="/workspace",
            session_id="same-thread",
            memory_environment=_environment(scope),
        )
        if scope == "private-a":
            await pool.start_or_resume(request)
        else:
            with pytest.raises(RuntimeError, match="another audience"):
                await pool.start_or_resume(request)
    await pool.close()


@pytest.mark.anyio
async def test_pool_reconstructs_snapshot_runtime_from_opaque_route_after_restart() -> None:
    runtimes: list[_Runtime] = []

    def factory(environment: Mapping[str, str]) -> _Runtime:
        runtime = _Runtime(environment)
        runtimes.append(runtime)
        return runtime

    pool = CodexRuntimePool(
        shared_environment=_environment("shared"),
        runtime_factory=factory,
        route_environment_factory=lambda _audience, scope: _environment(scope),
    )
    private_scope = "private-" + "a" * 32

    snapshot = await pool.read_session_snapshot(
        "thread-from-journal",
        bounds=TranscriptBounds(),
        memory_audience="private",
        memory_scope=private_scope,
    )

    assert snapshot.thread_hash == hashlib.sha256(b"thread-from-journal").hexdigest()
    assert len(runtimes) == 1
    assert runtimes[0].environment["CCC_MEMORY_SCOPE"] == private_scope
    assert runtimes[0].snapshot_requests == ["thread-from-journal"]


@pytest.mark.anyio
async def test_pool_rejects_snapshot_route_collision() -> None:
    pool = CodexRuntimePool(
        shared_environment=_environment("shared"),
        runtime_factory=_Runtime,
        route_environment_factory=lambda _audience, scope: _environment(scope),
    )
    first_scope = "private-" + "a" * 32
    second_scope = "private-" + "b" * 32
    await pool.start_or_resume(
        SessionRequest(
            working_directory="/workspace",
            session_id="same-thread",
            memory_environment=_environment(first_scope),
        )
    )

    with pytest.raises(RuntimeError, match="another audience"):
        await pool.read_session_snapshot(
            "same-thread",
            bounds=TranscriptBounds(),
            memory_audience="private",
            memory_scope=second_scope,
        )
