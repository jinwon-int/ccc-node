"""Telegram composition for the Danso CLI with explicit execution and OpenAI authentication modes."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import shutil
import stat
from typing import Any
from urllib.parse import urlsplit

from telegram_bot.core.agent_runtime import ModelInfo, SessionRequest
from telegram_bot.core.danso_worker import DansoRuntime as WorkerRuntime
from telegram_bot.utils.config import Settings
from telegram_bot.utils.secure_fs import ensure_private_directory

ASTRA_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _validate_execution_backend(settings: Settings) -> None:
    if settings.danso_sandbox not in {"host", "bubblewrap"}:
        raise ValueError("unsupported Danso execution backend")
    if settings.danso_sandbox == "bubblewrap" and shutil.which("bwrap") is None:
        raise ValueError("Danso requires bubblewrap; install bwrap")


def _validate_authentication(settings: Settings, cwd: Path) -> None:
    if settings.danso_auth_mode not in {"api-key", "chatgpt"}:
        raise ValueError("unsupported Danso authentication mode")
    if settings.danso_auth_mode == "api-key":
        if settings.danso_chatgpt_auth_file:
            raise ValueError("DANSO_CHATGPT_AUTH_FILE requires CCC_DANSO_AUTH_MODE=chatgpt")
        if not settings.openai_api_key or not settings.openai_api_key.strip():
            raise ValueError("Danso API-key mode requires OPENAI_API_KEY")
        return
    if not settings.danso_chatgpt_auth_file:
        raise ValueError("Danso ChatGPT mode requires DANSO_CHATGPT_AUTH_FILE")
    if settings.danso_chatgpt_base_url and settings.danso_chatgpt_base_url != "https://chatgpt.com/backend-api/codex":
        base = urlsplit(settings.danso_chatgpt_base_url)
        if (base.scheme != "http" or base.hostname not in {"127.0.0.1", "::1"}
                or base.username is not None or base.password is not None or base.query or base.fragment):
            raise ValueError("Danso ChatGPT endpoint must be the Codex service or literal-loopback fixture")
        _ = base.port  # Reject malformed/out-of-range ports without contacting the endpoint.
    auth = Path(settings.danso_chatgpt_auth_file)
    try:
        resolved_auth = auth.resolve()
    except RuntimeError:
        raise ValueError("Danso auth path contains a symlink loop") from None
    if not auth.is_absolute() or resolved_auth != auth:
        raise ValueError("Danso auth path must be absolute and contain no symlinks")
    if auth.parent.is_relative_to(cwd) or cwd.is_relative_to(auth.parent):
        raise ValueError("Danso auth directory must be disjoint from the workspace")
    metadata, parent = auth.lstat(), auth.parent.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
            or metadata.st_size > 65536 or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700):
        raise ValueError("Danso auth requires a private owner-controlled file and directory")
    if auth.name == "danso-auth.json":
        if os.path.lexists(auth.parent / ".danso-refresh-pending"):
            raise ValueError("Danso credential refresh is unresolved; reauthenticate into a new isolated store")
        if os.path.lexists(auth.parent / "auth.json"):
            raise ValueError("Codex auth.json reappeared; resolve credential ownership")
    # Metadata only; native validation remains authoritative at each dispatch.


def _configuration(settings: Settings) -> tuple[str, Path]:
    """Validate locally, without creating state or contacting any provider."""
    if settings.bridge_memory_mode != "off":
        raise ValueError("Danso requires CCC_BRIDGE_MEMORY_MODE=off; memory routing is unsupported")
    if settings.memory_distill_provider not in {"auto", "off"}:
        raise ValueError("Danso requires CCC_MEMORY_DISTILL_PROVIDER=auto or off; extraction is unsupported")
    if settings.danso_model != "gpt-6-astra":
        raise ValueError("Danso Telegram currently supports gpt-6-astra")
    if not settings.danso_state_dir or not Path(settings.danso_state_dir).is_absolute():
        raise ValueError("Danso requires an absolute CCC_DANSO_STATE_DIR outside the project")
    root = Path(settings.danso_state_dir)
    if not settings.danso_workspace or not Path(settings.danso_workspace).is_absolute():
        raise ValueError("Danso requires an absolute CCC_DANSO_WORKSPACE separate from bridge state")
    cwd = Path(settings.danso_workspace)
    if not cwd.is_dir() or cwd.resolve() != cwd:
        raise ValueError("Danso workspace must exist and contain no symlinks")
    protected = (Path(settings.bot_data_dir), Path(settings.session_store_path),
                 Path(settings.project_root) / ".telegram_bot",
                 Path(__file__).resolve().parents[1] / ".env",
                 Path(os.environ.get("CCC_BOT_ENV_FILE", str(Path(__file__).resolve().parents[1] / ".env"))))
    if any(cwd.is_relative_to(p.resolve()) or p.resolve().is_relative_to(cwd) for p in protected):
        raise ValueError("Danso workspace must not expose bridge configuration or session storage")
    _validate_authentication(settings, cwd)
    if root.resolve() != root or root.is_relative_to(cwd) or cwd.is_relative_to(root):
        raise ValueError("Danso state must be disjoint from the project and contain no symlinks")
    if root.exists():
        metadata = root.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700):
            raise ValueError("Danso state must be a private process-owned directory")
    binary = shutil.which(settings.danso_cli_path)
    if binary is None:
        raise ValueError("Danso executable unavailable; set CCC_DANSO_CLI_PATH")
    _validate_execution_backend(settings)
    if settings.danso_timeout_seconds + 10 > settings.process_timeout_seconds:
        raise ValueError("CLAUDE_PROCESS_TIMEOUT must exceed CCC_DANSO_TIMEOUT_SECONDS by at least 10s")
    return str(Path(binary).resolve(strict=True)), root


def probe_danso_readiness(settings: Settings) -> tuple[bool, str]:
    """Configuration readiness only; this does not claim account access."""
    try:
        _configuration(settings)
    except ValueError as exc:
        return False, str(exc)
    except OSError:
        return False, "Danso filesystem prerequisites unavailable"
    return True, ""


class DansoRuntime(WorkerRuntime):
    """One operator-selected Astra model with explicit default reasoning effort."""
    def __init__(self, *, default_effort: str = "medium", **kwargs: Any):
        if default_effort not in ASTRA_EFFORTS:
            raise ValueError("unsupported Astra effort")
        super().__init__(**kwargs)
        self.default_effort = default_effort

    async def list_models(self):
        return [ModelInfo(id=self.model, display_name=self.model, is_default=True,
                          supported_reasoning_efforts=ASTRA_EFFORTS,
                          default_reasoning_effort=self.default_effort)]

    async def start_or_resume(self, request: SessionRequest):
        effort = request.effort or self.default_effort
        if effort not in ASTRA_EFFORTS:
            raise ValueError("unsupported Astra effort")
        try:
            return await super().start_or_resume(replace(request, effort=effort))
        except FileNotFoundError:
            if not request.session_id:
                raise ValueError("Danso workspace is unavailable.") from None
            raise ValueError("Stored Danso journal is unavailable. Check previous work, then use /new; no automatic replay.") from None


def build_danso_runtime(settings: Settings) -> DansoRuntime:
    binary, root = _configuration(settings)
    ensure_private_directory(root)
    private_home = root / "home"
    ensure_private_directory(private_home)
    environment: dict[str, str | None] = {"PATH": os.defpath, "HOME": str(private_home)}
    if settings.danso_auth_mode == "chatgpt":
        environment["DANSO_CHATGPT_AUTH_FILE"] = settings.danso_chatgpt_auth_file
        if settings.danso_chatgpt_base_url:
            environment["DANSO_CHATGPT_BASE_URL"] = settings.danso_chatgpt_base_url
        provider, journals = "openai-codex", "chatgpt-journals"
    else:
        environment["OPENAI_API_KEY"] = settings.openai_api_key
        if settings.danso_base_url:
            environment["DANSO_OPENAI_BASE_URL"] = settings.danso_base_url
        provider, journals = "openai", "journals"
    return DansoRuntime(binary=binary, state_directory=root / journals,
                        provider=provider, model=settings.danso_model,
                        environment=environment, default_effort=settings.danso_effort,
                        sandbox=settings.danso_sandbox,
                        timeout_seconds=settings.danso_timeout_seconds,
                        provider_timeout_seconds=settings.danso_provider_timeout_seconds,
                        max_turns=settings.danso_max_turns,
                        compact_at_bytes=settings.danso_compact_at_bytes)
