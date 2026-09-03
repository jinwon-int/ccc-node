#!/usr/bin/env bash
# Hermetic tests for tunnel-audit-fleet.sh — ssh is stubbed; each "node" answers
# with canned tunnel-audit JSON from $TMP/reply/<node>. No node is contacted.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FLEET="$ROOT/scripts/tunnel-audit-fleet.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

mkdir -p "$TMP/reply" "$TMP/bin" "$TMP/state"
# Stub ssh: last arg before "bash -s" is the node; reply file = canned JSON.
write_exec_stub "$TMP/bin/ssh" <<EOF
node=""
for a in "\$@"; do case "\$a" in -*|bash|-s|"bash -s"|BatchMode=yes|ConnectTimeout=10) ;; *) node="\$a" ;; esac; done
cat >/dev/null   # consume the probe script on stdin
f="$TMP/reply/\$node"
[ -f "\$f" ] || exit 255
cat "\$f"
EOF

UFW_MISSING='{"status":"missing"}'
doc() { # <cloudflared-active> <extra-listener-json-or-empty> <funnel> <residue-json> <ufw-object-json>
  local ufw="${5:-$UFW_MISSING}"
  cat <<EOF
{"schema":"ccc.tunnel-audit.v1","node":"x","exposure":{"funnel_configured":$3},
 "units":[{"unit":"cloudflared.service","kind":"cloudflared","active":"$1"},{"unit":"seoseo-broker-tunnel.service","kind":"ssh-forward","active":"active"}],
 "cron":[],
 "listeners":[{"local":"0.0.0.0:22","bind":"public","process":"sshd"}$2],
 "tailscale":{},"firewall":{"ufw":$ufw},"residue":$4}
EOF
}
doc active "" false "[]" > "$TMP/reply/alpha"
doc inactive "" false "[]" > "$TMP/reply/beta"

export CCC_FLEET_NODES="alpha beta" CCC_FLEET_SSH="$TMP/bin/ssh" CCC_FLEET_SELF=_none_ \
       CCC_STATE_DIR="$TMP/state" CCC_FLEET_SSH_TIMEOUT=10 PATH="$TMP/bin:$PATH"

# 1) first run: no baseline → UNBASELINED, exit 0, current stored
out="$(bash "$FLEET" 2>&1)"; rc=$?
ok "unbaselined nodes exit 0" '[ "$rc" = 0 ]'
ok "unbaselined verdict per node" 'grep -q "^UNBASELINED alpha" <<<"$out" && grep -q "^UNBASELINED beta" <<<"$out"'
ok "current collection stored per node" 'jq -e ".schema" "$TMP/state/tunnel-audit/current/alpha.json" >/dev/null && jq -e ".schema" "$TMP/state/tunnel-audit/current/beta.json" >/dev/null'
ok "run history written" '[ "$(ls "$TMP/state/tunnel-audit/runs" | wc -l)" = 1 ]'

# 2) accept baseline for alpha only
out="$(bash "$FLEET" --accept-baseline=alpha 2>&1)"; rc=$?
ok "accept-baseline exits 0" '[ "$rc" = 0 ]'
ok "alpha baseline accepted, beta still unbaselined" 'grep -q "^BASELINE-ACCEPTED alpha" <<<"$out" && grep -q "^UNBASELINED beta" <<<"$out" && [ -f "$TMP/state/tunnel-audit/baseline/alpha.json" ] && [ ! -f "$TMP/state/tunnel-audit/baseline/beta.json" ]'

# 3) unchanged → OK
out="$(bash "$FLEET" 2>&1)"; rc=$?
ok "unchanged node is OK" '[ "$rc" = 0 ] && grep -q "^OK alpha" <<<"$out"'

# 4) a new public listener + funnel on → NEW, exit 1, names the items
doc active ',{"local":"0.0.0.0:8080","bind":"public","process":"python3"}' true "[]" > "$TMP/reply/alpha"
out="$(bash "$FLEET" 2>&1)"; rc=$?
ok "new exposure exits 1" '[ "$rc" = 1 ]'
ok "NEW verdict names the listener and funnel" 'grep -q "^NEW alpha: .*0.0.0.0:8080 python3 \[public\]" <<<"$out" && grep -q "enabled" <<<"$out"'
ok "baseline untouched by a non-accepting run" '! grep -q "8080" "$TMP/state/tunnel-audit/baseline/alpha.json"'

# 5) something vanished → GONE, exit 0
doc active "" false "[]" > "$TMP/reply/alpha"
jq '.units += [{"unit":"old-tunnel.service","kind":"ssh-reverse","active":"active"}]' "$TMP/state/tunnel-audit/baseline/alpha.json" > "$TMP/b.json" && mv "$TMP/b.json" "$TMP/state/tunnel-audit/baseline/alpha.json"
out="$(bash "$FLEET" 2>&1)"; rc=$?
ok "gone-only change exits 0" '[ "$rc" = 0 ]'
ok "GONE verdict names the unit" 'grep -q "^GONE alpha: .*old-tunnel.service \[ssh-reverse\]" <<<"$out"'

