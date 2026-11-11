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
  case "$arg" in alpha|beta|gamma|delta) node=$arg ;; esac
done
printf '%s\n' "$node" >> "$CALLS_FILE"
# A file named <node>.exit makes the fake transport end with that code and no
# output: 124 is what `timeout` reports, any other value a probe that died.
if [ -f "$REPLY_DIR/$node.exit" ]; then exit "$(cat "$REPLY_DIR/$node.exit")"; fi
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

# #2682: a retired Matrix frontend is reported and is not a failure.
printf 'MATRIX_STATUS=RETIRED\nMATRIX_REASON=frontends\nPROBE_COMPLETE=1\n' > "$TMP/reply/delta"
set +e
out=$(REPLY_DIR="$TMP/reply" CALLS_FILE="$TMP/calls" CCC_FLEET_NODES='alpha delta' CCC_FLEET_SSH="$TMP/ssh" \
  CCC_FLEET_SELF=_never_ CCC_FLEET_MATRIX_RETRY_DELAY=0 \
  bash "$ROOT/scripts/fleet-matrix-watch.sh")
rc=$?
set -e
assert test "$rc" = 0
assert grep -qx 'RETIRED delta channel=matrix reason=frontends' <<< "$out"

set +e
out=$(REPLY_DIR="$TMP/reply" CALLS_FILE="$TMP/calls" CCC_FLEET_NODES='gamma' CCC_FLEET_SSH="$TMP/ssh" \
  CCC_FLEET_SELF=_never_ CCC_FLEET_MATRIX_RETRY_DELAY=0 CCC_FLEET_MATRIX_RETRIES=8 \
  bash "$ROOT/scripts/fleet-matrix-watch.sh")
rc=$?
set -e
assert test "$rc" = 1
assert grep -qx 'UNREACHABLE gamma channel=matrix' <<< "$out"
assert test "$(grep -c '^gamma$' "$TMP/calls")" = 2

# #2766: an empty answer is classified by the transport's exit code. Only an
# ssh transport failure (255, above) is UNREACHABLE. A timeout (124) means the
# node took the connection but did not finish the probe inside the limit, and a
# probe that died is a failed inspection — both UNVERIFIED, which the state
# machine confirms over more runs before it pages.
: > "$TMP/calls"
printf '124\n' > "$TMP/reply/gamma.exit"
set +e
out=$(REPLY_DIR="$TMP/reply" CALLS_FILE="$TMP/calls" CCC_FLEET_NODES='alpha gamma' CCC_FLEET_SSH="$TMP/ssh" \
  CCC_FLEET_SELF=_never_ CCC_FLEET_MATRIX_RETRY_DELAY=0 \
  bash "$ROOT/scripts/fleet-matrix-watch.sh")
rc=$?
set -e
assert test "$rc" = 1
assert grep -qx 'UNVERIFIED gamma channel=matrix inspection=probe-timeout' <<< "$out"
assert test "$(grep -c 'UNREACHABLE' <<< "$out")" = 0
assert grep -qx 'OK alpha channel=matrix reason=available' <<< "$out"
# The retry still happens before the verdict (one retry, as before).
assert test "$(grep -c '^gamma$' "$TMP/calls")" = 2

printf '3\n' > "$TMP/reply/gamma.exit"
set +e
out=$(REPLY_DIR="$TMP/reply" CALLS_FILE="$TMP/calls" CCC_FLEET_NODES='gamma' CCC_FLEET_SSH="$TMP/ssh" \
  CCC_FLEET_SELF=_never_ CCC_FLEET_MATRIX_RETRY_DELAY=0 \
  bash "$ROOT/scripts/fleet-matrix-watch.sh")
rc=$?
set -e
assert test "$rc" = 1
assert grep -qx 'UNVERIFIED gamma channel=matrix inspection=probe-error rc=3' <<< "$out"

# A first attempt that times out and a retry that answers is the answer.
cat > "$TMP/ssh-flaky" <<'SH'
#!/bin/sh
cat >/dev/null
if [ ! -e "$REPLY_DIR/.flaky-seen" ]; then : > "$REPLY_DIR/.flaky-seen"; exit 124; fi
printf 'MATRIX_STATUS=RETIRED\nMATRIX_REASON=frontends\nPROBE_COMPLETE=1\n'
SH
chmod +x "$TMP/ssh-flaky"
set +e
out=$(REPLY_DIR="$TMP/reply" CCC_FLEET_NODES='delta' CCC_FLEET_SSH="$TMP/ssh-flaky" \
  CCC_FLEET_SELF=_never_ CCC_FLEET_MATRIX_RETRY_DELAY=0 \
  bash "$ROOT/scripts/fleet-matrix-watch.sh")
rc=$?
set -e
assert test "$rc" = 0
assert grep -qx 'RETIRED delta channel=matrix reason=frontends' <<< "$out"

# The local node is never "unreachable": a local probe that prints nothing is
# a failed inspection.
mkdir -p "$TMP/localroot/scripts"
cp "$ROOT/scripts/fleet-matrix-watch.sh" "$TMP/localroot/scripts/"
printf 'import sys\nsys.exit(7)\n' > "$TMP/localroot/scripts/fleet_matrix_probe.py"
set +e
out=$(CCC_FLEET_NODES='selfnode' CCC_FLEET_SSH="$TMP/ssh" CCC_FLEET_SELF=selfnode \
  CCC_FLEET_MATRIX_RETRY_DELAY=0 bash "$TMP/localroot/scripts/fleet-matrix-watch.sh")
rc=$?
set -e
assert test "$rc" = 1
assert grep -qx 'UNVERIFIED selfnode channel=matrix inspection=probe-error rc=7' <<< "$out"
rm -f "$TMP/reply/gamma.exit"
assert python3 -m unittest "$ROOT/scripts/fleet_matrix_probe_test.py"
printf 'PASS=%s FAIL=0\n' "$pass"
