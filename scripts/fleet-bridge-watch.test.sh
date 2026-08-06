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
    CCC_FLEET_RETRY_DELAY=0 \
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

# ---- gongmyoung dual-domain coherence (#980) -------------------------------
reply_dd() { # <node> <runtime> <avail> <unit> <doctor> <dualdomain>
  printf 'RUNTIME=%s\nAVAIL=%s\nUNIT=%s\nDOCTOR=%s\nDUALDOMAIN=%s\n' "$2" "$3" "$4" "$5" "$6" > "$TMP/reply/$1"
}

reply_dd gm /opt/ccc-node yes /opt/ccc-node 0 ok
run "gm"
okc "$RC" 0 "coherent dual-domain passes"
ok "coherent dual-domain reports OK" 'grep -q "^OK gm" "$OUT"'

reply_dd gm /opt/ccc-node yes /opt/ccc-node 0 "fail cron-bus-env-missing,linger=no"
run "gm"
okc "$RC" 1 "dual-domain incoherence exits nonzero"
ok "incoherence names the failing checks" 'grep -q "^DUALDOMAIN gm cron-bus-env-missing,linger=no" "$OUT"'
ok "incoherence is not reported as DOWN or OK" '! grep -q "^DOWN gm" "$OUT" && ! grep -q "^OK gm" "$OUT"'

# A single-domain node (DUALDOMAIN=-) and a root-less probe (skip) are not failures.
reply_dd plain /opt/ccc-node yes /opt/ccc-node 0 -
run "plain"
okc "$RC" 0 "single-domain node unaffected"
ok "single-domain node reports OK" 'grep -q "^OK plain" "$OUT"'
reply_dd noroot /opt/ccc-node yes /opt/ccc-node 0 "skip(non-root)"
run "noroot"
okc "$RC" 0 "root-less probe skip is not a failure"
ok "skipped dual-domain still reports OK" 'grep -q "^OK noroot" "$OUT"'

# The remote probe is a quoted heredoc — invisible to bash -n on the script
# itself. Extract and parse it as POSIX sh so a probe typo cannot ship.
probe_body="$TMP/probe-body.sh"
sed -n '/PROBE <<.PROBE_EOF.$/,/^PROBE_EOF$/p' "$SC" | sed '1d;$d' > "$probe_body"
ok "remote probe parses as POSIX sh" 'bash -n "$probe_body" && { ! command -v dash >/dev/null || dash -n "$probe_body"; }'

# ---- non-canonical runtime root (#842) ------------------------------------
# The seoseo 2026-08-01 shape, and the reason this check is separate from the
# boot-path comparison: unit and runtime AGREE, on a PR worktree. Every
# comparison-based check passes and the node reports healthy while serving code
# that never reached main.
reply beta /work/agent-codebench/ccc-node-pr833 yes /work/agent-codebench/ccc-node-pr833
run "beta"
okc "$RC" 1 "agreeing-but-noncanonical runtime exits nonzero"
ok "noncanonical runtime is named" \
  'grep -q "^NONCANONICAL beta runtime=/work/agent-codebench/ccc-node-pr833" "$OUT"'
ok "noncanonical is not reported as OK" '! grep -q "^OK beta" "$OUT"'
ok "noncanonical is not reported as BOOTPATH" '! grep -q "^BOOTPATH beta" "$OUT"'

# A work tree is created as a SIBLING of the real checkout, so it sits under a
# canonical parent and shares its prefix. A prefix or substring test would wave
# through exactly the shape this check exists to catch (bangtong, same day).
reply beta /root/ccc-node-840-terminal-stall yes /root/ccc-node-840-terminal-stall
run "beta"
okc "$RC" 1 "work tree under a canonical parent exits nonzero"
ok "sibling work tree is flagged, not prefix-matched" \
  'grep -q "^NONCANONICAL beta runtime=/root/ccc-node-840-terminal-stall" "$OUT"'

# Availability outranks it: a node that is down is DOWN, not NONCANONICAL.
reply beta /root/ccc-node-840-terminal-stall no /root/ccc-node-840-terminal-stall
run "beta"
ok "down bridge outranks noncanonical" \
  'grep -q "^DOWN beta" "$OUT" && ! grep -q "^NONCANONICAL beta" "$OUT"'

# The roots actually in use across the fleet must all stay clean.
reply alpha /opt/ccc-node yes /opt/ccc-node
reply beta  /root/ccc-node yes /root/ccc-node
reply gamma /home/gongmyoung/ccc-node yes /home/gongmyoung/ccc-node
reply delta /data/data/com.termux/files/home/ccc-node yes /data/data/com.termux/files/home/ccc-node
run "alpha beta gamma delta"
okc "$RC" 0 "every canonical fleet root passes"
ok "no canonical root is flagged" '! grep -q "^NONCANONICAL" "$OUT"'

# A node whose bridge is not running reports RUNTIME=-; that is DOWN's business,
# and it must not also be miscast as a non-canonical checkout.
reply beta - no -
run "beta"
ok "absent runtime is DOWN, not NONCANONICAL" \
  'grep -q "^DOWN beta" "$OUT" && ! grep -q "^NONCANONICAL beta" "$OUT"'

