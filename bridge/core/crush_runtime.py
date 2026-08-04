"""Crush runtime adapter — first-class bridge provider for the crush harness.

Crush (charmbracelet) is the ccc-node harness designated for the Kimi-K3 and
GLM-5.2 nodes (issue #923).  The adapter drives a spawned ``crush server``
process over its HTTP + SSE API:

- one workspace per working directory (crush keys workspaces by path),
- one SSE event stream per workspace held open for its lifetime (crush tears
  a workspace down when its last event stream disconnects),
- one active turn per session, serialized by a per-session lock,
- approvals normalized through the fail-closed chain: an omitted, failing,
  or late approval handler always resolves to ``deny``.

The transport is isolated behind :class:`CrushClient` so the conformance
suite can drive the adapter with a scripted fake and no live provider.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.error
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .agent_runtime import (
    AgentEvent,
    AgentSession,
    ApprovalDecision,
    ApprovalHandler,
    ApprovalRequestEvent,
    CompletionEvent,
    ErrorEvent,
    MessageCompletedEvent,
    ModelInfo,
    ReasoningDeltaEvent,
    ResultEvent,
    SessionHistory,
    SessionHistoryMessage,
    SessionRequest,
    SessionSummary,
    TextDeltaEvent,
    ToolCompletedEvent,
    ToolStartedEvent,
    deny_approval,
)

_CRUSH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

# Credential env keys that auto-activate crush's bundled Anthropic provider.
# The bridge process env carries these for the Claude lane; when they leak
# into the crush subprocess, crush's built-in model fallback silently targets
# api.anthropic.com with a key that is not valid there (canary4 401, #926).
# crushrc-defined providers are unaffected — only inherited credentials are
# stripped. Pass process_environment explicitly to opt out.
_INHERITED_ENV_BLOCKLIST = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"})


def _default_crush_config() -> Path:
    """Fleet-managed crushrc for the bridge lane (#938).

    Not `crushrc.readonly` — that one is the agent-cron runner's config and
    denies bash/edit/write. Staging it here left the owner-facing bot unable
    to run a shell, which is the opposite of this lane's policy
    (owner-operator / bash_policy=auto-approve). See crush/crushrc.bridge.
    """
    return Path(__file__).resolve().parents[2] / "crush" / "crushrc.bridge"
_SESSION_READ_LIMIT = 50
_SESSION_LIST_LIMIT = 100
_TEXT_BOUND = 2000

# Finish reasons (proto Finish.Reason). crush normalizes across provider
# families but passes each family's own spelling through, so one concept
# arrives under two names: OpenAI says `tool_calls` / `length`, Anthropic says
# `tool_use` / `max_tokens`.
#
# A tool-call finish ends the *assistant message*, not the turn: crush runs
# the tool and the model keeps going. Measured on dungae (2026-08-04) with
# GLM-5.2 asked to read a file:
#
#   finish reasons in order: ['tool_use', 'end_turn']
#
# Treating the first one as the end of the turn closes the stream before the
# answer exists — the turn returned empty. Treating it as a failure (the
# original behaviour, since only the OpenAI spelling was listed) surfaced
# "❌ Processing failed: tool_use". Neither is right: wait for the real
# terminal reason.
#
# `tool_calls` is the same signal under OpenAI naming. The kimi pilot only
# ever produced the Anthropic spelling, so its presence in the terminal set
# was never exercised — it would have truncated turns the same way.
_FINISH_CONTINUE = {"tool_calls", "tool_use"}
_FINISH_OK = {"end_turn", "stop", "stop_sequence", "length", "max_tokens"}
_FINISH_CANCELED = {"canceled", "cancelled"}


@dataclass(frozen=True, slots=True)
class CrushEvent:
    """One raw event from the crush workspace SSE stream."""

    kind: str  # envelope type: message | session | agent_event | permission_request | ...
    change: str  # created | updated | ""
    workspace_id: str
    session_id: str
    payload: Mapping[str, Any]


class CrushClient(Protocol):
    """Transport seam between the runtime and a crush server."""

    async def start(self) -> None: ...

    async def workspace_ensure(self, *, cwd: str, model: str | None) -> str: ...

    async def session_create(self, workspace_id: str) -> str: ...

    async def session_exists(self, workspace_id: str, session_id: str) -> bool: ...

    async def prompt_send(
        self, workspace_id: str, session_id: str, text: str, *, run_id: str
    ) -> None: ...

    async def turn_cancel(self, workspace_id: str, session_id: str) -> None: ...

    async def permission_reply(
        self, workspace_id: str, request: Mapping[str, Any], decision: str
    ) -> None: ...

    async def list_models(self, workspace_id: str) -> Sequence[Mapping[str, Any]]: ...

    async def session_list(
        self, workspace_id: str, *, limit: int
    ) -> Sequence[Mapping[str, Any]]: ...

    async def session_messages(
        self, workspace_id: str, session_id: str
    ) -> Sequence[Mapping[str, Any]]: ...

    async def session_usage(
        self, workspace_id: str, session_id: str
    ) -> Mapping[str, Any]: ...

    async def next_event(self) -> CrushEvent: ...

    async def close(self) -> None: ...


ClientFactory = Callable[[], CrushClient]


def _checked_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _CRUSH_ID_RE.match(value):
        raise RuntimeError(f"crush returned malformed {what}")
    return value


class CrushServerClient:
    """Live transport: spawn ``crush server`` and talk HTTP + SSE.

    HTTP calls are blocking stdlib calls pushed to a thread so the bridge's
    event loop is never blocked.  The SSE stream runs on a daemon thread that
    pushes parsed envelopes into an asyncio queue.
    """

    def __init__(
        self,
        *,
        executable: str = "crush",
        process_environment: Mapping[str, str] | None = None,
        config_path: str | os.PathLike[str] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        readiness_timeout_seconds: float = 15.0,
    ) -> None:
        self._executable = executable
        self._config_dir: tempfile.TemporaryDirectory[str] | None = None
        if process_environment is not None:
            # The caller owns the whole environment, staging included.
            self._env = dict(process_environment)
        else:
            self._env = {
                k: v for k, v in os.environ.items() if k not in _INHERITED_ENV_BLOCKLIST
            }
            # The crush server learns the fleet providers and the read-only
            # permission set only from CRUSH_GLOBAL_CONFIG. Without it crush
            # falls back to /etc/crush and the user data dir, which on a fresh
            # node hold nothing: measured on dungae (2026-08-04) an
            # unconfigured server died with "No providers configured" (#938).
            # Stage the same crushrc the headless runner uses.
            if "CRUSH_GLOBAL_CONFIG" not in self._env:
                source = Path(config_path) if config_path else _default_crush_config()
                if source.is_file():
                    # mode 700 — the config expands key files at load time.
                    self._config_dir = tempfile.TemporaryDirectory(
                        prefix="ccc-crush-cfg."
                    )
                    shutil.copyfile(source, Path(self._config_dir.name) / "crushrc")
                    self._env["CRUSH_GLOBAL_CONFIG"] = self._config_dir.name
        self._host = host
        self._port = port
        self._readiness_timeout = readiness_timeout_seconds
        self._proc: subprocess.Popen[bytes] | None = None
        self._base = ""
        self._client_id = ""
        self._queue: asyncio.Queue[CrushEvent] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sse_threads: list[threading.Thread] = []
        self._closed = False
        self._workspaces_by_path: dict[str, str] = {}
        self._sse_started: set[str] = set()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._proc is not None:
            return
        self._loop = asyncio.get_running_loop()
        port = self._port or _free_port(self._host)
        self._base = f"http://{self._host}:{port}/v1"
        self._client_id = str(uuid.uuid4())
        self._proc = subprocess.Popen(
            [self._executable, "server", "-H", f"tcp://{self._host}:{port}"],
            env=self._env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + self._readiness_timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError("crush server exited during startup")
            try:
                await self._http("GET", "/health")
                return
            except OSError:
                await asyncio.sleep(0.2)
        raise RuntimeError("crush server did not become ready")

    async def close(self) -> None:
        self._closed = True
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 10)
            except subprocess.TimeoutExpired:
                proc.kill()
        # Remove the staged config only after the server is gone, and never
        # let cleanup failure mask a shutdown error.
        cfg, self._config_dir = self._config_dir, None
        if cfg is not None:
            cfg.cleanup()

    # -- HTTP helpers ------------------------------------------------------

    async def _http(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> Any:
        url = self._base + path
        data = json.dumps(body).encode() if body is not None else None

        def call() -> Any:
            req = urllib.request.Request(
                url, data=data, method=method,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}

        try:
            return await asyncio.to_thread(call)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"crush server {method} {path} -> HTTP {exc.code}") from exc

    def _q(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}client_id={self._client_id}"

    # -- workspace / session ------------------------------------------------

    async def workspace_ensure(self, *, cwd: str, model: str | None) -> str:
        wsid = self._workspaces_by_path.get(cwd)
        if wsid is None:
            listed = await self._http("GET", "/workspaces")
            for ws in listed if isinstance(listed, list) else []:
                if isinstance(ws, Mapping) and ws.get("path") == cwd:
                    wsid = _checked_id(ws.get("id"), "workspace id")
                    break
            if wsid is None:
                created = await self._http(
                    "POST", "/workspaces",
                    {"path": cwd, "client_id": self._client_id},
                )
                wsid = _checked_id(created.get("id"), "workspace id")
            self._workspaces_by_path[cwd] = wsid
        # crush tears a workspace down when its last SSE stream closes, so the
        # stream is opened before any other workspace traffic and held open.
        if wsid not in self._sse_started:
            self._start_sse(wsid)
            self._sse_started.add(wsid)
        if model is not None:
            provider, _, model_id = model.partition("/")
            if not provider or not model_id:
                raise RuntimeError(f"crush model must be provider/model, got: {model}")
            await self._http(
                "POST", self._q(f"/workspaces/{wsid}/config/model"),
                {"model": {"provider": provider, "model": model_id},
                 "model_type": "large", "scope": 1},
            )
        return wsid

    async def session_create(self, workspace_id: str) -> str:
        created = await self._http(
            "POST", self._q(f"/workspaces/{workspace_id}/sessions"), {},
        )
        return _checked_id(created.get("id"), "session id")

    async def session_exists(self, workspace_id: str, session_id: str) -> bool:
        try:
            got = await self._http(
                "GET", self._q(f"/workspaces/{workspace_id}/sessions/{session_id}"),
            )
        except RuntimeError:
            return False
        return isinstance(got, Mapping) and got.get("id") == session_id

    async def prompt_send(
        self, workspace_id: str, session_id: str, text: str, *, run_id: str
    ) -> None:
        await self._http(
            "POST", self._q(f"/workspaces/{workspace_id}/agent"),
            {"session_id": session_id, "prompt": text, "run_id": run_id},
        )

    async def turn_cancel(self, workspace_id: str, session_id: str) -> None:
        await self._http(
            "POST",
            self._q(f"/workspaces/{workspace_id}/agent/sessions/{session_id}/cancel"),
            {},
        )

    async def permission_reply(
        self, workspace_id: str, request: Mapping[str, Any], decision: str
    ) -> None:
        await self._http(
            "POST", self._q(f"/workspaces/{workspace_id}/permissions/grant"),
            {"permission": dict(request), "action": decision},
        )

    async def list_models(self, workspace_id: str) -> Sequence[Mapping[str, Any]]:
        got = await self._http("GET", self._q(f"/workspaces/{workspace_id}/providers"))
        return got if isinstance(got, list) else []

    async def session_list(
        self, workspace_id: str, *, limit: int
    ) -> Sequence[Mapping[str, Any]]:
        got = await self._http("GET", self._q(f"/workspaces/{workspace_id}/sessions"))
        if not isinstance(got, list):
            return []
        return got[:limit]

    async def session_messages(
        self, workspace_id: str, session_id: str
    ) -> Sequence[Mapping[str, Any]]:
        got = await self._http(
            "GET", self._q(f"/workspaces/{workspace_id}/sessions/{session_id}/messages"),
        )
        return got if isinstance(got, list) else []

    async def session_usage(
        self, workspace_id: str, session_id: str
    ) -> Mapping[str, Any]:
        got = await self._http(
            "GET", self._q(f"/workspaces/{workspace_id}/sessions/{session_id}"),
        )
        return got if isinstance(got, Mapping) else {}

    # -- SSE -----------------------------------------------------------------

    def _start_sse(self, workspace_id: str) -> None:
        url = self._base + self._q(f"/workspaces/{workspace_id}/events")

        def run() -> None:
            while not self._closed:
                try:
                    req = urllib.request.Request(url)
                    with urllib.request.urlopen(req, timeout=None) as resp:
                        for raw in resp:
                            if self._closed:
                                return
                            line = raw.decode("utf-8", "replace").strip()
                            if not line.startswith("data:"):
                                continue
                            try:
                                envelope = json.loads(line[5:].strip())
                            except json.JSONDecodeError:
                                continue
                            event = _parse_envelope(envelope, workspace_id)
                            if event is not None and self._loop is not None:
                                self._loop.call_soon_threadsafe(
                                    self._queue.put_nowait, event,
                                )
                except OSError:
                    if not self._closed:
                        time.sleep(1.0)

        thread = threading.Thread(target=run, name=f"crush-sse-{workspace_id[:8]}", daemon=True)
        thread.start()
        self._sse_threads.append(thread)

    async def next_event(self) -> CrushEvent:
        return await self._queue.get()


def _free_port(host: str) -> int:
    import socket

    with socket.socket() as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _parse_envelope(envelope: Any, workspace_id: str) -> CrushEvent | None:
    if not isinstance(envelope, Mapping):
        return None
    kind = envelope.get("type")
    inner = envelope.get("payload")
    if not isinstance(kind, str) or not isinstance(inner, Mapping):
        return None
    raw_change = inner.get("type")
    change = raw_change if isinstance(raw_change, str) else ""
    payload = inner.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    raw_id = payload.get("session_id") or payload.get("id") or ""
    session_id = raw_id if isinstance(raw_id, str) else ""
    return CrushEvent(kind, change, workspace_id, session_id, payload)


@dataclass(slots=True)
class _ActiveTurn:
    queue: asyncio.Queue[AgentEvent]
    approval_handler: ApprovalHandler
    run_id: str | None = None
    turn_ready: asyncio.Event = field(default_factory=asyncio.Event)
    finished: bool = False
    # per-assistant-message streaming state, keyed by provider message id
    emitted_text: bool = False
    text_seen: dict[str, int] = field(default_factory=dict)
    reasoning_seen: dict[str, int] = field(default_factory=dict)
    current_message_id: str | None = None
    tools_seen: set[str] = field(default_factory=set)
    tools_completed: set[str] = field(default_factory=set)
    collected_text: list[str] = field(default_factory=list)


class CrushSession:
    """One provider-neutral session backed by a crush session."""

    def __init__(
        self,
        runtime: CrushRuntime,
        workspace_id: str,
        session_id: str,
    ) -> None:
        self._runtime = runtime
        self._workspace_id = workspace_id
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    def send_turn(
        self,
        message: str,
        *,
        approval_handler: ApprovalHandler = deny_approval,
    ) -> AsyncIterator[AgentEvent]:
        async def events() -> AsyncIterator[AgentEvent]:
            lock = self._runtime._turn_lock_for(self._session_id)
            async with lock:
                active = _ActiveTurn(asyncio.Queue(), approval_handler)
                self._runtime._active_turns[self._session_id] = active
                try:
                    run_id = str(uuid.uuid4())
                    active.run_id = run_id
                    client = await self._runtime._ensure_started()
                    await client.prompt_send(
                        self._workspace_id, self._session_id, message, run_id=run_id,
                    )
                    active.turn_ready.set()
                    while True:
                        event = await active.queue.get()
                        yield event
                        if isinstance(event, (CompletionEvent, ErrorEvent)):
                            return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    yield ErrorEvent(
                        code="crush_runtime_error",
                        message=str(exc) or "crush runtime request failed",
                    )
                finally:
                    active.finished = True
                    active.turn_ready.set()
                    if self._runtime._active_turns.get(self._session_id) is active:
                        self._runtime._active_turns.pop(self._session_id, None)

        return events()

    async def interrupt(self) -> None:
        active = self._runtime._active_turns.get(self._session_id)
        client = self._runtime._client
        if active is None or active.finished or not active.turn_ready.is_set():
            return
        if client is None:
            return
        await client.turn_cancel(self._workspace_id, self._session_id)


class CrushRuntime:
    """Own a crush server client, its SSE dispatcher, and the approval chain."""

    def __init__(
        self,
        *,
        client_factory: ClientFactory | None = None,
        executable: str = "crush",
        process_environment: Mapping[str, str] | None = None,
        config_path: str | os.PathLike[str] | None = None,
    ) -> None:
        if client_factory is not None:
            self._client_factory = client_factory
        else:
            def default_factory() -> CrushClient:
                return CrushServerClient(
                    executable=executable,
                    process_environment=process_environment,
                    config_path=config_path,
                )

            self._client_factory = default_factory
        self._client: CrushClient | None = self._client_factory()
        self._dispatcher: asyncio.Task[None] | None = None
        self._active_turns: dict[str, _ActiveTurn] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._workspaces: dict[str, str] = {}  # cwd -> workspace_id
        self._closed = False

    # -- lifecycle -----------------------------------------------------------

    async def _ensure_started(self) -> CrushClient:
        if self._closed:
            raise RuntimeError("crush runtime is closed")
        client = self._client
        if client is None:
            raise RuntimeError("crush runtime is closed")
        if self._dispatcher is None:
            await client.start()
            self._dispatcher = asyncio.create_task(self._dispatch_loop())
        return client

    async def close(self) -> None:
        self._closed = True
        for active in self._active_turns.values():
            if not active.finished:
                active.queue.put_nowait(
                    ErrorEvent(code="crush_runtime_closed", message="runtime closed")
                )
                active.finished = True
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            self._dispatcher = None
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def _any_workspace(self, client: CrushClient) -> str:
        wsid = next(iter(self._workspaces.values()), None)
        if wsid is None:
            wsid = await client.workspace_ensure(cwd=".", model=None)
            self._workspaces["."] = wsid
        return wsid

    def _turn_lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._turn_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._turn_locks[session_id] = lock
        return lock

    # -- factory -------------------------------------------------------------

    async def start_or_resume(self, request: SessionRequest) -> AgentSession:
        client = await self._ensure_started()
        wsid = self._workspaces.get(request.working_directory)
        if wsid is None:
            wsid = await client.workspace_ensure(
                cwd=request.working_directory, model=request.model,
            )
            self._workspaces[request.working_directory] = wsid
        if request.session_id is None:
            session_id = await client.session_create(wsid)
        else:
            if not await client.session_exists(wsid, request.session_id):
                raise RuntimeError("crush session not found: " + request.session_id)
            session_id = request.session_id
        return CrushSession(self, wsid, session_id)

    async def list_models(self) -> Sequence[ModelInfo]:
        client = await self._ensure_started()
        wsid = await self._any_workspace(client)
        raw = await client.list_models(wsid)
        models: list[ModelInfo] = []
        for index, entry in enumerate(raw):
            if not isinstance(entry, Mapping):
                continue
            provider = entry.get("provider") or entry.get("id") or ""
            for model in entry.get("models") or []:
                if not isinstance(model, Mapping):
                    continue
                model_id = model.get("id") or model.get("model")
                if not model_id:
                    continue
                models.append(ModelInfo(
                    id=f"{provider}/{model_id}" if provider else str(model_id),
                    display_name=str(model.get("name") or model_id),
                    is_default=(index == 0 and not models),
                ))
        return models

    # -- session browsing ------------------------------------------------------

    @property
    def supports_session_browsing(self) -> bool:
        return True

    async def list_sessions(self, *, limit: int = 10) -> Sequence[SessionSummary]:
        client = await self._ensure_started()
        bounded = max(1, min(limit, _SESSION_LIST_LIMIT))
        wsid = await self._any_workspace(client)
        raw = await client.session_list(wsid, limit=bounded)
        out: list[SessionSummary] = []
        for entry in raw[:bounded]:
            if not isinstance(entry, Mapping) or not entry.get("id"):
                continue
            updated = entry.get("updated_at")
            out.append(SessionSummary(
                id=str(entry["id"]),
                title=entry.get("title") or None,
                updated_at=float(updated) if isinstance(updated, (int, float)) else None,
            ))
        return out

    async def read_session(self, session_id: str, *, limit: int = 5) -> SessionHistory:
        client = await self._ensure_started()
        wsid = await self._any_workspace(client)
        raw = await client.session_messages(wsid, session_id)
        messages: list[SessionHistoryMessage] = []
        for msg in raw:
            if not isinstance(msg, Mapping):
                continue
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _message_text(msg)
            if not text:
                continue
            messages.append(SessionHistoryMessage(
                role=role, content=text[:_TEXT_BOUND],
            ))
        bounded = max(1, min(limit, _SESSION_READ_LIMIT))
        return SessionHistory(session_id=session_id, messages=tuple(messages[-bounded:]))

    # -- usage ---------------------------------------------------------------

    async def get_usage(self, session_id: str | None) -> Mapping[str, int | float | str]:
        client = await self._ensure_started()
        wsid = next(iter(self._workspaces.values()), None)
        if wsid is None or session_id is None:
            return {"provider": "crush"}
        got = await client.session_usage(wsid, session_id)
        prompt = got.get("prompt_tokens")
        completion = got.get("completion_tokens")
        cost = got.get("cost")
        out: dict[str, int | float | str] = {"provider": "crush"}
        if isinstance(prompt, int) and isinstance(completion, int):
            out["input_tokens"] = prompt
            out["output_tokens"] = completion
            out["total_tokens"] = prompt + completion
        if isinstance(cost, (int, float)):
            out["total_cost_usd"] = cost
        return out

    # -- dispatcher ------------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        assert self._client is not None
        while True:
            event = await self._client.next_event()
            try:
                self._route_event(event)
            except Exception:
                continue

    def _route_event(self, event: CrushEvent) -> None:
        if event.kind == "message":
            self._route_message(event)
        elif event.kind == "agent_event":
            self._route_agent_event(event)
        elif event.kind == "permission_request":
            self._spawn_permission(event)

    def _route_message(self, event: CrushEvent) -> None:
        active = self._active_turns.get(event.session_id)
        if active is None or active.finished:
            return
        if event.payload.get("role") != "assistant":
            return
        message_id = str(event.payload.get("id") or "")
        if active.current_message_id and message_id and message_id != active.current_message_id:
            if active.emitted_text:
                active.queue.put_nowait(MessageCompletedEvent())
                active.emitted_text = False
            active.current_message_id = message_id
        elif active.current_message_id is None and message_id:
            active.current_message_id = message_id
        parts = event.payload.get("parts")
        if not isinstance(parts, list):
            return
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            self._route_part(active, message_id, part)

    def _route_part(
        self, active: _ActiveTurn, message_id: str, part: Mapping[str, Any],
    ) -> None:
        ptype = part.get("type")
        data = part.get("data")
        if not isinstance(data, Mapping):
            data = {}
        if ptype == "text":
            text = data.get("text")
            if not isinstance(text, str):
                return
            seen = active.text_seen.get(message_id, 0)
            if len(text) > seen:
                delta = text[seen:]
                active.text_seen[message_id] = len(text)
                active.collected_text.append(delta)
                active.emitted_text = True
                active.queue.put_nowait(TextDeltaEvent(delta))
        elif ptype == "reasoning":
            text = data.get("text") or data.get("reasoning")
            if not isinstance(text, str):
                return
            seen = active.reasoning_seen.get(message_id, 0)
            if len(text) > seen:
                active.reasoning_seen[message_id] = len(text)
                active.queue.put_nowait(ReasoningDeltaEvent(text[seen:]))
        elif ptype in ("tool_call", "tool_result"):
            self._route_tool_part(active, ptype, data)
        elif ptype == "finish":
            self._complete_turn(active, data)

    def _route_tool_part(
        self, active: _ActiveTurn, ptype: str, data: Mapping[str, Any],
    ) -> None:
        if ptype == "tool_call":
            tool_id = data.get("id")
            name = data.get("name")
            if not tool_id or not name or data.get("finished"):
                return
            if str(tool_id) in active.tools_seen:
                return
            active.tools_seen.add(str(tool_id))
            active.queue.put_nowait(ToolStartedEvent(
                tool_call_id=str(tool_id), tool_name=str(name),
                arguments={"input": str(data.get("input") or "")},
            ))
            return
        tool_id = data.get("tool_call_id")
        name = data.get("name")
        if not tool_id or not name:
            return
        if str(tool_id) in active.tools_completed:
            return
        active.tools_completed.add(str(tool_id))
        if str(tool_id) not in active.tools_seen:
            active.tools_seen.add(str(tool_id))
            active.queue.put_nowait(ToolStartedEvent(
                tool_call_id=str(tool_id), tool_name=str(name), arguments={},
            ))
        active.queue.put_nowait(ToolCompletedEvent(
            tool_call_id=str(tool_id), tool_name=str(name),
            result=str(data.get("content") or data.get("data") or ""),
            success=not bool(data.get("is_error")),
        ))

    def _route_agent_event(self, event: CrushEvent) -> None:
        active = self._active_turns.get(event.session_id)
        if active is None or active.finished:
            return
        payload = event.payload
        if payload.get("type") == "error" or payload.get("error"):
            message = payload.get("error") or payload.get("message") or "crush agent error"
            if isinstance(message, Mapping):
                message = json.dumps(message, ensure_ascii=False)[:200]
            active.queue.put_nowait(ErrorEvent(
                code="crush_agent_error", message=str(message)[:500],
            ))
            active.finished = True

    def _complete_turn(self, active: _ActiveTurn, data: Mapping[str, Any]) -> None:
        reason = str(data.get("reason") or "")
        if reason in _FINISH_CONTINUE:
            # The assistant message ended so crush can run the tool; the turn
            # is still open. Emitting nothing here keeps the stream alive for
            # the follow-up message and its real terminal finish.
            return
        if reason in _FINISH_CANCELED:
            active.queue.put_nowait(ErrorEvent(
                code="interrupted", message="turn interrupted", retryable=False,
            ))
        elif reason in _FINISH_OK:
            result_text = "".join(active.collected_text)
            active.queue.put_nowait(ResultEvent({"text": result_text}))
            active.queue.put_nowait(CompletionEvent(reason))
        else:
            detail = data.get("message") or data.get("details") or reason or "unknown"
            active.queue.put_nowait(ErrorEvent(
                code="crush_turn_failed", message=str(detail)[:500], retryable=False,
            ))
        active.finished = True

    # -- approvals (fail-closed) ---------------------------------------------

    def _spawn_permission(self, event: CrushEvent) -> None:
        asyncio.create_task(self._handle_permission(event))

    async def _handle_permission(self, event: CrushEvent) -> None:
        decision = ApprovalDecision.DENY
        try:
            decision = await self._decide_permission(event)
        except Exception:
            decision = ApprovalDecision.DENY
        if self._client is not None:
            try:
                await self._client.permission_reply(
                    event.workspace_id, event.payload, decision.value,
                )
            except Exception:
                pass

    async def _decide_permission(self, event: CrushEvent) -> ApprovalDecision:
        payload = event.payload
        request_id = payload.get("id")
        action = payload.get("action") or payload.get("tool_name")
        description = payload.get("description")
        if not request_id or not action or not description:
            return ApprovalDecision.DENY
        active = self._active_turns.get(event.session_id)
        if active is None or active.finished:
            return ApprovalDecision.DENY
        try:
            await asyncio.wait_for(active.turn_ready.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            return ApprovalDecision.DENY
        if active.finished:
            return ApprovalDecision.DENY
        params = payload.get("params")
        arguments: Mapping[str, Any] = params if isinstance(params, Mapping) else {}
        request = ApprovalRequestEvent(
            request_id=str(request_id), action=str(action),
            arguments=arguments, description=str(description),
        )
        active.queue.put_nowait(request)
        try:
            return await active.approval_handler(request)
        except Exception:
            return ApprovalDecision.DENY


def _message_text(message: Mapping[str, Any]) -> str:
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, Mapping) or part.get("type") != "text":
            continue
        data = part.get("data")
        if isinstance(data, Mapping) and isinstance(data.get("text"), str):
            chunks.append(data["text"])
    return "".join(chunks)
