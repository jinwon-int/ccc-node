#!/usr/bin/env bash
# Tests for the root-owned, allowlist-bounded service-control wrapper.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WRAPPER="$HERE/ccc-service-control.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0

ok() {
  local name="$1"; shift
  if "$@"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $name"; fi
}

ALLOW="$TMP/service-control.allow"
printf '%s\n' '# exact systemd unit names only' 'a2a-broker.service' > "$ALLOW"
chmod 600 "$ALLOW"

run_wrapper() {
  CCC_SERVICE_CONTROL_ALLOWLIST="$ALLOW" \
  CCC_SERVICE_CONTROL_DRY_RUN=1 \
    bash "$WRAPPER" "$@"
}

run_wrapper restart a2a-broker.service >"$TMP/out" 2>&1; rc=$?
ok "allowlisted restart succeeds" test "$rc" -eq 0
ok "dry-run shows the exact bounded call" grep -qx -- 'DRY-RUN: /usr/bin/systemctl restart -- a2a-broker.service' "$TMP/out"

run_wrapper restart ssh.service >"$TMP/out" 2>&1; rc=$?
ok "non-allowlisted unit is denied" test "$rc" -ne 0
ok "denied unit emits no dry-run execution" test "$(grep -c '^DRY-RUN:' "$TMP/out")" -eq 0

run_wrapper stop a2a-broker.service >/dev/null 2>&1; rc=$?
ok "unsupported action is denied" test "$rc" -ne 0

printf '%s\n' 'a2a-broker.service' > "$ALLOW"
chmod 666 "$ALLOW"
run_wrapper restart a2a-broker.service >/dev/null 2>&1; rc=$?
ok "group/world-writable allowlist is denied" test "$rc" -ne 0

rm -f "$ALLOW"
ln -s "$TMP/real.allow" "$ALLOW"
printf '%s\n' 'a2a-broker.service' > "$TMP/real.allow"
chmod 600 "$TMP/real.allow"
run_wrapper restart a2a-broker.service >/dev/null 2>&1; rc=$?
ok "symlink allowlist is denied" test "$rc" -ne 0

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
