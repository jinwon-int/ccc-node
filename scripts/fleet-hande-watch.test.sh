#!/usr/bin/env bash
# Hermetic transport and alert contract for the 「한데」 fleet watch (#2569).
set -euo pipefail
pass=0
assert() {
  "$@"
  pass=$((pass + 1))
}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP_BASE="${TMPDIR:-$(dirname "$ROOT")}"
mkdir -p "$TMP_BASE"
TMP="$(mktemp -d "$TMP_BASE/fleet-hande-watch-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/reply"
cat > "$TMP/ssh" <<'SH'
#!/bin/sh
cat >/dev/null
for arg in "$@"; do
  case "$arg" in alpha|beta|gamma|delta|eps) node=$arg ;; esac
done
printf '%s\n' "$node" >> "$CALLS_FILE"
[ -f "$REPLY_DIR/$node" ] || exit 255
cat "$REPLY_DIR/$node"
SH
chmod +x "$TMP/ssh"
printf 'HANDE_STATUS=OK\nHANDE_REASON=available\nPROBE_COMPLETE=1\n' > "$TMP/reply/alpha"
printf 'HANDE_STATUS=RETIRED\nHANDE_REASON=not-installed\nPROBE_COMPLETE=1\n' > "$TMP/reply/beta"
run() {
  env REPLY_DIR="$TMP/reply" CALLS_FILE="$TMP/calls" CCC_FLEET_NODES="$1" CCC_FLEET_SSH="$TMP/ssh" \
    CCC_FLEET_SELF=_never_ CCC_FLEET_HANDE_RETRY_DELAY=0 "${@:2}" \
    bash "$ROOT/scripts/fleet-hande-watch.sh"
}

# OK and RETIRED are both healthy: a node without the frontend is not paged.
set +e; out=$(run 'alpha beta'); rc=$?; set -e
assert test "$rc" = 0
assert grep -qx 'OK alpha channel=hande reason=available' <<< "$out"
assert grep -qx 'RETIRED beta channel=hande reason=not-installed' <<< "$out"

# DOWN (the 2026-10-27 outage shape) and notices-off both fail the sweep.
printf 'HANDE_STATUS=DOWN\nHANDE_REASON=no-process\nPROBE_COMPLETE=1\n' > "$TMP/reply/delta"
printf 'HANDE_STATUS=DEGRADED\nHANDE_REASON=notices-off\nPROBE_COMPLETE=1\n' > "$TMP/reply/eps"
set +e; out=$(run 'alpha delta eps'); rc=$?; set -e
assert test "$rc" = 1
assert grep -qx 'DOWN delta channel=hande reason=no-process' <<< "$out"
assert grep -qx 'DEGRADED eps channel=hande reason=notices-off' <<< "$out"

# A reply outside the known vocabulary is never echoed as a verdict.
printf 'HANDE_STATUS=OK\nHANDE_REASON=whatever\nPROBE_COMPLETE=1\n' > "$TMP/reply/eps"
set +e; out=$(run 'eps'); rc=$?; set -e
assert test "$rc" = 1
assert grep -qx 'UNVERIFIED eps channel=hande inspection=malformed-result' <<< "$out"

# Unreachable: bounded retries (cap 1 even when asked for more).
: > "$TMP/calls"
set +e; out=$(run 'gamma' CCC_FLEET_HANDE_RETRIES=8); rc=$?; set -e
assert test "$rc" = 1
assert grep -qx 'UNREACHABLE gamma channel=hande' <<< "$out"
assert test "$(grep -c '^gamma$' "$TMP/calls")" = 2

# No built-in node list: a standalone run without CCC_FLEET_NODES refuses.
set +e; out=$(env -u CCC_FLEET_NODES bash "$ROOT/scripts/fleet-hande-watch.sh" 2>&1); rc=$?; set -e
assert test "$rc" = 2
assert grep -q 'CCC_FLEET_NODES is required' <<< "$out"

assert python3 -m unittest "$ROOT/scripts/fleet_hande_probe_test.py"
printf 'PASS=%s FAIL=0\n' "$pass"