# 6) unit state change alone (active→inactive) is not NEW: identity is name+kind
bash "$FLEET" --accept-baseline=alpha >/dev/null 2>&1
doc inactive "" false "[]" > "$TMP/reply/alpha"
out="$(bash "$FLEET" 2>&1)"; rc=$?
ok "active/inactive flip alone is not a NEW exposure" '[ "$rc" = 0 ] && grep -q "^OK alpha" <<<"$out"'

# 6b) firewall posture lands in the signature (#1431): accepted, then any
# policy/rule/status change is NEW — including ufw being dropped.
doc inactive "" false "[]" '{"status":"active","default_incoming":"deny","rules":["22/tcp ALLOW IN Anywhere"],"rules_sha256":"deadbeefcafe0123"}' > "$TMP/reply/alpha"
bash "$FLEET" --accept-baseline=alpha >/dev/null 2>&1
out="$(bash "$FLEET" 2>&1)"; rc=$?
ok "accepted ufw posture with no change is OK" '[ "$rc" = 0 ] && grep -q "^OK alpha" <<<"$out"'
ok "baseline stores the accepted ufw posture" 'jq -e ".firewall.ufw.status == \"active\" and .firewall.ufw.default_incoming == \"deny\" and .firewall.ufw.rules_sha256 == \"deadbeefcafe0123\"" "$TMP/state/tunnel-audit/baseline/alpha.json" >/dev/null'
doc inactive "" false "[]" '{"status":"active","default_incoming":"deny","rules":["22/tcp ALLOW IN Anywhere","8080 ALLOW IN Anywhere"],"rules_sha256":"0011223344556677"}' > "$TMP/reply/alpha"
out="$(bash "$FLEET" 2>&1)"; rc=$?
ok "widened allowlist is NEW exit 1 naming the hash8" '[ "$rc" = 1 ] && grep -q "^NEW alpha: ufw active default-in=deny rules=00112233" <<<"$out"'
doc inactive "" false "[]" '{"status":"inactive"}' > "$TMP/reply/alpha"
out="$(bash "$FLEET" 2>&1)"; rc=$?
ok "ufw dropped (inactive) is NEW exit 1 — the scary direction pages too" '[ "$rc" = 1 ] && grep -q "ufw inactive" <<<"$out"'
doc inactive "" false "[]" > "$TMP/reply/alpha"
bash "$FLEET" --accept-baseline=alpha >/dev/null 2>&1

# 7) unreachable node → UNREACHABLE, exit 1; other node still evaluated
rm -f "$TMP/reply/beta"
out="$(bash "$FLEET" 2>&1)"; rc=$?
ok "unreachable node exits 1" '[ "$rc" = 1 ]'
ok "unreachable verdict + reachable node still reported" 'grep -q "^UNREACHABLE beta" <<<"$out" && grep -q "^OK alpha" <<<"$out"'

# 8) node whose checkout lacks the collector → NOSCRIPT (exit 0, visible)
printf '{"schema":"ccc.tunnel-audit.v1","error":"no-script"}\n' > "$TMP/reply/beta"
out="$(bash "$FLEET" 2>&1)"; rc=$?
ok "missing collector is NOSCRIPT, not a failure" '[ "$rc" = 0 ] && grep -q "^NOSCRIPT beta" <<<"$out"'

# 9) garbage reply is UNREACHABLE (never parsed as a clean node)
printf 'Permission denied\n' > "$TMP/reply/beta"
out="$(bash "$FLEET" 2>&1)"; rc=$?
ok "non-JSON reply is UNREACHABLE" '[ "$rc" = 1 ] && grep -q "^UNREACHABLE beta" <<<"$out"'

# 10) --quiet suppresses stdout but still records the run
out="$(bash "$FLEET" --quiet 2>&1)"; rc=$?
ok "quiet mode prints nothing" '[ -z "$out" ]'
ok "quiet mode still writes the run file" '[ "$(ls "$TMP/state/tunnel-audit/runs" | wc -l)" -ge 5 ]'

# 11) run history is bounded
CCC_TUNNEL_AUDIT_KEEP_RUNS=3 bash "$FLEET" --quiet >/dev/null 2>&1
ok "run history pruned to KEEP_RUNS" '[ "$(ls "$TMP/state/tunnel-audit/runs" | wc -l)" = 3 ]'

out="$(bash "$FLEET" --bogus 2>&1)"; rc=$?
ok "unknown flag exits 2" '[ "$rc" = 2 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
