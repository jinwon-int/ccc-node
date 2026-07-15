from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import SecretStr

from telegram_bot.core import project_chat
from telegram_bot.core.web_mcp import (
    FIRECRAWL_SCRAPE_TOOL,
    FIRECRAWL_SEARCH_TOOL,
    SEARXNG_FETCH_TOOL,
    SEARXNG_SEARCH_TOOL,
    build_curated_web_mcp,
)


class _FakeSDKClient:
    last_options = None

    def __init__(self, options):
        type(self).last_options = options

    async def connect(self):
        return None


class _CapturedOptions(SimpleNamespace):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)


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


def test_project_chat_injects_curated_web_without_setting_sources(tmp_path: Path) -> None:
    def close_task(coro):
        coro.close()
        return object()

    with (
        patch.object(project_chat.asyncio, "create_task", side_effect=close_task),
        patch.object(project_chat, "ClaudeAgentOptions", _CapturedOptions),
    ):
        handler = project_chat.ProjectChatHandler(
            settings=_settings(tmp_path), sdk_client_factory=_FakeSDKClient
        )
        asyncio.run(handler._create_user_stream(1, None))

    options = _FakeSDKClient.last_options
    assert options.setting_sources == []
    assert set(options.mcp_servers) == {"searxng", "firecrawl"}
    assert options.env["FIRECRAWL_API_KEY"] == "fc-test-secret"
    assert SEARXNG_SEARCH_TOOL in options.allowed_tools
    assert FIRECRAWL_SCRAPE_TOOL in options.allowed_tools
    assert "WebSearch" not in options.allowed_tools
    assert "WebFetch" not in options.allowed_tools
    assert "WebSearch" in options.disallowed_tools
    assert "WebFetch" in options.disallowed_tools
    assert SEARXNG_FETCH_TOOL in options.disallowed_tools
    assert FIRECRAWL_SEARCH_TOOL in options.disallowed_tools
    assert "Curated web routing" in options.system_prompt


def test_serialized_mcp_config_excludes_firecrawl_key(tmp_path: Path) -> None:
    web = build_curated_web_mcp(_settings(tmp_path))
    assert web is not None
    assert "fc-test-secret" not in json.dumps(web["mcp_servers"])
    assert web["process_env"] == {"FIRECRAWL_API_KEY": "fc-test-secret"}
