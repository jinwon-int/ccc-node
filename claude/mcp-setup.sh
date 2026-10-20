#!/usr/bin/env bash
# Register node-global MCP tool servers (user scope) for a Claude Code node.
# Idempotent: each server is removed (if present) then re-added, so re-running is safe.
# Secret-free in this repo: the Firecrawl API key is read at runtime from ~/.hermes/.env
# (node-local, gitignored) — never hardcoded here.
#
# Requires: `claude` CLI on PATH, node/npm (npx only as the Linux fallback). SearXNG also
# requires Tailscale up.
#
# Termux/Android: the package bins start with `#!/usr/bin/env node`, and the
# agent's MCP spawn context does not carry termux-exec (Claude Code subprocess;
# Codex `rmcp` `env_clear()`), so `/usr/bin/env` is unresolved and
# `npx -y <pkg>` fails with "<bin>: not found". There we install the package
# globally and launch it via `node <abs cli>`, which bypasses the shebang
# (#663). Linux does the same (#2299): `npx -y <pkg>` leaves a resident
# `npm exec` wrapper (~80 MiB) per server per session, so `npx -y` is only the
# fallback when the global install is unavailable. The bridge's curated web MCP
# (bridge/core/web_mcp.py) launches from the same global install when present.
# Global installs are not upgraded by re-running this script; refresh them
# with `npm install -g <pkg>@latest`.
#
# Usage: ./claude/mcp-setup.sh   (or from anywhere: bash mcp-setup.sh)
#        ./claude/mcp-setup.sh --family-only
#          Register only the in-repo, stdlib-only, secret-free family-skills and
#          family-ops servers (no npx, network or keys). setup.sh runs this mode
#          from the managed checkout (#2011 D); an identical registration is
#          left untouched so re-runs do not rewrite ~/.claude.json.
#        CCC_BROWSER_MCP_HOST=<ssh destination>|off ./claude/mcp-setup.sh --browser-only
#          Register (or with =off remove) only the fleet-browser server. Every
#          other user-scope server is left exactly as it is and `claude mcp list`
#          is not run. Use this to repoint fleet-browser on a node that gets its
#          other servers another way (e.g. the bridge bundle) — a full run would
#          add them to ~/.claude.json, Firecrawl key included (#2044).
#        CCC_REA_MCP=on|off ./claude/mcp-setup.sh --rea-only
#          Register (on) or remove (off) only the opt-in REA reverse-engineering
#          server. Every other user-scope server is left exactly as it is and
#          `claude mcp list` is not run — same repoint-without-churn use as
#          --browser-only (a full run would add the others, Firecrawl key
#          included). REA ships 170 tools, so it is never registered by default.
#        ./claude/mcp-setup.sh --web-launch-only
#          Refresh only the launch command of web MCP servers (searxng,
#          context7, firecrawl) that are ALREADY registered at user scope
#          (#2315). An entry is re-registered as `node <abs cli>` only when its
#          launch is a form this script produced (`npx -y <pkg>`, the package
#          bin, or `node <old cli>`) and differs from the current global
#          install; the entry's env (SearXNG URL, Firecrawl key) is kept
#          exactly and never printed. Unregistered servers are not added,
#          custom launches are left alone, nothing is installed (a server
#          whose package is not installed globally is skipped) and `claude mcp
#          list` is not run. setup.sh runs this mode from the managed checkout.
set -uo pipefail

FAMILY_ONLY=0
BROWSER_ONLY=0
REA_ONLY=0
WEB_LAUNCH_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --family-only) FAMILY_ONLY=1 ;;
    --browser-only) BROWSER_ONLY=1 ;;
    --rea-only) REA_ONLY=1 ;;
    --web-launch-only) WEB_LAUNCH_ONLY=1 ;;
    -h|--help) sed -n '2,/^set -uo/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "mcp-setup.sh: unknown argument: $arg" >&2; exit 2 ;;
  esac
done
if [ $((FAMILY_ONLY + BROWSER_ONLY + REA_ONLY + WEB_LAUNCH_ONLY)) -gt 1 ]; then
  echo "mcp-setup.sh: --family-only, --browser-only, --rea-only and --web-launch-only are mutually exclusive" >&2
  exit 2
