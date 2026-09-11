"""Bounded one-shot SSH transport; credentials never leave the Grok host.

This is not a runtime: the caller must journal before `send`, and reconcile
uncertain outcomes. Cancelling SSH does not interrupt the remote Bot.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import signal
from typing import Any

from .grok_gateway import request_spec
from .grok_protocol import MAX_WIRE, ProtocolError, decode_wire


def helper_source(operation: str, agent_id: str, arguments: Any) -> bytes:
    request_spec(operation, agent_id, arguments)
    directory = Path(__file__).parent
    protocol = (directory / "grok_protocol.py").read_text(encoding="utf-8")
    gateway = (directory / "grok_gateway.py").read_text(encoding="utf-8")
    # Package imports are replaced only in our trusted, fixed helper source.
    gateway = gateway.replace("from __future__ import annotations\n", "")
    gateway = gateway.replace(
        "from .grok_protocol import MAX_WIRE, ProtocolError, _uuid, decode_wire, prompt_digest\n", "")
    request = json.dumps({"operation": operation, "agent_id": agent_id, "arguments": arguments},
                         ensure_ascii=True, separators=(",", ":"))
    # repr encodes Python data, not shell input. No request bytes enter argv.
    return (protocol + "\n" + gateway + "\ngateway_main(decode_wire(" +
            repr(request.encode("ascii")) + "))\n").encode("utf-8")


async def _read_bounded(stream: asyncio.StreamReader, maximum: int) -> bytes:
    chunks: list[bytes] = []
    count = 0
    while True:
        chunk = await stream.read(min(65536, maximum + 1 - count))
        if not chunk:
            return b"".join(chunks)
        count += len(chunk)
        if count > maximum:
            raise ProtocolError("ssh_output_limit")
        chunks.append(chunk)


class GrokSshTransport:
    def __init__(self, destination: str, agent_id: str) -> None:
        # Explicit operator configuration, not a message-provided target.
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}@[A-Za-z0-9][A-Za-z0-9.-]{0,252}", destination):
            raise ProtocolError("invalid_ssh_destination")
        request_spec("health", agent_id, {})
        self.destination = destination
        self.agent_id = agent_id

    async def call(self, operation: str, arguments: Any = None) -> Any:
        source = helper_source(operation, self.agent_id, {} if arguments is None else arguments)
        try:
            process = await asyncio.create_subprocess_exec(
                "ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-o", "StrictHostKeyChecking=yes", "-o", "ClearAllForwardings=yes",
                self.destination, "python3 -I -",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True,
            )
        except OSError:
            raise ProtocolError("ssh_launch_failure") from None
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        stdout = asyncio.create_task(_read_bounded(process.stdout, MAX_WIRE))
        stderr = asyncio.create_task(_read_bounded(process.stderr, 4096))
        settled = False
        try:
            async with asyncio.timeout(20):
                process.stdin.write(source)
                await process.stdin.drain()
                process.stdin.close()
                out, _ = await asyncio.gather(stdout, stderr)
                code = await process.wait()
                if code:
                    raise ProtocolError("ssh_gateway_failure")
                decoded = decode_wire(out)
                settled = True
                return decoded
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProtocolError("ssh_outcome_unknown") from None
        finally:
            # SSH/ProxyCommand children can inherit pipes. Killing only the
            # immediate child leaves Process.wait pending on their open pipes.
            if not settled:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # A naturally exited parent can have returncode=0 while its
                # descendants still hold pipes; cancelled readers are also
                # done without EOF. Always close local pipes on an unsettled
                # exchange, including an escaped helper we cannot terminate.
                getattr(process, "_transport").close()
            for task in (stdout, stderr):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stdout, stderr, return_exceptions=True)
            try:
                async with asyncio.timeout(2):
                    await process.wait()
            except TimeoutError:
                # asyncio exposes no public pipe-close on Process. CPython's
                # subprocess transport closes local descriptors if a configured
                # helper escaped the owned group. This does not kill that helper
                # or remotely interrupt Grok. Qualified on CI Python3.11-3.14.
                getattr(process, "_transport").close()


class GrokLocalTransport:
    """Same helper as SSH, executed on this host. No ssh(1) required.

    Production Grok Telegram runs on the Grok computer itself. Seoseo SSH hop
    is retired. Destination remains the journal binding label.
    """

    def __init__(self, destination: str, agent_id: str) -> None:
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}@[A-Za-z0-9][A-Za-z0-9.-]{0,252}", destination):
            raise ProtocolError("invalid_ssh_destination")
        request_spec("health", agent_id, {})
        self.destination = destination
        self.agent_id = agent_id

    async def call(self, operation: str, arguments: Any = None) -> Any:
        source = helper_source(operation, self.agent_id, {} if arguments is None else arguments)
        try:
            process = await asyncio.create_subprocess_exec(
                "python3", "-I", "-",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True,
            )
        except OSError:
            raise ProtocolError("ssh_launch_failure") from None
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        stdout = asyncio.create_task(_read_bounded(process.stdout, MAX_WIRE))
        stderr = asyncio.create_task(_read_bounded(process.stderr, 4096))
        settled = False
        try:
            async with asyncio.timeout(20):
                process.stdin.write(source)
                await process.stdin.drain()
                process.stdin.close()
                out, _ = await asyncio.gather(stdout, stderr)
                code = await process.wait()
                if code:
                    raise ProtocolError("ssh_gateway_failure")
                decoded = decode_wire(out)
                settled = True
                return decoded
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProtocolError("ssh_outcome_unknown") from None
        finally:
            if not settled:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                getattr(process, "_transport").close()
            for task in (stdout, stderr):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stdout, stderr, return_exceptions=True)
            try:
                async with asyncio.timeout(2):
                    await process.wait()
            except TimeoutError:
                getattr(process, "_transport").close()
