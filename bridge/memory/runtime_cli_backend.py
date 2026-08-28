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

from .codex_exec_backend import _DEFAULT_SCHEMA, DISTILL_EXTRACTION_PROMPT
from .distill_extraction import (
    MAX_EXTRACTION_JSON_BYTES,
    DecisionReasonMissingError,
    DistillExtractionInput,
    DistillExtractionOutput,
    DistillProvenance,
    canonical_extraction_input_bytes,
    parse_extraction_output,
    validate_live_decision_reasons,
)
from .distill_types import CodexTranscriptSnapshot
from .skill_candidate import (
    SkillCandidateOutput,
    SkillCandidateParseError,
    parse_skill_candidate_output,
)
from .skill_candidate_backend import (
    MAX_SKILL_CANDIDATE_OUTPUT_BYTES,
    SKILL_CANDIDATE_PROMPT,
    SkillCandidateBackendError,
    SKILL_SCHEMA_PATH,
    canonical_skill_candidate_input_bytes,
)
from .skill_candidate_inventory import (
    SkillCandidateInventoryBuilder,
    SkillInventoryError,
)
from .distill_guard import (
    classify_provider_failure,
    communicate_with_bounded_stderr,
)

_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
_MAX_SCHEMA_BYTES = 256 * 1024
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
    # ccc-piri wrapper launches (CCC_PIRI_CLI_PATH pointing at the wrapper)
    # resolve the real CLI only through this variable; without it the
    # minimal extractor environment falls back to a bare `piri` PATH lookup
    # that fails on nodes where the wrapper fronts a non-PATH real CLI.
    "CCC_PIRI_REAL_CLI_PATH",
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


def _load_schema_text(path: Path) -> str:
    """Read the output schema with the same safety bar as the Codex path."""

    try:
        candidate = path.expanduser().absolute()
        metadata = candidate.lstat()
    except OSError:
        raise RuntimeDistillBackendError("distill_schema_unsafe") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_size <= 0
        or metadata.st_size > _MAX_SCHEMA_BYTES
    ):
        raise RuntimeDistillBackendError("distill_schema_unsafe")
    try:
        return candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise RuntimeDistillBackendError("distill_schema_unsafe") from None


def _strip_markdown_fence(payload: bytes) -> bytes:
    """Unwrap one ```json fence; anything else stays strict-parse bound.

    Real CLI extractors intermittently wrap valid contract JSON in markdown
    fences despite the prompt contract. The legacy hook distiller stripped
    them as a safety net; only a single, well-formed fence around an object
    literal is removed here — every other byte pattern still reaches the
    strict parser unchanged.
    """

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload
    stripped = text.strip()
    if not stripped.startswith("```"):
        return payload
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[-1].strip() != "```":
        return payload
    inner = "\n".join(lines[1:-1]).strip()
    if not inner.startswith("{"):
        return payload
    return inner.encode("utf-8")


def _command(
    provider: Literal["claude", "piri"],
    executable: str,
    model: str,
    schema_text: str,
    prompt: str = DISTILL_EXTRACTION_PROMPT,
) -> tuple[str, ...]:
    prompt = prompt + " The output JSON schema is:\n" + schema_text
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
            prompt,
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
            prompt,
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