fi
if [ "$BROWSER_ONLY" = 1 ] && [ -z "${CCC_BROWSER_MCP_HOST:-}" ]; then
  echo "mcp-setup.sh: --browser-only needs CCC_BROWSER_MCP_HOST=<ssh destination> (or =off to remove); nothing changed" >&2
  exit 2
fi
if [ "$REA_ONLY" = 1 ] && [ -z "${CCC_REA_MCP:-}" ]; then
  echo "mcp-setup.sh: --rea-only needs CCC_REA_MCP=on (or =off to remove); nothing changed" >&2
  exit 2
fi

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
# Every platform: global-install + `node <cli>`. Linux falls back to
# `npx -y <pkg>` when the global install cannot be made or resolved (e.g. no
# write access to the npm prefix); Termux has no such fallback (#663).
add_stdio() {
  local name="$1" pkg="$2"; shift 2
  npm ls -g "$pkg" >/dev/null 2>&1 || npm install -g "$pkg" >/dev/null 2>&1 || true
  local cli
  cli="$(global_cli "$pkg")"
  if [ -n "$cli" ] && [ -f "$cli" ]; then
    add "$name" "$@" -- node "$cli"
  elif [ "$IS_TERMUX" = 1 ]; then
    echo "  ! $name: SKIPPED — could not install/resolve $pkg globally"
    return 1
  else
    echo "  ! $name: global install of $pkg unavailable — falling back to npx -y (keeps a resident npm exec wrapper, #2299)"
    add "$name" "$@" -- npx -y "$pkg"
  fi
}

# fleet-browser — opt-in windowed-browser MCP on the fleet browser pilot node
# (#2034; jinwon-int/fleet-mcp). Stdio over ssh, no secrets on this side.
# CCC_BROWSER_MCP_HOST=<ssh destination> registers, =off removes, unset leaves
# any existing registration untouched. Not pre-allowed in settings: under the
# governed (non-bypass) path each call asks the owner (fleet-mcp decision E3).
# Returns 1 only when the host/command is rejected or registration fails.
register_browser() {
  local host="${CCC_BROWSER_MCP_HOST:-}"
  local cmd="${CCC_BROWSER_MCP_COMMAND:-/opt/fleet-mcp/current/deploy/bin/browser-mcp}"
  if [ -z "$host" ]; then
    echo "  - fleet-browser: SKIPPED (set CCC_BROWSER_MCP_HOST to opt in)"
  elif [ "$host" = off ]; then
    claude mcp remove fleet-browser -s user >/dev/null 2>&1 || true
    echo "  - fleet-browser: removed (CCC_BROWSER_MCP_HOST=off)"
  elif [ "${CCC_NODE_ISOLATION_PROFILE:-fleet}" = "external" ]; then
    echo "  - fleet-browser: SKIPPED (external isolation)"
  elif ! printf '%s' "$host" | grep -Eq '^([A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9._-]*$' \
    || ! printf '%s' "$cmd" | grep -Eq '^/[A-Za-z0-9._/-]+$'; then
    echo "  ! fleet-browser: SKIPPED — CCC_BROWSER_MCP_HOST/COMMAND must be a plain ssh destination and absolute path" >&2
    return 1
  else
    add_if_changed fleet-browser ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -- "$host" "$cmd" || return 1
    echo "  - fleet-browser: ssh $host $cmd"
  fi
}

