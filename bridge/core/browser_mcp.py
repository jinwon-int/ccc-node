"""Opt-in fleet-browser MCP bundle for owner Claude sessions (#2034).

The browser runtime lives on one pilot node (jinwon-int/fleet-mcp: windowed
Chrome + Playwright MCP, owner-observable over Tailnet noVNC). Agents reach it
as a stdio MCP server over SSH:

    ssh <host> /opt/fleet-mcp/current/deploy/bin/browser-mcp

Owner profiles that suppress filesystem settings (``setting_sources=[]``)
cannot see a user-scope registration from ``claude/mcp-setup.sh``, so the
bridge injects the same server here when ``CCC_BRIDGE_BROWSER_MCP_HOST`` is
set. Default off. Externally isolated nodes and shared audiences never get it:
the browser holds agent-account logins, and a group chat must not be able to
drive them.

No secrets are passed: site credentials stay in the pilot node's
``secrets.env`` and are typed by name (fleet-mcp docs/ACCOUNTS.md).
"""

from __future__ import annotations

import re
from typing import Any

AUDIENCE_SHARED = "shared"
BROWSER_SERVER = "fleet-browser"
DEFAULT_BROWSER_COMMAND = "/opt/fleet-mcp/current/deploy/bin/browser-mcp"


def _tool(name: str) -> str:
    return f"mcp__{BROWSER_SERVER}__{name}"


# Reading, navigating and ordinary page interaction.
BROWSER_ALLOWED_TOOLS = [
    _tool(name)
    for name in (
        "browser_navigate",
        "browser_navigate_back",
        "browser_snapshot",
        "browser_take_screenshot",
        "browser_click",
        "browser_type",
        "browser_fill_form",
        "browser_select_option",
        "browser_press_key",
        "browser_hover",
        "browser_wait_for",
        "browser_tabs",
        "browser_handle_dialog",
        "browser_console_messages",
        "browser_find",
        "browser_drag",
        "browser_drop",
        "browser_resize",
        "browser_emulate_media",
        "browser_close",
    )
]

# Tools that can move session material into the transcript or files off the
# pilot node. Cookies/localStorage are not covered by --secrets redaction (and
# are only exposed when a storage capability is enabled — denied regardless);
# network_request(s) return request headers such as Cookie/Authorization;
# evaluate and run_code_unsafe reach all of it via arbitrary code;
# file_upload sends pilot node files to a site. Under bypassPermissions an
# unlisted tool is allowed, so every server tool must be classified — see
# test_browser_mcp.py.
BROWSER_DISALLOWED_TOOLS = [
    _tool(name)
    for name in (
        "browser_cookie_list",
        "browser_cookie_get",
        "browser_cookie_set",
        "browser_cookie_delete",
        "browser_cookie_clear",
        "browser_localstorage_list",
        "browser_localstorage_get",
        "browser_localstorage_set",
        "browser_localstorage_delete",
        "browser_localstorage_clear",
        "browser_network_request",
        "browser_network_requests",
        "browser_evaluate",
        "browser_run_code_unsafe",
        "browser_file_upload",
    )
]

BROWSER_ROUTING_PROMPT = """

## Fleet browser (`mcp__fleet-browser__*`)

A real windowed Chrome on the fleet browser pilot node, for sites that need a
login or interaction. Prefer Firecrawl for plain reading of public pages.

- Log in only with agent-dedicated accounts. Never use finance/payment,
  government or public-certificate authentication, the owner's work systems,
  or personal/SSO master accounts.
- Type credentials by their secret NAME (the value is filled server-side).
  Do not screenshot pages that display secret values.
- Ask the owner before any action with an outside effect: posting, sending,
  buying, booking, or changing account settings.
- When a second factor or CAPTCHA appears, stop and ask the owner to handle
  it on the noVNC view.
- Call `mcp__fleet-browser__browser_close` when the task is done: the
  browser profile allows one session at a time. On "Browser is already in
  use", another session holds it — tell the owner; never kill it.
"""

# ssh destination: optional user@, then a host/alias. No options, no spaces.
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9._-]*$")
# ssh hands the command to the remote shell: one absolute path, no metacharacters.
_COMMAND_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")


def build_browser_mcp(settings: Any, *, audience_kind: str | None = None) -> dict[str, Any] | None:
    """Return the fleet-browser bundle, or None when this context gets none."""

    host = str(getattr(settings, "bridge_browser_mcp_host", "") or "").strip()
    if not host:
        return None
    profile = str(getattr(settings, "node_isolation_profile", "fleet") or "fleet")
    if profile == "external":
        return None
    if audience_kind == AUDIENCE_SHARED:
        return None
    if not _HOST_RE.match(host):
        raise ValueError("CCC_BRIDGE_BROWSER_MCP_HOST must be a plain ssh destination")
    command = str(
        getattr(settings, "bridge_browser_mcp_command", "") or DEFAULT_BROWSER_COMMAND
    ).strip()
    if not _COMMAND_RE.match(command):
        raise ValueError("CCC_BRIDGE_BROWSER_MCP_COMMAND must be one plain absolute path")
    return {
        "mcp_servers": {
            BROWSER_SERVER: {
                "type": "stdio",
                "command": "ssh",
                "args": [
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    "-o",
                    "ServerAliveInterval=30",
                    "--",
                    host,
                    command,
                ],
            }
        },
        "allowed_tools": list(BROWSER_ALLOWED_TOOLS),
        "disallowed_tools": list(BROWSER_DISALLOWED_TOOLS),
        "system_prompt": BROWSER_ROUTING_PROMPT,
    }
