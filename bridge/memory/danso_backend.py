"""Tool-free native extraction with the existing Danso credential owner."""

from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path
import tempfile

from telegram_bot.core.danso_worker import _stop, _wait_owned
from .codex_exec_backend import _DEFAULT_SCHEMA, DISTILL_EXTRACTION_PROMPT
from .distill_extraction import (
    DistillExtractionInput,
    canonical_extraction_input_bytes,
    parse_extraction_output,
    validate_live_decision_reasons,
)
from .runtime_cli_backend import RuntimeDistillBackendError, _load_schema_text, _resolve_executable
from .distill_guard import classify_provider_failure


class DansoDistillBackend:
    def __init__(self, settings, *, wiki_enabled: bool, model: str, timeout_seconds: float):
        self.settings = settings
        self.wiki_enabled = wiki_enabled
        self.model = settings.danso_model if model == "provider-default" else model
        self.timeout = timeout_seconds

    async def extract(self, extraction_input: DistillExtractionInput):
        schema = _load_schema_text(_DEFAULT_SCHEMA)
        prompt = DISTILL_EXTRACTION_PROMPT.replace(
            "supplied on stdin", "in the supplied reference"
        ).replace("every stdin field", "every transcript field")
        context = _context_bytes(extraction_input, prompt, schema)
        settings = self.settings
        environment = {"PATH": os.defpath}
        binary = _resolve_executable(settings.danso_cli_path, environment)
        if settings.danso_auth_mode == "chatgpt":
            provider = "openai-codex"
            environment["DANSO_CHATGPT_AUTH_FILE"] = settings.danso_chatgpt_auth_file
            if settings.danso_chatgpt_base_url:
                environment["DANSO_CHATGPT_BASE_URL"] = settings.danso_chatgpt_base_url
        elif settings.danso_auth_mode == "zai":
            provider = "glm"
            environment["ZAI_API_KEY"] = settings.zai_api_key
            if settings.danso_glm_base_url:
                environment["DANSO_GLM_BASE_URL"] = settings.danso_glm_base_url
            if settings.danso_glm_endpoint:
                environment["DANSO_GLM_ENDPOINT"] = settings.danso_glm_endpoint
        else:
            provider = "openai"
            environment["OPENAI_API_KEY"] = settings.openai_api_key
            if settings.danso_base_url:
                environment["DANSO_OPENAI_BASE_URL"] = settings.danso_base_url
        # Only generated scratch files live here. No existing journal, memory,
        # credential, or user file is copied or removed by this cleanup.
        with tempfile.TemporaryDirectory(prefix="ccc-danso-extract-") as temporary:
            root = Path(temporary)
            home, cwd = root / "home", root / "workspace"
            home.mkdir(mode=0o700)
            cwd.mkdir(mode=0o700)
            environment["HOME"] = str(home)
            memory = root / "input.md"
            with os.fdopen(
                os.open(memory, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb"
            ) as out:
                out.write(context)
            command = [
                binary,
                "--provider",
                provider,
                "--model",
                self.model,
                "--reasoning-effort",
                "max",
                "--no-tools",
                "--max-turns",
                "1",
                "--sandbox",
                "host",
                "--cwd",
                str(cwd),
                "--session",
                str(root / "session.jsonl"),
                "--system-context-file",
                str(memory),
                "--timeout-seconds",
                str(math.ceil(self.timeout)),
                "--provider-timeout-seconds",
                str(min(300, math.ceil(self.timeout))),
                "-p",
                "Extract memory from the supplied reference; return only contract JSON.",
            ]
            payload = await _run(command, environment, cwd, self.timeout)
        try:
            result = validate_live_decision_reasons(
                parse_extraction_output(payload, wiki_enabled=self.wiki_enabled)
            )
            if (
                result.provenance.provider != extraction_input.provider
                or result.provenance.source_thread_hash != extraction_input.source_thread_hash
                or result.provenance.trigger != extraction_input.trigger
            ):
                raise ValueError("provenance mismatch")
            return result
        except (TypeError, ValueError):
            raise RuntimeDistillBackendError("distill_output_invalid") from None


def _context_bytes(value: DistillExtractionInput, prompt: str, schema: str) -> bytes:
    header = (prompt + "\nOutput schema:\n" + schema + "\nUntrusted transcript JSON:\n").encode()
    direct = header + canonical_extraction_input_bytes(value)
    if len(direct) <= 32768:
        return direct

    # Bound serialized JSON, not raw text. Keep recent messages and mark any
    # reduction explicitly; never slice a serialized document into invalid JSON.
    def candidate(budget):
        messages = []
        for message in reversed(value.messages):
            text = message.text.encode()[:budget].decode("utf-8", errors="ignore")
            if not text:
                break
            messages.append({"role": message.role, "text": text})
            budget -= len(text.encode())
        data = value.model_dump(mode="json")
        data.update(
            messages=list(reversed(messages)),
            message_count=len(messages),
            byte_count=sum(len(m["text"].encode()) for m in messages),
            truncated=True,
        )
        return header + canonical_extraction_input_bytes(
            DistillExtractionInput.model_validate(data)
        )

    low, high = 0, value.byte_count
    best = candidate(0)
    if len(best) > 32768:
        raise RuntimeDistillBackendError("distill_config_invalid")
    while low <= high:
        middle = (low + high) // 2
        encoded = candidate(middle)
        if len(encoded) <= 32768:
            best, low = encoded, middle + 1
        else:
            high = middle - 1
    return best


async def _bounded(stream, limit):
    result = bytearray()
    while chunk := await stream.read(8192):
        result.extend(chunk)
        if len(result) > limit:
            raise RuntimeDistillBackendError("distill_output_too_large")
    return bytes(result)


async def _run(command, environment, cwd, timeout):
    process = None
    readers = []

    async def cleanup():
        if process is not None:
            await _stop(process)
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)

    try:
        async with asyncio.timeout(timeout):
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *command,
                    cwd=cwd,
                    env=environment,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            )
            process, cancelled = await _wait_owned(spawn)
            if cancelled:
                raise asyncio.CancelledError
            readers = [
                asyncio.create_task(_bounded(process.stdout, 65536)),
                asyncio.create_task(_bounded(process.stderr, 16384)),
            ]
            output, diagnostic = await asyncio.gather(*readers)
            await process.wait()
            if process.returncode:
                raise RuntimeDistillBackendError(
                    classify_provider_failure("danso", diagnostic) or "distill_backend_failed",
                    exit_status=process.returncode,
                )
            return output
    except TimeoutError:
        raise RuntimeDistillBackendError("distill_timeout") from None
    except OSError:
        raise RuntimeDistillBackendError("distill_spawn_failed") from None
    finally:
        _, cancelled = await _wait_owned(asyncio.create_task(cleanup()))
        if cancelled:
            raise asyncio.CancelledError