# rea — opt-in reverse-engineering MCP server (REA "Reverse Engineer Anything",
# MIT; morluto/rea, repackaged from the NousResearch Hermes plugin). Inspects
# native binaries, .NET, Electron/JavaScript apps, Android packages, firmware
# and captures. It ships ~170 tools, so it is NEVER registered by default
# (context cost) — CCC_REA_MCP=on opts a node in, =off removes it, unset leaves
# any existing registration untouched. Global-install + `node <cli> mcp`, the
# Termux/Linux launch rule from #663 (the package bin is /usr/bin/env node).
# No env override is set here: REA's native-decompiler timeout only matters on
# nodes that supply their own Ghidra/Hopper/IDA, and setting an (otherwise
# inert) env would defeat add_if_changed's no-churn re-run. Such nodes export
# REA_GHIDRA_STARTUP_TIMEOUT_MS for their own engine. Not pre-allowed in
# settings.json: like fleet-browser, the governed (non-bypass) path asks the
# owner before each call, which runs/reads local binaries. On arm64 the
# lightweight ELF layout provider is x64-only and decompilation needs an engine,
# so there REA is useful mainly for JS/Electron/ASAR/archive static analysis and
# binary identity (format/arch/sha256).
REA_PKG="rea-agents@6.4.0"
register_rea() {
  local mode="${CCC_REA_MCP:-}"
  if [ -z "$mode" ]; then
    echo "  - rea: SKIPPED (set CCC_REA_MCP=on to opt in)"
    return 0
  elif [ "$mode" = off ]; then
    claude mcp remove rea -s user >/dev/null 2>&1 || true
    echo "  - rea: removed (CCC_REA_MCP=off)"
    return 0
  elif [ "$mode" != on ]; then
    echo "  ! rea: SKIPPED — CCC_REA_MCP must be 'on' or 'off'" >&2
    return 1
  elif [ "${CCC_NODE_ISOLATION_PROFILE:-fleet}" = "external" ]; then
    echo "  - rea: SKIPPED (external isolation)"
    return 0
  fi
  local pkgname="${REA_PKG%@*}" cli
  npm ls -g "$pkgname" >/dev/null 2>&1 || npm install -g "$REA_PKG" >/dev/null 2>&1 || true
  cli="$(global_cli "$pkgname")"
  if [ -n "$cli" ] && [ -f "$cli" ]; then
    add_if_changed rea node "$cli" mcp
    echo "  - rea: node $cli mcp"
  else
    echo "  ! rea: SKIPPED — could not install/resolve $REA_PKG globally"
    return 1
  fi
}

# web_launch_state <name> <npm-pkg> <bin-name> <cli-or-empty>
# Read-only classification of the user-scope entry for a web MCP server,
# written to stdout as NUL-terminated fields for the caller's array (never to
# the terminal): the state, then for `stale` the old command, the old arg count,
# the old args and the entry's env as KEY=VALUE pairs. States:
#   absent     not registered (or no Claude user config)
#   custom     a launch this script never produced, or fields `claude mcp add`
#              cannot reproduce — left alone (operator choice)
#   noinstall  a known launch, but no global install resolves
#   current    already `node <cli>`
#   stale      a known launch that differs from `node <cli>`
#   unreadable the config cannot be parsed
web_launch_state() {
  local cfg="${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json"
  [ -f "$cfg" ] || { printf 'absent\0'; return 0; }
  python3 - "$cfg" "$@" 2>/dev/null <<'PY' || printf 'unreadable\0'
import json, os, sys
cfg, name, pkg, binname, cli = sys.argv[1:]
def out(*fields):
    sys.stdout.write("".join(f + "\0" for f in fields))
    sys.exit(0)
try:
    with open(cfg, encoding="utf-8") as fh:
        servers = json.load(fh).get("mcpServers") or {}
    if not isinstance(servers, dict):
        raise ValueError("mcpServers")
except Exception:
    out("unreadable")
entry = servers.get(name)
if entry is None:
    out("absent")
if not isinstance(entry, dict) or set(entry) - {"type", "command", "args", "env"} \
        or entry.get("type", "stdio") != "stdio":
    out("custom")
command, args, env = entry.get("command"), entry.get("args", []), entry.get("env") or {}
if not isinstance(command, str) or not isinstance(args, list) or not isinstance(env, dict) \
        or not all(isinstance(a, str) and "\0" not in a for a in args + [command]) \
        or not all(isinstance(k, str) and isinstance(v, str) and k and "=" not in k
                   and "\0" not in k + v for k, v in env.items()):
    out("custom")
base = os.path.basename(command)
if base == "npx":
    rest = [a for a in args if a not in ("-y", "--yes")]
    known = len(rest) == 1 and (rest[0] == pkg or rest[0].startswith(pkg + "@"))
elif base == binname:
    known = args == []
elif base == "node":
    known = len(args) == 1 and ("/" + pkg + "/") in args[0]
else:
    known = False
if not known:
    out("custom")
if not cli:
    out("noinstall")
if command == "node" and args == [cli]:
    out("current")
out("stale", command, str(len(args)), *args, *(f"{k}={v}" for k, v in env.items()))
PY
}

