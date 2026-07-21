from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

from telegram_bot.core.web_mcp import (
    FIRECRAWL_SCRAPE_TOOL,
    FIRECRAWL_SEARCH_TOOL,
    SEARXNG_FETCH_TOOL,
    SEARXNG_SEARCH_TOOL,
    build_curated_web_mcp,
)


def _settings(tmp_path: Path, mode: str = "searxng-firecrawl") -> SimpleNamespace:
    return SimpleNamespace(
        project_root=tmp_path,
        execution_profile="disabled",
        bash_policy="disabled",
        allowed_user_ids=[1, 2, 3, 4],
        require_allowlist=True,
        claude_cli_path=None,
        enable_streaming=False,
        enable_partial_streaming=False,
        bot_data_dir=None,
        task_ledger_path=None,
        telegram_session_scope="shared-all",
        image_context_guard=False,
        bridge_memory_mode="off",
        bridge_web_mcp_mode=mode,
        bridge_searxng_url="https://search.example.com/",
        bridge_firecrawl_api_key=SecretStr("fc-test-secret"),
    )


def test_curated_web_mcp_is_off_by_default(tmp_path: Path) -> None:
    assert build_curated_web_mcp(_settings(tmp_path, "off")) is None


def test_curated_web_mcp_builds_only_search_and_scrape(tmp_path: Path) -> None:
    options = build_curated_web_mcp(_settings(tmp_path))
    assert options is not None
    assert options["allowed_tools"] == [SEARXNG_SEARCH_TOOL, FIRECRAWL_SCRAPE_TOOL]
    assert options["disallowed_tools"] == [
        "WebSearch",
        "WebFetch",
        SEARXNG_FETCH_TOOL,
        FIRECRAWL_SEARCH_TOOL,
    ]
    assert options["mcp_servers"]["searxng"]["env"] == {
        "SEARXNG_URL": "https://search.example.com"
    }
    assert "env" not in options["mcp_servers"]["firecrawl"]
    assert options["process_env"] == {"FIRECRAWL_API_KEY": "fc-test-secret"}


def test_serialized_mcp_config_excludes_firecrawl_key(tmp_path: Path) -> None:
    web = build_curated_web_mcp(_settings(tmp_path))
    assert web is not None
    assert "fc-test-secret" not in json.dumps(web["mcp_servers"])
    assert web["process_env"] == {"FIRECRAWL_API_KEY": "fc-test-secret"}
