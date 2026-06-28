---
name: mcp-add
description: Register a Claude Code MCP tool server at user scope (node-global), reading any API key from ~/.hermes/.env so the secret never appears in a command, transcript, or commit. Use when adding web search, fetch/scrape, docs, or other MCP tools to this node. Idempotent; also pre-allows the tool in settings.json.
---

# mcp-add — register an MCP tool server (secret-safe)

Adds an MCP server so its tools (`mcp__<name>__*`) are available every session. Keys live node-local in `~/.hermes/.env` and `~/.claude.json` (perm 600); never hardcode a key in a command or commit.

## Procedure

1. **Identify** the package (npm: `npx -y <pkg>`) and any required env. Non-secret config (e.g. a shared URL) can be inline; secrets come from `~/.hermes/.env`.

2. **Register at user scope.** For a keyless server:
   ```bash
   claude mcp add <name> -s user -- npx -y <pkg>
   ```
   For a server needing a key — read it into a shell var so it is never printed:
   ```bash
   KEY=$(grep -E '^<ENV_NAME>=' $HOME/.hermes/.env | head -1 | cut -d= -f2- | tr -d '"'\'' ')
   claude mcp add <name> -s user -e <ENV_NAME>="$KEY" -- npx -y <pkg>
   unset KEY
   ```

3. **Pre-allow the tool** so it never prompts — add `mcp__<name>__*` to `permissions.allow` in `~/.claude/settings.json` (Read then Edit; keep existing entries).

4. **Protect the secret store**
   ```bash
   chmod 600 $HOME/.claude.json
   ```

5. **Verify**
   ```bash
   claude mcp list      # expect: <name> ... ✔ Connected
   ```
   New MCP tools become available as `Agent`/tool calls **from the next session** (a server added mid-session shows Connected in health but its tools load next session).

## Standard Seoyoon set
The canonical node set (searxng / firecrawl / context7) is registered idempotently by
`/opt/ccc-node/claude/mcp-setup.sh` — prefer running that for the standard tools; use this
skill for one-off or new servers.

## Safety
- Never echo or commit a key value; read it from `~/.hermes/.env` at use time.
- For templates/repos, reference the env name only (placeholder), never the value.
