"""ClaudeRuntime._build_options execution-profile wiring (#623).

With bound bridge settings the adapter path regains the boundary the legacy
``_create_user_stream`` built from the four in-tree builders: the tool_policy
permission bundle, the strict-project OS Bash sandbox, setting_sources
control, curated memory injection, and curated web MCP routing.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from telegram_bot.core import claude_runtime
from telegram_bot.core.agent_runtime import SessionRequest
from telegram_bot.core.claude_runtime import (
    ClaudeRuntime,
    ClaudeSession,
    _classify_cli_stderr,
)
from telegram_bot.core.memory_audience import MemoryAudience
from telegram_bot.core.web_mcp import FIRECRAWL_SCRAPE_TOOL, SEARXNG_SEARCH_TOOL


async def _reject(_tool_name, _tool_input, _context):
    raise AssertionError("can_use_tool must not run while building options")


def _settings(tmp_path: Path, **overrides) -> SimpleNamespace:
    values = dict(
        execution_profile="strict-project",
        allowed_user_ids=[1],
        require_allowlist=True,
        bash_policy="auto-approve",
        claude_unrestricted=False,
        claude_cli_path=None,
        telegram_session_scope="per-user-chat",
        bridge_memory_mode="off",
        bridge_unsafe_shared_all_memory=False,
        bot_data_dir=tmp_path / ".telegram_bot",
        bridge_memory_audience_root=None,
        claude_settings_path=tmp_path / ".claude" / "settings.json",
        honcho_config_path=tmp_path / ".hermes" / "honcho.json",
        honcho_memory_enabled=True,
        hook_policy_environment=lambda: {"CCC_WIKI_MEMORY_ENABLED": "0"},
        bridge_web_mcp_mode="off",
        bridge_searxng_url="https://search.example.com",
        bridge_firecrawl_api_key=SecretStr("fc-test-secret"),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _build(
    runtime: ClaudeRuntime,
    tmp_path: Path,
    *,
    memory_environment: dict[str, str] | None = None,
):
    request = SessionRequest(
        working_directory=str(tmp_path),
        memory_environment=memory_environment,
    )
    return runtime._build_options(request, _reject)


def test_bare_runtime_keeps_request_only_options(tmp_path: Path) -> None:
    # Without bound settings (unit tests, conformance harness) the adapter
    # builds exactly the pre-#623 request-derived options.
    options = _build(ClaudeRuntime(), tmp_path)
    assert options.allowed_tools == []
    assert options.disallowed_tools == []
    assert options.hooks is None
    assert options.mcp_servers == {}
    assert options.setting_sources is None
    assert options.settings is None
    assert options.sandbox is None


def test_strict_project_applies_permission_bundle_and_sandbox(tmp_path: Path) -> None:
    runtime = ClaudeRuntime(settings=_settings(tmp_path))
    options = _build(runtime, tmp_path)
    # auto-approve keeps the bare Bash allow rule and installs no ask hook.
    assert "Bash" in options.allowed_tools
    assert options.hooks == {}
    assert "AskUserQuestion" in options.disallowed_tools
    assert options.setting_sources == []
    assert options.settings is None
    assert options.sandbox is not None
    assert options.sandbox["enabled"] is True
    root = str(tmp_path.resolve())
    assert options.sandbox["filesystem"]["allowWrite"] == [root]
    assert root in options.sandbox["filesystem"]["allowRead"]


def test_approve_each_forces_bash_through_ask_hook(tmp_path: Path) -> None:
    runtime = ClaudeRuntime(settings=_settings(tmp_path, bash_policy="approve-each"))
    options = _build(runtime, tmp_path)
    assert "Bash" not in options.allowed_tools
    assert options.hooks is not None
    assert [matcher.matcher for matcher in options.hooks["PreToolUse"]] == ["Bash"]


def test_bash_disabled_denies_bash_and_omits_sandbox(tmp_path: Path) -> None:
    runtime = ClaudeRuntime(settings=_settings(tmp_path, bash_policy="disabled"))
    options = _build(runtime, tmp_path)
    assert "Bash" in options.disallowed_tools
    assert options.sandbox is None
    assert options.setting_sources == []


def test_owner_operator_retains_host_settings_chain(tmp_path: Path) -> None:
    runtime = ClaudeRuntime(
        settings=_settings(tmp_path, execution_profile="owner-operator")
    )
    options = _build(runtime, tmp_path)
    assert options.setting_sources == ["user", "project", "local"]
    assert options.sandbox is None
    assert options.settings is None


def test_owner_operator_audience_scope_suppresses_global_settings(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        execution_profile="owner-operator",
        bridge_memory_mode="audience-scoped",
    )
    audience = MemoryAudience(
        "shared",
        "shared",
        settings.bot_data_dir / "memory-audiences",
    )
    options = _build(
        ClaudeRuntime(settings=settings),
        tmp_path,
        memory_environment=audience.claude_environment(settings),
    )

    assert options.setting_sources == []
    assert options.settings is not None
    env = json.loads(options.settings)["env"]
    assert env["CCC_MEMORY_AUDIENCE"] == "shared"
    assert env["CCC_MEMORY_SCOPE"] == "shared"


def test_owner_operator_unrestricted_bypasses_with_curated_memory(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(claude_runtime, "running_as_root", lambda: False)
    runtime = ClaudeRuntime(
        settings=_settings(
            tmp_path,
            execution_profile="owner-operator",
            claude_unrestricted=True,
            bridge_memory_mode="curated",
        )
    )
    options = _build(runtime, tmp_path)
    assert options.permission_mode == "bypassPermissions"
    assert options.setting_sources == []
    assert options.sandbox is None
    assert options.settings is not None
    payload = json.loads(options.settings)
    assert "SessionStart" in payload["hooks"]


def test_curated_memory_injected_for_strict_project(tmp_path: Path) -> None:
    runtime = ClaudeRuntime(settings=_settings(tmp_path, bridge_memory_mode="curated"))
    options = _build(runtime, tmp_path)
    assert options.settings is not None
    payload = json.loads(options.settings)
    assert payload["env"]["CCC_WIKI_MEMORY_ENABLED"] == "0"
    assert "load-memory.sh" in json.dumps(payload["hooks"]["SessionStart"])


def test_audience_scoped_memory_fails_closed_without_route(tmp_path: Path) -> None:
    settings = _settings(tmp_path, bridge_memory_mode="audience-scoped")
    runtime = ClaudeRuntime(settings=settings)
    with pytest.raises(ValueError, match="audience-scoped"):
        _build(runtime, tmp_path)


@pytest.mark.parametrize(
    ("kind", "scope"),
    [
        ("shared", "shared"),
        ("private", "private-" + "a" * 32),
    ],
)
def test_audience_scoped_memory_injects_only_resolved_route(
    tmp_path: Path,
    kind: str,
    scope: str,
) -> None:
    settings = _settings(tmp_path, bridge_memory_mode="audience-scoped")
    audience = MemoryAudience(
        kind,
        scope,
        settings.bot_data_dir / "memory-audiences",
    )
    options = _build(
        ClaudeRuntime(settings=settings),
        tmp_path,
        memory_environment=audience.claude_environment(settings),
    )

    assert options.settings is not None
    env = json.loads(options.settings)["env"]
    assert env["CCC_MEMORY_AUDIENCE"] == kind
    assert env["CCC_MEMORY_SCOPE"] == scope
    assert env["CCC_STATE_DIR"] == str(audience.state_dir)
    assert env["CCC_MEMORY_SHARED_STATE_DIR"] == str(audience.shared_root / "state")
    assert env["CCC_WIKI_MEMORY_ENABLED"] == "0"
    assert "CODEX_HOME" not in env


def test_audience_scoped_memory_rejects_tampered_route(tmp_path: Path) -> None:
    settings = _settings(tmp_path, bridge_memory_mode="audience-scoped")
    audience = MemoryAudience(
        "shared",
        "shared",
        settings.bot_data_dir / "memory-audiences",
    )
    environment = audience.claude_environment(settings)
    environment["CCC_STATE_DIR"] = str(tmp_path / "untrusted")

    with pytest.raises(ValueError, match="does not match"):
        _build(
            ClaudeRuntime(settings=settings),
            tmp_path,
            memory_environment=environment,
        )


def test_curated_web_mcp_replaces_native_web_tools(tmp_path: Path) -> None:
    runtime = ClaudeRuntime(
        settings=_settings(tmp_path, bridge_web_mcp_mode="searxng-firecrawl")
    )
    options = _build(runtime, tmp_path)
    assert SEARXNG_SEARCH_TOOL in options.allowed_tools
    assert FIRECRAWL_SCRAPE_TOOL in options.allowed_tools
    assert "WebSearch" not in options.allowed_tools
    assert "WebFetch" not in options.allowed_tools
    assert "WebSearch" in options.disallowed_tools
    assert set(options.mcp_servers) == {"searxng", "firecrawl"}
    assert options.env == {"FIRECRAWL_API_KEY": "fc-test-secret"}
    assert "Curated web routing" in options.system_prompt


# ---- CLI stderr capture (#846) --------------------------------------------
# The SDK transport pipes stderr ONLY when a callback is registered
# (`stderr_dest = PIPE if self._options.stderr is not None else None`), so
# without this wiring the CLI's dying words are discarded by the kernel and an
# admission timeout has nothing to report.


def test_build_options_leaves_stderr_unset_when_no_sink_given(tmp_path: Path) -> None:
    # The default keeps the pre-#846 transport behaviour for callers that do
    # not want a pipe (unit tests, conformance harness).
    assert _build(ClaudeRuntime(), tmp_path).stderr is None


def test_build_options_registers_the_supplied_stderr_sink(tmp_path: Path) -> None:
    seen: list[str] = []

    def sink(line: str) -> None:
        seen.append(line)

    request = SessionRequest(working_directory=str(tmp_path))
    options = ClaudeRuntime()._build_options(request, _reject, stderr=sink)
    assert options.stderr is sink
    options.stderr("boom")
    assert seen == ["boom"]


@pytest.mark.asyncio
async def test_start_or_resume_pipes_cli_stderr_into_the_session(tmp_path: Path) -> None:
    # End-to-end wiring: whatever the transport reports on stderr has to reach
    # the session that will later be asked why the turn never started.
    captured: dict[str, object] = {}

    class _Client:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self):
            return None

        async def query(self, *_args, **_kwargs):
            return None

        async def receive_messages(self):
            if False:  # pragma: no cover - never yields
                yield None

        async def interrupt(self):
            return None

        async def disconnect(self):
            return None

    runtime = ClaudeRuntime(sdk_client_factory=_Client)
    session = await runtime.start_or_resume(
        SessionRequest(working_directory=str(tmp_path))
    )

    sink = captured["options"].stderr
    assert callable(sink)
    sink("Error: connect ECONNRESET")

    details = session.transport_diagnostics()
    assert details["stderr_lines"] == 1
    assert details["stderr_class"] == "network"


def test_transport_diagnostics_reports_silence_as_no_class(tmp_path: Path) -> None:
    # A process that said nothing is the most common admission-timeout shape and
    # must be distinguishable from one that failed loudly.
    session = ClaudeSession(ClaudeRuntime(), "sid")
    details = session.transport_diagnostics()
    assert details == {"exit_code": None, "stderr_class": None, "stderr_lines": 0}


def test_stderr_sink_is_bounded_and_ignores_blank_lines() -> None:
    session = ClaudeSession(ClaudeRuntime(), "sid")
    for index in range(200):
        session._record_stderr(f"line {index}")
    session._record_stderr("   ")
    session._record_stderr("")
    assert session.transport_diagnostics()["stderr_lines"] == 20


def test_stderr_sink_never_raises_on_hostile_input() -> None:
    # Runs on the SDK reader task: raising here would disturb a live turn.
    class _Explodes:
        def __str__(self):
            raise RuntimeError("boom")

    session = ClaudeSession(ClaudeRuntime(), "sid")
    session._record_stderr(_Explodes())
    assert session.transport_diagnostics()["stderr_lines"] == 0


def test_stderr_classes_cover_the_shapes_worth_telling_apart() -> None:
    assert _classify_cli_stderr(["Error: rate limit exceeded"]) == "rate-limit"
    assert _classify_cli_stderr(["Not logged in · Please run /login"]) == "auth"
    assert _classify_cli_stderr(["request timed out"]) == "timeout"
    assert _classify_cli_stderr(["certificate verify failed"]) == "tls"
    assert _classify_cli_stderr(["something nobody enumerated"]) == "other"
    assert _classify_cli_stderr([]) is None
