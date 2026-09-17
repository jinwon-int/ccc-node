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

# ---- degraded: alive and serving, but not restartable ----------------------
# The gongyung 2026-08-11 shape: the bot answered Telegram normally while
# start.sh reported "degraded" (running with no pid file). Reported as DOWN,
# that reads as an outage and trains the operator to discount the alert.
reply beta /root/ccc-node degraded /root/ccc-node
run "alpha beta"
okc "$RC" 1 "a degraded bridge still exits nonzero"
ok "degraded reported under its own name" 'grep -q "^DEGRADED beta runtime=/root/ccc-node" "$OUT"'
ok "degraded is not reported as DOWN"     '! grep -q "^DOWN beta" "$OUT"'
ok "degraded is not reported as OK"       '! grep -q "^OK beta" "$OUT"'
ok "healthy node unaffected by degraded peer" 'grep -q "^OK alpha" "$OUT"'

# A truly absent bridge must still be DOWN — the split must not soften it.
reply beta /root/ccc-node no /root/ccc-node
run "beta"
okc "$RC" 1 "absent bridge still exits nonzero"
ok "absent bridge is DOWN, not DEGRADED" 'grep -q "^DOWN beta" "$OUT" && ! grep -q "^DEGRADED beta" "$OUT"'

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
sed -n "/^read .* PROBE <<'PROBE_EOF'/,/^PROBE_EOF$/p" "$SC" | sed '1d;$d' > "$probe_body"
ok "remote probe parses as POSIX sh" '[ -s "$probe_body" ] && bash -n "$probe_body" && { ! command -v dash >/dev/null || dash -n "$probe_body"; }'

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

# ---- the doctor is run without the bridge's CLI-path environment -----------
# Not an oversight: carrying CCC_CODEX_CLI_PATH across points the doctor's
# Codex --version probe at the ccc-codex memory wrapper, which times out and
# turns every healthy codex node into a false DRIFT (daegyo, 2026-08-11). The
# piri false positive this would have fixed is tracked separately, doctor-side.
ok "doctor call does not inject bridge CLI paths" \
  '! grep -q "env \$cli_env" "$SC"'
ok "probe does not read the serving process environ" \
  '! grep -q "environ" "$SC"'

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

