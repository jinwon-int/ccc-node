#!/usr/bin/env bash
# Tests for claude/mcp-setup.sh platform branching (#663): Termux/Android must
# register stdio MCP servers as `node <abs cli>`; other platforms keep
# `npx -y <pkg>`. Uses fake `claude`/`npm`/`uname` on PATH so the assertion runs
# anywhere (including on a Termux host, where real `uname -o` would otherwise
# force Termux detection).
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SUT="$HERE/mcp-setup.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/hooks/lib/test-stub.sh"
# Fixtures supply every CCC_* input this suite needs; ambient harness variables
# from a live node must not reach them (#1023).
ccc_test_reset_hook_env
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FAIL: $1"; fi; }

command -v node >/dev/null 2>&1 || { echo "SKIP: node unavailable"; echo "PASS=0 FAIL=0"; exit 0; }
NODE_DIR="$(dirname "$(command -v node)")"
# Use the real bash path in the fake stubs' shebang: on Termux there is no
# /usr/bin/env, so `#!/usr/bin/env bash` stubs would not execute under `env -i`.
BASH_BIN="$(command -v bash)"

TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"; mkdir -p "$BIN"
GROOT="$TMP/gmodules"

mkpkg() { # mkpkg <pkg> <binname>
  local d="$GROOT/$1"; mkdir -p "$d/dist"
  printf '#!/usr/bin/env node\nprocess.exit(0);\n' > "$d/dist/cli.js"
  printf '{"name":"%s","version":"1.0.0","bin":{"%s":"dist/cli.js"}}\n' "$1" "$2" > "$d/package.json"
}
mkpkg mcp-searxng mcp-searxng
mkpkg @upstash/context7-mcp context7-mcp
mkpkg firecrawl-mcp firecrawl-mcp

cat > "$BIN/claude" <<EOF
#!$BASH_BIN
echo "\$*" >> "$TMP/claude.log"
exit 0
EOF
cat > "$BIN/npm" <<EOF
#!$BASH_BIN
if [ "\$1" = "root" ] && [ "\$2" = "-g" ]; then echo "$GROOT"; exit 0; fi
exit 0
EOF
cat > "$BIN/uname" <<EOF
#!$BASH_BIN
[ "\${1:-}" = "-o" ] && echo "GNU/Linux" || echo "Linux"
EOF
chmod +x "$BIN/claude" "$BIN/npm" "$BIN/uname"

FHOME="$TMP/home"; mkdir -p "$FHOME/.hermes"
echo 'FIRECRAWL_API_KEY=fc-test-key' > "$FHOME/.hermes/.env"

run() { # run <1=termux|0=linux>
  : > "$TMP/claude.log"
  local extra=()
  [ "$1" = 1 ] && extra=(TERMUX_VERSION=0.test)
  env -i PATH="$BIN:$NODE_DIR:/usr/bin:/bin" HOME="$FHOME" "${extra[@]}" \
    bash "$SUT" >/dev/null 2>&1 || true
}

# Termux/Android → node <cli>
run 1
ok "termux: searxng via node cli"   'grep -Eq "add searxng .* -- node .*/mcp-searxng/dist/cli.js" "$TMP/claude.log"'
ok "termux: context7 via node cli"  'grep -Eq "add context7 .* -- node .*/@upstash/context7-mcp/dist/cli.js" "$TMP/claude.log"'
ok "termux: firecrawl via node cli" 'grep -Eq "add firecrawl .* -- node .*/firecrawl-mcp/dist/cli.js" "$TMP/claude.log"'
ok "termux: no npx used"            '! grep -q -- "-- npx" "$TMP/claude.log"'
ok "termux: searxng env preserved"  'grep -q "SEARXNG_URL=" "$TMP/claude.log"'

# Non-Termux → npx -y
run 0
ok "linux: searxng via npx -y"      'grep -Eq "add searxng .* -- npx -y mcp-searxng" "$TMP/claude.log"'
ok "linux: firecrawl via npx -y"    'grep -Eq "add firecrawl .* -- npx -y firecrawl-mcp" "$TMP/claude.log"'
ok "linux: no node-cli launch"      '! grep -q -- "-- node " "$TMP/claude.log"'

# family-skills + family-wiki (#1678). The absolute python3 path launches the
# in-repo stdlib server; wiki registers only when wiki-agent is on PATH and
# neither the external profile nor a wiki disable flag is set. The idempotent
# add() removes first, so re-runs never duplicate.
FAKE_WIKI="$BIN/wiki-agent"
cat > "$FAKE_WIKI" <<EOF
#!$BASH_BIN
exit 0
EOF
chmod +x "$FAKE_WIKI"
: > "$TMP/claude.log"
env -i PATH="$BIN:$NODE_DIR:/usr/bin:/bin" HOME="$FHOME" \
  bash "$SUT" >/dev/null 2>&1 || true