# web_relaunch <name> <npm-pkg> <bin-name>: --web-launch-only for one server.
# Remove + re-add keeps the entry's env verbatim; `claude` output is discarded
# because it would echo the arguments, the Firecrawl key included. If the
# re-add fails, the previous launch is restored. Returns 1 only on failure.
web_relaunch() {
  local name="$1" pkg="$2" binname="$3" cli f state old_cmd nargs
  local fields=() old_args=() eflags=()
  cli="$(global_cli "$pkg")" || cli=""
  { [ -n "$cli" ] && [ -f "$cli" ]; } || cli=""
  while IFS= read -r -d '' f; do fields+=("$f"); done \
    < <(web_launch_state "$name" "$pkg" "$binname" "$cli")
  state="${fields[0]:-unreadable}"
  case "$state" in
    absent) echo "  - $name: not registered — left unregistered (a full run opts in)"; return 0 ;;
    custom) echo "  - $name: custom launch — left untouched"; return 0 ;;
    noinstall) echo "  - $name: SKIPPED — $pkg is not installed globally (npm install -g $pkg, then re-run)"; return 0 ;;
    current) echo "  - $name: already registered (unchanged)"; return 0 ;;
    stale) ;;
    *) echo "  - $name: Claude user config unreadable — left untouched"; return 0 ;;
  esac
  old_cmd="${fields[1]}"; nargs="${fields[2]}"
  old_args=("${fields[@]:3:$nargs}")
  for f in "${fields[@]:$((3 + nargs))}"; do eflags+=(-e "$f"); done
  claude mcp remove "$name" -s user >/dev/null 2>&1 || true
  if claude mcp add "$name" -s user ${eflags[@]+"${eflags[@]}"} -- node "$cli" >/dev/null 2>&1; then
    echo "  - $name: re-registered as node $cli (was $(basename "$old_cmd"); env kept, $(( ${#eflags[@]} / 2 )) var(s))"
    return 0
  fi
  if claude mcp add "$name" -s user ${eflags[@]+"${eflags[@]}"} -- "$old_cmd" ${old_args[@]+"${old_args[@]}"} >/dev/null 2>&1; then
    echo "  ! $name: re-registration failed — previous launch restored" >&2
  else
    echo "  ! $name: re-registration failed and the previous launch could not be restored — re-run ./claude/mcp-setup.sh" >&2
  fi
  return 1
}

if [ "$WEB_LAUNCH_ONLY" = 1 ]; then
  # No `claude mcp list` (it health-checks networked servers), no npm install,
  # no ~/.hermes/.env read: the existing registration's env is the source.
  echo "==> Refreshing web MCP launch commands (user scope, registered servers only)…"
  rc=0
  web_relaunch searxng mcp-searxng mcp-searxng || rc=1
  web_relaunch context7 @upstash/context7-mcp context7-mcp || rc=1
  web_relaunch firecrawl firecrawl-mcp firecrawl-mcp || rc=1
  echo "==> Done (web-launch-only: family-skills, family-ops, family-wiki, fleet-browser, rea untouched; unregistered servers not added)."
  exit "$rc"
fi

if [ "$REA_ONLY" = 1 ]; then
  # No `claude mcp list`: it health-checks every server, including networked ones.
  echo "==> Registering rea only (user scope)…"
  register_rea; rc=$?
  echo "==> Done (rea-only: family-skills, family-ops, family-wiki, searxng, context7, firecrawl, fleet-browser untouched)."
  exit "$rc"
fi

if [ "$BROWSER_ONLY" = 1 ]; then
  # No `claude mcp list`: it health-checks every server, including networked ones.
  echo "==> Registering fleet-browser only (user scope)…"
  register_browser; rc=$?
  echo "==> Done (browser-only: family-skills, family-ops, family-wiki, searxng, context7, firecrawl untouched)."
  exit "$rc"
fi

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

# fleet-browser (see register_browser above). A full run keeps its old
# behaviour: a rejected host is reported but does not fail the whole run.
register_browser || true

# rea (see register_rea above). Opt-in via CCC_REA_MCP; a full run reports its
# skip/add but never fails the whole run on a bad value or install miss.
register_rea || true

echo "==> Done. Verifying:"
claude mcp list
echo
echo "Note: tool permissions for these servers (mcp__searxng__*, mcp__firecrawl__*,"
echo "      mcp__context7__*, mcp__family-skills__*, mcp__family-wiki__*,"
echo "      mcp__family-ops__*) are pre-allowed in claude/settings.json."
