- **Opt-in fleet-browser MCP (#2034).** Nodes can reach the fleet browser
  pilot (jinwon-int/fleet-mcp: windowed Chrome + Playwright MCP on soonwook,
  owner-observable over Tailnet noVNC) as a stdio MCP server over ssh.
  `claude/mcp-setup.sh` registers `fleet-browser` when `CCC_BROWSER_MCP_HOST`
  is set (`off` removes it; not pre-allowed, so governed sessions ask per
  call). The bridge injects the same server via `CCC_BRIDGE_BROWSER_MCP_HOST`
  into owner sessions without a host settings chain, never for external
  isolation or shared audiences, with session-material tools disallowed
  (cookies, localStorage, network requests, evaluate, run_code_unsafe,
  file_upload) and account rules in the routing prompt. Default off
  everywhere; no secrets cross to this side.