# The list is patterns, not paths: nothing on the WATCHER's filesystem may
# decide how a REMOTE node's checkout is classified. Without `set -f` the glob
# below collapses to the one sibling that happens to exist here, and every other
# node under the same pattern is reported non-canonical.
mkdir -p "$TMP/homes/ccc/ccc-node"
reply beta "$TMP/homes/gongmyoung/ccc-node" yes "$TMP/homes/gongmyoung/ccc-node"
OUT="$TMP/out"; RC=0
CCC_FLEET_NODES="beta" CCC_FLEET_SSH="$STUB" CCC_FLEET_SELF=_never_ \
  CCC_FLEET_CANONICAL_ROOTS="$TMP/homes/*/ccc-node" bash "$SC" >"$OUT" 2>&1 || RC=$?
okc "$RC" 0 "glob root is not expanded against the watcher's filesystem"
ok "unmaterialized sibling still matches the pattern" \
  'grep -q "^OK beta" "$OUT" && ! grep -q "^NONCANONICAL beta" "$OUT"'

# Operators can widen the list without editing the script.
reply beta /srv/ccc-node yes /srv/ccc-node
run "beta"
ok "unknown root flagged by default" 'grep -q "^NONCANONICAL beta" "$OUT"'
OUT="$TMP/out"; RC=0
CCC_FLEET_NODES="beta" CCC_FLEET_SSH="$STUB" CCC_FLEET_SELF=_never_ \
  CCC_FLEET_CANONICAL_ROOTS="/srv/ccc-node" bash "$SC" >"$OUT" 2>&1 || RC=$?
okc "$RC" 0 "CCC_FLEET_CANONICAL_ROOTS override accepted"
ok "overridden root reports OK" 'grep -q "^OK beta (/srv/ccc-node)" "$OUT"'

# ---- no hardcoded per-node checkout paths in the script -------------------
# The whole point: paths come from the running process, never a baked table.
ok "no hardcoded node->path table" \
  '! grep -nE "^(check|[a-z]+) +(seoseo|yukson|sogyo|nosuk|dungae) +.*(/opt/ccc-node|/root/ccc-node)" "$SC"'

# ---- transport retry (#972) ------------------------------------------------
# Flaky stub: fails while $TMP/flaky/<node> holds a positive counter, then
# answers from reply/<node>. Every invocation is logged to $TMP/calls so the
# tests can count attempts per node.
FLAKY="$TMP/ssh-flaky"
cat > "$FLAKY" <<FLAKYEOF
#!$(command -v bash)
cat >/dev/null
node=""
for a in "\$@"; do case "\$a" in -o|-*) ;; sh|-s) ;; *) node="\$a" ;; esac; done
printf '%s\n' "\$node" >> "$TMP/calls"
budget="$TMP/flaky/\$node"
left=0; [ -f "\$budget" ] && left=\$(cat "\$budget")
if [ "\$left" -gt 0 ] 2>/dev/null; then printf '%s\n' \$((left - 1)) > "\$budget"; exit 255; fi
f="$TMP/reply/\$node"
[ -f "\$f" ] || exit 255
cat "\$f"
FLAKYEOF
chmod +x "$FLAKY"
mkdir -p "$TMP/flaky"
: > "$TMP/calls"

run_flaky() { # <nodes> <retries>
  OUT="$TMP/out"; RC=0
  CCC_FLEET_NODES="$1" CCC_FLEET_SSH="$FLAKY" CCC_FLEET_SELF=_never_ \
    CCC_FLEET_RETRIES="$2" CCC_FLEET_RETRY_DELAY=0 \
    bash "$SC" >"$OUT" 2>&1 || RC=$?
}

# One blip, then healthy: the retry absorbs it and the node reports OK.
reply blip /opt/ccc-node yes /opt/ccc-node
printf '1\n' > "$TMP/flaky/blip"
run_flaky "blip" 2
okc "$RC" 0 "single blip recovered by retry exits 0"
ok "blip node reports OK, never UNREACHABLE" 'grep -q "^OK blip" "$OUT" && ! grep -q "^UNREACHABLE blip" "$OUT"'
ok "blip took exactly two attempts" '[ "$(grep -c "^blip$" "$TMP/calls")" = 2 ]'

# Persistent transport failure: still UNREACHABLE, reported once, after
# retries+1 attempts.
: > "$TMP/calls"
run_flaky "ghost2" 2
okc "$RC" 1 "persistent failure exits nonzero"
ok "persistent failure reported once" '[ "$(grep -c "^UNREACHABLE ghost2" "$OUT")" = 1 ]'
ok "persistent failure used every attempt" '[ "$(grep -c "^ghost2$" "$TMP/calls")" = 3 ]'

# DOWN is a real answer, not a transport failure: never retried.
: > "$TMP/calls"
reply sick /opt/ccc-node no /opt/ccc-node
run_flaky "sick" 2
okc "$RC" 1 "down node exits nonzero"
ok "down node reported, never retried" 'grep -q "^DOWN sick" "$OUT" && [ "$(grep -c "^sick$" "$TMP/calls")" = 1 ]'

# CCC_FLEET_RETRIES=0 keeps the single-attempt contract.
: > "$TMP/calls"
printf '1\n' > "$TMP/flaky/blip0"
reply blip0 /opt/ccc-node yes /opt/ccc-node
run_flaky "blip0" 0
okc "$RC" 1 "retries disabled still fails on one blip"
ok "retries disabled means exactly one attempt" '[ "$(grep -c "^blip0$" "$TMP/calls")" = 1 ] && grep -q "^UNREACHABLE blip0" "$OUT"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
