"""Prepare CCC memory for Danso without giving the worker hook credentials."""
from __future__ import annotations

import os
from pathlib import Path

from telegram_bot.core.codex_runtime import _run_materializer_command
from telegram_bot.core.memory_audience import MemoryAudience
from telegram_bot.utils.config import Settings
from telegram_bot.utils.secure_fs import ensure_private_directory, read_owner_only_bytes, SecureFsError


async def prepare_memory_context(settings: Settings, audience: MemoryAudience) -> Path:
    route = audience.danso_environment(settings)
    home = Path(route["CCC_DANSO_BOOTSTRAP_HOME"])
    ensure_private_directory(home)
    environment = dict(os.environ)
    environment.update(route)
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
