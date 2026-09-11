"""Telegram composition for the Danso CLI with explicit execution and OpenAI authentication modes."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import shutil
import stat
import subprocess
from typing import Any
from urllib.parse import urlsplit

from telegram_bot.core.agent_runtime import ModelInfo, SessionRequest
from telegram_bot.core.danso_worker import DansoRuntime as WorkerRuntime
from telegram_bot.core.danso_memory import prepare_memory_context
from telegram_bot.core.memory_audience import audience_from_danso_environment, shared_memory_audience
from telegram_bot.utils.config import Settings
from telegram_bot.utils.secure_fs import ensure_private_directory

ASTRA_EFFORTS = ("low", "medium", "high", "xhigh", "max")
LONG_TASK_FLAGS = (
    "--long-task",
    "--resume-task",
    "--task-status",
    "--task-stage-requests",
    "--task-max-requests",
    "--task-max-tokens",
    "--task-repeat-limit",
    "--task-pause-after-stage",
    "--task-progress",
)
TOOL_HOME_FLAG = "--tool-home"


def _validate_execution_backend(settings: Settings) -> None:
    if settings.danso_sandbox not in {"host", "bubblewrap"}:
        raise ValueError("unsupported Danso execution backend")
    if settings.danso_sandbox == "bubblewrap" and shutil.which("bwrap") is None:
        raise ValueError("Danso requires bubblewrap; install bwrap")


def _validate_zai_authentication(settings: Settings) -> None:
    """zai lane credentials: GLM key required, ChatGPT file is foreign."""
    if settings.danso_chatgpt_auth_file:
        raise ValueError("DANSO_CHATGPT_AUTH_FILE requires CCC_DANSO_AUTH_MODE=chatgpt")
    if not settings.zai_api_key or not settings.zai_api_key.strip():
        raise ValueError("Danso zai mode requires ZAI_API_KEY")


def _validate_api_key_authentication(settings: Settings) -> None:
    """api-key lane credentials: an OpenAI key is required."""
    if settings.danso_chatgpt_auth_file:
        raise ValueError("DANSO_CHATGPT_AUTH_FILE requires CCC_DANSO_AUTH_MODE=chatgpt")
    if not settings.openai_api_key or not settings.openai_api_key.strip():
        raise ValueError("Danso API-key mode requires OPENAI_API_KEY")


def _validate_authentication(settings: Settings, cwd: Path) -> None:
    if settings.danso_auth_mode not in {"api-key", "chatgpt", "zai"}:
        raise ValueError("unsupported Danso authentication mode")
    if settings.danso_auth_mode == "api-key":
        _validate_api_key_authentication(settings)
        return
    if settings.danso_auth_mode == "zai":
        _validate_zai_authentication(settings)
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


def _validate_memory(settings: Settings) -> None:
    if settings.bridge_memory_mode not in {"off", "audience-scoped"}:
        raise ValueError("Danso memory requires off or audience-scoped mode")
    if settings.bridge_memory_mode == "audience-scoped":
        if not Path(settings.codex_memory_materializer_path).is_file():
            raise ValueError("Danso memory materializer is unavailable")
        audience_root = shared_memory_audience(settings).root
        workspace = Path(settings.danso_workspace or "/")
        if (audience_root.resolve() != audience_root or audience_root.is_relative_to(workspace)
                or workspace.is_relative_to(audience_root)):
            raise ValueError("Danso memory must be disjoint from workspace and contain no symlinks")


def _validate_long_task_binary(binary: str, cwd: Path) -> None:
    """Require the complete long-task CLI surface before enabling the profile.

    This is a local ``--help`` capability check only.  It deliberately passes a
    scrubbed environment and never contacts a provider; an older Danso binary
    must fail closed instead of receiving a partial set of task flags and
    silently running a different workflow.
    """

    if not _validate_cli_flags(binary, cwd, LONG_TASK_FLAGS):
        raise ValueError("Danso executable does not expose the long-task CLI")


def _validate_cli_flags(binary: str, cwd: Path, flags: tuple[str, ...]) -> bool:
    """Check a local CLI surface without inheriting provider credentials."""
    try:
        completed = subprocess.run(
            [binary, "--help"],
            cwd=cwd,
            env={"PATH": os.defpath, "HOME": str(Path.home())},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    help_text = (completed.stdout + completed.stderr)[: 256 * 1024].decode(
        "utf-8", errors="replace"
    )
    return completed.returncode == 0 and all(flag in help_text for flag in flags)


def _validate_tool_home_binary(binary: str, cwd: Path) -> None:
    """Require the optional child-tool HOME flag only when it is configured."""
    if not _validate_cli_flags(binary, cwd, (TOOL_HOME_FLAG,)):
        raise ValueError("Danso executable does not expose --tool-home")


def _validate_tool_home(settings: Settings) -> Path | None:
    value = settings.danso_tool_home
    if value is None:
        return None
    if settings.danso_sandbox != "host":
        raise ValueError("CCC_DANSO_TOOL_HOME requires CCC_DANSO_SANDBOX=host")
    raw = str(value)
    path = Path(raw)
    if not raw.strip() or not path.is_absolute():
        raise ValueError("CCC_DANSO_TOOL_HOME must be an absolute path in host mode")
    if "\x00" in raw or os.pathsep in raw:
        raise ValueError("CCC_DANSO_TOOL_HOME contains an invalid PATH component")
    return path


def _configuration(settings: Settings) -> tuple[str, Path]:  # noqa: C901 -- provider prerequisites plus opt-in long-task profile
    """Validate locally, without creating state or contacting any provider."""
    _validate_memory(settings)
    if settings.memory_distill_provider == "danso" and settings.bridge_memory_mode != "audience-scoped":
        raise ValueError("Danso extraction requires audience-scoped memory")
    if settings.memory_distill_provider not in {"auto", "off", "danso"}:
        raise ValueError("Danso requires CCC_MEMORY_DISTILL_PROVIDER=auto, off or danso")
    if settings.danso_auth_mode == "zai" and settings.danso_model == "gpt-6-astra":
        # Issue #70: the zai default model follows the auth mode.
        settings.danso_model = "glm-5.3-flash"
    if settings.danso_auth_mode == "zai":
        if not settings.danso_model.startswith("glm-"):
            raise ValueError("Danso zai mode supports GLM models")
    elif settings.danso_model != "gpt-6-astra":
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
    tool_home = _validate_tool_home(settings)
    if settings.danso_long_task_enabled:
        if settings.danso_task_stage_requests > settings.danso_task_max_requests:
            raise ValueError("CCC_DANSO_TASK_STAGE_REQUESTS must not exceed CCC_DANSO_TASK_MAX_REQUESTS")
        if settings.danso_long_task_timeout_seconds + 10 > settings.process_timeout_seconds:
            raise ValueError(
                "CLAUDE_PROCESS_TIMEOUT must exceed CCC_DANSO_LONG_TASK_TIMEOUT_SECONDS by at least 10s"
            )
        _validate_long_task_binary(binary, cwd)
    elif settings.danso_timeout_seconds + 10 > settings.process_timeout_seconds:
        raise ValueError("CLAUDE_PROCESS_TIMEOUT must exceed CCC_DANSO_TIMEOUT_SECONDS by at least 10s")
    if tool_home is not None:
        _validate_tool_home_binary(binary, cwd)
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
    def __init__(self, *, default_effort: str = "medium", memory_settings: Settings | None = None, **kwargs: Any):
        if default_effort not in ASTRA_EFFORTS:
            raise ValueError("unsupported Astra effort")
        super().__init__(**kwargs)
        self.default_effort = default_effort
        self.memory_settings = memory_settings
        self._worker_kwargs = kwargs

    async def read_session_snapshot(self, session_id, *, bounds, memory_audience=None, memory_scope=None):
        import asyncio
        from dataclasses import replace
        from telegram_bot.core.memory_audience import MemoryAudience
        from telegram_bot.memory.danso_snapshot import read_danso_snapshot
        if self.memory_settings is None or memory_audience is None or memory_scope is None:
            raise ValueError("Danso extraction requires an explicit audience route")
        root = shared_memory_audience(self.memory_settings).root
        audience = MemoryAudience(memory_audience, memory_scope, root)
        audience_from_danso_environment(self.memory_settings, audience.danso_environment(self.memory_settings))
        # Reserve room for the strict schema and JSON escaping in native context.
        bounds = replace(bounds, max_bytes=min(bounds.max_bytes, 8192), max_message_bytes=min(bounds.max_message_bytes, 4096))
        return await asyncio.to_thread(read_danso_snapshot, self.root / audience.scope, session_id, bounds=bounds, cwd=Path(self.memory_settings.danso_workspace))

    async def inspect_recovery(self, request: SessionRequest):
        from telegram_bot.core.danso_recovery import inspect_session
        if not request.session_id:
            raise ValueError("recovery requires an existing session")
        session = await self.start_or_resume(request)
        return await inspect_session(session)

    async def list_models(self):
        return [ModelInfo(id=self.model, display_name=self.model, is_default=True,
                          supported_reasoning_efforts=ASTRA_EFFORTS,
                          default_reasoning_effort=self.default_effort)]

    async def start_or_resume(self, request: SessionRequest):
        effort = request.effort or self.default_effort
        if effort not in ASTRA_EFFORTS:
            raise ValueError("unsupported Astra effort")
        try:
            if self.memory_settings is not None:
                audience = audience_from_danso_environment(self.memory_settings, request.memory_environment)
                from .danso_memory import native_memory_command_args, native_memory_enabled
                loader = None
                native_args: list[str] | None = None
                if native_memory_enabled(self.memory_settings):
                    # Native snapshot: danso assembles and injects its own
                    # bounded memory; no materializer file, no loader task.
                    native_args = native_memory_command_args(self.memory_settings, audience)
                else:
                    async def load_context():
                        return await prepare_memory_context(self.memory_settings, audience)
                    loader = load_context
                kwargs = dict(self._worker_kwargs)
                kwargs.update(state_directory=self.root / audience.scope,
                              system_context_loader=loader,
                              native_memory_args=native_args)
                worker = WorkerRuntime(**kwargs)
                return await worker.start_or_resume(replace(request, effort=effort, memory_environment=None))
            return await super().start_or_resume(replace(request, effort=effort))
        except FileNotFoundError:
            if not request.session_id:
                raise ValueError("Danso workspace is unavailable.") from None
            raise ValueError("Stored Danso journal is unavailable. Check previous work, then use /new; no automatic replay.") from None


def build_danso_runtime(settings: Settings) -> DansoRuntime:
    binary, root = _configuration(settings)
    tool_home = _validate_tool_home(settings)
    ensure_private_directory(root)
    private_home = root / "home"
    ensure_private_directory(private_home)
    environment: dict[str, str | None] = {"PATH": os.defpath, "HOME": str(private_home)}
    if settings.danso_auth_mode == "chatgpt":
        environment["DANSO_CHATGPT_AUTH_FILE"] = settings.danso_chatgpt_auth_file
        if settings.danso_chatgpt_base_url:
            environment["DANSO_CHATGPT_BASE_URL"] = settings.danso_chatgpt_base_url
        provider, journals = "openai-codex", "chatgpt-journals"
    elif settings.danso_auth_mode == "zai":
        environment["ZAI_API_KEY"] = settings.zai_api_key
        if settings.danso_glm_base_url:
            environment["DANSO_GLM_BASE_URL"] = settings.danso_glm_base_url
        if settings.danso_glm_endpoint:
            environment["DANSO_GLM_ENDPOINT"] = settings.danso_glm_endpoint
        provider, journals = "glm", "glm-journals"
    else:
        environment["OPENAI_API_KEY"] = settings.openai_api_key
        if settings.danso_base_url:
            environment["DANSO_OPENAI_BASE_URL"] = settings.danso_base_url
        provider, journals = "openai", "journals"
    # Issue #70: the default model follows the auth mode. An explicitly
    # configured CCC_DANSO_MODEL always wins.
    model = settings.danso_model
    if settings.danso_auth_mode == "zai" and model == "gpt-6-astra":
        model = "glm-5.3-flash"
    memory_settings = settings if settings.bridge_memory_mode == "audience-scoped" else None
    if memory_settings is not None:
        journals += "-audience"
    return DansoRuntime(binary=binary, state_directory=root / journals, memory_settings=memory_settings,
                        provider=provider, model=model,
                        environment=environment, default_effort=settings.danso_effort,
                        sandbox=settings.danso_sandbox,
                        tool_home=str(tool_home) if tool_home is not None else None,
                        timeout_seconds=(
                            settings.danso_long_task_timeout_seconds
                            if settings.danso_long_task_enabled
                            else settings.danso_timeout_seconds
                        ),
                        outer_timeout_seconds=settings.process_timeout_seconds,
                        provider_timeout_seconds=settings.danso_provider_timeout_seconds,
                        max_turns=settings.danso_max_turns,
                        max_output_tokens=settings.danso_max_output_tokens,
                        compact_at_bytes=settings.danso_compact_at_bytes,
                        long_task=settings.danso_long_task_enabled,
                        task_stage_requests=settings.danso_task_stage_requests,
                        task_max_requests=settings.danso_task_max_requests,
                        task_max_tokens=settings.danso_task_max_tokens,
                        task_repeat_limit=settings.danso_task_repeat_limit,
                        task_pause_after_stage=settings.danso_task_pause_after_stage,
                        progress_jsonl=bool(settings.danso_progress_enabled and _validate_cli_flags(
                            binary, Path(settings.danso_workspace), ("--progress-jsonl",),
                        )))