# Execute the remote body, not only canned SSH replies: a non-root SSH account
# must inspect a root-owned Danso/CCC runtime with non-interactive sudo.
mkdir -p "$TMP/probe-bin" "$TMP/probe-repo/scripts" "$TMP/probe-home"
touch "$TMP/probe-repo/scripts/ccc-doctor.sh"
chmod +x "$TMP/probe-repo/scripts/ccc-doctor.sh"
cat > "$TMP/probe-bin/ps" <<EOF
#!$(command -v sh)
case "\$*" in *uid=,pid=*) printf '0 42 ' ;; *) printf '0 ' ;; esac
echo "$TMP/probe-repo/bridge/venv/bin/python -m telegram_bot --path $TMP/probe-home"
EOF
cat > "$TMP/probe-bin/id" <<EOF
#!$(command -v sh)
case "\$1" in -u) echo 1000 ;; -nu) echo root ;; *) exit 0 ;; esac
EOF
cat > "$TMP/probe-bin/sudo" <<EOF
#!$(command -v sh)
printf '%s\n' "\$*" >> "$TMP/sudo-calls"
[ "\$*" != "" ] || exit 99
[ "\${PROBE_DENIED:-0}" = 0 ] || exit 1
case "\$*" in *--status) printf '%s\n' "Bot status: \${PROBE_STATUS:-available}" ;; *) exit "\${PROBE_DOCTOR:-0}" ;; esac
EOF
cat > "$TMP/probe-bin/su" <<EOF
#!$(command -v sh)
echo 'unexpected su' >> "$TMP/su-calls"
exit 1
EOF
chmod +x "$TMP/probe-bin/"*
# Simulate the historical account/home existing; root runtime must not inspect
# or require its now-retired user-service/cron layout.
sed "s#/home/gongmyoung#$TMP/probe-home#g" "$probe_body" > "$TMP/owner-probe.sh"
PATH="$TMP/probe-bin:$PATH" CCC_FLEET_DOCTOR=1 sh "$TMP/owner-probe.sh" > "$TMP/owner-out"
ok "root-owned runtime is available through noninteractive sudo" 'grep -q "^AVAIL=yes$" "$TMP/owner-out"'
ok "doctor runs as the actual runtime owner" 'grep -q "^DOCTOR=0$" "$TMP/owner-out" && grep -q -- "-n -H -u root -- env CCC_DOCTOR_CLAUDE_DIR=$TMP/probe-home/.claude timeout 60 bash" "$TMP/sudo-calls"'
ok "status preserves runtime project path" 'grep -q -- "-n -H -u root -- bash $TMP/probe-repo/bridge/start.sh --path $TMP/probe-home --status" "$TMP/sudo-calls"'
ok "root runtime does not require retired user service" 'grep -q "^DUALDOMAIN=-$" "$TMP/owner-out"'
ok "unprivileged caller never attempts interactive su" '[ ! -e "$TMP/su-calls" ]'
PATH="$TMP/probe-bin:$PATH" PROBE_STATUS=unavailable sh "$TMP/owner-probe.sh" > "$TMP/owner-out"
ok "confirmed unavailable status remains down" 'grep -q "^AVAIL=no$" "$TMP/owner-out"'
PATH="$TMP/probe-bin:$PATH" PROBE_DENIED=1 sh "$TMP/owner-probe.sh" > "$TMP/owner-out"
ok "permission failure is unverified, not down" 'grep -q "^AVAIL=unverified$" "$TMP/owner-out"'
reply beta /opt/ccc-node unverified /opt/ccc-node
run beta
okc "$RC" 1 "unverified inspection still alerts"
ok "unverified has distinct failure classification" 'grep -q "^UNVERIFIED beta runtime=/opt/ccc-node$" "$OUT" && ! grep -q "^DOWN beta" "$OUT"'

# ---- activated Termux prepared runtime (#1527, #1761) ----------------------
# The daegyo 2026-09-16 shape: the self-update activated `<prep>/source` with
# the venv in `<prep>/job`. The bridge answered Telegram, yet the watch paged
# DOWN because the worker's interpreter path carries no /bridge/ token, and even
# with the root known the source is not a canonical checkout root.
reply_p() { # <node> <runtime> <avail> <unit> <prepared-job>
  printf 'RUNTIME=%s\nAVAIL=%s\nUNIT=%s\nPREPARED=%s\n' "$2" "$3" "$4" "$5" > "$TMP/reply/$1"
}
TX=/data/data/com.termux/files/home
reply_p dg "$TX/.ccc-node/preparations/self-update-4fd1575-20260916/source" yes - "$TX/.ccc-node/preparations/self-update-4fd1575-20260916/job"
run "dg"
okc "$RC" 0 "activated prepared runtime passes"
ok "prepared launch is OK and names the job" \
  'grep -q "^OK dg ($TX/.ccc-node/preparations/self-update-4fd1575-20260916/source, prepared:self-update-4fd1575-20260916)$" "$OUT"'
ok "prepared launch is not NONCANONICAL" '! grep -q "^NONCANONICAL dg" "$OUT"'

# The job must vouch for THIS root: a completed job next to some other serving
# checkout is a work tree with a receipt nearby, not an activated runtime.
reply_p dg /work/agent-codebench/ccc-node-pr833 yes - "$TX/.ccc-node/preparations/self-update-4fd1575-20260916/job"
run "dg"
okc "$RC" 1 "prepared job does not vouch for a foreign root"
ok "foreign root with a job nearby is NONCANONICAL" 'grep -q "^NONCANONICAL dg runtime=/work/agent-codebench/ccc-node-pr833" "$OUT"'

