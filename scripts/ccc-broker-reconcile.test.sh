#!/usr/bin/env bash
# Tests for the root-owned, allowlist-bounded broker Compose reconcile wrapper.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WRAPPER="$HERE/ccc-broker-reconcile.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0

ok() {
  local name="$1"; shift
  if "$@"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $name"; fi
}

DIRF="$TMP/broker-reconcile.dir"
ALLOW="$TMP/broker-reconcile.allow"
BROKER_DIR="$TMP/broker"
mkdir -p "$BROKER_DIR"
printf '%s\n' '# operator-fixed broker project dir' "$BROKER_DIR" > "$DIRF"; chmod 600 "$DIRF"
printf '%s\n' '# exact compose service names only' 'a2a-broker' 't2-broker' > "$ALLOW"; chmod 600 "$ALLOW"

run_wrapper() {
  CCC_BROKER_RECONCILE_DIR_FILE="$DIRF" \
  CCC_BROKER_RECONCILE_ALLOWLIST="$ALLOW" \
  CCC_BROKER_RECONCILE_DRY_RUN=1 \
    "$WRAPPER" "$@"
}

# --- happy path -------------------------------------------------------------
run_wrapper a2a-broker >"$TMP/out" 2>&1; rc=$?
ok "allowlisted service succeeds" test "$rc" -eq 0
ok "dry-run shows the bounded reconcile call" \
  grep -qx -- "DRY-RUN: cd $BROKER_DIR && export A2A_BROKER_REVISION=\$(git rev-parse HEAD) && docker compose up -d a2a-broker" "$TMP/out"

run_wrapper a2a-broker t2-broker >"$TMP/out" 2>&1; rc=$?
ok "multiple allowlisted services succeed" test "$rc" -eq 0
ok "dry-run lists all requested services" grep -q -- 'docker compose up -d a2a-broker t2-broker' "$TMP/out"

# --- allowlist enforcement --------------------------------------------------
run_wrapper nginx >"$TMP/out" 2>&1; rc=$?
ok "non-allowlisted service is denied" test "$rc" -ne 0
ok "denied service emits no dry-run execution" test "$(grep -c '^DRY-RUN:' "$TMP/out")" -eq 0

run_wrapper a2a-broker nginx >/dev/null 2>&1; rc=$?
ok "one non-allowlisted service denies the whole call" test "$rc" -ne 0

# --- input validation -------------------------------------------------------
run_wrapper >/dev/null 2>&1; rc=$?
ok "no service argument is a usage error" test "$rc" -ne 0

run_wrapper 'a2a-broker;reboot' >/dev/null 2>&1; rc=$?
ok "service token with shell metacharacter is denied" test "$rc" -ne 0

run_wrapper '../../etc' >/dev/null 2>&1; rc=$?
ok "path-like service token is denied" test "$rc" -ne 0

printf '%s\n' 'a2a-broker' '--build' > "$ALLOW"; chmod 600 "$ALLOW"
run_wrapper a2a-broker >/dev/null 2>&1; rc=$?
ok "allowlist rejects a leading-option pseudo-service" test "$rc" -ne 0
printf '%s\n' 'a2a-broker' 't2-broker' > "$ALLOW"; chmod 600 "$ALLOW"

COMPOSE_FILE="$TMP/evil.yml" run_wrapper a2a-broker >/dev/null 2>&1; rc=$?
ok "COMPOSE_FILE override is denied" test "$rc" -ne 0
DOCKER_HOST='tcp://other:2375' run_wrapper a2a-broker >/dev/null 2>&1; rc=$?
ok "remote DOCKER_HOST override is denied" test "$rc" -ne 0

printf '%s\n' "touch '$TMP/bash-env-ran'" > "$TMP/evil-bash-env"
BASH_ENV="$TMP/evil-bash-env" run_wrapper a2a-broker >/dev/null 2>&1; rc=$?
ok "privileged shebang ignores caller BASH_ENV" \
  sh -c '[ "$1" -eq 0 ] && [ ! -e "$2" ]' _ "$rc" "$TMP/bash-env-ran"

# --- config integrity -------------------------------------------------------
chmod 666 "$ALLOW"
run_wrapper a2a-broker >/dev/null 2>&1; rc=$?
ok "group/world-writable allowlist is denied" test "$rc" -ne 0
chmod 600 "$ALLOW"

chmod 666 "$DIRF"
run_wrapper a2a-broker >/dev/null 2>&1; rc=$?
ok "group/world-writable dir file is denied" test "$rc" -ne 0
chmod 600 "$DIRF"

mv "$ALLOW" "$TMP/real.allow"; ln -s "$TMP/real.allow" "$ALLOW"
run_wrapper a2a-broker >/dev/null 2>&1; rc=$?
ok "symlink allowlist is denied" test "$rc" -ne 0
rm -f "$ALLOW"; mv "$TMP/real.allow" "$ALLOW"

rm -f "$ALLOW"
run_wrapper a2a-broker >/dev/null 2>&1; rc=$?
ok "missing allowlist is denied" test "$rc" -ne 0
printf '%s\n' 'a2a-broker' 't2-broker' > "$ALLOW"; chmod 600 "$ALLOW"

# --- broker dir must be an operator-fixed absolute path ---------------------
printf '%s\n' 'relative/broker' > "$DIRF"; chmod 600 "$DIRF"
run_wrapper a2a-broker >/dev/null 2>&1; rc=$?
ok "non-absolute broker dir is denied" test "$rc" -ne 0
printf '%s\n' "$BROKER_DIR" > "$DIRF"; chmod 600 "$DIRF"

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
