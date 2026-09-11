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
ok "family-wiki registered via wiki-agent" \
  'grep -Eq "add family-wiki .* -- wiki-agent mcp-serve" "$TMP/claude.log"'
ok "family-skills removed before add (idempotent)" \
  'grep -q "remove family-skills -s user" "$TMP/claude.log"'

: > "$TMP/claude.log"
env -i PATH="$BIN:$NODE_DIR:/usr/bin:/bin" HOME="$FHOME" CCC_WIKI_MEMORY_ENABLED=0 \
  bash "$SUT" >/dev/null 2>&1 || true
ok "family-wiki skipped when wiki disabled" \
  '! grep -q "add family-wiki" "$TMP/claude.log"'
ok "family-skills still registered when wiki disabled" \
  'grep -q "add family-skills" "$TMP/claude.log"'

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

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