# A preparation source without a completed job (PREPARED=-) is a plain
# non-canonical checkout: the probe withholds the job when the receipt is not
# ready, and the caller must not infer readiness from the path alone.
reply "dg" "$TX/.ccc-node/preparations/self-update-4fd1575-20260916/source" yes -
run "dg"
okc "$RC" 1 "preparation source without a ready job exits nonzero"
ok "unready preparation is NONCANONICAL" 'grep -q "^NONCANONICAL dg runtime=$TX/.ccc-node/preparations/self-update-4fd1575-20260916/source" "$OUT"'

# Preparation roots are a pattern list like the canonical roots.
reply_p dg /srv/.ccc-node/preparations/self-update-x/source yes - /srv/.ccc-node/preparations/self-update-x/job
run "dg"
okc "$RC" 1 "unknown preparation root flagged by default"
OUT="$TMP/out"; RC=0
CCC_FLEET_NODES="dg" CCC_FLEET_SSH="$STUB" CCC_FLEET_SELF=_never_ \
  CCC_FLEET_PREPARED_ROOTS="/srv/.ccc-node/preparations" bash "$SC" >"$OUT" 2>&1 || RC=$?
okc "$RC" 0 "CCC_FLEET_PREPARED_ROOTS override accepted"
ok "overridden preparation root reports OK" 'grep -q "^OK dg (/srv/.ccc-node/preparations/self-update-x/source, prepared:self-update-x)$" "$OUT"'

# Availability still outranks it: a down prepared bridge is DOWN.
reply_p dg "$TX/.ccc-node/preparations/self-update-4fd1575-20260916/source" no - "$TX/.ccc-node/preparations/self-update-4fd1575-20260916/job"
run "dg"
ok "down prepared bridge is DOWN, not OK" 'grep -q "^DOWN dg" "$OUT" && ! grep -q "^OK dg" "$OUT"'

# A canonical checkout is unaffected by the new line, and older probes that
# emit no PREPARED line keep their verdict.
reply_p alpha /opt/ccc-node yes /opt/ccc-node -
run "alpha"
okc "$RC" 0 "canonical root with PREPARED=- passes"
ok "canonical root has no prepared tag" 'grep -q "^OK alpha (/opt/ccc-node)$" "$OUT"'

# Execute the remote body against the daegyo process shape: the worker line
# has no /bridge/ token; the supervisor names the source and the job.
mkdir -p "$TMP/prep/source/bridge" "$TMP/prep/job/runtime/bin" "$TMP/prep-bin"
printf '{"schema": "ccc.termux-preparation.v1", "status": "ready"}\n' > "$TMP/prep/job/receipt.json"
cat > "$TMP/prep-bin/ps" <<EOF
#!$(command -v sh)
echo "0 bash $TMP/prep/source/bridge/start.sh --path $TMP/probe-home --_daemon_supervisor --prepared-runtime $TMP/prep/job"
case "\$*" in *uid=,pid=*) printf '0 42 ' ;; *) printf '0 ' ;; esac
echo "$TMP/prep/job/runtime/bin/python -m telegram_bot --path $TMP/probe-home"
EOF
chmod +x "$TMP/prep-bin/ps"
: > "$TMP/sudo-calls"
PATH="$TMP/prep-bin:$TMP/probe-bin:$PATH" sh "$TMP/owner-probe.sh" > "$TMP/prep-out"
ok "prepared worker resolves the root from its supervisor" 'grep -q "^RUNTIME=$TMP/prep/source$" "$TMP/prep-out"'
ok "prepared worker is available, not DOWN"                'grep -q "^AVAIL=yes$" "$TMP/prep-out"'
ok "probe reports the completed job"                       'grep -q "^PREPARED=$TMP/prep/job$" "$TMP/prep-out"'
ok "status runs the serving source start.sh with the worker path" \
  'grep -q -- "-n -H -u root -- bash $TMP/prep/source/bridge/start.sh --path $TMP/probe-home --status" "$TMP/sudo-calls"'

