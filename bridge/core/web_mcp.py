"""Curated Firecrawl search + scrape/developer routing, with explicit SearXNG fallback."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any


WEB_MCP_MODE = "searxng-firecrawl"
FIRECRAWL_MCP_MODE = "firecrawl"
SEARXNG_SEARCH_TOOL = "mcp__searxng__searxng_web_search"
SEARXNG_FETCH_TOOL = "mcp__searxng__web_url_read"
FIRECRAWL_SCRAPE_TOOL = "mcp__firecrawl__firecrawl_scrape"
FIRECRAWL_SEARCH_TOOL = "mcp__firecrawl__firecrawl_search"
FIRECRAWL_DEVELOPER_SEARCH_TOOL = "mcp__firecrawl__firecrawl_developer_search"
NATIVE_WEB_TOOLS = ("WebSearch", "WebFetch")

WEB_ROUTING_PROMPT = """

## Curated web routing

- For every general web search, use `mcp__firecrawl__firecrawl_search`.
- Use `mcp__searxng__searxng_web_search` only as an explicit fallback: Korean
  or Naver-oriented lookup, Tailnet-local privacy, or when Firecrawl search
  failed. Do not switch silently; pick the tool explicitly.
- For every known-URL fetch, read, scrape, or extraction, use
  `mcp__firecrawl__firecrawl_scrape`.
- For public developer artifacts (documentation, repository READMEs, issues,
  and merged pull requests), use
  `mcp__firecrawl__firecrawl_developer_search`.
- Claude's built-in WebSearch/WebFetch and SearXNG URL fetch are unavailable
  in this mode. Do not claim that you used them.
"""

FIRECRAWL_ROUTING_PROMPT = """

## Curated web routing

- For every web search, use `mcp__firecrawl__firecrawl_search`.
- For every known-URL fetch, read, scrape, or extraction, use
  `mcp__firecrawl__firecrawl_scrape`.
- For public developer artifacts (documentation, repository READMEs, issues,
  and merged pull requests), use
  `mcp__firecrawl__firecrawl_developer_search`.
- Claude's built-in WebSearch/WebFetch and SearXNG are unavailable in this
  mode. Do not claim that you used them.
"""


def _secret_value(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if callable(getter) else value or "").strip()


def _node_global_root() -> Path | None:
    """`<node prefix>/lib/node_modules` for the `node` on PATH (the npm default
    global root, e.g. /usr/lib/node_modules or an nvm prefix), or None."""
    node = shutil.which("node")
    if not node:
        return None
    return Path(os.path.realpath(node)).parent.parent / "lib" / "node_modules"


def _global_module_roots() -> list[Path]:
    """Candidate npm global roots, most specific first: the Termux `$PREFIX`,
    an explicit npm prefix, then the prefix of the `node` on PATH."""
    roots: list[Path] = []
    for prefix in (
        os.environ.get("PREFIX"),
        os.environ.get("npm_config_prefix") or os.environ.get("NPM_CONFIG_PREFIX"),
    ):
        if prefix:
            roots.append(Path(prefix) / "lib" / "node_modules")
    node_root = _node_global_root()
    if node_root is not None:
        roots.append(node_root)
    return list(dict.fromkeys(roots))


def _package_cli(pkg_dir: Path) -> str | None:
    """Absolute path to a package's first bin entry (from its package.json)."""
    try:
        meta = json.loads((pkg_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    bin_field = meta.get("bin") if isinstance(meta, dict) else None
    rel = bin_field if isinstance(bin_field, str) else next(iter((bin_field or {}).values()), None)
    if not isinstance(rel, str) or not rel:
        return None
    cli = pkg_dir / rel
    return str(cli) if cli.is_file() else None


def _global_node_cli(pkg: str) -> str | None:
    """Absolute path to a globally-installed package's first bin entry, or None."""
    for root in _global_module_roots():
        cli = _package_cli(root / pkg)
        if cli:
            return cli
    return None


def _stdio_server(pkg: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Build a stdio MCP server launcher for an npm package.

    A globally-installed package is launched as `node <abs cli>` on every
    platform:
    - Termux/Android (#663): the bins start with `#!/usr/bin/env node`, and
      the agent's MCP spawn context does not carry termux-exec, so `npx -y`
      fails with "<bin>: not found".
    - Linux (#2299): `npx -y` keeps a resident `npm exec` wrapper (~80 MiB)
      per server per session. On 4 GiB nodes that is a quarter of the
      session-guard RSS limit.

    `npx -y <pkg>` remains the fallback when no global install resolves.
    """
    server: dict[str, Any] = {"type": "stdio"}
    cli = _global_node_cli(pkg)
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

    mode = getattr(settings, "bridge_web_mcp_mode", "off")
    firecrawl_key = _secret_value(getattr(settings, "bridge_firecrawl_api_key", None))

    if mode == FIRECRAWL_MCP_MODE:
        if not firecrawl_key:
            raise ValueError("curated bridge web MCP configuration is incomplete")
        return {
            "mcp_servers": {"firecrawl": _stdio_server("firecrawl-mcp")},
            "process_env": {"FIRECRAWL_API_KEY": firecrawl_key},
            "allowed_tools": [
                FIRECRAWL_SEARCH_TOOL,
                FIRECRAWL_SCRAPE_TOOL,
                FIRECRAWL_DEVELOPER_SEARCH_TOOL,
            ],
            "disallowed_tools": [
                *NATIVE_WEB_TOOLS,
                SEARXNG_SEARCH_TOOL,
                SEARXNG_FETCH_TOOL,
            ],
            "system_prompt": FIRECRAWL_ROUTING_PROMPT,
        }

    if mode != WEB_MCP_MODE:
        return None
    searxng_url = str(getattr(settings, "bridge_searxng_url", "") or "").strip().rstrip("/")
    if not searxng_url.startswith("https://") or not firecrawl_key:
        raise ValueError("curated bridge web MCP configuration is incomplete")
    return {
        "mcp_servers": {
            "searxng": _stdio_server("mcp-searxng", {"SEARXNG_URL": searxng_url}),
            "firecrawl": _stdio_server("firecrawl-mcp"),
        },
        "process_env": {"FIRECRAWL_API_KEY": firecrawl_key},
        "allowed_tools": [
            FIRECRAWL_SEARCH_TOOL,
            SEARXNG_SEARCH_TOOL,
            FIRECRAWL_SCRAPE_TOOL,
            FIRECRAWL_DEVELOPER_SEARCH_TOOL,
        ],
        "disallowed_tools": [
            *NATIVE_WEB_TOOLS,
            SEARXNG_FETCH_TOOL,
        ],
        "system_prompt": WEB_ROUTING_PROMPT,
    }
