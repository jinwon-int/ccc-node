#!/usr/bin/env bash
# Hermetic transport and alert contract for the Matrix fleet watch.
set -euo pipefail
pass=0
assert() {
  "$@"
  pass=$((pass + 1))
}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP_BASE="${TMPDIR:-$(dirname "$ROOT")}"
mkdir -p "$TMP_BASE"
TMP="$(mktemp -d "$TMP_BASE/fleet-matrix-watch-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/reply"
cat > "$TMP/ssh" <<'SH'
#!/bin/sh
cat >/dev/null
for arg in "$@"; do
  case "$arg" in alpha|beta|gamma) node=$arg ;; esac
done
printf '%s\n' "$node" >> "$CALLS_FILE"
[ -f "$REPLY_DIR/$node" ] || exit 255
cat "$REPLY_DIR/$node"
SH
chmod +x "$TMP/ssh"
printf 'MATRIX_STATUS=OK\nMATRIX_REASON=available\nPROBE_COMPLETE=1\n' > "$TMP/reply/alpha"
printf 'MATRIX_STATUS=DOWN\nMATRIX_REASON=no-process\nPROBE_COMPLETE=1\n' > "$TMP/reply/beta"

set +e
out=$(REPLY_DIR="$TMP/reply" CALLS_FILE="$TMP/calls" CCC_FLEET_NODES='alpha beta' CCC_FLEET_SSH="$TMP/ssh" \
  CCC_FLEET_SELF=_never_ CCC_FLEET_MATRIX_RETRY_DELAY=0 \
  bash "$ROOT/scripts/fleet-matrix-watch.sh")
rc=$?
set -e
assert test "$rc" = 1
assert grep -qx 'OK alpha channel=matrix reason=available' <<< "$out"
assert grep -qx 'DOWN beta channel=matrix reason=no-process' <<< "$out"

set +e
out=$(REPLY_DIR="$TMP/reply" CALLS_FILE="$TMP/calls" CCC_FLEET_NODES='gamma' CCC_FLEET_SSH="$TMP/ssh" \
  CCC_FLEET_SELF=_never_ CCC_FLEET_MATRIX_RETRY_DELAY=0 CCC_FLEET_MATRIX_RETRIES=8 \
  bash "$ROOT/scripts/fleet-matrix-watch.sh")
rc=$?
set -e
assert test "$rc" = 1
assert grep -qx 'UNREACHABLE gamma channel=matrix' <<< "$out"
assert test "$(grep -c '^gamma$' "$TMP/calls")" = 2
assert python3 -m unittest "$ROOT/scripts/fleet_matrix_probe_test.py"
printf 'PASS=%s FAIL=0\n' "$pass"
