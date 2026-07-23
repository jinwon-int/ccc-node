#!/usr/bin/env bash
# Register node-global MCP tool servers (user scope) for a Claude Code node.
# Idempotent: each server is removed (if present) then re-added, so re-running is safe.
# Secret-free in this repo: the Firecrawl API key is read at runtime from ~/.hermes/.env
# (node-local, gitignored) — never hardcoded here.
#
# Requires: `claude` CLI on PATH, node/npm (npx on non-Termux). SearXNG also
# requires Tailscale up.
#
# Termux/Android: the package bins start with `#!/usr/bin/env node`, and the
# agent's MCP spawn context does not carry termux-exec (Claude Code subprocess;
# Codex `rmcp` `env_clear()`), so `/usr/bin/env` is unresolved and
# `npx -y <pkg>` fails with "<bin>: not found". There we install the package
# globally and launch it via `node <abs cli>`, which bypasses the shebang.
# Linux keeps `npx -y <pkg>`. (#663)
#
# Usage: ./claude/mcp-setup.sh   (or from anywhere: bash mcp-setup.sh)
set -uo pipefail

IS_TERMUX=0
case "${PREFIX:-}" in */com.termux/files/usr) IS_TERMUX=1 ;; esac
[ -n "${TERMUX_VERSION:-}" ] && IS_TERMUX=1
[ "$(uname -o 2>/dev/null)" = "Android" ] && IS_TERMUX=1

add() { # add <name> [claude-mcp-add args...]
  local name="$1"; shift
  claude mcp remove "$name" -s user >/dev/null 2>&1 || true
  claude mcp add "$name" -s user "$@"
}

# Absolute path to a globally-installed package's first bin entry. Reads `bin`
# from the package's own package.json, so it does not depend on the executable
# name and works for scoped packages.
global_cli() { # global_cli <npm-pkg>
  local pkg="$1" groot
  groot="$(npm root -g 2>/dev/null)" || return 1
  [ -n "$groot" ] || return 1
  node -e '
    const p = require("path"), fs = require("fs");
    const dir = p.join(process.argv[1], process.argv[2]);
    const j = JSON.parse(fs.readFileSync(p.join(dir, "package.json"), "utf8"));
    const b = j.bin;
    const rel = typeof b === "string" ? b : b[Object.keys(b)[0]];
    process.stdout.write(p.resolve(dir, rel));
  ' "$groot" "$pkg" 2>/dev/null
}

# add_stdio <name> <npm-pkg> [extra claude-mcp-add flags...]
# Termux: global-install + `node <cli>`; elsewhere: `npx -y <pkg>`.
add_stdio() {
  local name="$1" pkg="$2"; shift 2
  if [ "$IS_TERMUX" = 1 ]; then
    npm ls -g "$pkg" >/dev/null 2>&1 || npm install -g "$pkg" >/dev/null 2>&1 || true
    local cli
    cli="$(global_cli "$pkg")"
    if [ -z "$cli" ] || [ ! -f "$cli" ]; then
      echo "  ! $name: SKIPPED — could not install/resolve $pkg globally"
      return 1
    fi
    add "$name" "$@" -- node "$cli"
  else
    add "$name" "$@" -- npx -y "$pkg"
  fi
}

echo "==> Registering MCP servers (user scope)…"
[ "$IS_TERMUX" = 1 ] && echo "  (Termux/Android detected: launching via 'node <cli>' — see #663)"

# SearXNG — Seoyoon shared web search (Tailnet-only endpoint; needs `tailscale` up).
# URL is shared infra, not a secret. Override SEARXNG_URL env before running to repoint.
SEARXNG_URL="${SEARXNG_URL:-https://vps4.tail1546e7.ts.net:18443}"
add_stdio searxng mcp-searxng -e "SEARXNG_URL=$SEARXNG_URL"
echo "  - searxng: $SEARXNG_URL"

# Context7 — library/SDK docs injection. Works keyless (rate-limited).
add_stdio context7 @upstash/context7-mcp
echo "  - context7: keyless"

# Firecrawl — web scrape/fetch. Key is node-local (~/.hermes/.env), never committed.
FCKEY="$(grep -E '^FIRECRAWL_API_KEY=' "$HOME/.hermes/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"' ')"
if [ -n "$FCKEY" ]; then
  add_stdio firecrawl firecrawl-mcp -e "FIRECRAWL_API_KEY=$FCKEY"
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
