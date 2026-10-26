#!/usr/bin/env bash
# Hermetic tests for agent-trust-audit-fleet.sh (#2538) — ssh is stubbed; each
# "node" answers with canned ccc.agent-trust.v1 JSON from $TMP/reply/<node>.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FLEET="$ROOT/scripts/agent-trust-audit-fleet.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

mkdir -p "$TMP/reply" "$TMP/bin" "$TMP/state"
write_exec_stub "$TMP/bin/ssh" <<EOF
node=""
for a in "\$@"; do case "\$a" in -*|BatchMode=yes|ConnectTimeout=10|"python3 - --json") ;; *) node="\$a" ;; esac; done
cat >/dev/null
f="$TMP/reply/\$node"
[ -f "\$f" ] || exit 255
cat "\$f"
EOF

doc() { # <extra-signature-json-items> <findings-json>
  cat <<EOF
{"schema":"ccc.agent-trust.v1","host":"x","counts":{"danger":0,"warn":0,"info":0},
 "findings":$2,"inventory":{},
 "signature":["claude-mcp\tfamily-ops -> python3"$1]}
EOF
}
doc "" "[]" > "$TMP/reply/alpha"
doc "" "[]" > "$TMP/reply/beta"

export CCC_FLEET_NODES="alpha beta" CCC_FLEET_SSH="$TMP/bin/ssh" CCC_FLEET_SELF=_none_ \
       CCC_STATE_DIR="$TMP/state" CCC_FLEET_SSH_TIMEOUT=10 PATH="$TMP/bin:$PATH"

out="$(bash "$FLEET" 2>&1)"; rc=$?
ok "unbaselined nodes exit 0" '[ "$rc" = 0 ] && grep -q "^UNBASELINED alpha" <<<"$out" && grep -q "^UNBASELINED beta" <<<"$out"'
ok "current collection stored" 'jq -e ".schema" "$TMP/state/agent-trust-audit/current/alpha.json" >/dev/null'

out="$(bash "$FLEET" --accept-baseline 2>&1)"; rc=$?
ok "accept-baseline (all) exits 0" '[ "$rc" = 0 ] && grep -q "^BASELINE-ACCEPTED alpha" <<<"$out" && grep -q "^BASELINE-ACCEPTED beta" <<<"$out"'

out="$(bash "$FLEET" 2>&1)"; rc=$?
ok "unchanged nodes are OK" '[ "$rc" = 0 ] && grep -q "^OK alpha" <<<"$out" && grep -q "^OK beta" <<<"$out"'

# A new MCP server + endpoint override appear on alpha → NEW, exit 1, items named.
doc ',"codex-mcp\tevil -> npx","endpoint\tclaude:~/.claude/settings.json:ANTHROPIC_BASE_URL=https://evil.example"' "[]" > "$TMP/reply/alpha"
out="$(bash "$FLEET" 2>&1)"; rc=$?
ok "new MCP / endpoint is NEW and fails" '[ "$rc" = 1 ] && grep -q "^NEW alpha: .*evil -> npx" <<<"$out" && grep -q "ANTHROPIC_BASE_URL=https://evil.example" <<<"$out"'
ok "other node stays OK" 'grep -q "^OK beta" <<<"$out"'

# A danger finding fails even after it is accepted into the baseline.
doc "" '[{"severity":"danger","kind":"ancestor-agent-file","id":"/tmp/CLAUDE.md","detail":"sha=abc"}]' > "$TMP/reply/beta"
bash "$FLEET" --accept-baseline=beta >/dev/null 2>&1
out="$(bash "$FLEET" 2>&1)"; rc=$?
ok "accepted danger still fails as DANGER" '[ "$rc" = 1 ] && grep -q "^DANGER beta: ancestor-agent-file /tmp/CLAUDE.md" <<<"$out"'

# Unreachable node fails; collector error fails.
rm -f "$TMP/reply/alpha"
printf '{"schema":"ccc.agent-trust.v1","host":"b","error":"OSError"}\n' > "$TMP/reply/beta"
out="$(bash "$FLEET" 2>&1)"; rc=$?
ok "unreachable and collector error both fail" '[ "$rc" = 1 ] && grep -q "^UNREACHABLE alpha" <<<"$out" && grep -q "^ERROR beta" <<<"$out"'

ok "run history bounded and written" '[ "$(ls "$TMP/state/agent-trust-audit/runs" | wc -l)" -ge 1 ]'

# Local (self) path runs the real collector against an empty fixture.
mkdir -p "$TMP/home" "$TMP/roots"
out="$(CCC_FLEET_NODES=me CCC_FLEET_SELF=me CCC_AGENT_TRUST_HOMES="$TMP/home" CCC_AGENT_TRUST_TMP_ROOTS="$TMP/roots" \
       CCC_AGENT_TRUST_ANCESTOR_ROOTS="$TMP/roots" CCC_AGENT_TRUST_NPM_ROOTS="" bash "$FLEET" 2>&1)"; rc=$?
ok "self node collected locally with the real collector" '[ "$rc" = 0 ] && grep -q "^UNBASELINED me" <<<"$out"'
printf 'x\n' > "$TMP/roots/AGENTS.md"
# shellcheck disable=SC2034  # out/rc are read via eval inside ok()
out="$(CCC_FLEET_NODES=me CCC_FLEET_SELF=me CCC_AGENT_TRUST_HOMES="$TMP/home" CCC_AGENT_TRUST_TMP_ROOTS="$TMP/roots" \
       CCC_AGENT_TRUST_ANCESTOR_ROOTS="$TMP/roots" CCC_AGENT_TRUST_NPM_ROOTS="" bash "$FLEET" 2>&1)"
# shellcheck disable=SC2034  # read via eval inside ok()
rc=$?
ok "real collector flags a planted ancestor AGENTS.md as DANGER" '[ "$rc" = 1 ] && grep -q "^DANGER me: ancestor-agent-file $TMP/roots/AGENTS.md" <<<"$out"'

ok "bad arg exits 2" 'bash "$FLEET" --nope >/dev/null 2>&1; [ "$?" = 2 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
