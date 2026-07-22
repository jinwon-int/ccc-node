"""Leased Codex Honcho delivery through an owner-only durable outbox."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from typing import Iterator, Protocol
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from telegram_bot.utils.secure_fs import _atomic_write_bytes, ensure_private_directory

from .distill_extraction import DistillExtractionOutput, parse_extraction_output
from .distill_journal import DistillJournal
from .distill_types import DistillJob


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_RECORD_BYTES = 64 * 1024


class HonchoDeliveryError(RuntimeError):
    def __init__(self, code: str, *, terminal: bool) -> None:
        self.terminal = terminal
        super().__init__(code)


class HonchoSender(Protocol):
    def send(self, record: dict[str, object]) -> None: ...


class HonchoHttpSender:
    """Send one body-safe outbox record using a node-local Honcho config."""

    def __init__(
        self, config_path: Path, *, node_label: str = "ccc-node",
        timeout_seconds: float = 10.0,
    ) -> None:
        if not node_label or timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("invalid Honcho sender configuration")
        self._config_path = Path(os.path.abspath(os.fspath(config_path)))
        self._node_label = node_label[:80]
        self._timeout_seconds = float(timeout_seconds)

    def _config(self) -> tuple[str, str, str, str]:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._config_path, flags)
        except FileNotFoundError:
            raise HonchoDeliveryError("honcho_config_missing", terminal=False) from None
        except OSError:
            raise HonchoDeliveryError("honcho_config_unsafe", terminal=True) from None
        try:
            meta = os.fstat(fd)
            unsafe = (
                not stat.S_ISREG(meta.st_mode)
                or meta.st_nlink != 1
                or (hasattr(os, "getuid") and meta.st_uid != os.getuid())
                or stat.S_IMODE(meta.st_mode) & 0o077
                or meta.st_size > 64 * 1024
            )
            if unsafe:
                raise HonchoDeliveryError("honcho_config_unsafe", terminal=True)
            payload = os.read(fd, 64 * 1024 + 1)
        finally:
            os.close(fd)
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise HonchoDeliveryError("honcho_config_invalid", terminal=True) from None
        if not isinstance(value, dict):
            raise HonchoDeliveryError("honcho_config_invalid", terminal=True)
        hosts = value.get("hosts") if isinstance(value.get("hosts"), dict) else {}
        hermes = hosts.get("hermes") if isinstance(hosts.get("hermes"), dict) else {}
        base = value.get("baseUrl") or hermes.get("baseUrl")
        workspace = value.get("workspace") or hermes.get("workspace") or "seoyoon-family"
        peer = hermes.get("aiPeer") or value.get("aiPeer") or "family-assistant"
        token = value.get("authToken") or value.get("apiKey") or hermes.get("apiKey") or ""
        parsed = urllib_parse.urlparse(base if isinstance(base, str) else "")
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not isinstance(workspace, str)
            or not isinstance(peer, str)
            or not isinstance(token, str)
        ):
            raise HonchoDeliveryError("honcho_config_invalid", terminal=True)
        return base.rstrip("/"), workspace, peer, token

    def _request(
        self, url: str, *, payload: dict[str, object] | None,
        token: str, idempotency_key: str,
        acceptable: frozenset[int] = frozenset({200, 201, 202, 204}),
    ) -> None:
        data = None if payload is None else json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib_request.Request(url, data=data, headers=headers)
        try:
            with urllib_request.urlopen(request, timeout=self._timeout_seconds) as response:
                status = int(getattr(response, "status", 0))
        except urllib_error.HTTPError as error:
            if error.code in acceptable:
                return
            raise HonchoDeliveryError("honcho_http_failed", terminal=False) from None
        except (urllib_error.URLError, TimeoutError, OSError):
            raise HonchoDeliveryError("honcho_request_failed", terminal=False) from None
        if status not in acceptable:
            raise HonchoDeliveryError("honcho_http_failed", terminal=False)

    def send(self, record: dict[str, object]) -> None:
        base, workspace, peer, token = self._config()
        session_id = record.get("session_id")
        key = record.get("idempotency_key")
        facts = record.get("facts")
        provenance = record.get("provenance")
        if (
            not isinstance(session_id, str)
            or not isinstance(key, str)
            or not isinstance(facts, list)
            or not isinstance(provenance, dict)
        ):
            raise HonchoDeliveryError("honcho_record_invalid", terminal=True)
        workspace_path = urllib_parse.quote(workspace, safe="")
        session_path = urllib_parse.quote(session_id, safe="")
        self._request(
            f"{base}/v3/workspaces/{workspace_path}/sessions",
            payload={
                "id": session_id,
                "metadata": {"source": "codex-distill", "node": self._node_label},
            },
            token=token,
            idempotency_key=key + "-session",
            acceptable=frozenset({200, 201, 202, 204, 409, 422}),
        )
        content = "\n".join(
            f"- ({item.get('kind', 'observation')}) {item.get('text', '')}"
            for item in facts if isinstance(item, dict)
        )
        self._request(
            f"{base}/v3/workspaces/{workspace_path}/sessions/{session_path}/messages",
            payload={
                "messages": [{
                    "peer_id": peer,
                    "content": "[codex distill]\n" + content,
                    "metadata": {
                        "source": "codex-distill", "node": self._node_label,
                        "idempotency_key": key, "provenance": provenance,
                        "facts": facts,
                    },
                }],
            },
            token=token,
            idempotency_key=key,
        )


class CodexHonchoOutbox:
    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(os.fspath(root)))
        self._lock_path = self.root / ".honcho-outbox.lock"
        self._thread_lock = threading.RLock()

    @staticmethod
    def _validate_file(path: Path) -> None:
        try:
            meta = path.lstat()
        except FileNotFoundError:
            return
        if (
            stat.S_ISLNK(meta.st_mode)
            or not stat.S_ISREG(meta.st_mode)
            or meta.st_nlink != 1
            or (hasattr(os, "getuid") and meta.st_uid != os.getuid())
            or stat.S_IMODE(meta.st_mode) != 0o600
            or meta.st_size > _MAX_RECORD_BYTES
        ):
            raise PermissionError("unsafe Honcho outbox state")

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        ensure_private_directory(self.root)
        with self._thread_lock:
            self._validate_file(self._lock_path)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self._lock_path, flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
                meta = os.fstat(fd)
                if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
                    raise PermissionError("unsafe Honcho outbox lock")
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    @staticmethod
    def record(output: DistillExtractionOutput, *, job_id: str) -> dict[str, object]:
        provenance = output.provenance
        return {
            "schema_version": output.schema_version,
            "idempotency_key": f"ccc-distill-{job_id}",
            "session_id": f"codex-distill-{job_id[:24]}",
            "provenance": {
                "provider": provenance.provider,
                "source_thread_hash": provenance.source_thread_hash,
                "trigger": provenance.trigger.value,
                "distilled_at": provenance.distilled_at,
            },
            "facts": [fact.model_dump(mode="json") for fact in output.honcho],
        }

    def enqueue(
        self, output: DistillExtractionOutput, *, job_id: str
    ) -> dict[str, object] | None:
        if not isinstance(output, DistillExtractionOutput) or not _SHA256_RE.fullmatch(job_id):
            raise ValueError("invalid Honcho outbox input")
        if not output.honcho:
            return None
        record = self.record(output, job_id=job_id)
        payload = json.dumps(
            record, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(payload) > _MAX_RECORD_BYTES:
            raise ValueError("Honcho outbox record exceeds its bound")
        path = self.root / f"{job_id}.json"
        with self._exclusive():
            self._validate_file(path)
            if path.exists():
                if path.read_bytes() != payload:
                    raise ValueError("Honcho outbox job collision")
            else:
                _atomic_write_bytes(path, payload)
                self._validate_file(path)
        return record

    def ack(self, job_id: str) -> None:
        if not _SHA256_RE.fullmatch(job_id):
            raise ValueError("invalid Honcho outbox job id")
        path = self.root / f"{job_id}.json"
        with self._exclusive():
            self._validate_file(path)
            if path.exists():
                path.unlink()


class CodexDistillHonchoSinkWorker:
    def __init__(
        self, journal: DistillJournal, *, outbox_dir: Path, sender: HonchoSender,
        owner_token: str | None = None, lease_seconds: int = 300,
        max_attempts: int = 5,
    ) -> None:
        if lease_seconds <= 0 or max_attempts <= 0:
            raise ValueError("invalid Honcho sink worker configuration")
        self._journal = journal
        self._outbox = CodexHonchoOutbox(outbox_dir)
        self._sender = sender
        self._owner_token = owner_token or secrets.token_hex(16)
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts

    async def _fail(
        self, claimed: DistillJob, *, code: str, terminal: bool
    ) -> DistillJob:
        method = (
            self._journal.mark_honcho_sink_terminal_failed
            if terminal
            else self._journal.mark_honcho_sink_retryable_failed
        )
        return await asyncio.to_thread(
            method, claimed.job_id, owner_token=self._owner_token,
            lease_epoch=claimed.honcho_sink_lease_epoch, error_code=code,
        )

    async def write_once(self, *, job_id: str) -> DistillJob:
        claimed = await asyncio.to_thread(
            self._journal.claim_honcho_sink, job_id,
            owner_token=self._owner_token, lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
        )
        if claimed is None:
            return await asyncio.to_thread(self._journal.get, job_id)
        try:
            if claimed.extraction_output is None:
                return await self._fail(
                    claimed, code="honcho_sink_output_missing", terminal=True
                )
            output = parse_extraction_output(claimed.extraction_output, wiki_enabled=True)
            record = await asyncio.to_thread(
                self._outbox.enqueue, output, job_id=claimed.job_id
            )
            if record is not None:
                await asyncio.to_thread(self._sender.send, record)
                await asyncio.to_thread(self._outbox.ack, claimed.job_id)
        except asyncio.CancelledError:
            await self._fail(claimed, code="honcho_sink_cancelled", terminal=False)
            raise
        except HonchoDeliveryError as error:
            return await self._fail(
                claimed, code="honcho_sink_delivery_failed", terminal=error.terminal
            )
        except (PermissionError, NotADirectoryError):
            return await self._fail(
                claimed, code="honcho_sink_path_unsafe", terminal=True
            )
        except ValueError:
            return await self._fail(
                claimed, code="honcho_sink_output_invalid", terminal=True
            )
        except OSError:
            return await self._fail(
                claimed, code="honcho_sink_io_failed", terminal=False
            )
        return await asyncio.to_thread(
            self._journal.mark_honcho_sink_done, claimed.job_id,
            owner_token=self._owner_token,
            lease_epoch=claimed.honcho_sink_lease_epoch,
        )


__all__ = [
    "CodexDistillHonchoSinkWorker", "CodexHonchoOutbox",
    "HonchoDeliveryError", "HonchoSender",
    "HonchoHttpSender",
]
