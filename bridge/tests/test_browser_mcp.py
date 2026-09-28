from __future__ import annotations

from types import SimpleNamespace

import pytest

from telegram_bot.core.browser_mcp import (
    BROWSER_ALLOWED_TOOLS,
    BROWSER_DISALLOWED_TOOLS,
    BROWSER_SERVER,
    DEFAULT_BROWSER_COMMAND,
    build_browser_mcp,
)
from telegram_bot.core.family_mcp import merge_mcp_bundle

# `tools/list` of @playwright/mcp 0.0.82 (the version pinned by
# jinwon-int/fleet-mcp package-lock.json), captured 2026-09-28. When fleet-mcp
# bumps the server, refresh this list: an unclassified tool would be allowed
# under bypassPermissions.
PINNED_SERVER_TOOLS = [
    "browser_click", "browser_close", "browser_console_messages", "browser_drag",
    "browser_drop", "browser_emulate_media", "browser_evaluate", "browser_file_upload",
    "browser_fill_form", "browser_find", "browser_handle_dialog", "browser_hover",
    "browser_navigate", "browser_navigate_back", "browser_network_request",
    "browser_network_requests", "browser_press_key", "browser_resize",
    "browser_run_code_unsafe", "browser_select_option", "browser_snapshot",
    "browser_tabs", "browser_take_screenshot", "browser_type", "browser_wait_for",
]


def _settings(**overrides: object) -> SimpleNamespace:
    base = {
        "bridge_browser_mcp_host": "soonwook",
        "bridge_browser_mcp_command": None,
        "node_isolation_profile": "fleet",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _short(tools: list[str]) -> set[str]:
    return {tool.removeprefix(f"mcp__{BROWSER_SERVER}__") for tool in tools}


def test_off_by_default() -> None:
    assert build_browser_mcp(_settings(bridge_browser_mcp_host=None)) is None
    assert build_browser_mcp(_settings(bridge_browser_mcp_host="  ")) is None


def test_external_isolation_and_shared_audience_get_nothing() -> None:
    assert build_browser_mcp(_settings(node_isolation_profile="external")) is None
    assert build_browser_mcp(_settings(), audience_kind="shared") is None
    assert build_browser_mcp(_settings(), audience_kind="private") is not None


def test_ssh_stdio_server_without_env_or_secrets() -> None:
    bundle = build_browser_mcp(_settings())
    assert bundle is not None
    server = bundle["mcp_servers"][BROWSER_SERVER]
    assert server["type"] == "stdio"
    assert server["command"] == "ssh"
    assert server["args"][-3:] == ["--", "soonwook", DEFAULT_BROWSER_COMMAND]
    assert "BatchMode=yes" in server["args"]
    assert "env" not in server
    assert "process_env" not in bundle


def test_custom_command_and_user_host() -> None:
    bundle = build_browser_mcp(
        _settings(bridge_browser_mcp_host="root@soonwook", bridge_browser_mcp_command="/opt/x/browser-mcp")
    )
    assert bundle is not None
    assert bundle["mcp_servers"][BROWSER_SERVER]["args"][-2:] == ["root@soonwook", "/opt/x/browser-mcp"]


@pytest.mark.parametrize("host", ["-oProxyCommand=sh", "soonwook other", "soon;wook", "a b@c"])
def test_rejects_hosts_that_could_become_ssh_options(host: str) -> None:
    with pytest.raises(ValueError):
        build_browser_mcp(_settings(bridge_browser_mcp_host=host))


@pytest.mark.parametrize("command", ["relative/path", "/opt/x y", "/opt/x;rm -rf ~", "/opt/$(id)", "/opt/x|sh"])
def test_rejects_commands_the_remote_shell_could_reinterpret(command: str) -> None:
    with pytest.raises(ValueError):
        build_browser_mcp(_settings(bridge_browser_mcp_command=command))


def test_every_pinned_server_tool_is_classified_exactly_once() -> None:
    allowed = _short(BROWSER_ALLOWED_TOOLS)
    denied = _short(BROWSER_DISALLOWED_TOOLS)
    assert not allowed & denied
    unclassified = set(PINNED_SERVER_TOOLS) - allowed - denied
    assert not unclassified, f"classify new fleet-browser tools: {sorted(unclassified)}"


def test_session_material_tools_are_denied() -> None:
    denied = _short(BROWSER_DISALLOWED_TOOLS)
    for name in (
        "browser_evaluate",
        "browser_run_code_unsafe",
        "browser_network_request",
        "browser_network_requests",
        "browser_file_upload",
        "browser_cookie_get",
        "browser_localstorage_get",
    ):
        assert name in denied


def test_prompt_carries_the_account_rules() -> None:
    bundle = build_browser_mcp(_settings())
    assert bundle is not None
    prompt = bundle["system_prompt"]
    assert "agent-dedicated accounts" in prompt
    assert "browser_close" in prompt
    assert "Ask the owner before any action with an outside effect" in prompt


def test_merges_with_other_bundles_and_rejects_name_collisions() -> None:
    options = SimpleNamespace(
        mcp_servers={"family-skills": {"type": "stdio"}},
        allowed_tools=["Read"],
        disallowed_tools=[],
        env=None,
        system_prompt="base",
    )
    bundle = build_browser_mcp(_settings())
    assert bundle is not None
    merge_mcp_bundle(options, bundle)
    assert set(options.mcp_servers) == {"family-skills", BROWSER_SERVER}
    assert "Read" in options.allowed_tools
    assert f"mcp__{BROWSER_SERVER}__browser_evaluate" in options.disallowed_tools
    assert options.system_prompt.startswith("base")
    with pytest.raises(ValueError):
        merge_mcp_bundle(options, build_browser_mcp(_settings()))
