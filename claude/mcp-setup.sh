#!/usr/bin/env bash
# Register node-global MCP tool servers (user scope) for a Claude Code node.
# Idempotent: each server is removed (if present) then re-added, so re-running is safe.
# Secret-free in this repo: the Firecrawl API key is read at runtime from ~/.hermes/.env
# (node-local, gitignored) — never hardcoded here.
#
# Requires: `claude` CLI on PATH, node/npx. SearXNG also requires Tailscale up.
# Usage: ./claude/mcp-setup.sh   (or from anywhere: bash mcp-setup.sh)
set -uo pipefail

add() { # add <name> [claude-mcp-add args...]
  local name="$1"; shift
  claude mcp remove "$name" -s user >/dev/null 2>&1 || true
  claude mcp add "$name" -s user "$@"
}

echo "==> Registering MCP servers (user scope)…"

# SearXNG — Seoyoon shared web search (Tailnet-only endpoint; needs `tailscale` up).
# URL is shared infra, not a secret. Override SEARXNG_URL env before running to repoint.
SEARXNG_URL="${SEARXNG_URL:-https://vps4.tail1546e7.ts.net:18443}"
add searxng -e "SEARXNG_URL=$SEARXNG_URL" -- npx -y mcp-searxng
echo "  - searxng: $SEARXNG_URL"

# Context7 — library/SDK docs injection. Works keyless (rate-limited).
add context7 -- npx -y @upstash/context7-mcp
echo "  - context7: keyless"

# Firecrawl — web scrape/fetch. Key is node-local (~/.hermes/.env), never committed.
FCKEY="$(grep -E '^FIRECRAWL_API_KEY=' "$HOME/.hermes/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"' ')"
if [ -n "$FCKEY" ]; then
  add firecrawl -e "FIRECRAWL_API_KEY=$FCKEY" -- npx -y firecrawl-mcp
  echo "  - firecrawl: registered (key from ~/.hermes/.env)"
else
  echo "  - firecrawl: SKIPPED — set FIRECRAWL_API_KEY in ~/.hermes/.env, then re-run."
fi
unset FCKEY

echo "==> Done. Verifying:"
claude mcp list
echo
echo "Note: tool permissions for these servers (mcp__searxng__*, mcp__firecrawl__*,"
echo "      mcp__context7__*) are pre-allowed in claude/settings.json."
