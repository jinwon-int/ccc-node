"""Low-level async JSONL transport for ``codex app-server``."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias, cast

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
JsonRpcId: TypeAlias = int | str

# asyncio's StreamReader defaults to a 64 KiB line buffer. ``codex app-server``
# routinely emits single JSONL frames larger than that (e.g. big tool outputs or
# thread listings), and ``StreamReader.readline`` raises ``ValueError`` once a
# line exceeds the limit — which the reader loop turns into a fatal
# ``CodexConnectionClosedError``. Raise the stdout buffer so oversized frames are
# read whole instead of tearing down the connection.
STDOUT_BUFFER_LIMIT = 16 * 1024 * 1024  # 16 MiB


@dataclass(frozen=True, slots=True)
class CodexNotification:
    """A server notification, which never expects a response."""

    method: str
    params: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class CodexServerRequest:
    """A server-initiated request observed by the fail-closed handler."""

    id: JsonRpcId
    method: str
    params: Mapping[str, JsonValue]


ServerRequestHandler: TypeAlias = Callable[
    [CodexServerRequest], Awaitable[Mapping[str, JsonValue]]
]


class AsyncLineReader(Protocol):
    async def readline(self) -> bytes: ...


class AsyncWriter(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


class AppServerProcess(Protocol):
    @property
    def stdout(self) -> AsyncLineReader | None: ...

    @property
    def stdin(self) -> AsyncWriter | None: ...

    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


ProcessFactory: TypeAlias = Callable[[], Awaitable[AppServerProcess]]


class CodexConnectionClosedError(RuntimeError):
    """The app-server connection closed before a request completed."""


class CodexProtocolError(RuntimeError):
    """The app-server emitted a malformed JSON-RPC response."""


@dataclass(frozen=True, slots=True)
class CodexThreadSummary:
    """Defensively parsed metadata from a ``thread/list`` entry."""

    id: str
    title: str | None
    preview: str | None
    updated_at: float | None
    cwd: str | None
    model: str | None


@dataclass(frozen=True, slots=True)
class CodexThreadListPage:
    """One parsed page returned by ``thread/list``."""

    data: tuple[CodexThreadSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class CodexThread:
    """The bounded thread structure needed by the runtime history adapter."""

    id: str
    turns: tuple[Mapping[str, JsonValue], ...]


class CodexAppServerClient:
    """A single-connection Codex app-server JSONL client."""

    def __init__(
        self,
        *,
        reader: AsyncLineReader | None = None,
        writer: AsyncWriter | None = None,
        process_factory: ProcessFactory | None = None,
        executable: str = "codex",
        server_request_handler: ServerRequestHandler | None = None,
        server_request_timeout: float = 30.0,
        process_shutdown_timeout: float = 5.0,
    ) -> None:
        if (reader is None) != (writer is None):
            raise ValueError("reader and writer must be provided together")
        if server_request_timeout <= 0:
            raise ValueError("server request timeout must be positive")
        if process_shutdown_timeout <= 0:
            raise ValueError("process shutdown timeout must be positive")
        if not executable.strip():
            raise ValueError("Codex executable must not be empty")
        self._reader = reader
        self._writer = writer
        self._executable = executable
        self._process_factory = process_factory or self._spawn_default_process
        self._server_request_handler = server_request_handler
        self._server_request_timeout = server_request_timeout
        self._process_shutdown_timeout = process_shutdown_timeout
        self._process: AppServerProcess | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[JsonValue]] = {}
        self._notifications: asyncio.Queue[CodexNotification] = asyncio.Queue()
        self._server_requests: asyncio.Queue[CodexServerRequest] = asyncio.Queue()
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._process_task: asyncio.Task[None] | None = None
        self._server_request_tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._connection_error: CodexConnectionClosedError | None = None
        self._initialize_result: JsonValue = None
        self._started = False

    async def start(self) -> JsonValue:
        """Open the transport and complete the required initialization handshake."""

        async with self._start_lock:
            if self._closed:
                raise CodexConnectionClosedError("client is closed")
            if self._connection_error is not None:
                raise self._connection_error
            if self._started:
                return self._initialize_result
            if self._reader is None or self._writer is None:
                self._process = await self._process_factory()
                if self._process.stdout is None or self._process.stdin is None:
                    raise RuntimeError("app-server process did not provide stdio pipes")
                self._reader = self._process.stdout
                self._writer = self._process.stdin
                self._process_task = asyncio.create_task(self._watch_process())
            if self._reader_task is None:
                self._reader_task = asyncio.create_task(self._read_stdout())
            result = await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "ccc_node",
                        "title": "CCC Node",
                        "version": "0.1.0",
                    }
                },
            )
            await self.notify("initialized")
            self._initialize_result = result
            self._started = True
            return result

    async def request(self, method: str, params: Mapping[str, JsonValue] | None = None) -> JsonValue:
        """Send one request and await its correlated response."""

        if self._connection_error is not None:
            raise self._connection_error
        if self._closed or self._reader_task is None:
            raise CodexConnectionClosedError("client is not running")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[JsonValue] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: JsonObject = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = dict(params)
        try:
            await self._write(message)
            return await future
        except BaseException:
            if future.done() and not future.cancelled():
                future.exception()
            else:
                future.cancel()
            raise
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Mapping[str, JsonValue] | None = None) -> None:
        """Send one notification."""

        if self._connection_error is not None:
            raise self._connection_error
        if self._closed or self._reader_task is None:
            raise CodexConnectionClosedError("client is not running")
        message: JsonObject = {"method": method}
        if params is not None:
            message["params"] = dict(params)
        await self._write(message)

    async def thread_start(self, *, cwd: str, model: str | None = None) -> JsonValue:
        """Start a Codex thread."""

        params: JsonObject = {"cwd": cwd}
        if model is not None:
            params["model"] = model
        return await self.request("thread/start", params)

    async def thread_resume(
        self,
        thread_id: str,
        *,
        cwd: str | None = None,
        model: str | None = None,
    ) -> JsonValue:
        """Resume an existing Codex thread."""

        params: JsonObject = {"threadId": thread_id}
        if cwd is not None:
            params["cwd"] = cwd
        if model is not None:
            params["model"] = model
        return await self.request("thread/resume", params)

    async def thread_list(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
        sort_key: Literal["created_at", "updated_at", "recency_at"] = "updated_at",
    ) -> CodexThreadListPage:
        """List threads using the app-server v2 request shape."""

        if not 1 <= limit <= 100:
            raise ValueError("thread list limit must be between 1 and 100")
        params: JsonObject = {"limit": limit, "sortKey": sort_key}
        if cursor is not None:
            if not cursor:
                raise ValueError("thread list cursor must not be empty")
            params["cursor"] = cursor
        try:
            result = await self.request("thread/list", params)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise CodexProtocolError("thread/list request failed") from None
        if not isinstance(result, Mapping):
            return CodexThreadListPage((), None)
        raw_data = result.get("data")
        if not isinstance(raw_data, (list, tuple)):
            return CodexThreadListPage((), None)
        summaries: list[CodexThreadSummary] = []
        for raw in raw_data[:100]:
            if not isinstance(raw, Mapping):
                continue
            thread_id = raw.get("id")
            if not isinstance(thread_id, str) or not thread_id:
                continue
            updated = raw.get("updatedAt")
            if not isinstance(updated, (int, float)) or isinstance(updated, bool):
                updated = None
            summaries.append(
                CodexThreadSummary(
                    id=thread_id,
                    title=self._optional_string(raw.get("title", raw.get("name"))),
                    preview=self._optional_string(raw.get("preview")),
                    updated_at=float(updated) if updated is not None else None,
                    cwd=self._optional_string(raw.get("cwd")),
                    model=self._optional_string(raw.get("model")),
                )
            )
        return CodexThreadListPage(
            tuple(summaries), self._optional_string(result.get("nextCursor"))
        )

    async def thread_read(
        self,
        thread_id: str,
        *,
        include_turns: bool = True,
    ) -> CodexThread | None:
        """Read one thread using the app-server v2 request shape."""

        if not thread_id:
            raise ValueError("thread id must not be empty")
        try:
            result = await self.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": include_turns},
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise CodexProtocolError("thread/read request failed") from None
        if not isinstance(result, Mapping):
            return None
        thread = result.get("thread")
        if not isinstance(thread, Mapping):
            return None
        returned_id = thread.get("id")
        if not isinstance(returned_id, str) or returned_id != thread_id:
            return None
        raw_turns = thread.get("turns", ())
        if not isinstance(raw_turns, (list, tuple)):
            return None
        turns = tuple(
            cast(Mapping[str, JsonValue], dict(turn))
            for turn in raw_turns[:200]
            if isinstance(turn, Mapping)
        )
        return CodexThread(returned_id, turns)

    async def turn_start(
        self,
        thread_id: str,
        input_items: Sequence[Mapping[str, JsonValue]],
        *,
        model: str | None = None,
    ) -> JsonValue:
        """Start a turn with protocol input items."""

        params: JsonObject = {
            "threadId": thread_id,
            "input": [dict(item) for item in input_items],
        }
        if model is not None:
            params["model"] = model
        return await self.request("turn/start", params)

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> JsonValue:
        """Interrupt an in-flight turn."""

        return await self.request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
        )

    async def list_models(self, *, include_hidden: bool = False) -> JsonValue:
        """List models advertised by Codex."""

        params: JsonObject = {}
        if include_hidden:
            params["includeHidden"] = True
        return await self.request("model/list", params)

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    async def next_notification(self) -> CodexNotification:
        """Wait for the next server notification."""

        return await self._notifications.get()

    async def next_server_request(self) -> CodexServerRequest:
        """Wait for the next server request observed by the fail-closed handler."""

        return await self._server_requests.get()

    async def close(self) -> None:
        """Close the transport. Repeated calls are harmless and bounded."""

        if self._closed:
            return
        self._closed = True
        error = CodexConnectionClosedError("client closed")
        self._fail_pending(error)

        for task in tuple(self._server_request_tasks):
            task.cancel()
        if self._server_request_tasks:
            await asyncio.gather(*self._server_request_tasks, return_exceptions=True)

        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)

        if self._writer is not None:
            self._writer.close()
            try:
                await asyncio.wait_for(
                    self._writer.wait_closed(),
                    timeout=self._process_shutdown_timeout,
                )
            except TimeoutError:
                pass

        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
        if self._process_task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._process_task),
                    timeout=self._process_shutdown_timeout,
                )
            except TimeoutError:
                if self._process is not None and self._process.returncode is None:
                    self._process.kill()
                await asyncio.gather(self._process_task, return_exceptions=True)

    async def _spawn_default_process(self) -> AppServerProcess:
        process = await asyncio.create_subprocess_exec(
            self._executable,
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=STDOUT_BUFFER_LIMIT,
        )
        return cast(AppServerProcess, process)

    async def _watch_process(self) -> None:
        process = cast(AppServerProcess, self._process)
        try:
            returncode = await process.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                self._record_connection_error(
                    CodexConnectionClosedError(f"app-server process wait failed: {exc}")
                )
            return
        if not self._closed:
            self._record_connection_error(
                CodexConnectionClosedError(
                    f"app-server process exited with status {returncode}"
                )
            )

    async def _write(self, message: JsonObject) -> None:
        writer = cast(AsyncWriter, self._writer)
        payload = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        try:
            async with self._write_lock:
                writer.write(payload)
                await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = CodexConnectionClosedError(f"app-server write failed: {exc}")
            self._record_connection_error(error)
            raise error from exc

    async def _read_stdout(self) -> None:
        reader = cast(AsyncLineReader, self._reader)
        try:
            while line := await reader.readline():
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue

                request_id = message.get("id")
                if "method" in message:
                    method = message.get("method")
                    params = message.get("params", {})
                    if not isinstance(method, str) or not isinstance(params, dict):
                        continue
                    if request_id is None:
                        await self._notifications.put(CodexNotification(method, params))
                    elif self._is_rpc_id(request_id):
                        request = CodexServerRequest(request_id, method, params)
                        await self._server_requests.put(request)
                        task = asyncio.create_task(self._dispatch_server_request(request))
                        self._server_request_tasks.add(task)
                        task.add_done_callback(self._server_request_tasks.discard)
                    continue

                if not isinstance(request_id, int) or isinstance(request_id, bool):
                    continue
                future = self._pending.get(request_id)
                if future is None or future.done():
                    continue
                has_result = "result" in message
                has_error = "error" in message
                if has_result == has_error:
                    future.set_exception(
                        CodexProtocolError("response must contain exactly one of result or error")
                    )
                elif has_result:
                    future.set_result(cast(JsonValue, message["result"]))
                else:
                    future.set_exception(RuntimeError(str(message["error"])))

            if not self._closed:
                self._record_connection_error(
                    CodexConnectionClosedError("app-server stdout reached EOF")
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                self._record_connection_error(
                    CodexConnectionClosedError(f"app-server reader failed: {exc}")
                )

    async def _dispatch_server_request(self, request: CodexServerRequest) -> None:
        response = self._default_server_response_payload(request.method)
        if self._server_request_handler is not None:
            try:
                candidate = await asyncio.wait_for(
                    self._server_request_handler(request),
                    timeout=self._server_request_timeout,
                )
                response = self._validate_server_response_payload(candidate)
            except asyncio.CancelledError:
                raise
            except Exception:
                response = self._default_server_response_payload(request.method)

        message: JsonObject = {"id": request.id}
        message.update(response)
        try:
            await self._write(message)
        except CodexConnectionClosedError:
            return

    def _fail_malformed_response(self, request_id: object, reason: str) -> None:
        if not isinstance(request_id, int) or isinstance(request_id, bool):
            return
        future = self._pending.get(request_id)
        if future is not None and not future.done():
            future.set_exception(CodexProtocolError(reason))

    @staticmethod
    def _validate_server_response_payload(
        response: Mapping[str, JsonValue],
    ) -> JsonObject:
        payload = dict(response)
        if set(payload) not in ({"result"}, {"error"}):
            raise CodexProtocolError(
                "server request handler must return exactly one of result or error"
            )
        return payload

    @staticmethod
    def _is_rpc_id(value: object) -> bool:
        return isinstance(value, (int, str)) and not isinstance(value, bool)

    def _record_connection_error(self, error: CodexConnectionClosedError) -> None:
        if self._connection_error is None:
            self._connection_error = error
        self._fail_pending(self._connection_error)

    def _fail_pending(self, error: BaseException) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    @staticmethod
    def _default_server_response_payload(method: str) -> JsonObject:
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            return {"result": {"decision": "decline"}}
        if method == "item/permissions/requestApproval":
            return {"result": {"permissions": {}, "scope": "turn"}}
        if method == "mcpServer/elicitation/request":
            return {
                "result": {"action": "decline", "content": None, "_meta": None},
            }
        return {
            "error": {
                "code": -32601,
                "message": "Client does not support server request",
            },
        }