# Receipt not ready: the root is still found (the bridge is up), the job is not
# vouched for, and the caller then classifies the source as non-canonical.
printf '{"schema": "ccc.termux-preparation.v1", "status": "failed"}\n' > "$TMP/prep/job/receipt.json"
PATH="$TMP/prep-bin:$TMP/probe-bin:$PATH" sh "$TMP/owner-probe.sh" > "$TMP/prep-out"
ok "unready receipt withholds the job but keeps the root" \
  'grep -q "^PREPARED=-$" "$TMP/prep-out" && grep -q "^RUNTIME=$TMP/prep/source$" "$TMP/prep-out" && grep -q "^AVAIL=yes$" "$TMP/prep-out"'

# A plain worker (interpreter under <root>/bridge/venv) still reads the root
# from its own line and reports PREPARED=- when no supervisor names a job.
PATH="$TMP/probe-bin:$PATH" sh "$TMP/owner-probe.sh" > "$TMP/plain-out"
ok "plain worker still reports its own root" 'grep -q "^RUNTIME=$TMP/probe-repo$" "$TMP/plain-out" && grep -q "^PREPARED=-$" "$TMP/plain-out"'

# ---- Danso nodes (danso #118 4-a) -----------------------------------------
# A migrated node reports a binary path as its runtime, plus the generation of
# that binary. The caller must not judge it with the ccc-node checkout globs.
dreply() { # <node> <runtime-exe> <avail> <unit-exe> <generation>
  printf 'KIND=danso\nRUNTIME=%s\nAVAIL=%s\nUNIT=%s\nGENERATION=%s\nPREPARED=-\n' \
    "$2" "$3" "$4" "$5" > "$TMP/reply/$1"
}

GEN=aaaaaaaaaaaabbbbbbbbbbbbccccccccccccddddddddddddeeeeeeeeeeeeffff
dreply delta /usr/local/bin/danso yes /usr/local/bin/danso "$GEN"
run "delta"
okc "$RC" 0 "a healthy danso node exits 0"
ok "danso node is not judged by the ccc checkout globs" '! grep -q "^NONCANONICAL" "$OUT"'
ok "danso OK line carries the generation" 'grep -q "^OK delta (/usr/local/bin/danso, generation:aaaaaaaaaaaa)" "$OUT"'

# The same runtime under a ccc node WOULD be non-canonical: proves the branch
# is what spares it, not that the check silently stopped working for everyone.
reply epsilon /usr/local/bin/danso yes /usr/local/bin/danso
run "epsilon"
okc "$RC" 1 "the same path on a ccc node is still non-canonical"
ok "ccc node still reports NONCANONICAL" 'grep -q "^NONCANONICAL epsilon" "$OUT"'

# Opt-in allowlist: empty by default, enforced when set.
dreply zeta /work/build/danso yes /work/build/danso "$GEN"
run "zeta"
okc "$RC" 0 "no danso allowlist means no canonicality verdict"
CANON_SAVE="${CCC_FLEET_CANONICAL_DANSO_EXES:-}"
OUT="$TMP/out"; RC=0
CCC_FLEET_NODES="zeta" CCC_FLEET_SSH="$STUB" CCC_FLEET_SELF=_never_ \
  CCC_FLEET_RETRY_DELAY=0 CCC_FLEET_CANONICAL_DANSO_EXES="/usr/local/bin/danso" \
  bash "$SC" >"$OUT" 2>&1 || RC=$?
okc "$RC" 1 "an allowlist, once set, rejects an unlisted binary"
ok "unlisted danso binary reported" 'grep -q "^NONCANONICAL zeta runtime=/work/build/danso" "$OUT"'
export CCC_FLEET_CANONICAL_DANSO_EXES="$CANON_SAVE"

