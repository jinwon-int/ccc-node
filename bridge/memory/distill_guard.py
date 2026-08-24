"""Provider-neutral circuit breaker for autonomous distill extraction.

The guard stores only body-free failure classes and expiry timestamps. Provider
stderr is inspected at the subprocess boundary and must never cross into this
module, the journal, logs, or doctor output.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Callable, Final, Literal

from telegram_bot.utils.secure_fs import (
    atomic_write_bytes_at,
    ensure_private_directory,
    owner_only_regular_violation,
)

DistillProvider = Literal["claude", "codex", "piri"]

GLOBAL_DISABLED_CODE: Final = "distill_globally_disabled"
COOLDOWN_ACTIVE_CODE: Final = "distill_provider_cooldown"
COOLDOWN_UNSAFE_CODE: Final = "distill_cooldown_state_unsafe"
PROVIDER_CIRCUIT_CODES: Final = frozenset(
    {
        "distill_auth_unavailable",
        "distill_quota_exhausted",
        "distill_rate_limited",
        "distill_model_unavailable",
    }
)

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
_MAX_COOLDOWN_BYTES = 4096
MAX_PROVIDER_STDERR_BYTES = 64 * 1024

_FAILURE_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "distill_auth_unavailable",
        tuple(
            re.compile(pattern, re.IGNORECASE)
            for pattern in (
                r"\b(?:401|unauthorized)\b",
                r"\bnot logged in\b",
                r"\blogin (?:is )?required\b",
                r"\bauthentication (?:failed|required|error)\b",
                r"\binvalid (?:api[ _-]?key|access token|oauth token|credentials)\b",
                r"\b(?:api[ _-]?key|access token|oauth token|credentials) (?:is |are )?(?:missing|expired|revoked)\b",
            )
        ),
    ),
    (
        "distill_quota_exhausted",
        tuple(
            re.compile(pattern, re.IGNORECASE)
            for pattern in (
                r"\bquota (?:exceeded|exhausted)\b",
                r"\busage limit (?:reached|exceeded)\b",
                r"\binsufficient (?:credit|credits|balance|quota)\b",
                r"\b(?:credit|billing) limit\b",
            )
        ),
    ),
    (
        "distill_rate_limited",
        tuple(
            re.compile(pattern, re.IGNORECASE)
            for pattern in (
                r"\b429\b",
                r"\brate[ _-]?limit(?:ed| exceeded)?\b",
                r"\btoo many requests\b",
                r"\bretry[ _-]?after\b",
                r"\btemporarily overloaded\b",
            )
        ),
    ),
    (
        "distill_model_unavailable",
        tuple(
            re.compile(pattern, re.IGNORECASE)
            for pattern in (
                r"\bunknown model\b",
                r"\bunsupported model\b",
                r"\bmodel (?:is )?(?:not found|unavailable|disabled)\b",
                r"\b(?:no|do not|does not) have access to (?:the )?model\b",
            )
        ),
    ),
)


def distill_state_dir(
    environment: Mapping[str, str] | None = None,
) -> Path:
    source = os.environ if environment is None else environment
    configured = (source.get("CCC_STATE_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    home = (source.get("HOME") or "").strip()
    return Path(home).expanduser() / ".claude" / "state" if home else Path.home() / ".claude" / "state"


def global_distill_disabled(
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Fail closed when the shared legacy/bridge disable marker is present."""

    marker = distill_state_dir(environment) / "distill.disabled"
    return marker.exists() or marker.is_symlink()


def classify_provider_failure(provider: str, stderr: bytes) -> str | None:
    """Map bounded private stderr to one body-free provider failure class."""

    if provider not in {"claude", "codex", "piri"}:
        raise ValueError("unsupported distill provider")
    if not isinstance(stderr, bytes):
        raise TypeError("stderr must be bytes")
    text = stderr[: 64 * 1024].decode("utf-8", errors="ignore")
    for code, patterns in _FAILURE_PATTERNS:
        if any(pattern.search(text) for pattern in patterns):
            return code
    return None


async def communicate_with_bounded_stderr(
    process: asyncio.subprocess.Process,
    *,
    input_bytes: bytes,
    max_stderr_bytes: int = MAX_PROVIDER_STDERR_BYTES,
) -> bytes:
    """Feed stdin while draining stderr without retaining more than the bound."""

    if type(max_stderr_bytes) is not int or max_stderr_bytes <= 0:
        raise ValueError("max_stderr_bytes must be positive")
    stdin = getattr(process, "stdin", None)
    stderr = getattr(process, "stderr", None)
    if stdin is None or stderr is None:
        # Hermetic fake-process compatibility. Production processes below are
        # always spawned with PIPE for both streams.
        _stdout, diagnostic = await process.communicate(input=input_bytes)
        return bytes(diagnostic or b"")[:max_stderr_bytes]

    async def feed_stdin() -> None:
        try:
            stdin.write(input_bytes)
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            stdin.close()
            try:
                await stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def drain_stderr() -> bytes:
        retained = bytearray()
        while True:
            chunk = await stderr.read(8192)
            if not chunk:
                return bytes(retained)
            remaining = max_stderr_bytes - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])

    _fed, diagnostic, _returncode = await asyncio.gather(
        feed_stdin(),
        drain_stderr(),
        process.wait(),
    )
    return diagnostic


