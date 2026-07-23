"""Curated SearXNG search + Firecrawl fetch routing for the Claude bridge."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


WEB_MCP_MODE = "searxng-firecrawl"
SEARXNG_SEARCH_TOOL = "mcp__searxng__searxng_web_search"
SEARXNG_FETCH_TOOL = "mcp__searxng__web_url_read"
FIRECRAWL_SCRAPE_TOOL = "mcp__firecrawl__firecrawl_scrape"
FIRECRAWL_SEARCH_TOOL = "mcp__firecrawl__firecrawl_search"
NATIVE_WEB_TOOLS = ("WebSearch", "WebFetch")

WEB_ROUTING_PROMPT = """

## Curated web routing

- For every web search, use `mcp__searxng__searxng_web_search`.
- For every known-URL fetch, read, scrape, or extraction, use
  `mcp__firecrawl__firecrawl_scrape`.
- Claude's built-in WebSearch/WebFetch, Firecrawl search, and SearXNG URL fetch
  are unavailable in this mode. Do not claim that you used them.
"""


def _secret_value(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if callable(getter) else value or "").strip()


def _is_termux() -> bool:
    if os.environ.get("TERMUX_VERSION"):
        return True
    if sys.platform.startswith("android"):
        return True
    return os.environ.get("PREFIX", "").endswith("/com.termux/files/usr")


def _global_node_cli(pkg: str) -> str | None:
    """Absolute path to a globally-installed package's first bin entry under the
    Termux prefix, or None. Reads `bin` from the package's own package.json."""
    prefix = os.environ.get("PREFIX")
    if not prefix:
        return None
    pkg_dir = Path(prefix) / "lib" / "node_modules" / pkg
    try:
        meta = json.loads((pkg_dir / "package.json").read_text(encoding="utf-8"))
    except OSError:
        return None
    bin_field = meta.get("bin")
    rel = bin_field if isinstance(bin_field, str) else next(iter((bin_field or {}).values()), None)
    if not rel:
        return None
    cli = pkg_dir / rel
    return str(cli) if cli.is_file() else None


def _stdio_server(pkg: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Build a stdio MCP server launcher for an npm package.

    The package bins start with `#!/usr/bin/env node`; on Termux/Android the
    agent's MCP spawn context does not carry termux-exec, so `/usr/bin/env` is
    unresolved and `npx -y <pkg>` fails with "<bin>: not found". There, launch
    the globally-installed package via `node <abs cli>` (bypasses the shebang);
    fall back to `npx -y <pkg>` elsewhere and when the global cli is unresolved.
    (#663)
    """
    server: dict[str, Any] = {"type": "stdio"}
    cli = _global_node_cli(pkg) if _is_termux() else None
    if cli:
        server["command"] = "node"
        server["args"] = [cli]
    else:
        server["command"] = "npx"
        server["args"] = ["-y", pkg]
    if env:
        server["env"] = env
    return server


def build_curated_web_mcp(settings: Any) -> dict[str, Any] | None:
    """Return explicit MCP/permission options without loading filesystem settings."""

    if getattr(settings, "bridge_web_mcp_mode", "off") != WEB_MCP_MODE:
        return None
    searxng_url = str(getattr(settings, "bridge_searxng_url", "") or "").strip().rstrip("/")
    firecrawl_key = _secret_value(getattr(settings, "bridge_firecrawl_api_key", None))
    if not searxng_url.startswith("https://") or not firecrawl_key:
        raise ValueError("curated bridge web MCP configuration is incomplete")
    return {
        "mcp_servers": {
            "searxng": _stdio_server("mcp-searxng", {"SEARXNG_URL": searxng_url}),
            "firecrawl": _stdio_server("firecrawl-mcp"),
        },
        "process_env": {"FIRECRAWL_API_KEY": firecrawl_key},
        "allowed_tools": [SEARXNG_SEARCH_TOOL, FIRECRAWL_SCRAPE_TOOL],
        "disallowed_tools": [
            *NATIVE_WEB_TOOLS,
            SEARXNG_FETCH_TOOL,
            FIRECRAWL_SEARCH_TOOL,
        ],
        "system_prompt": WEB_ROUTING_PROMPT,
    }
