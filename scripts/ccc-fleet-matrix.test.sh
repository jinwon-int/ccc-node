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
{"ok":true,"checks":[{"status":"정상"}]}
===== nosuk =====
{"ok":true,"checks":[{"status":"경고","reason":"stale cache"}]}
===== soonwook =====
permission denied
EOF

out="$(bash "$ROOT/scripts/ccc-doctor-fleet-matrix.sh" --evidence "$TMP/doctor.txt" --node-list dungae,nosuk,soonwook --json)"; rc=$?
ok "doctor matrix emits JSON" '[ "$rc" = 0 ] && jq -e ".kind == \"ccc-doctor-fleet-matrix\" and .mutations.serviceRestart == false and .mutations.secretRead == false" <<<"$out" >/dev/null'
ok "doctor matrix classifies Korean statuses" 'jq -e ".nodes[] | select(.node == \"dungae\" and .status == \"정상\")" <<<"$out" >/dev/null && jq -e ".nodes[] | select(.node == \"nosuk\" and .status == \"경고\")" <<<"$out" >/dev/null && jq -e ".nodes[] | select(.node == \"soonwook\" and .status == \"수동필요\")" <<<"$out" >/dev/null'

cat > "$TMP/security.txt" <<'EOF'
===== daegyo =====
PASS=12 FAIL=0
===== gongyung =====
{"ok":false,"risk":"위험"}
EOF

out="$(bash "$ROOT/scripts/ccc-security-audit-fleet-matrix.sh" --evidence "$TMP/security.txt" --node-list daegyo,gongyung --json)"; rc=$?
ok "security matrix emits read-only JSON" '[ "$rc" = 0 ] && jq -e ".kind == \"ccc-security-audit-fleet-matrix\" and .mutations.permissionChange == false and .mutations.secretRead == false" <<<"$out" >/dev/null'
ok "security matrix classifies normal and danger" 'jq -e ".nodes[] | select(.node == \"daegyo\" and .status == \"정상\")" <<<"$out" >/dev/null && jq -e ".nodes[] | select(.node == \"gongyung\" and .status == \"위험\")" <<<"$out" >/dev/null'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
