from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

from telegram_bot.core import web_mcp
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


def _install_fake_pkg(prefix: Path, pkg: str, rel: str) -> Path:
    d = prefix / "lib" / "node_modules" / pkg
    (d / Path(rel).parent).mkdir(parents=True, exist_ok=True)
    (d / rel).write_text("#!/usr/bin/env node\nprocess.exit(0);\n", encoding="utf-8")
    (d / "package.json").write_text(
        json.dumps({"name": pkg, "bin": {pkg.split("/")[-1]: rel}}), encoding="utf-8"
    )
    return d / rel


def test_termux_launches_servers_via_node(tmp_path: Path, monkeypatch) -> None:
    # #663: on Termux the `#!/usr/bin/env node` bin shebang is unresolved in the
    # agent MCP spawn context, so launch the global install via `node <cli>`.
    prefix = tmp_path / "usr"
    sx_cli = _install_fake_pkg(prefix, "mcp-searxng", "dist/cli.js")
    fc_cli = _install_fake_pkg(prefix, "firecrawl-mcp", "dist/index.js")
    monkeypatch.setattr(web_mcp, "_is_termux", lambda: True)
    monkeypatch.setenv("PREFIX", str(prefix))
    opts = build_curated_web_mcp(_settings(tmp_path))
    assert opts is not None
    sx = opts["mcp_servers"]["searxng"]
    assert sx["command"] == "node" and sx["args"] == [str(sx_cli)]
    assert sx["env"] == {"SEARXNG_URL": "https://search.example.com"}
    fc = opts["mcp_servers"]["firecrawl"]
    assert fc["command"] == "node" and fc["args"] == [str(fc_cli)]
    assert "env" not in fc


def test_non_termux_uses_npx(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web_mcp, "_is_termux", lambda: False)
    opts = build_curated_web_mcp(_settings(tmp_path))
    assert opts is not None
    assert opts["mcp_servers"]["searxng"]["command"] == "npx"
    assert opts["mcp_servers"]["searxng"]["args"] == ["-y", "mcp-searxng"]
    assert opts["mcp_servers"]["firecrawl"]["command"] == "npx"


def test_termux_without_global_install_falls_back_to_npx(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(web_mcp, "_is_termux", lambda: True)
    monkeypatch.setenv("PREFIX", str(tmp_path / "empty-usr"))  # no node_modules
    opts = build_curated_web_mcp(_settings(tmp_path))
    assert opts is not None
    assert opts["mcp_servers"]["searxng"]["command"] == "npx"
