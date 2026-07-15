"""Curated SearXNG search + Firecrawl fetch routing for the Claude bridge."""

from __future__ import annotations

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
            "searxng": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "mcp-searxng"],
                "env": {"SEARXNG_URL": searxng_url},
            },
            "firecrawl": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "firecrawl-mcp"],
            },
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
