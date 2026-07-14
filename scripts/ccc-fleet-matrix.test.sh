#!/usr/bin/env bash
# Tests for ccc doctor/security fleet matrix wrappers. No SSH, no service changes.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
BASE_TMP="${TMPDIR:-/tmp}"
mkdir -p "$BASE_TMP"
TMP="$(mktemp -d "$BASE_TMP/ccc-fleet-matrix-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

cat > "$TMP/doctor.txt" <<'EOF'
===== dungae =====
{"ok":true,"harnessVersion":"v0.1.0-2-gabc1234","checks":[{"status":"정상"}]}
===== nosuk =====
# ccc doctor
- harness version: `v0.1.0-dirty`

경고: stale cache
===== soonwook =====
permission denied
EOF

out="$(bash "$ROOT/scripts/ccc-doctor-fleet-matrix.sh" --evidence "$TMP/doctor.txt" --node-list dungae,nosuk,soonwook --json)"; rc=$?
ok "doctor matrix emits JSON" '[ "$rc" = 0 ] && jq -e ".kind == \"ccc-doctor-fleet-matrix\" and .mutations.serviceRestart == false and .mutations.secretRead == false" <<<"$out" >/dev/null'
ok "doctor matrix classifies Korean statuses" 'jq -e ".nodes[] | select(.node == \"dungae\" and .status == \"정상\")" <<<"$out" >/dev/null && jq -e ".nodes[] | select(.node == \"nosuk\" and .status == \"경고\")" <<<"$out" >/dev/null && jq -e ".nodes[] | select(.node == \"soonwook\" and .status == \"수동필요\")" <<<"$out" >/dev/null'
ok "doctor matrix includes harness versions" 'jq -e ".nodes[] | select(.node == \"dungae\" and .version == \"v0.1.0-2-gabc1234\")" <<<"$out" >/dev/null && jq -e ".nodes[] | select(.node == \"nosuk\" and .version == \"v0.1.0-dirty\")" <<<"$out" >/dev/null'

cat > "$TMP/security.txt" <<'EOF'
===== daegyo =====
PASS=12 FAIL=0
===== gongyung =====
{"ok":false,"risk":"위험"}
EOF

out="$(bash "$ROOT/scripts/ccc-security-audit-fleet-matrix.sh" --evidence "$TMP/security.txt" --node-list daegyo,gongyung --json)"; rc=$?
ok "security matrix emits read-only JSON" '[ "$rc" = 0 ] && jq -e ".kind == \"ccc-security-audit-fleet-matrix\" and .mutations.permissionChange == false and .mutations.secretRead == false" <<<"$out" >/dev/null'
ok "security matrix classifies normal and danger" 'jq -e ".nodes[] | select(.node == \"daegyo\" and .status == \"정상\")" <<<"$out" >/dev/null && jq -e ".nodes[] | select(.node == \"gongyung\" and .status == \"위험\")" <<<"$out" >/dev/null'

# --- #451: single-sourced parser/classifier -------------------------------
# Both wrappers must delegate to the shared scripts/lib/fleet_matrix.py so the
# evidence-block parser is defined in exactly one place (no more byte-identical
# copies drifting apart).
ok "doctor wrapper delegates to shared lib" 'grep -q "lib/fleet_matrix.py" "$ROOT/scripts/ccc-doctor-fleet-matrix.sh" && grep -q -- "--domain doctor" "$ROOT/scripts/ccc-doctor-fleet-matrix.sh"'
ok "security wrapper delegates to shared lib" 'grep -q "lib/fleet_matrix.py" "$ROOT/scripts/ccc-security-audit-fleet-matrix.sh" && grep -q -- "--domain security" "$ROOT/scripts/ccc-security-audit-fleet-matrix.sh"'
ok "neither wrapper re-implements the block parser inline" '! grep -q "def classify" "$ROOT/scripts/ccc-doctor-fleet-matrix.sh" && ! grep -q "def classify" "$ROOT/scripts/ccc-security-audit-fleet-matrix.sh"'

# Previously-drifted branches must stay covered by the shared classifier.
cat > "$TMP/drift.txt" <<'EOF'
===== a =====
{"ok":false,"level":"critical"}
===== b =====
critical: world-writable file
===== c =====
some bearer token=abc leaked
===== d =====
{"result":"교정가능","fixable":true}
EOF

sout="$(bash "$ROOT/scripts/ccc-security-audit-fleet-matrix.sh" --evidence "$TMP/drift.txt" --node-list a,b,c,d)"
ok "security: JSON 'critical' → 위험" 'jq -e ".nodes[] | select(.node==\"a\" and .status==\"위험\" and .reason==\"security_audit_reported_failure\")" <<<"$sout" >/dev/null'
ok "security: text 'critical' → 위험" 'jq -e ".nodes[] | select(.node==\"b\" and .status==\"위험\" and .reason==\"security_failures_present\")" <<<"$sout" >/dev/null'
ok "security: secret word mention flagged (read-only)" 'jq -e ".nodes[] | select(.node==\"c\" and .secretWordOnlyMention==true)" <<<"$sout" >/dev/null'
ok "security: fixable → 교정가능" 'jq -e ".nodes[] | select(.node==\"d\" and .status==\"교정가능\" and .reason==\"fixable_security_drift\")" <<<"$sout" >/dev/null'

dout="$(bash "$ROOT/scripts/ccc-doctor-fleet-matrix.sh" --evidence "$TMP/drift.txt" --node-list a,b,c,d)"
ok "doctor: JSON body has version field (extractor)" 'jq -e ".nodes[] | select(.node==\"a\") | has(\"version\")" <<<"$dout" >/dev/null'
ok "doctor: no secretWordOnlyMention field (domain-scoped)" 'jq -e "[.nodes[] | has(\"secretWordOnlyMention\")] | any | not" <<<"$dout" >/dev/null'
ok "doctor: 'critical' is NOT a danger keyword here (domain-scoped)" 'jq -e ".nodes[] | select(.node==\"b\" and .status!=\"위험\")" <<<"$dout" >/dev/null'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