@dataclass(frozen=True, slots=True)
class DistillGuardDecision:
    allowed: bool
    code: str | None = None
    retry_after_epoch: float | None = None

    def remaining_seconds(self, now: float) -> int:
        if self.retry_after_epoch is None:
            return 0
        return max(0, int(self.retry_after_epoch - now + 0.999))


class DistillGuard:
    """Owner-only global disable marker plus provider/model cooldown state."""

    def __init__(
        self,
        *,
        state_dir: Path | str | None = None,
        environment: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state_dir = (
            Path(state_dir).expanduser()
            if state_dir is not None
            else distill_state_dir(environment)
        )
        self.disabled_marker = self.state_dir / "distill.disabled"
        self.cooldown_dir = self.state_dir / "distill-provider-cooldowns"
        self._clock = clock

    @staticmethod
    def _validate_scope(provider: str, model: str) -> None:
        if provider not in {"claude", "codex", "piri"}:
            raise ValueError("unsupported distill provider")
        if not isinstance(model, str) or _MODEL_RE.fullmatch(model) is None:
            raise ValueError("invalid distill model")

    def cooldown_path(self, provider: str, model: str) -> Path:
        self._validate_scope(provider, model)
        digest = hashlib.sha256(f"{provider}\0{model}".encode("utf-8")).hexdigest()
        return self.cooldown_dir / f"{digest}.json"

    def decision(self, provider: str, model: str) -> DistillGuardDecision:
        self._validate_scope(provider, model)
        if self.disabled_marker.exists() or self.disabled_marker.is_symlink():
            return DistillGuardDecision(False, GLOBAL_DISABLED_CODE)
        path = self.cooldown_path(provider, model)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return DistillGuardDecision(True)
        except OSError:
            return DistillGuardDecision(False, COOLDOWN_UNSAFE_CODE)
        if path.is_symlink() or owner_only_regular_violation(
            metadata,
            owner_id=os.getuid(),
            unsafe_mode_mask=0o077,
        ) is not None or metadata.st_size <= 0 or metadata.st_size > _MAX_COOLDOWN_BYTES:
            return DistillGuardDecision(False, COOLDOWN_UNSAFE_CODE)
        descriptor = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or owner_only_regular_violation(
                    opened,
                    owner_id=os.getuid(),
                    unsafe_mode_mask=0o077,
                )
                is not None
            ):
                return DistillGuardDecision(False, COOLDOWN_UNSAFE_CODE)
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                raw = stream.read(_MAX_COOLDOWN_BYTES + 1)
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, TypeError):
            return DistillGuardDecision(False, COOLDOWN_UNSAFE_CODE)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(value, dict):
            return DistillGuardDecision(False, COOLDOWN_UNSAFE_CODE)
        expiry = value.get("retry_after_epoch")
        if (
            value.get("version") != 1
            or value.get("provider") != provider
            or value.get("model") != model
            or value.get("error_code") not in PROVIDER_CIRCUIT_CODES
            or isinstance(expiry, bool)
            or not isinstance(expiry, (int, float))
            or not math.isfinite(float(expiry))
            or expiry < 0
        ):
            return DistillGuardDecision(False, COOLDOWN_UNSAFE_CODE)
        if float(expiry) <= self._clock():
            return DistillGuardDecision(True)
        return DistillGuardDecision(False, COOLDOWN_ACTIVE_CODE, float(expiry))

    def trip(
        self,
        provider: str,
        model: str,
        *,
        error_code: str,
        cooldown_seconds: int,
    ) -> DistillGuardDecision:
        self._validate_scope(provider, model)
        if error_code not in PROVIDER_CIRCUIT_CODES:
            raise ValueError("unsupported provider circuit error")
        if type(cooldown_seconds) is not int or cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")
        retry_after = self._clock() + cooldown_seconds
        payload = json.dumps(
            {
                "version": 1,
                "provider": provider,
                "model": model,
                "error_code": error_code,
                "retry_after_epoch": retry_after,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        ensure_private_directory(self.cooldown_dir)
        directory_fd = os.open(
            self.cooldown_dir,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            atomic_write_bytes_at(
                directory_fd,
                self.cooldown_path(provider, model).name,
                payload,
            )
        finally:
            os.close(directory_fd)
        return DistillGuardDecision(False, COOLDOWN_ACTIVE_CODE, retry_after)


__all__ = [
    "COOLDOWN_ACTIVE_CODE",
    "COOLDOWN_UNSAFE_CODE",
    "GLOBAL_DISABLED_CODE",
    "PROVIDER_CIRCUIT_CODES",
    "DistillGuard",
    "DistillGuardDecision",
    "classify_provider_failure",
    "communicate_with_bounded_stderr",
    "distill_state_dir",
    "global_distill_disabled",
]
