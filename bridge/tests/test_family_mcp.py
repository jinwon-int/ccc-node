"""family_mcp injection tests (#1678): builder conditions (external/shared
refusal, wiki gating), fail-closed configuration errors, bundle merging
without clobbering the curated web MCP, and the owner-profile wiring through
ClaudeRuntime._build_options.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_bot.core import claude_runtime, family_mcp
from telegram_bot.core.claude_runtime import ClaudeRuntime
from telegram_bot.core.family_mcp import build_family_mcp, merge_mcp_bundle
from telegram_bot.core.memory_audience import MemoryAudience
from telegram_bot.core.web_mcp import build_curated_web_mcp
from telegram_bot.core.agent_runtime import SessionRequest
from telegram_bot.core.family_skills_server import SERVER_NAME


def _settings(tmp_path: Path, **overrides) -> SimpleNamespace:
    """Full settings stub — mirrors test_claude_runtime_options._settings."""

    values = dict(
        project_root=tmp_path,
        node_isolation_profile="fleet",
        wiki_memory_enabled=False,
        execution_profile="strict-project",
        allowed_user_ids=[1],
        require_allowlist=True,
        bash_policy="auto-approve",
        claude_unrestricted=False,
        claude_cli_path=None,
        telegram_session_scope="per-user-chat",
        bridge_memory_mode="off",
        memory_distill_provider="auto",
        bridge_unsafe_shared_all_memory=False,
        bot_data_dir=tmp_path / ".telegram_bot",
        bridge_memory_audience_root=None,
        claude_settings_path=tmp_path / ".claude" / "settings.json",
        hook_policy_environment=lambda: {"CCC_WIKI_MEMORY_ENABLED": "0"},
        bridge_web_mcp_mode="off",
        bridge_searxng_url="https://search.example.com",
        bridge_firecrawl_api_key=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


async def _reject(_tool_name, _tool_input, _context):
    raise AssertionError("can_use_tool must not run while building options")


def _owner_runtime(tmp_path: Path, **overrides) -> ClaudeRuntime:
    return ClaudeRuntime(
        settings=_settings(
            tmp_path,
            execution_profile="owner-operator",
            memory_distill_provider="off",
            **overrides,
        )
    )


def _build(runtime: ClaudeRuntime, tmp_path: Path, memory_environment=None):
    request = SessionRequest(
        working_directory=str(tmp_path),
        memory_environment=memory_environment,
    )
    return runtime._build_options(request, _reject)


def _install_server(tmp_path: Path) -> Path:
    """Create stand-ins; workspace copies must never become launch targets."""

    core = tmp_path / "bridge" / "core"
    core.mkdir(parents=True, exist_ok=True)
    for name in ("family_skills_server.py", "family_ops_server.py"):
        (core / name).write_text("# fixture server\n", encoding="utf-8")
    return core / "family_skills_server.py"


@pytest.fixture(autouse=True)
def installed_servers(tmp_path: Path, monkeypatch) -> Path:
    """Model an installed package separate from the user's workspace."""
    skills = _install_server(tmp_path / "installation")
    monkeypatch.setattr(family_mcp, "__file__", str(skills.with_name("family_mcp.py")))
    return skills.parent