ok "family-skills registered with abs python3" \
  'grep -Eq "add family-skills .* -- .*python3 .*family_skills_server.py" "$TMP/claude.log"'
ok "family-ops registered with abs python3" \
  'grep -Eq "add family-ops .* -- .*python3 .*family_ops_server.py" "$TMP/claude.log"'
ok "family-wiki registered via wiki-agent" \
  'grep -Eq "add family-wiki .* -- wiki-agent mcp-serve" "$TMP/claude.log"'
ok "family-skills removed before add (idempotent)" \
  'grep -q "remove family-skills -s user" "$TMP/claude.log"'
ok "family-ops removed before add (idempotent)" \
  'grep -q "remove family-ops -s user" "$TMP/claude.log"'

: > "$TMP/claude.log"
env -i PATH="$BIN:$NODE_DIR:/usr/bin:/bin" HOME="$FHOME" CCC_WIKI_MEMORY_ENABLED=0 \
  bash "$SUT" >/dev/null 2>&1 || true
ok "family-wiki skipped when wiki disabled" \
  '! grep -q "add family-wiki" "$TMP/claude.log"'
ok "family-skills still registered when wiki disabled" \
  'grep -q "add family-skills" "$TMP/claude.log"'
ok "family-ops still registered when wiki disabled" \
  'grep -q "add family-ops" "$TMP/claude.log"'

: > "$TMP/claude.log"
env -i PATH="$BIN:$NODE_DIR:/usr/bin:/bin" HOME="$FHOME" CCC_NODE_ISOLATION_PROFILE=external \
  bash "$SUT" >/dev/null 2>&1 || true
ok "family-wiki skipped under external isolation" \
  '! grep -q "add family-wiki" "$TMP/claude.log"'

: > "$TMP/claude.log"
mv "$FAKE_WIKI" "$FAKE_WIKI.bak"
env -i PATH="$BIN:$NODE_DIR:/usr/bin:/bin" HOME="$FHOME" \
  bash "$SUT" >/dev/null 2>&1 || true
mv "$FAKE_WIKI.bak" "$FAKE_WIKI"
ok "family-wiki skipped without wiki-agent" \
  '! grep -q "add family-wiki" "$TMP/claude.log"'

# --family-only (#2011 D): setup.sh's non-interactive mode registers only the
# in-repo stdlib servers, never the networked/keyed ones, and does not rewrite
# an identical registration.
REPO_ROOT="$(cd "$HERE/.." && pwd)"
PY3_SEEN="$(env -i PATH="$BIN:$NODE_DIR:/usr/bin:/bin" bash -c 'command -v python3')"
run_family() { # run_family [extra env...]; sets $family_rc
  : > "$TMP/claude.log"
  env -i PATH="$BIN:$NODE_DIR:/usr/bin:/bin" HOME="$FHOME" "$@" \
    bash "$SUT" --family-only >/dev/null 2>&1
  # shellcheck disable=SC2034  # family_rc is read via eval inside ok()
  family_rc=$?
}
write_cfg() { # write_cfg <file> <skills-server-path> [env-json]
  python3 - "$1" "$PY3_SEEN" "$2" "$REPO_ROOT/bridge/core/family_ops_server.py" "${3:-{\}}" <<'PY'
import json, sys
path, py, skills, ops, env = sys.argv[1:]
servers = {
    "family-skills": {"type": "stdio", "command": py, "args": [skills], "env": json.loads(env)},
    "family-ops": {"type": "stdio", "command": py, "args": [ops], "env": {}},
    "context7": {"type": "stdio", "command": "npx", "args": ["-y", "x"], "env": {}},
}
json.dump({"numStartups": 3, "mcpServers": servers}, open(path, "w"))
PY
}

rm -f "$FHOME/.claude.json"
run_family
ok "family-only: exit 0" '[ "$family_rc" = 0 ]'
ok "family-only: family-skills added with abs python3" \
  'grep -Eq "^mcp add family-skills -s user -- /.*python3 $REPO_ROOT/bridge/core/family_skills_server.py$" "$TMP/claude.log"'
ok "family-only: family-ops added" 'grep -q "^mcp add family-ops -s user" "$TMP/claude.log"'
ok "family-only: networked/keyed/wiki servers untouched" \
  '! grep -Eq "searxng|context7|firecrawl|family-wiki" "$TMP/claude.log"'
ok "family-only: no health-checking mcp list" '! grep -q "^mcp list" "$TMP/claude.log"'

write_cfg "$FHOME/.claude.json" "$REPO_ROOT/bridge/core/family_skills_server.py"
# shellcheck disable=SC2034  # cfg_before is read via eval inside ok()
cfg_before="$(cat "$FHOME/.claude.json")"
run_family
ok "family-only: identical registration left untouched (no remove/add)" \
  '[ "$family_rc" = 0 ] && ! grep -q "family-" "$TMP/claude.log"'