# Availability states survive the danso path unchanged.
dreply eta /usr/local/bin/danso degraded /usr/local/bin/danso "$GEN"
run "eta"
ok "danso degraded is DEGRADED, not DOWN" 'grep -q "^DEGRADED eta" "$OUT"'
dreply theta /usr/local/bin/danso unverified /usr/local/bin/danso "$GEN"
run "theta"
ok "danso unverified is UNVERIFIED, not DOWN" 'grep -q "^UNVERIFIED theta" "$OUT"'

# Boot path still compares: a unit pointing at a different binary is the same
# "next reboot serves the wrong thing" failure as on a ccc node.
dreply iota /usr/local/bin/danso yes /opt/danso/bin/danso "$GEN"
run "iota"
okc "$RC" 1 "a danso unit pointing elsewhere is reported"
ok "danso bootpath mismatch reported" 'grep -q "^BOOTPATH iota unit=/opt/danso/bin/danso runtime=/usr/local/bin/danso" "$OUT"'

# An older probe emits no KIND line; it must still be treated as a ccc node.
reply kappa /opt/ccc-node yes /opt/ccc-node
run "kappa"
ok "a probe without KIND is still judged as ccc" 'grep -q "^OK kappa (/opt/ccc-node)" "$OUT"'

# ---- Danso probe body ------------------------------------------------------
# Stub ps so the probe sees a danso service and no ccc bridge, and stub the
# danso binary so its exit code drives AVAIL. The uid matches the `id -u` stub
# (1000) so the probe runs the binary directly: that is the path whose JSON
# parsing and exit-code mapping are under test. The privileged routing is
# exercised separately below — a stub that answers for the binary would prove
# nothing about either.
mkdir -p "$TMP/danso-bin"
cat > "$TMP/danso-bin/ps" <<EOF
#!$(command -v sh)
echo "1000 $TMP/danso-bin/danso service run --data-dir $TMP/probe-home/.danso/telegram"
EOF
cat > "$TMP/danso-bin/danso" <<EOF
#!$(command -v sh)
printf '%s' '{"state":"available","pid":42,"runtime_generation":{"schema":"danso.runtime-generation.v1","binary_sha256":"$GEN","version":"0.1.0","exe_path":"/usr/local/bin/danso","observed_at":"2026-09-16T00:00:00Z"},"mutations":{}}'
exit \${DANSO_RC:-0}
EOF
chmod +x "$TMP/danso-bin/"*
PATH="$TMP/danso-bin:$TMP/probe-bin:$PATH" sh "$TMP/owner-probe.sh" > "$TMP/danso-out"
ok "danso service is seen, not reported DOWN" 'grep -q "^AVAIL=yes$" "$TMP/danso-out"'
ok "probe marks the node kind"               'grep -q "^KIND=danso$" "$TMP/danso-out"'
ok "runtime comes from the json exe_path"    'grep -q "^RUNTIME=/usr/local/bin/danso$" "$TMP/danso-out"'
ok "generation is reported"                  "grep -q '^GENERATION=$GEN\$' \"\$TMP/danso-out\""
ok "danso node reports no ccc doctor/dualdomain" \
  'grep -q "^DOCTOR=-$" "$TMP/danso-out" && grep -q "^DUALDOMAIN=-$" "$TMP/danso-out"'

for rc_case in "1 degraded" "2 no" "3 unverified"; do
  set -- $rc_case
  PATH="$TMP/danso-bin:$TMP/probe-bin:$PATH" DANSO_RC="$1" sh "$TMP/owner-probe.sh" > "$TMP/danso-out"
  ok "danso exit $1 maps to AVAIL=$2" "grep -q '^AVAIL=$2\$' \"\$TMP/danso-out\""
done
# Any unexpected code is a failed inspection, never evidence of a down service.
PATH="$TMP/danso-bin:$TMP/probe-bin:$PATH" DANSO_RC=77 sh "$TMP/owner-probe.sh" > "$TMP/danso-out"
ok "an unknown danso exit code is unverified, not down" 'grep -q "^AVAIL=unverified$" "$TMP/danso-out"'

