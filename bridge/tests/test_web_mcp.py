from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

from telegram_bot.core import web_mcp
from telegram_bot.core.web_mcp import (
    FIRECRAWL_DEVELOPER_SEARCH_TOOL,
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


def test_curated_web_mcp_builds_search_scrape_and_developer_index(tmp_path: Path) -> None:
    options = build_curated_web_mcp(_settings(tmp_path))
    assert options is not None
    assert options["allowed_tools"] == [
        FIRECRAWL_SEARCH_TOOL,
        SEARXNG_SEARCH_TOOL,
        FIRECRAWL_SCRAPE_TOOL,
        FIRECRAWL_DEVELOPER_SEARCH_TOOL,
    ]
    assert options["disallowed_tools"] == [
        "WebSearch",
        "WebFetch",
        SEARXNG_FETCH_TOOL,
    ]
    assert FIRECRAWL_SEARCH_TOOL in options["system_prompt"]
    assert "silently" in options["system_prompt"]
    assert options["mcp_servers"]["searxng"]["env"] == {
        "SEARXNG_URL": "https://search.example.com"
    }
    assert "env" not in options["mcp_servers"]["firecrawl"]
    assert options["process_env"] == {"FIRECRAWL_API_KEY": "fc-test-secret"}


def test_firecrawl_mode_builds_search_scrape_and_developer_without_searxng(
    tmp_path: Path,
) -> None:
    options = build_curated_web_mcp(_settings(tmp_path, "firecrawl"))
    assert options is not None
    assert options["allowed_tools"] == [
        FIRECRAWL_SEARCH_TOOL,
        FIRECRAWL_SCRAPE_TOOL,
        FIRECRAWL_DEVELOPER_SEARCH_TOOL,
    ]
    assert options["disallowed_tools"] == [
        "WebSearch",
        "WebFetch",
        SEARXNG_SEARCH_TOOL,
        SEARXNG_FETCH_TOOL,
    ]
    assert set(options["mcp_servers"]) == {"firecrawl"}
    assert options["process_env"] == {"FIRECRAWL_API_KEY": "fc-test-secret"}
    assert options["system_prompt"] == web_mcp.FIRECRAWL_ROUTING_PROMPT


def test_firecrawl_mode_requires_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "firecrawl")
    settings.bridge_firecrawl_api_key = None
    try:
        build_curated_web_mcp(settings)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for missing firecrawl key")


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


def _isolate_global_roots(monkeypatch, node_prefix: Path | None = None) -> None:
    """Pin every global-root input so the host's real installs never leak in."""
    for var in ("PREFIX", "npm_config_prefix", "NPM_CONFIG_PREFIX"):
        monkeypatch.delenv(var, raising=False)
    root = None if node_prefix is None else node_prefix / "lib" / "node_modules"
    monkeypatch.setattr(web_mcp, "_node_global_root", lambda: root)


def test_termux_launches_servers_via_node(tmp_path: Path, monkeypatch) -> None:
    # #663: on Termux the `#!/usr/bin/env node` bin shebang is unresolved in the
    # agent MCP spawn context, so launch the global install via `node <cli>`.
    prefix = tmp_path / "usr"
    sx_cli = _install_fake_pkg(prefix, "mcp-searxng", "dist/cli.js")
    fc_cli = _install_fake_pkg(prefix, "firecrawl-mcp", "dist/index.js")
    _isolate_global_roots(monkeypatch)
    monkeypatch.setenv("PREFIX", str(prefix))
    opts = build_curated_web_mcp(_settings(tmp_path))
    assert opts is not None
    sx = opts["mcp_servers"]["searxng"]
    assert sx["command"] == "node" and sx["args"] == [str(sx_cli)]
    assert sx["env"] == {"SEARXNG_URL": "https://search.example.com"}
    fc = opts["mcp_servers"]["firecrawl"]
    assert fc["command"] == "node" and fc["args"] == [str(fc_cli)]
    assert "env" not in fc