def test_builder_uses_installation_without_workspace_servers(
    tmp_path: Path, installed_servers: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    bundle = build_family_mcp(_settings(workspace))
    for server, filename in (
        (SERVER_NAME, "family_skills_server.py"),
        ("family-ops", "family_ops_server.py"),
    ):
        assert bundle["mcp_servers"][server]["args"] == [str(installed_servers / filename)]
    assert not (workspace / "bridge").exists()


def test_builder_ignores_workspace_decoy_servers(
    tmp_path: Path, installed_servers: Path
) -> None:
    decoy = _install_server(tmp_path / "workspace")
    bundle = build_family_mcp(_settings(tmp_path / "workspace"))
    assert bundle["mcp_servers"][SERVER_NAME]["args"] == [
        str(installed_servers / "family_skills_server.py")
    ]
    assert bundle["mcp_servers"]["family-ops"]["args"] == [
        str(installed_servers / "family_ops_server.py")
    ]
    assert str(decoy) not in bundle["mcp_servers"][SERVER_NAME]["args"]


def test_builder_resolves_installed_module_symlink(
    tmp_path: Path, installed_servers: Path, monkeypatch
) -> None:
    module = installed_servers / "family_mcp.py"
    module.write_text("# installed module\n", encoding="utf-8")
    alias = tmp_path / "module_alias.py"
    alias.symlink_to(module)
    monkeypatch.setattr(family_mcp, "__file__", str(alias))
    bundle = build_family_mcp(_settings(tmp_path / "workspace"))
    assert bundle["mcp_servers"][SERVER_NAME]["args"] == [
        str(installed_servers / "family_skills_server.py")
    ]


def test_builder_refuses_external_and_shared(tmp_path: Path) -> None:
    _install_server(tmp_path)
    assert build_family_mcp(_settings(tmp_path, node_isolation_profile="external")) is None
    assert build_family_mcp(_settings(tmp_path), audience_kind="shared") is None


def test_builder_builds_skills_server_and_gates_wiki(
    tmp_path: Path, monkeypatch
) -> None:
    _install_server(tmp_path)
    bundle = build_family_mcp(_settings(tmp_path))
    assert set(bundle["mcp_servers"]) == {SERVER_NAME, "family-ops"}
    assert bundle["allowed_tools"] == [
        "mcp__family-skills__skill_search",
        "mcp__family-skills__skill_read",
        "mcp__family-ops__node_status",
    ]
    server = bundle["mcp_servers"][SERVER_NAME]
    assert server["env"] == {"CCC_NODE_ISOLATION_PROFILE": "fleet"}
    assert bundle["mcp_servers"][SERVER_NAME]["args"][0].endswith(
        "bridge/core/family_skills_server.py"
    )

    monkeypatch.setattr(family_mcp.shutil, "which", lambda _: None)
    disabled = build_family_mcp(_settings(tmp_path, wiki_memory_enabled=True))
    assert set(disabled["mcp_servers"]) == {SERVER_NAME, "family-ops"}

    monkeypatch.setattr(family_mcp.shutil, "which", lambda _: "/usr/local/bin/wiki-agent")
    enabled = build_family_mcp(_settings(tmp_path, wiki_memory_enabled=True))
    assert set(enabled["mcp_servers"]) == {SERVER_NAME, "family-ops", "family-wiki"}
    assert enabled["mcp_servers"]["family-wiki"]["args"] == ["mcp-serve"]
    assert "mcp__family-wiki__wiki_find" in enabled["allowed_tools"]


def test_builder_private_audience_marker_passed_to_server(tmp_path: Path) -> None:
    _install_server(tmp_path)
    bundle = build_family_mcp(_settings(tmp_path), audience_kind="private")
    assert bundle["mcp_servers"][SERVER_NAME]["env"] == {
        "CCC_NODE_ISOLATION_PROFILE": "fleet",
        "CCC_MEMORY_AUDIENCE": "private",
    }


def test_builder_fails_closed_without_configuration(tmp_path: Path) -> None:
    stripped = _settings(tmp_path)
    del stripped.project_root
    with pytest.raises(ValueError, match="project settings"):
        build_family_mcp(stripped)


@pytest.mark.parametrize("missing", ["family_skills_server.py", "family_ops_server.py"])
def test_builder_fails_closed_when_installed_server_missing(
    tmp_path: Path, installed_servers: Path, missing: str
) -> None:
    # A workspace decoy must not rescue an incomplete installation.
    _install_server(tmp_path)
    (installed_servers / missing).rename(installed_servers / (missing + ".backup"))
    server_name = "family-skills" if missing.startswith("family_skills") else "family-ops"
    with pytest.raises(ValueError, match=server_name + " MCP server file is missing"):
        build_family_mcp(_settings(tmp_path))


def test_merge_combines_web_and_family_without_clobber(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        bridge_web_mcp_mode="firecrawl",
        bridge_firecrawl_api_key=SimpleNamespace(get_secret_value=lambda: "fc-test"),
    )
    web = build_curated_web_mcp(settings)
    assert web is not None
    _install_server(tmp_path)
    family = build_family_mcp(_settings(tmp_path))
    assert family is not None

    options = SimpleNamespace(
        mcp_servers={},
        allowed_tools=["Bash"],
        disallowed_tools=["AskUserQuestion"],
        env=None,
        system_prompt=None,
    )
    merge_mcp_bundle(options, web)
    merge_mcp_bundle(options, family)
    assert set(options.mcp_servers) == {"firecrawl", SERVER_NAME, "family-ops"}
    assert options.allowed_tools[0] == "Bash"
    assert "mcp__firecrawl__firecrawl_search" in options.allowed_tools
    assert "mcp__family-skills__skill_search" in options.allowed_tools
    assert "mcp__family-ops__node_status" in options.allowed_tools
    assert options.disallowed_tools == [
        "AskUserQuestion",
        "WebSearch",
        "WebFetch",
        "mcp__searxng__searxng_web_search",
        "mcp__searxng__web_url_read",
    ]
    assert options.env["FIRECRAWL_API_KEY"] == "fc-test"
    assert "Curated web routing" in options.system_prompt
    assert "Family skill, wiki & ops lookup" in options.system_prompt


def test_merge_fails_closed_on_server_name_collision(tmp_path: Path) -> None:
    _install_server(tmp_path)
    family = build_family_mcp(_settings(tmp_path))
    options = SimpleNamespace(
        mcp_servers={"family-ops": {"type": "stdio"}},
        allowed_tools=[],
        disallowed_tools=[],
        env=None,
        system_prompt=None,
    )
    with pytest.raises(ValueError, match="conflicting MCP server"):
        merge_mcp_bundle(options, family)


def test_owner_unrestricted_profile_injects_family_servers(
    tmp_path: Path, monkeypatch
) -> None:
    # Unrestricted parity is opt-in and root denied; force the non-root path.
    monkeypatch.setattr(claude_runtime, "running_as_root", lambda: False)
    _install_server(tmp_path)
    runtime = _owner_runtime(tmp_path, claude_unrestricted=True)
    options = _build(runtime, tmp_path)
    assert options.setting_sources == []
    assert set(options.mcp_servers) == {SERVER_NAME, "family-ops"}
    assert "mcp__family-skills__skill_read" in options.allowed_tools
    assert "mcp__family-ops__node_status" in options.allowed_tools


def test_owner_audience_scoped_private_injects_shared_refused(
    tmp_path: Path,
) -> None:
    _install_server(tmp_path)
    settings = _settings(
        tmp_path,
        execution_profile="owner-operator",
        bridge_memory_mode="audience-scoped",
        memory_distill_provider="off",
    )
    private = MemoryAudience("private", "private-" + "a" * 32, settings.bot_data_dir / "memory-audiences")
    options = _build(
        ClaudeRuntime(settings=settings), tmp_path, private.claude_environment(settings)
    )
    assert SERVER_NAME in options.mcp_servers
    server = options.mcp_servers[SERVER_NAME]
    assert server["env"] == {
        "CCC_NODE_ISOLATION_PROFILE": "fleet",
        "CCC_MEMORY_AUDIENCE": "private",
    }
    assert options.mcp_servers["family-ops"]["env"] == server["env"]

    shared = MemoryAudience("shared", "shared", settings.bot_data_dir / "memory-audiences")
    options = _build(
        ClaudeRuntime(settings=settings), tmp_path, shared.claude_environment(settings)
    )
    assert not options.mcp_servers


def test_plain_owner_keeps_host_chain_without_family_injection(
    tmp_path: Path,
) -> None:
    _install_server(tmp_path)
    options = _build(_owner_runtime(tmp_path), tmp_path)
    assert options.setting_sources == ["user", "project", "local"]
    assert not options.mcp_servers