# When the JSON carries no exe_path the process path is used rather than
# reporting nothing: the node is serving and the operator needs to know from
# where, even if the report was truncated.
cat > "$TMP/danso-bin/danso" <<EOF
#!$(command -v sh)
printf '%s' '{"state":"available","mutations":{}}'
EOF
chmod +x "$TMP/danso-bin/danso"
PATH="$TMP/danso-bin:$TMP/probe-bin:$PATH" sh "$TMP/owner-probe.sh" > "$TMP/danso-out"
ok "a json without a generation falls back to the process path" \
  'grep -q "^RUNTIME=$TMP/danso-bin/danso$" "$TMP/danso-out" && grep -q "^GENERATION=-$" "$TMP/danso-out"'

# Root-owned danso service reached from an unprivileged account: the probe must
# use the same noninteractive sudo path the ccc branch uses, and never su.
mkdir -p "$TMP/danso-root-bin"
cat > "$TMP/danso-root-bin/ps" <<EOF
#!$(command -v sh)
echo "0 /usr/local/bin/danso service run --data-dir $TMP/probe-home/.danso/telegram"
EOF
cat > "$TMP/danso-root-bin/sudo" <<EOF
#!$(command -v sh)
printf '%s\n' "\$*" >> "$TMP/danso-sudo-calls"
printf '%s' '{"state":"available","runtime_generation":{"binary_sha256":"$GEN","exe_path":"/usr/local/bin/danso"},"mutations":{}}'
exit 0
EOF
chmod +x "$TMP/danso-root-bin/"*
: > "$TMP/danso-sudo-calls"
PATH="$TMP/danso-root-bin:$TMP/danso-bin:$TMP/probe-bin:$PATH" sh "$TMP/owner-probe.sh" > "$TMP/danso-root-out"
ok "root-owned danso service is reached through noninteractive sudo" \
  'grep -q -- "-n -H -u root -- /usr/local/bin/danso service status --data-dir $TMP/probe-home/.danso/telegram --json" "$TMP/danso-sudo-calls"'
ok "root-owned danso service is available" 'grep -q "^AVAIL=yes$" "$TMP/danso-root-out"'
ok "no su fallback was taken" '[ ! -s "$TMP/su-calls" ] || ! grep -q danso "$TMP/su-calls"'

# The state root has to come from the command line the probe already has.
# `danso service status` resolves it from `--data-dir` or the environment, and
# this probe runs a fresh process with the *watch's* environment — the unit's
# `Environment=` applies only to the service systemd itself started. A stub
# that ignores its arguments cannot show this, so this one refuses to answer
# without the directory, exactly as the real binary does.
# Measured on yukson 2026-09-17 (danso #118): a serving node reported AVAIL=no.
mkdir -p "$TMP/danso-strict-bin"
cat > "$TMP/danso-strict-bin/ps" <<EOF
#!$(command -v sh)
echo "1000 $TMP/danso-strict-bin/danso service run --data-dir $TMP/probe-home/.danso/telegram"
EOF
cat > "$TMP/danso-strict-bin/danso" <<EOF
#!$(command -v sh)
want="$TMP/probe-home/.danso/telegram"
got=""
while [ \$# -gt 0 ]; do
  case "\$1" in --data-dir) got=\$2; shift 2 ;; *) shift ;; esac
done
if [ "\$got" != "\$want" ]; then
  # What the real binary does when it resolves a state root nobody is serving.
  printf '%s' '{"state":"unavailable","mutations":{}}'
  exit 2
fi
printf '%s' '{"state":"available","runtime_generation":{"binary_sha256":"$GEN","exe_path":"/usr/local/bin/danso"},"mutations":{}}'
exit 0
EOF
chmod +x "$TMP/danso-strict-bin/"*
PATH="$TMP/danso-strict-bin:$TMP/probe-bin:$PATH" sh "$TMP/owner-probe.sh" > "$TMP/danso-strict-out"
ok "the probe passes the state root it read from the command line" \
  'grep -q "^AVAIL=yes$" "$TMP/danso-strict-out"'
