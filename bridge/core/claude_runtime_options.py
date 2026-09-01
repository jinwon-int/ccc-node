"""Claude SDK option and execution-policy composition."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk import (
    CanUseTool,
    ClaudeAgentOptions,
    EffortLevel,
    PermissionMode,
)
from claude_agent_sdk.types import SandboxSettings

from telegram_bot.utils.memory_policy import MEMORY_MODE_AUDIENCE_SCOPED, MEMORY_MODE_OFF

from .agent_runtime import SessionRequest
from .curated_memory import build_curated_memory_settings
from .memory_audience import audience_from_claude_environment
from .tool_policy import (
    BASH_DISABLED,
    EXECUTION_OWNER_OPERATOR,
    EXECUTION_STRICT_PROJECT,
    sdk_permission_options,
    strict_bash_sandbox_settings,
)
from .web_mcp import build_curated_web_mcp


_PERMISSION_MODES = frozenset(
    {"default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"}
)
_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})


def resolve_claude_cli_path(settings: Any) -> str | None:
    """Choose the Claude CLI the SDK should spawn.

    Bound settings with ``claude_cli_path`` win. Otherwise a ``claude`` on
    PATH is used so fleet-updated system CLIs beat the SDK-bundled binary
    (which lags and 400s on newer model IDs). Unbound runtimes keep the SDK
    default so unit tests stay request-only.
    """
    if settings is None:
        return None
    configured = getattr(settings, "claude_cli_path", None)
    if configured not in (None, ""):
        return str(configured)
    found = shutil.which("claude")
    return found or None


class ClaudeRuntimeOptionsMixin:
    """Build request-scoped Claude SDK options from the resolved host policy."""

    _settings: Any
    _max_buffer_size: int
    _execution_profile: str | None
    _bash_policy: str | None
    _claude_unrestricted: bool

    def _build_options(
        self,
        request: SessionRequest,
        can_use_tool: CanUseTool,
        *,
        stderr: Callable[[str], None] | None = None,
    ) -> ClaudeAgentOptions:
        # Fail closed on request fields this adapter cannot express through
        # the SDK: silently dropping a policy would weaken the boundary the
        # caller asked for.
        if request.sandbox_policy is not None:
            raise ValueError("Claude runtime does not support sandbox policies yet")
        if request.approvals_reviewer is not None:
            raise ValueError("Claude runtime does not support approvals reviewers")
        permission_mode: PermissionMode | None = None
        if request.approval_policy is not None:
            if request.approval_policy not in _PERMISSION_MODES:
                raise ValueError(
                    f"unsupported Claude approval policy: {request.approval_policy!r}"
                )
            permission_mode = cast(PermissionMode, request.approval_policy)
        effort: EffortLevel | None = None
        if request.effort is not None:
            if request.effort not in _EFFORT_LEVELS:
                raise ValueError(f"unsupported Claude effort: {request.effort!r}")
            effort = cast(EffortLevel, request.effort)
        options = ClaudeAgentOptions(
            cwd=request.working_directory,
            model=request.model,
            resume=request.session_id,
            permission_mode=permission_mode,
            effort=effort,
            can_use_tool=can_use_tool,
            include_partial_messages=True,
            stderr=stderr,
            # Always explicit: None here means the SDK's 1 MiB NDJSON line
            # limit, which an image-bearing tool result overflows and kills
            # the reader task for the rest of the turn (incident 2026-08-03).
            max_buffer_size=self._max_buffer_size,
            # None → SDK bundled CLI. An explicit path skips that binary so a
            # fleet-updated /usr/bin/claude is what actually serves the turn.
            cli_path=resolve_claude_cli_path(self._settings),
        )
        if self._settings is not None:
            self._apply_execution_profile(options, request)
            if getattr(self._settings, "memory_distill_provider", "auto") != "off":
                options.env = {
                    **(dict(options.env) if options.env is not None else {}),
                    "CCC_BRIDGE_DISTILL_MANAGED": "1",
                }
        return options

    def _apply_execution_profile(
        self, options: ClaudeAgentOptions, request: SessionRequest
    ) -> None:
        """Wire the execution-profile builders into the adapter options (#623).

        Mirrors the reference wiring of the legacy ``_create_user_stream``
        (removed in #584 C-2): the tool_policy permission bundle (allow/deny
        lists plus per-call ask hooks feeding ``can_use_tool``), curated web
        MCP routing, setting_sources control, curated memory injection, and
        the strict-project OS Bash sandbox.
        """

        settings = self._settings
        permission_options = sdk_permission_options(self._bash_policy)
        allowed_tools = list(permission_options["allowed_tools"])
        disallowed_tools = list(permission_options["disallowed_tools"])
        options.hooks = {
            event: list(matchers) for event, matchers in permission_options["hooks"].items()
        }
        web_mcp = build_curated_web_mcp(settings)
        if web_mcp is not None:
            allowed_tools = [
                tool for tool in allowed_tools if tool not in web_mcp["disallowed_tools"]
            ] + web_mcp["allowed_tools"]
            disallowed_tools = list(
                dict.fromkeys(disallowed_tools + web_mcp["disallowed_tools"])
            )
            options.mcp_servers = web_mcp["mcp_servers"]
            options.env = dict(web_mcp["process_env"])
            options.system_prompt = web_mcp["system_prompt"]
        options.allowed_tools = allowed_tools
        options.disallowed_tools = disallowed_tools
        if self._execution_profile == EXECUTION_OWNER_OPERATOR:
            if self._claude_unrestricted:
                # Opt-in Codex parity (owner-operator only): bypass permission
                # checks and drop the host settings chain, preserving memory
                # context through the curated settings block.
                options.permission_mode = "bypassPermissions"
                options.setting_sources = []
                self._apply_curated_memory(options, request)
            elif (
                getattr(settings, "bridge_memory_mode", MEMORY_MODE_OFF)
                == MEMORY_MODE_AUDIENCE_SCOPED
            ):
                # Audience isolation is stronger than owner-operator's normal
                # host-settings convenience. Loading the global user/project
                # settings chain here could re-register unscoped memory hooks.
                options.setting_sources = []
                self._apply_curated_memory(options, request)
            else:
                # Owner-operated bridges intentionally retain host utility and
                # the normal Claude Code settings/context chain.
                options.setting_sources = ["user", "project", "local"]
            return
        # Every non-owner profile suppresses filesystem settings. Even when
        # Bash is disallowed, user/project/local settings can register host
        # shell hooks independently of the model-facing Bash tool.
        options.setting_sources = []
        self._apply_curated_memory(options, request)
        if (
            self._execution_profile == EXECUTION_STRICT_PROJECT
            and self._bash_policy != BASH_DISABLED
        ):
            # Strict-project uses the SDK OS sandbox as the Bash boundary.
            cli_path = getattr(settings, "claude_cli_path", None)
            options.sandbox = cast(
                SandboxSettings,
                strict_bash_sandbox_settings(
                    Path(request.working_directory),
                    str(cli_path) if cli_path else None,
                ),
            )

    def _apply_curated_memory(
        self, options: ClaudeAgentOptions, request: SessionRequest
    ) -> None:
        """Attach only the canonical memory route resolved for this request."""

        mode = getattr(self._settings, "bridge_memory_mode", MEMORY_MODE_OFF)
        audience = None
        if mode == MEMORY_MODE_AUDIENCE_SCOPED:
            audience = audience_from_claude_environment(
                self._settings,
                request.memory_environment,
            )
        elif request.memory_environment is not None:
            raise ValueError(
                "Claude memory environment requires audience-scoped bridge memory"
            )
        curated_settings = build_curated_memory_settings(
            self._settings,
            audience=audience,
        )
        if curated_settings is not None:
            options.settings = curated_settings
