- **`mcp-setup.sh --browser-only` (#2044).** Repointing `fleet-browser`
  (`CCC_BROWSER_MCP_HOST=<ssh destination>|off ./claude/mcp-setup.sh --browser-only`)
  now touches that one user-scope server only: no family/searxng/context7/
  firecrawl registration and no `claude mcp list` health check. A full run
  still registers everything, which on nodes that get those servers from the
  bridge bundle added them to `~/.claude.json` — Firecrawl key included.
  Missing host exits 2 without changes; `--family-only` and `--browser-only`
  are mutually exclusive. 12 new cases in `claude/mcp-setup.test.sh`.