ok "and the generation comes back with it" \
  "grep -q '^GENERATION=$GEN\$' \"\$TMP/danso-strict-out\""

# A service started without the argument resolves the directory from its own
# environment; the probe must not invent one.
mkdir -p "$TMP/danso-noarg-bin"
cat > "$TMP/danso-noarg-bin/ps" <<EOF
#!$(command -v sh)
echo "1000 $TMP/danso-noarg-bin/danso service run"
EOF
cat > "$TMP/danso-noarg-bin/danso" <<EOF
#!$(command -v sh)
for a in "\$@"; do
  [ "\$a" = "--data-dir" ] && { echo "unexpected --data-dir" >&2; exit 90; }
done
printf '%s' '{"state":"available","runtime_generation":{"binary_sha256":"$GEN","exe_path":"/usr/local/bin/danso"},"mutations":{}}'
EOF
chmod +x "$TMP/danso-noarg-bin/"*
PATH="$TMP/danso-noarg-bin:$TMP/probe-bin:$PATH" sh "$TMP/owner-probe.sh" > "$TMP/danso-noarg-out"
ok "a service without --data-dir is asked without one" \
  'grep -q "^AVAIL=yes$" "$TMP/danso-noarg-out"'

# No ccc bridge and no danso service: still DOWN, as before.
mkdir -p "$TMP/empty-bin"
cat > "$TMP/empty-bin/ps" <<EOF
#!$(command -v sh)
exit 0
EOF
chmod +x "$TMP/empty-bin/ps"
PATH="$TMP/empty-bin:$TMP/probe-bin:$PATH" sh "$TMP/owner-probe.sh" > "$TMP/empty-out"
ok "a node with neither runtime is still DOWN" \
  'grep -q "^AVAIL=no$" "$TMP/empty-out" && grep -q "^RUNTIME=-$" "$TMP/empty-out" && ! grep -q "^KIND=danso$" "$TMP/empty-out"'

# ---- runtime owner is not the service manager ------------------------------
# Execute the POSIX probe: a system service may use User=gongmyoung without
# needing a user unit, user bus, or linger. Keep legacy user-service coverage.
mkdir -p "$TMP/domain-bin" "$TMP/domain-repo/.git" "$TMP/domain-home" "$TMP/domain-proc/42"
cat > "$TMP/domain-bin/ps" <<EOF
#!$(command -v sh)
case "\$*" in *uid=,pid=*) printf '1000 42 ' ;; *) printf '1000 ' ;; esac
if [ "\${UNKNOWN_ROOT:-0}" = 1 ]; then
  echo '/unrecognized/python -m telegram_bot --path $TMP/domain-home'
else
  echo '$TMP/domain-repo/bridge/venv/bin/python -m telegram_bot --path $TMP/domain-home'
fi
EOF
cat > "$TMP/domain-bin/id" <<EOF
#!$(command -v sh)
case "\$1" in -nu) echo gongmyoung ;; -u) echo 0 ;; *) exit 0 ;; esac
EOF
cat > "$TMP/domain-bin/su" <<EOF
#!$(command -v sh)
printf '%s\n' "\$*" >> '$TMP/domain-calls'
case "\$*" in *--status) echo 'Bot status: available' ;; *) sh -c "\$4" ;; esac
EOF
cat > "$TMP/domain-bin/crontab" <<EOF
#!$(command -v sh)
echo '0 0 * * * ccc-self-update'
[ "\${USER_BUS:-0}" = 0 ] || echo 'XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus'
EOF
cat > "$TMP/domain-bin/systemctl" <<EOF
#!$(command -v sh)
printf '%s\n' "\$*" >> '$TMP/domain-calls'
case "\$*" in
  *show*User*) echo "\${UNIT_USER:-gongmyoung}" ;;
  *is-active*) echo "\${UNIT_STATE:-active}"; [ "\${UNIT_STATE:-active}" = active ] ;;
  *) exit 1 ;;