async def _run_isolated_cli(
    *,
    provider: Literal["claude", "piri"],
    executable: str,
    model: str,
    schema_text: str,
    prompt: str,
    stdin_bytes: bytes,
    timeout_seconds: float,
    max_output_bytes: int,
    environment: Mapping[str, str],
    temp_root: str | Path | None,
) -> bytes:
    """Run one ephemeral tool-free CLI extraction and return the payload.

    Shared isolation boundary for the distill and skill-candidate runtime
    backends. Failure codes keep the historical ``distill_*`` namespace;
    callers re-label at their own boundary (the codex runner mirrors this with
    ``codex_distill_*``).
    """
    try:
        with tempfile.TemporaryDirectory(
            prefix=f"ccc-{provider}-distill-",
            dir=temp_root,
        ) as private_root_raw:
            private_root = Path(private_root_raw)
            private_root.chmod(0o700)
            cwd = private_root / "cwd"
            cwd.mkdir(mode=0o700)
            output = private_root / "output.json"
            output_descriptor = -1
            try:
                output_descriptor = os.open(
                    output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                process = await asyncio.create_subprocess_exec(
                    *_command(provider, executable, model, schema_text, prompt),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=output_descriptor,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(cwd),
                    env=_minimal_environment(
                        environment,
                        provider=provider,
                        temp_root=private_root,
                    ),
                    start_new_session=True,
                    preexec_fn=_private_umask,
                )
            except OSError:
                raise RuntimeDistillBackendError("distill_spawn_failed") from None
            finally:
                if output_descriptor >= 0:
                    os.close(output_descriptor)
            try:
                diagnostic = await asyncio.wait_for(
                    communicate_with_bounded_stderr(
                        process,
                        input_bytes=stdin_bytes,
                    ),
                    timeout=timeout_seconds,
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
                try:
                    stdout_diagnostic = _read_output(
                        output,
                        min(max_output_bytes, 64 * 1024),
                    )
                except RuntimeDistillBackendError:
                    stdout_diagnostic = b""
                classified = classify_provider_failure(
                    provider,
                    diagnostic + b"\n" + stdout_diagnostic,
                )
                raise RuntimeDistillBackendError(
                    classified or "distill_nonzero_exit",
                    exit_status=process.returncode,
                )
            return _strip_markdown_fence(_read_output(output, max_output_bytes))
    except RuntimeDistillBackendError:
        raise
    except OSError:
        raise RuntimeDistillBackendError("distill_io_failed") from None


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
        schema_path: str | Path = _DEFAULT_SCHEMA,
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
        self._schema_path = Path(schema_path)

    async def extract(self, extraction_input: DistillExtractionInput) -> DistillExtractionOutput:
        if not isinstance(extraction_input, DistillExtractionInput):
            raise RuntimeDistillBackendError("distill_input_invalid")
        executable = _resolve_executable(self._executable, self._environment)
        schema_text = _load_schema_text(self._schema_path)
        stdin_bytes = canonical_extraction_input_bytes(extraction_input)
        payload = await _run_isolated_cli(
            provider=self.provider,
            executable=executable,
            model=self._model,
            schema_text=schema_text,
            prompt=DISTILL_EXTRACTION_PROMPT,
            stdin_bytes=stdin_bytes,
            timeout_seconds=self._timeout_seconds,
            max_output_bytes=self._max_output_bytes,
            environment=self._environment,
            temp_root=self._temp_root,
        )
        try:
            result = parse_extraction_output(payload, wiki_enabled=self._wiki_enabled)
        except (TypeError, ValueError):
            raise RuntimeDistillBackendError("distill_output_invalid") from None
        try:
            result = validate_live_decision_reasons(result)
        except DecisionReasonMissingError:
            raise RuntimeDistillBackendError("distill_decision_reason_missing") from None
        provenance = result.provenance
        if (
            provenance.provider != extraction_input.provider
            or provenance.source_thread_hash != extraction_input.source_thread_hash
            or provenance.trigger != extraction_input.trigger
        ):
            raise RuntimeDistillBackendError("distill_output_invalid")
        return result


class RuntimeCliSkillCandidateBackend:
    """Run one isolated Claude/Piri skill-candidate extraction. No journal/sink.

    Skill-candidate twin of :class:`RuntimeCliDistillBackend` (#667): the same
    ephemeral tool-free CLI isolation with the skill-candidate prompt, schema,
    and parser. Runtime failure codes are re-labeled into the
    ``skill_candidate_*`` namespace at this boundary, mirroring how
    :class:`CodexExecSkillCandidateBackend` re-labels the codex runner codes.
    """

    def __init__(
        self,
        provider: Literal["claude", "piri"],
        *,
        executable: str,
        schema_path: str | Path = SKILL_SCHEMA_PATH,
        model: str = _PROVIDER_DEFAULT_MODEL,
        timeout_seconds: float = 120.0,
        max_output_bytes: int = MAX_SKILL_CANDIDATE_OUTPUT_BYTES,
        environment: Mapping[str, str] | None = None,
        temp_root: str | Path | None = None,
        inventory_builder: SkillCandidateInventoryBuilder | None = None,
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
            or type(max_output_bytes) is not int
            or max_output_bytes <= 0
            or max_output_bytes > MAX_SKILL_CANDIDATE_OUTPUT_BYTES
        ):
            raise SkillCandidateBackendError("skill_candidate_config_invalid")
        self.provider = provider
        self._executable = executable
        self._schema_path = Path(schema_path)
        self._model = model
        self._timeout_seconds = float(timeout_seconds)
        self._max_output_bytes = max_output_bytes
        self._environment = dict(os.environ if environment is None else environment)
        self._temp_root = Path(temp_root) if temp_root is not None else None
        self._inventory_builder = inventory_builder or (
            SkillCandidateInventoryBuilder.from_environment(
                self._environment,
                provider=provider,
            )
        )

    async def extract(
        self,
        *,
        snapshot: CodexTranscriptSnapshot,
        provenance: DistillProvenance,
    ) -> SkillCandidateOutput:
        if provenance.source_thread_hash != snapshot.thread_hash:
            raise SkillCandidateBackendError("skill_candidate_input_invalid")
        try:
            inventory = await asyncio.to_thread(self._inventory_builder.build)
        except (OSError, SkillInventoryError, TypeError, ValueError):
            raise SkillCandidateBackendError(
                "skill_candidate_inventory_failed"
            ) from None
        payload = canonical_skill_candidate_input_bytes(
            snapshot,
            provenance,
            skill_inventory=inventory,
        )
        try:
            output_payload = await _run_isolated_cli(
                provider=self.provider,
                executable=_resolve_executable(self._executable, self._environment),
                model=self._model,
                schema_text=_load_schema_text(self._schema_path),
                prompt=SKILL_CANDIDATE_PROMPT,
                stdin_bytes=payload,
                timeout_seconds=self._timeout_seconds,
                max_output_bytes=self._max_output_bytes,
                environment=self._environment,
                temp_root=self._temp_root,
            )
        except RuntimeDistillBackendError as exc:
            # Re-label the runner's distill-named code into a skill code.
            raise SkillCandidateBackendError(
                exc.code.replace("distill_", "skill_candidate_", 1),
                exit_status=exc.exit_status,
            ) from None
        try:
            result = parse_skill_candidate_output(output_payload)
        except SkillCandidateParseError:
            raise SkillCandidateBackendError("skill_candidate_output_invalid") from None
        echoed = result.provenance
        if (
            echoed.provider != provenance.provider
            or echoed.source_thread_hash != provenance.source_thread_hash
            or echoed.trigger != provenance.trigger
        ):
            raise SkillCandidateBackendError("skill_candidate_output_invalid")
        return result


__all__ = [
    "RuntimeCliDistillBackend",
    "RuntimeCliSkillCandidateBackend",
    "RuntimeDistillBackendError",
]
