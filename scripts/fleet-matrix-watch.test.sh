#!/usr/bin/env bash
# Hermetic transport and alert contract for the Matrix fleet watch.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d /tmp/fleet-matrix-watch-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/reply"
cat > "$TMP/ssh" <<'SH'
#!/bin/sh
cat >/dev/null
for arg in "$@"; do
  case "$arg" in alpha|beta|gamma) node=$arg ;; esac
done
[ -f "$REPLY_DIR/$node" ] || exit 255
cat "$REPLY_DIR/$node"
SH
chmod +x "$TMP/ssh"
printf 'MATRIX_STATUS=OK\nMATRIX_REASON=available\nPROBE_COMPLETE=1\n' > "$TMP/reply/alpha"
printf 'MATRIX_STATUS=DOWN\nMATRIX_REASON=no-process\nPROBE_COMPLETE=1\n' > "$TMP/reply/beta"

set +e
out=$(REPLY_DIR="$TMP/reply" CCC_FLEET_NODES='alpha beta' CCC_FLEET_SSH="$TMP/ssh" \
  CCC_FLEET_SELF=_never_ CCC_FLEET_RETRY_DELAY=0 \
  bash "$ROOT/scripts/fleet-matrix-watch.sh")
rc=$?
set -e
[ "$rc" = 1 ]
grep -qx 'OK alpha channel=matrix reason=available' <<< "$out"
grep -qx 'DOWN beta channel=matrix reason=no-process' <<< "$out"

set +e
out=$(REPLY_DIR="$TMP/reply" CCC_FLEET_NODES='gamma' CCC_FLEET_SSH="$TMP/ssh" \
  CCC_FLEET_SELF=_never_ CCC_FLEET_RETRY_DELAY=0 \
  bash "$ROOT/scripts/fleet-matrix-watch.sh")
rc=$?
set -e
[ "$rc" = 1 ]
grep -qx 'UNREACHABLE gamma channel=matrix' <<< "$out"
echo 'fleet-matrix-watch tests passed'