esac
EOF
cat > "$TMP/domain-bin/loginctl" <<EOF
#!$(command -v sh)
echo 'Linger=yes'
EOF
cat > "$TMP/domain-bin/stat" <<EOF
#!$(command -v sh)
echo gongmyoung
EOF
cat > "$TMP/domain-bin/git" <<EOF
#!$(command -v sh)
case "\$*" in
  *status*) [ "\${GIT_FAILURE:-0}" = 0 ] || exit 128 ;;
  *rev-parse*) echo main ;;
esac
EOF
cat > "$TMP/domain-bin/find" <<EOF
#!$(command -v sh)
case "\$*" in
  *'-user root'*) echo '$TMP/domain-repo/.git/refs/root-owned-but-accessible' ;;
  *) [ "\${GIT_DENIED:-0}" = 0 ] || echo '$TMP/domain-repo/.git/objects/unwritable' ;;
esac
[ "\${FIND_FAILURE:-0}" = 0 ] || exit 1
EOF
chmod +x "$TMP/domain-bin/"*
sed -e "s#/home/gongmyoung#$TMP/domain-home#g" \
    -e "s#/opt/ccc-node#$TMP/domain-repo#g" \
    -e "s#/proc/#$TMP/domain-proc/#g" "$probe_body" > "$TMP/domain-probe.sh"
domain_probe() {
  : > "$TMP/domain-calls"
  env PATH="$TMP/domain-bin:$PATH" CCC_FLEET_DOCTOR=1 "$@" sh "$TMP/domain-probe.sh" > "$TMP/domain-out"
}
printf '0::/system.slice/ccc-telegram-bridge.service\n' > "$TMP/domain-proc/42/cgroup"
domain_probe
ok "system service with user owner is coherent without user bus" 'grep -q "^DUALDOMAIN=ok$" "$TMP/domain-out"'
ok "system service does not inspect user unit" '! grep -q -- "--user is-active" "$TMP/domain-calls"'
ok "accessible root-owned git objects are not drift" '! grep -q "git-root-owned-objects" "$TMP/domain-out"'
domain_probe UNIT_STATE=inactive
ok "inactive system manager is reported distinctly" 'grep -q "system-unit=inactive" "$TMP/domain-out"'
domain_probe UNIT_USER=root
ok "system unit owner mismatch is still drift" 'grep -q "system-unit-owner=root" "$TMP/domain-out"'
domain_probe GIT_DENIED=1
ok "unwritable git directory is still drift" 'grep -q "git-access-denied" "$TMP/domain-out"'
domain_probe FIND_FAILURE=1
ok "git inspection error is not a clean tree" 'grep -q "git-access-unverified" "$TMP/domain-out"'
domain_probe GIT_FAILURE=1
ok "git status failure is not a clean tree" 'grep -q "repo-status-unverified" "$TMP/domain-out"'
printf '0::/user.slice/user-1000.slice/user@1000.service/app.slice/ccc-telegram-bridge.service\n' > "$TMP/domain-proc/42/cgroup"
domain_probe USER_BUS=1
ok "legacy user service remains supported" 'grep -q "^DUALDOMAIN=ok$" "$TMP/domain-out"'
domain_probe
ok "legacy user service still requires user bus" 'grep -q "cron-bus-env-missing" "$TMP/domain-out"'
printf '0::/system.slice/unrelated.service\n' > "$TMP/domain-proc/42/cgroup"
domain_probe
ok "unknown service domain is unverified" 'grep -q "service-domain=unverified" "$TMP/domain-out"'
domain_probe UNKNOWN_ROOT=1
ok "visible process with unknown root is not reported DOWN" 'grep -q "^AVAIL=unverified$" "$TMP/domain-out" && ! grep -q "^AVAIL=no$" "$TMP/domain-out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
