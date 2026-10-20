- **Self-update now refreshes the launch of registered web MCP servers
  (#2315).** #2300 moved `searxng`, `context7` and `firecrawl` from `npx -y`
  to the global install's `node <cli>`, but `setup.sh` only ran
  `claude/mcp-setup.sh --family-only`, so nodes that had registered them
  earlier kept a resident `npm exec` wrapper per server per session.
  `setup.sh` now also runs the new `mcp-setup.sh --web-launch-only` from the
  managed checkout. It re-registers only entries that are already at user
  scope, use a launch the script produced, and differ from the current global
  install. The entry's env, including the Firecrawl key, is kept verbatim and
  never printed. An identical entry is not rewritten, and a custom launch or an
  unregistered server is left alone. Nothing is installed: a server without a
  global install is skipped with a log line. A failed re-add restores the
  previous launch. Failure is a warning; `CCC_SETUP_WEB_MCP_LAUNCH=0` opts
  out.
