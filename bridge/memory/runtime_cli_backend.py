"""Isolated Claude/Piri CLI adapters for the shared distill contract."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import tempfile
from typing import Final, Literal

from .codex_exec_backend import DISTILL_EXTRACTION_PROMPT
from .distill_extraction import (
    MAX_EXTRACTION_JSON_BYTES,
    DistillExtractionInput,
    DistillExtractionOutput,
    canonical_extraction_input_bytes,
    parse_extraction_output,
)

_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
_MAX_TIMEOUT_SECONDS = 10 * 60.0
_PROVIDER_DEFAULT_MODEL = "provider-default"

_COMMON_ENV_NAMES: Final = (
    "HOME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
)
_CLAUDE_ENV_NAMES: Final = (
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
)
_PIRI_ENV_NAMES: Final = (
    "PI_CODING_AGENT_DIR",
    "PIRI_CODING_AGENT_DIR",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "ZAI_API_KEY",
    "ZAI_CODING_CN_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENCODE_API_KEY",
    "FIREWORKS_API_KEY",
    "BASETEN_API_KEY",
)


class RuntimeDistillBackendError(RuntimeError):
    """Stable body-free error emitted by a non-Codex extractor boundary."""

    def __init__(self, code: str, *, exit_status: int | None = None) -> None:
        self.code = code
        self.exit_status = exit_status
        super().__init__(code)


def _private_umask() -> None:
    os.umask(0o077)


def _resolve_executable(value: str, environment: Mapping[str, str]) -> str:
    if not isinstance(value, str) or not value.strip() or value.startswith("-"):
        raise RuntimeDistillBackendError("distill_executable_unsafe")
    candidate = value.strip()
    if "/" not in candidate:
        candidate = shutil.which(candidate, path=environment.get("PATH") or _DEFAULT_PATH) or ""
    try:
        resolved = Path(candidate).expanduser().resolve(strict=True)
        metadata = resolved.stat()
    except OSError:
        raise RuntimeDistillBackendError("distill_executable_unsafe") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not os.access(resolved, os.X_OK)
    ):
        raise RuntimeDistillBackendError("distill_executable_unsafe")
    return str(resolved)


def _trusted_prefix_bin(source: Mapping[str, str]) -> str | None:
    raw = source.get("PREFIX")
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None
    path = Path(raw) / "bin"
    try:
        canonical = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError:
        return None
    if canonical != path or not stat.S_ISDIR(metadata.st_mode):
        return None
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        return None
    return str(path)


def _minimal_environment(
    source: Mapping[str, str],
    *,
    provider: Literal["claude", "piri"],
    temp_root: Path,
) -> dict[str, str]:
    names = _COMMON_ENV_NAMES + (
        _CLAUDE_ENV_NAMES if provider == "claude" else _PIRI_ENV_NAMES
    )
    environment = {
        name: value
        for name in names
        if (value := source.get(name)) is not None and "\x00" not in value
    }
    prefix_bin = _trusted_prefix_bin(source)
    environment["PATH"] = (
        f"{prefix_bin}:{_DEFAULT_PATH}" if prefix_bin else _DEFAULT_PATH
    )
    environment["TMPDIR"] = str(temp_root)
    environment["TERM"] = "dumb"
    environment["NO_COLOR"] = "1"
    if provider == "claude":
        # Prevent a nested Claude CLI from re-entering the legacy hook distiller.
        environment["CLAUDE_DISTILL_INFLIGHT"] = "1"
    return environment


def _command(
    provider: Literal["claude", "piri"],
    executable: str,
    model: str,
) -> tuple[str, ...]:
    if provider == "claude":
        arguments = [
            executable,
            "-p",
            "--tools",
            "",
            "--disallowedTools",
            "mcp__*",
            "--strict-mcp-config",
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
            "--output-format",
            "text",
            "--append-system-prompt",
            DISTILL_EXTRACTION_PROMPT,
        ]
    else:
        arguments = [
            executable,
            "--mode",
            "text",
            "--print",
            "--no-session",
            "--no-tools",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-approve",
            "--system-prompt",
            DISTILL_EXTRACTION_PROMPT,
        ]
    if model != _PROVIDER_DEFAULT_MODEL:
        arguments.extend(("--model", model))
    return tuple(arguments)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=0.25)
        return
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except TimeoutError:
        pass


def _read_output(path: Path, max_bytes: int) -> bytes:
    descriptor = -1
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise RuntimeDistillBackendError("distill_output_unsafe")
        if metadata.st_size == 0:
            raise RuntimeDistillBackendError("distill_output_missing")
        if metadata.st_size > max_bytes:
            raise RuntimeDistillBackendError("distill_output_too_large")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise RuntimeDistillBackendError("distill_output_unsafe")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            payload = stream.read(max_bytes + 1)
    except RuntimeDistillBackendError:
        raise
    except OSError:
        raise RuntimeDistillBackendError("distill_output_unsafe") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > max_bytes:
        raise RuntimeDistillBackendError("distill_output_too_large")
    return payload


class RuntimeCliDistillBackend:
    """Run Claude or Piri as an ephemeral, tool-free contract extractor."""

    def __init__(
        self,
        provider: Literal["claude", "piri"],
        *,
        executable: str,
        model: str = _PROVIDER_DEFAULT_MODEL,
        timeout_seconds: float = 120.0,
        wiki_enabled: bool = True,
        max_output_bytes: int = MAX_EXTRACTION_JSON_BYTES,
        environment: Mapping[str, str] | None = None,
        temp_root: str | Path | None = None,
    ) -> None:
        if (
            provider not in {"claude", "piri"}
            or not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > _MAX_TIMEOUT_SECONDS
            or not isinstance(model, str)
            or _MODEL_RE.fullmatch(model) is None
            or type(wiki_enabled) is not bool
            or type(max_output_bytes) is not int
            or max_output_bytes <= 0
            or max_output_bytes > MAX_EXTRACTION_JSON_BYTES
        ):
            raise RuntimeDistillBackendError("distill_config_invalid")
        self.provider = provider
        self._executable = executable
        self._model = model
        self._timeout_seconds = float(timeout_seconds)
        self._wiki_enabled = wiki_enabled
        self._max_output_bytes = max_output_bytes
        self._environment = dict(os.environ if environment is None else environment)
        self._temp_root = Path(temp_root) if temp_root is not None else None

    async def extract(
        self, extraction_input: DistillExtractionInput
    ) -> DistillExtractionOutput:
        if not isinstance(extraction_input, DistillExtractionInput):
            raise RuntimeDistillBackendError("distill_input_invalid")
        executable = _resolve_executable(self._executable, self._environment)
        stdin_bytes = canonical_extraction_input_bytes(extraction_input)
        try:
            with tempfile.TemporaryDirectory(
                prefix=f"ccc-{self.provider}-distill-",
                dir=self._temp_root,
            ) as private_root_raw:
                private_root = Path(private_root_raw)
                private_root.chmod(0o700)
                cwd = private_root / "cwd"
                cwd.mkdir(mode=0o700)
                output = private_root / "output.json"
                descriptor = os.open(
                    output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                try:
                    process = await asyncio.create_subprocess_exec(
                        *_command(self.provider, executable, self._model),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=descriptor,
                        stderr=asyncio.subprocess.DEVNULL,
                        cwd=str(cwd),
                        env=_minimal_environment(
                            self._environment,
                            provider=self.provider,
                            temp_root=private_root,
                        ),
                        start_new_session=True,
                        preexec_fn=_private_umask,
                    )
                except OSError:
                    raise RuntimeDistillBackendError("distill_spawn_failed") from None
                finally:
                    os.close(descriptor)
                try:
                    await asyncio.wait_for(
                        process.communicate(input=stdin_bytes),
                        timeout=self._timeout_seconds,
                    )
                except asyncio.CancelledError:
                    await _stop_process(process)
                    raise
                except TimeoutError:
                    await _stop_process(process)
                    raise RuntimeDistillBackendError("distill_timeout") from None
                except OSError:
                    await _stop_process(process)
                    raise RuntimeDistillBackendError("distill_io_failed") from None
                if process.returncode != 0:
                    raise RuntimeDistillBackendError(
                        "distill_nonzero_exit", exit_status=process.returncode
                    )
                payload = _read_output(output, self._max_output_bytes)
        except RuntimeDistillBackendError:
            raise
        except OSError:
            raise RuntimeDistillBackendError("distill_io_failed") from None
        try:
            result = parse_extraction_output(payload, wiki_enabled=self._wiki_enabled)
        except (TypeError, ValueError):
            raise RuntimeDistillBackendError("distill_output_invalid") from None
        provenance = result.provenance
        if (
            provenance.provider != extraction_input.provider
            or provenance.source_thread_hash != extraction_input.source_thread_hash
            or provenance.trigger != extraction_input.trigger
        ):
            raise RuntimeDistillBackendError("distill_output_invalid")
        return result


__all__ = ["RuntimeCliDistillBackend", "RuntimeDistillBackendError"]
