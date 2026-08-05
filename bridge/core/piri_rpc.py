"""Async JSONL transport for Piri's headless RPC mode."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import suppress
import json
import os
import signal
from typing import Any, TypeAlias, cast


JsonObject: TypeAlias = dict[str, Any]
STDOUT_BUFFER_LIMIT = 16 * 1024 * 1024


class PiriConnectionClosedError(RuntimeError):
    """The Piri subprocess closed before an RPC operation completed."""


class PiriProtocolError(RuntimeError):
    """Piri emitted a malformed or unsuccessful RPC response."""


class PiriRpcProcessClient:
    """One Piri RPC subprocess with correlated commands and streamed events.

    Piri's built-in tools do not have a permission-request protocol: they run
    with the permissions of this subprocess.  ``auto_confirm`` applies only to
    yes/no dialogs emitted by optional extensions.  Selection and text-entry
    dialogs are cancelled because they have no unambiguous unattended answer.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        working_directory: str,
        environment: Mapping[str, str] | None = None,
        auto_confirm: bool = True,
        request_timeout: float = 30.0,
        shutdown_timeout: float = 5.0,
    ) -> None:
        if not command or any(not isinstance(part, str) or not part for part in command):
            raise ValueError("Piri command must not be empty")
        if not working_directory:
            raise ValueError("Piri working directory must not be empty")
        if request_timeout <= 0:
            raise ValueError("Piri request timeout must be positive")
        if shutdown_timeout <= 0:
            raise ValueError("Piri shutdown timeout must be positive")
        if environment is not None:
            for name, value in environment.items():
                if not isinstance(name, str) or not name or "\x00" in name:
                    raise ValueError("Piri process environment name is invalid")
                if not isinstance(value, str) or "\x00" in value:
                    raise ValueError("Piri process environment value is invalid")

        self._command = tuple(command)
        self._working_directory = working_directory
        self._environment = dict(environment) if environment is not None else None
        self._auto_confirm = auto_confirm
        self._request_timeout = request_timeout
        self._shutdown_timeout = shutdown_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[Mapping[str, Any]]] = {}
        self._events: asyncio.Queue[Mapping[str, Any] | BaseException] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._next_id = 1
        self._closed = False
        self._connection_error: PiriConnectionClosedError | None = None

    async def start(self) -> None:
        """Start the subprocess and its stdout/stderr drain tasks."""

        async with self._start_lock:
            if self._closed:
                raise PiriConnectionClosedError("Piri client is closed")
            if self._connection_error is not None:
                raise self._connection_error
            if self._process is not None:
                return
            process_options: dict[str, Any] = {}
            if os.name == "posix":
                # Piri session JSONL and extension state must stay owner-only
                # even when the bridge service inherited a permissive umask.
                process_options["umask"] = 0o077
            process = await asyncio.create_subprocess_exec(
                *self._command,
                cwd=self._working_directory,
                env=self._environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=STDOUT_BUFFER_LIMIT,
                **process_options,
            )
            if process.stdin is None or process.stdout is None or process.stderr is None:
                process.kill()
                await process.wait()
                raise RuntimeError("Piri RPC process did not provide stdio pipes")
            self._process = process
            self._reader_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def prompt(self, message: str) -> None:
        if not message:
            raise ValueError("Piri prompt must not be empty")
        await self._request("prompt", message=message)

    async def abort(self) -> None:
        await self._request("abort")

    async def get_state(self) -> Mapping[str, Any]:
        response = await self._request("get_state")
        return self._response_data(response, command="get_state")

    async def get_available_models(self) -> Sequence[Mapping[str, Any]]:
        response = await self._request("get_available_models")
        data = self._response_data(response, command="get_available_models")
        models = data.get("models")
        if not isinstance(models, list) or any(not isinstance(model, Mapping) for model in models):
            raise PiriProtocolError("Piri model response is malformed")
        return tuple(cast(Mapping[str, Any], model) for model in models)

    async def next_event(self) -> Mapping[str, Any]:
        value = await self._events.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self) -> None:
        """Close stdin, then terminate the isolated process group if needed."""

        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is None:
            return

        if process.stdin is not None:
            process.stdin.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.wait_closed()
        try:
            await asyncio.wait_for(process.wait(), timeout=self._shutdown_timeout)
        except TimeoutError:
            await self._signal_process_group(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except TimeoutError:
                await self._signal_process_group(signal.SIGKILL)
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=1.0)

        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await task
        self._fail_pending(PiriConnectionClosedError("Piri client is closed"))

    async def _request(self, command: str, **params: Any) -> Mapping[str, Any]:
        if self._process is None:
            await self.start()
        if self._connection_error is not None:
            raise self._connection_error
        if self._closed or self._reader_task is None:
            raise PiriConnectionClosedError("Piri client is not running")

        request_id = f"ccc-{self._next_id}"
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = {"id": request_id, "type": command, **params}
        try:
            await self._write_json(payload)
            response = await asyncio.wait_for(future, timeout=self._request_timeout)
        except BaseException:
            self._pending.pop(request_id, None)
            raise
        if response.get("command") != command:
            raise PiriProtocolError("Piri response command did not match its request")
        if response.get("success") is not True:
            error = response.get("error")
            message = error if isinstance(error, str) and error else "Piri command failed"
            raise PiriProtocolError(message)
        return response

    async def _write_json(self, payload: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise PiriConnectionClosedError("Piri RPC process is not running")
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        except (TypeError, ValueError) as exc:
            raise PiriProtocolError("Piri command is not JSON serializable") from exc
        async with self._write_lock:
            try:
                process.stdin.write(encoded + b"\n")
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise PiriConnectionClosedError("Piri RPC stdin closed") from exc

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while line := await process.stdout.readline():
                try:
                    decoded = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise PiriProtocolError("Piri emitted malformed JSONL") from exc
                if not isinstance(decoded, dict):
                    raise PiriProtocolError("Piri emitted a non-object JSONL frame")
                payload = cast(JsonObject, decoded)
                if payload.get("type") == "response":
                    request_id = payload.get("id")
                    if isinstance(request_id, str):
                        future = self._pending.pop(request_id, None)
                        if future is not None and not future.done():
                            future.set_result(payload)
                            continue
                    raise PiriProtocolError("Piri emitted an uncorrelated response")
                if payload.get("type") == "extension_ui_request":
                    await self._handle_extension_ui(payload)
                    continue
                await self._events.put(payload)
            raise PiriConnectionClosedError("Piri RPC stdout closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, PiriConnectionClosedError)
                else PiriConnectionClosedError("Piri RPC connection failed")
            )
            self._connection_error = error
            self._fail_pending(error)
            await self._events.put(error)

    async def _drain_stderr(self) -> None:
        """Drain stderr without retaining provider output or possible secrets."""

        process = self._process
        if process is None or process.stderr is None:
            return
        while await process.stderr.read(65536):
            pass

    async def _handle_extension_ui(self, payload: Mapping[str, Any]) -> None:
        method = payload.get("method")
        request_id = payload.get("id")
        if not isinstance(request_id, str):
            return
        if method == "confirm" and self._auto_confirm:
            response: Mapping[str, Any] = {
                "type": "extension_ui_response",
                "id": request_id,
                "confirmed": True,
            }
        else:
            response = {
                "type": "extension_ui_response",
                "id": request_id,
                "cancelled": True,
            }
        await self._write_json(response)

    @staticmethod
    def _response_data(response: Mapping[str, Any], *, command: str) -> Mapping[str, Any]:
        data = response.get("data")
        if not isinstance(data, Mapping):
            raise PiriProtocolError(f"Piri {command} response is missing data")
        return cast(Mapping[str, Any], data)

    async def _signal_process_group(self, signal_number: signal.Signals) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal_number)
        except OSError:
            with suppress(ProcessLookupError):
                if signal_number == signal.SIGKILL:
                    process.kill()
                else:
                    process.terminate()

    def _fail_pending(self, error: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
