"""Restricted remote Grok gateway helper, sent over SSH stdin, never installed.

This module is stdlib-only and executes with Python isolated mode. Its source
is composed with grok_protocol by the trusted local transport. Credentials are
read only on the gateway host. No token or exception body is returned.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

from .grok_protocol import MAX_WIRE, ProtocolError, _uuid, decode_wire, prompt_digest


def request_spec(operation: str, agent_id: str, arguments: Any) -> tuple[str, str, bytes | None]:
    """Reconstruct exact allowlisted bodies; never accept a caller URL/header."""
    _uuid(agent_id)
    if not isinstance(arguments, dict):
        raise ProtocolError("invalid_rpc_arguments")
    if operation in {"health", "status", "tail"}:
        if arguments:
            raise ProtocolError("invalid_rpc_arguments")
        if operation == "health":
            return "GET", "/health", None
        if operation == "status":
            return "POST", "/api/getHostStatus", b"{}"
        body = {"id": agent_id, "limit": 64}
        path = "/api/getAgentTranscriptTail"
    elif operation in {"send", "acceptance"}:
        expected = {"nonce", "prompt"} if operation == "send" else {"nonce"}
        if set(arguments) != expected:
            raise ProtocolError("invalid_rpc_arguments")
        nonce = _uuid(arguments["nonce"])
        if operation == "send":
            prompt_digest(agent_id, nonce, arguments["prompt"])
            body = {"agentId": agent_id, "prompt": arguments["prompt"], "clientNonce": nonce}
            path = "/api/sendPrompt"
        else:
            body = {"accountSlot": "host", "agentId": agent_id, "clientNonce": nonce}
            path = "/api/promptAcceptanceStatus"
    else:
        raise ProtocolError("unsupported_rpc")
    return "POST", path, json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


def _gateway_config(home: Path) -> dict[str, Any]:
    """Vendor file may be 0644 only inside the owning user's private directory."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    directory = os.open(home / "sand-data", flags | os.O_DIRECTORY)
    try:
        ds = os.fstat(directory)
        if ds.st_uid != os.geteuid() or stat.S_IMODE(ds.st_mode) != 0o700:
            raise ProtocolError("unsafe_gateway_directory")
        fd = os.open("gateway.json", flags, dir_fd=directory)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or before.st_nlink != 1 or before.st_mode & 0o022
                    or not 0 < before.st_size <= 16384):
                raise ProtocolError("unsafe_gateway_config")
            raw = os.read(fd, 16385)
            after = os.fstat(fd)
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ProtocolError("changed_gateway_config")
            if len(raw) != before.st_size:
                raise ProtocolError("changed_gateway_config")
        finally:
            os.close(fd)
    finally:
        os.close(directory)
    config = decode_wire(raw)
    if not isinstance(config, dict):
        raise ProtocolError("invalid_gateway_config")
    token = config.get("token")
    if (config.get("scheme") != "http" or type(config.get("port")) is not int
            or config["port"] != 1340 or not isinstance(token, str)
            or not 16 <= len(token) <= 4096 or not token.isascii()
            or any(ord(c) <= 32 or ord(c) == 127 for c in token)):
        raise ProtocolError("unqualified_gateway_config")
    return config


def gateway_call(operation: str, agent_id: str, arguments: Any) -> Any:
    method, path, body = request_spec(operation, agent_id, arguments)
    config = _gateway_config(Path.home())
    connection = http.client.HTTPConnection("127.0.0.1", 1340, timeout=10)
    try:
        connection.request(method, path, body=body, headers={
            "Authorization": "Bearer " + config["token"],
            "Content-Type": "application/json", "Accept": "application/json",
        })
        reply = connection.getresponse()
        if reply.status != 200:
            raise ProtocolError("gateway_http_failure")
        # http.client never follows redirects or consults proxy environment.
        return decode_wire(reply.read(MAX_WIRE + 1))
    finally:
        connection.close()


def gateway_main(request: Any) -> None:
    """Exactly one RPC. Uncertain outcomes never trigger a resend here."""
    try:
        if not isinstance(request, dict) or set(request) != {"operation", "agent_id", "arguments"}:
            raise ProtocolError("invalid_rpc_request")
        value = gateway_call(request["operation"], request["agent_id"], request["arguments"])
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        if len(raw) > MAX_WIRE:
            raise ProtocolError("gateway_response_limit")
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
    except Exception:
        # Includes OS, HTTP and parsing errors; never expose token/body/path.
        os.write(2, b"grok_gateway_failure\n")
        raise SystemExit(1) from None