def test_linux_global_install_launches_via_node_not_npx(
    tmp_path: Path, monkeypatch
) -> None:
    # #2299: `npx -y` keeps a resident `npm exec` wrapper per server per
    # session (~80 MiB each); a global install under the node prefix is
    # launched directly instead.
    node_prefix = tmp_path / "usr"
    sx_cli = _install_fake_pkg(node_prefix, "mcp-searxng", "dist/cli.js")
    fc_cli = _install_fake_pkg(node_prefix, "firecrawl-mcp", "dist/index.js")
    _isolate_global_roots(monkeypatch, node_prefix)
    opts = build_curated_web_mcp(_settings(tmp_path))
    assert opts is not None
    assert opts["mcp_servers"]["searxng"] == {
        "type": "stdio",
        "command": "node",
        "args": [str(sx_cli)],
        "env": {"SEARXNG_URL": "https://search.example.com"},
    }
    assert opts["mcp_servers"]["firecrawl"] == {
        "type": "stdio",
        "command": "node",
        "args": [str(fc_cli)],
    }


def test_npm_config_prefix_is_honoured(tmp_path: Path, monkeypatch) -> None:
    prefix = tmp_path / "npm-global"
    fc_cli = _install_fake_pkg(prefix, "firecrawl-mcp", "dist/index.js")
    _isolate_global_roots(monkeypatch)
    monkeypatch.setenv("npm_config_prefix", str(prefix))
    opts = build_curated_web_mcp(_settings(tmp_path, mode="firecrawl"))
    assert opts is not None
    assert opts["mcp_servers"]["firecrawl"]["args"] == [str(fc_cli)]


def test_partial_global_install_mixes_node_and_npx(tmp_path: Path, monkeypatch) -> None:
    node_prefix = tmp_path / "usr"
    _install_fake_pkg(node_prefix, "mcp-searxng", "dist/cli.js")
    _isolate_global_roots(monkeypatch, node_prefix)
    opts = build_curated_web_mcp(_settings(tmp_path))
    assert opts is not None
    assert opts["mcp_servers"]["searxng"]["command"] == "node"
    assert opts["mcp_servers"]["firecrawl"]["command"] == "npx"
    assert opts["mcp_servers"]["firecrawl"]["args"] == ["-y", "firecrawl-mcp"]


def test_without_global_install_falls_back_to_npx(tmp_path: Path, monkeypatch) -> None:
    _isolate_global_roots(monkeypatch, tmp_path / "empty-usr")  # no node_modules
    monkeypatch.setenv("PREFIX", str(tmp_path / "empty-termux-usr"))
    opts = build_curated_web_mcp(_settings(tmp_path))
    assert opts is not None
    assert opts["mcp_servers"]["searxng"]["command"] == "npx"
    assert opts["mcp_servers"]["searxng"]["args"] == ["-y", "mcp-searxng"]
    assert opts["mcp_servers"]["firecrawl"]["command"] == "npx"


def test_unreadable_package_json_falls_back_to_npx(tmp_path: Path, monkeypatch) -> None:
    node_prefix = tmp_path / "usr"
    pkg = node_prefix / "lib" / "node_modules" / "firecrawl-mcp"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text("not json", encoding="utf-8")
    _isolate_global_roots(monkeypatch, node_prefix)
    opts = build_curated_web_mcp(_settings(tmp_path, mode="firecrawl"))
    assert opts is not None
    assert opts["mcp_servers"]["firecrawl"]["command"] == "npx"


def test_node_global_root_follows_node_symlink(tmp_path: Path, monkeypatch) -> None:
    real_bin = tmp_path / "opt" / "node-v22" / "bin"
    real_bin.mkdir(parents=True)
    (real_bin / "node").write_text("", encoding="utf-8")
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    (link_dir / "node").symlink_to(real_bin / "node")
    monkeypatch.setattr(web_mcp.shutil, "which", lambda name: str(link_dir / "node"))
    assert web_mcp._node_global_root() == (
        tmp_path / "opt" / "node-v22" / "lib" / "node_modules"
    )
    monkeypatch.setattr(web_mcp.shutil, "which", lambda name: None)
    assert web_mcp._node_global_root() is None
