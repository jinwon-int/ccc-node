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
#        ./claude/mcp-setup.sh --family-only
#          Register only the in-repo, stdlib-only, secret-free family-skills and
#          family-ops servers (no npx, network or keys). setup.sh runs this mode
#          from the managed checkout (#2011 D); an identical registration is
#          left untouched so re-runs do not rewrite ~/.claude.json.
set -uo pipefail

FAMILY_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --family-only) FAMILY_ONLY=1 ;;
    -h|--help) sed -n '2,/^set -uo/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "mcp-setup.sh: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

IS_TERMUX=0
case "${PREFIX:-}" in */com.termux/files/usr) IS_TERMUX=1 ;; esac
[ -n "${TERMUX_VERSION:-}" ] && IS_TERMUX=1
[ "$(uname -o 2>/dev/null)" = "Android" ] && IS_TERMUX=1

add() { # add <name> [claude-mcp-add args...]
  local name="$1"; shift
  claude mcp remove "$name" -s user >/dev/null 2>&1 || true
  claude mcp add "$name" -s user "$@"
}

# True when the user-scope registration of <name> already launches exactly
# <command> <args...> with no env overrides. Read-only JSON inspection of the
# Claude user config (never printed), so an unchanged server is not removed and
# re-added on every run. Any doubt (missing/unreadable config) -> not registered.
registered_as() { # registered_as <name> <command> [args...]
  local cfg="${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json"
  [ -f "$cfg" ] || return 1
  python3 - "$cfg" "$@" <<'PY' 2>/dev/null
import json, sys
cfg, name, command, *args = sys.argv[1:]
try:
    with open(cfg, encoding="utf-8") as fh:
        entry = json.load(fh).get("mcpServers", {}).get(name)
except Exception:
    sys.exit(1)
ok = (
    isinstance(entry, dict)
    and entry.get("type", "stdio") == "stdio"
    and entry.get("command") == command
    and entry.get("args", []) == args
    and not entry.get("env")
)
sys.exit(0 if ok else 1)
PY
}

# add_if_changed <name> <command> [args...]: idempotent without churn.
add_if_changed() {
  local name="$1"; shift
  if registered_as "$name" "$@"; then
    echo "  - $name: already registered (unchanged)"
    return 0
  fi
  add "$name" -- "$@"
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

# family-skills — local skill search/read MCP (#1678). stdlib-only server from
# this checkout; launches with the absolute python3 path so no /usr/bin/env
# shebang resolution is needed on any platform (Termux rule from #663). The
# server re-checks the node isolation/audience policy at every tools/call.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY3="$(command -v python3)"
FAMILY_FAIL=0
if [ -n "$PY3" ] && [ -f "$REPO_ROOT/bridge/core/family_skills_server.py" ] && [ -f "$REPO_ROOT/bridge/core/family_ops_server.py" ]; then
  add_if_changed family-skills "$PY3" "$REPO_ROOT/bridge/core/family_skills_server.py" || FAMILY_FAIL=1
  echo "  - family-skills: $REPO_ROOT/bridge/core/family_skills_server.py"
  add_if_changed family-ops "$PY3" "$REPO_ROOT/bridge/core/family_ops_server.py" || FAMILY_FAIL=1
  echo "  - family-ops: $REPO_ROOT/bridge/core/family_ops_server.py"
else
  echo "  - family-skills/family-ops: SKIPPED — python3 or server file missing"
  FAMILY_FAIL=1
fi

if [ "$FAMILY_ONLY" = 1 ]; then
  # No `claude mcp list`: it health-checks every server, including networked ones.
  # Exit status reports the family registrations so setup.sh can warn.
  echo "==> Done (family-only: family-wiki, searxng, context7, firecrawl untouched)."
  exit "$FAMILY_FAIL"
fi

# family-wiki — the existing wiki-agent read-only server, reused verbatim
# (wiki_find / wiki_load / wiki_prefetch); never reimplemented here. Skipped
# on externally isolated nodes or where wiki memory is disabled — the same
# policy the bridge uses before injecting it (#1678).
if [ "${CCC_NODE_ISOLATION_PROFILE:-fleet}" = "external" ] || [ "${CCC_WIKI_MEMORY_ENABLED:-1}" = "0" ]; then
  echo "  - family-wiki: SKIPPED (external isolation or wiki memory disabled)"
elif command -v wiki-agent >/dev/null 2>&1; then
  add family-wiki -- wiki-agent mcp-serve
  echo "  - family-wiki: wiki-agent mcp-serve"
else
  echo "  - family-wiki: SKIPPED — wiki-agent not on PATH"
fi

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
echo "      mcp__context7__*, mcp__family-skills__*, mcp__family-wiki__*,"
echo "      mcp__family-ops__*) are pre-allowed in claude/settings.json."
