#!/usr/bin/env bash
# Tests for fleet-bridge-watch — hermetic: ssh is stubbed, no node is contacted.
#
# The stub answers as a node would, so the caller's classification (OK / DOWN /
# BOOTPATH / UNREACHABLE) and its exit contract are exercised without a fleet.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SC="$ROOT/scripts/fleet-bridge-watch.sh"
pass=0; fail=0
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = "$2" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $3 (rc=$1 want=$2)"; fi; }

TMP_BASE="${TMPDIR:-$(dirname "$ROOT")}"; mkdir -p "$TMP_BASE"
TMP="$(mktemp -d "$TMP_BASE/fleet-watch-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

# Stub ssh: replies per node from $TMP/reply/<node>; missing file = unreachable.
# The shebang is resolved at run time: the script under test invokes the stub
# through `timeout`, which execs it directly, and `/usr/bin/env` does not exist
# on Termux — a hardcoded env shebang makes every case read as UNREACHABLE.
STUB="$TMP/ssh"
cat > "$STUB" <<STUBEOF
#!$(command -v bash)
# args: -o.. -o.. <node> sh -s   (probe arrives on stdin and is discarded)
cat >/dev/null
node=""
for a in "\$@"; do case "\$a" in -o|-*) ;; sh|-s) ;; *) node="\$a" ;; esac; done
f="$TMP/reply/\$node"
[ -f "\$f" ] || exit 255
cat "\$f"
STUBEOF
chmod +x "$STUB"
mkdir -p "$TMP/reply"

reply() { # <node> <runtime> <avail> <unit>
  printf 'RUNTIME=%s\nAVAIL=%s\nUNIT=%s\n' "$2" "$3" "$4" > "$TMP/reply/$1"
}

run() { # <nodes>
  OUT="$TMP/out"; RC=0
  CCC_FLEET_NODES="$1" CCC_FLEET_SSH="$STUB" CCC_FLEET_SELF=_never_ \
    bash "$SC" >"$OUT" 2>&1 || RC=$?
}

# ---- all healthy ----------------------------------------------------------
reply alpha /opt/ccc-node yes /opt/ccc-node
reply beta  /root/ccc-node yes /root/ccc-node
run "alpha beta"
okc "$RC" 0 "all healthy exits 0"
ok "reports OK per node"        'grep -q "^OK alpha (/opt/ccc-node)" "$OUT" && grep -q "^OK beta" "$OUT"'
ok "healthy run has no failures" '! grep -qE "^(DOWN|BOOTPATH|UNREACHABLE)" "$OUT"'

# ---- bridge down ----------------------------------------------------------
reply beta /root/ccc-node no /root/ccc-node
run "alpha beta"
okc "$RC" 1 "a down bridge exits nonzero"
ok "down node reported" 'grep -q "^DOWN beta" "$OUT"'
ok "healthy node still OK" 'grep -q "^OK alpha" "$OUT"'

# ---- boot-path mismatch: available, but the unit points elsewhere ----------
# The yukson 2026-07-27 shape. Availability alone would call this healthy.
reply beta /root/ccc-node yes /opt/ccc-node
run "beta"
okc "$RC" 1 "boot-path mismatch exits nonzero"
ok "mismatch names both paths" 'grep -q "^BOOTPATH beta unit=/opt/ccc-node runtime=/root/ccc-node" "$OUT"'
ok "mismatch is not reported as DOWN" '! grep -q "^DOWN beta" "$OUT"'

# ---- node with no unit: available, nothing to compare ---------------------
reply gamma /opt/ccc-node yes -
run "gamma"
okc "$RC" 0 "no unit declared still passes"
ok "no-unit node reported OK" 'grep -q "^OK gamma" "$OUT"'

# ---- unreachable ----------------------------------------------------------
run "ghost"
okc "$RC" 1 "unreachable node exits nonzero"
ok "unreachable reported" 'grep -q "^UNREACHABLE ghost" "$OUT"'

# ---- every node appears exactly once --------------------------------------
reply alpha /opt/ccc-node yes /opt/ccc-node
reply beta  /root/ccc-node yes /root/ccc-node
reply gamma /opt/ccc-node yes -
run "alpha beta gamma"
ok "one line per node" '[ "$(grep -cE "^(OK|DOWN|BOOTPATH|UNREACHABLE) " "$OUT")" = 3 ]'

# ---- doctor sweep (opt-in) ------------------------------------------------
# Only 3 of 12 nodes have agent-cron, so without this sweep the other 9 have no
# periodic harness-drift check at all.
reply_d() { # <node> <runtime> <avail> <unit> <doctor>
  printf 'RUNTIME=%s\nAVAIL=%s\nUNIT=%s\nDOCTOR=%s\n' "$2" "$3" "$4" "$5" > "$TMP/reply/$1"
}

reply_d alpha /opt/ccc-node yes /opt/ccc-node 0
run "alpha"
okc "$RC" 0 "clean doctor passes"
ok "clean doctor reports OK" 'grep -q "^OK alpha" "$OUT"'

reply_d beta /opt/ccc-node yes /opt/ccc-node 1
run "beta"
okc "$RC" 1 "doctor drift exits nonzero"
ok "drift names the exit code" 'grep -q "^DRIFT beta doctor_exit=1" "$OUT"'
ok "drift is not reported as DOWN" '! grep -q "^DOWN beta" "$OUT"'

# A node that cannot run doctor must not be reported as drifted.
reply_d gamma /opt/ccc-node yes /opt/ccc-node -
run "gamma"
okc "$RC" 0 "absent doctor is not a failure"
ok "absent doctor still reports OK" 'grep -q "^OK gamma" "$OUT"'

# Bridge problems outrank doctor: a node that is down is DOWN, not DRIFT.
reply_d delta /opt/ccc-node no /opt/ccc-node 1
run "delta"
ok "down bridge outranks doctor drift" 'grep -q "^DOWN delta" "$OUT" && ! grep -q "^DRIFT delta" "$OUT"'

# ---- no hardcoded per-node checkout paths in the script -------------------
# The whole point: paths come from the running process, never a baked table.
ok "no hardcoded node->path table" \
  '! grep -nE "^(check|[a-z]+) +(seoseo|yukson|sogyo|nosuk|dungae) +.*(/opt/ccc-node|/root/ccc-node)" "$SC"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
