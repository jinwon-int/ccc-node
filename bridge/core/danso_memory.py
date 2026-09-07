"""Prepare CCC memory for Danso without giving the worker hook credentials."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from telegram_bot.core.danso_worker import _read, _stop, _wait_owned
from telegram_bot.core.memory_audience import MemoryAudience
from telegram_bot.utils.config import Settings
from telegram_bot.utils.secure_fs import ensure_private_directory, read_owner_only_bytes, SecureFsError


async def prepare_memory_context(settings: Settings, audience: MemoryAudience) -> Path:
    route = audience.danso_environment(settings)
    home = Path(route["CCC_DANSO_BOOTSTRAP_HOME"])
    ensure_private_directory(home)
    environment = {key: os.environ[key] for key in
                   ("PATH", "LANG", "LC_ALL", "CCC_CODEX_MEMORY_LOADER") if key in os.environ}
    claude = Path(settings.claude_settings_path).expanduser().parent
    environment.update(HOME=str(claude.parent), CCC_CLAUDE_DIR=str(claude))
    environment.update(route)
    environment.update(CCC_MEMORY_NO_REFRESH="1", CCC_MEMORY_INJECT_PENDING_PROMISES="0",
                       CCC_MEMORY_INJECT_DETACHED_JOBS="0", CCC_MEMORY_TIMING="0",
                       CCC_EXTERNAL_WAIT_HOME=str(audience.state_dir / "external-wait"))
    environment.update(CODEX_HOME=str(home), CODEX_SQLITE_HOME=str(home),
                       CCC_MEMORY_MATERIALIZER_PROVIDER="danso")
    # This is the local CCC Python materializer, not a Codex CLI/model call.
    # Require this refresh to succeed; never silently use an older snapshot.
    if not await _run_materializer_command(
        settings.codex_memory_materializer_path, "materialize",
        settings.codex_memory_bootstrap_timeout_seconds, environment=environment,
    ):
        raise ValueError("Danso memory refresh failed")
    path = home / "AGENTS.md"
    try:
        data, _ = read_owner_only_bytes(path, max_bytes=32768, exact_mode=0o600, require_nonempty=True)
        data.decode("utf-8")
    except (SecureFsError, UnicodeError):
        raise ValueError("Danso memory snapshot is invalid") from None
    # Native code pins every parent/file descriptor and validates again before
    # provider dispatch. The memory body is never placed in argv or user history.
    return path


async def _run_materializer_command(path, command, timeout_seconds, *, environment):
    """Own the whole Danso materializer process group through cancellation."""
    process = None
    reader = None
    async def cleanup():
        try:
            if process is not None:
                await _stop(process)
        finally:
            if reader is not None:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
    try:
        async with asyncio.timeout(timeout_seconds):
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                path, command, "--json", env=environment, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True))
            process, cancelled = await _wait_owned(spawn)
            if cancelled:
                raise asyncio.CancelledError
            reader = asyncio.create_task(_read(process.stdout))
            output = await reader
            await process.wait()
            return process.returncode == 0 and len(output) <= 16384
    except (OSError, ValueError, TimeoutError):
        return False
    finally:
        _, cancelled = await _wait_owned(asyncio.create_task(cleanup()))
        if cancelled:
            raise asyncio.CancelledError