ok "family-only: never writes the Claude config itself" \
  '[ "$cfg_before" = "$(cat "$FHOME/.claude.json")" ]'

write_cfg "$FHOME/.claude.json" "/stale/checkout/bridge/core/family_skills_server.py"
run_family
ok "family-only: stale server path is removed then re-added" \
  'grep -q "^mcp remove family-skills -s user" "$TMP/claude.log" && grep -q "^mcp add family-skills" "$TMP/claude.log"'
ok "family-only: unchanged sibling not re-added" '! grep -q "family-ops" "$TMP/claude.log"'

write_cfg "$FHOME/.claude.json" "$REPO_ROOT/bridge/core/family_skills_server.py" '{"X":"1"}'
run_family
ok "family-only: env override on the entry forces re-add" 'grep -q "^mcp add family-skills" "$TMP/claude.log"'

printf 'not json' > "$FHOME/.claude.json"
run_family
ok "family-only: unreadable config falls back to remove+add" \
  'grep -q "^mcp add family-skills" "$TMP/claude.log" && grep -q "^mcp add family-ops" "$TMP/claude.log"'
rm -f "$FHOME/.claude.json"

CFGDIR="$TMP/cfgdir"; mkdir -p "$CFGDIR"
write_cfg "$CFGDIR/.claude.json" "$REPO_ROOT/bridge/core/family_skills_server.py"
run_family CLAUDE_CONFIG_DIR="$CFGDIR"
ok "family-only: CLAUDE_CONFIG_DIR config is honored" '! grep -q "family-" "$TMP/claude.log"'

FAIL_BIN="$TMP/failbin"; mkdir -p "$FAIL_BIN"
cat > "$FAIL_BIN/claude" <<EOF
#!$BASH_BIN
echo "\$*" >> "$TMP/claude.log"
[ "\$1 \$2" = "mcp add" ] && exit 1
exit 0
EOF
chmod +x "$FAIL_BIN/claude"
: > "$TMP/claude.log"
env -i PATH="$FAIL_BIN:$BIN:$NODE_DIR:/usr/bin:/bin" HOME="$FHOME" bash "$SUT" --family-only >/dev/null 2>&1
# shellcheck disable=SC2034  # fail_rc is read via eval inside ok()
fail_rc=$?
ok "family-only: a failed claude mcp add is reported via exit status" '[ "$fail_rc" = 1 ]'

: > "$TMP/claude.log"
env -i PATH="$BIN:$NODE_DIR:/usr/bin:/bin" HOME="$FHOME" bash "$SUT" --bogus >/dev/null 2>&1
# shellcheck disable=SC2034  # bogus_rc is read via eval inside ok()
bogus_rc=$?
ok "unknown argument rejected before any registration" '[ "$bogus_rc" = 2 ] && [ ! -s "$TMP/claude.log" ]'

# fleet-browser opt-in (#2034): unset → untouched, host → ssh stdio, off → removed,
# external isolation and option-shaped/metachar values → skipped.
brun() { # brun <env assignments...>
  : > "$TMP/claude.log"
  env -i PATH="$BIN:$NODE_DIR:/usr/bin:/bin" HOME="$FHOME" "$@" bash "$SUT" >/dev/null 2>&1 || true
}
brun
ok "fleet-browser: unset leaves registrations untouched" '! grep -q "fleet-browser" "$TMP/claude.log"'
brun CCC_BROWSER_MCP_HOST=browser-pilot
ok "fleet-browser: registered as ssh stdio with the default entrypoint" \
  'grep -Eq "add fleet-browser -s user -- ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -- browser-pilot /opt/fleet-mcp/current/deploy/bin/browser-mcp$" "$TMP/claude.log"'
brun CCC_BROWSER_MCP_HOST=off
ok "fleet-browser: off removes and does not add" \
  'grep -q "remove fleet-browser -s user" "$TMP/claude.log" && ! grep -q "add fleet-browser" "$TMP/claude.log"'
brun CCC_BROWSER_MCP_HOST=browser-pilot CCC_NODE_ISOLATION_PROFILE=external
ok "fleet-browser: external isolation skips" '! grep -q "add fleet-browser" "$TMP/claude.log"'
brun "CCC_BROWSER_MCP_HOST=-oProxyCommand=sh"
ok "fleet-browser: option-shaped host rejected" '! grep -q "add fleet-browser" "$TMP/claude.log"'
brun CCC_BROWSER_MCP_HOST=browser-pilot "CCC_BROWSER_MCP_COMMAND=/opt/x;id"
ok "fleet-browser: shell metacharacters in command rejected" '! grep -q "add fleet-browser" "$TMP/claude.log"'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
