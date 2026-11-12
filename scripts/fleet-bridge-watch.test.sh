#!/usr/bin/env bash
# Tests for fleet-bridge-watch — hermetic: ssh is stubbed, no node is contacted.
#
# The stub answers as a node would, so the caller's classification (OK / DOWN /
# BOOTPATH / UNREACHABLE) and its exit contract are exercised without a fleet.
set -uo pipefail
export CCC_FLEET_MATRIX=0
export CCC_FLEET_HANDE=0
# Degraded rechecks re-probe immediately in tests; the real default waits 75s.
export CCC_FLEET_DEGRADED_RECHECK_DELAY=0
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SC="$ROOT/scripts/fleet-bridge-watch.sh"
pass=0; fail=0
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = "$2" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $3 (rc=$1 want=$2)"; fi; }

TMP_BASE="${TMPDIR:-$(dirname "$ROOT")}"; mkdir -p "$TMP_BASE"
TMP="$(mktemp -d "$TMP_BASE/fleet-watch-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
# Doctor-sweep memory streaks (#2659) never touch the real ~/.claude/state.
MEMSTATE="$TMP/mem-state/fleet-watch/memory-degraded.tsv"
export CCC_FLEET_MEMORY_STATE="$MEMSTATE"

# Stub ssh: replies per node from $TMP/reply/<node>; missing file = unreachable.
# The shebang is resolved at run time: the script under test invokes the stub
# through `timeout`, which execs it directly, and `/usr/bin/env` does not exist
# on Termux — a hardcoded env shebang makes every case read as UNREACHABLE.
STUB="$TMP/ssh"
cat > "$STUB" <<STUBEOF
#!$(command -v bash)
# args: -o.. -o.. <node> sh -s   (probe arrives on stdin; its first line — the
# CCC_FLEET_DOCTOR flag — is logged per node, the rest discarded)
node=""
for a in "\$@"; do case "\$a" in -o|-*) ;; sh|-s) ;; *) node="\$a" ;; esac; done
IFS= read -r first_line || true
IFS= read -r second_line || true
cat >/dev/null
printf '%s\n' "\$first_line" >> "$TMP/reply/\$node.flags"
printf '%s\n' "\$second_line" >> "$TMP/reply/\$node.acct"
f="$TMP/reply/\$node"
# Second and later answers come from <node>.2 when it exists (a degraded
# recheck); <node>.2.unreachable makes every later answer a transport failure.
if [ -f "\$f.seen" ]; then
  [ ! -f "\$f.2.unreachable" ] || exit 255
  [ ! -f "\$f.2" ] || f="\$f.2"
elif [ -f "\$f" ]; then
  : > "\$f.seen"
fi
[ -f "\$f" ] || exit 255
cat "\$f"
[ -f "\$f.incomplete" ] || echo PROBE_COMPLETE=1
[ ! -f "\$f.rc" ] || exit "\$(cat "\$f.rc")"
exit 0
STUBEOF
chmod +x "$STUB"
mkdir -p "$TMP/reply"

reply() { # <node> <runtime> <avail> <unit>
  printf 'RUNTIME=%s\nAVAIL=%s\nUNIT=%s\n' "$2" "$3" "$4" > "$TMP/reply/$1"
}

run() { # <nodes>
  OUT="$TMP/out"; RC=0
  rm -f "$TMP"/reply/*.seen "$TMP"/reply/*.flags "$TMP"/reply/*.acct
  CCC_FLEET_NODES="$1" CCC_FLEET_SSH="$STUB" CCC_FLEET_SELF=_never_ \
    CCC_FLEET_RETRY_DELAY=0 CCC_FLEET_MATRIX=0 CCC_FLEET_HANDE=0 \
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

# ---- retired on purpose (DOC-4014, #2343) ----------------------------------
# The node answered AVAIL=retired: no bridge, but a retirement marker. Not a
# failure, and not OK either — reported under its own name with the marker.
reply beta - retired -
echo "RETIRED=telegram-retire-20261016" >> "$TMP/reply/beta"
run "alpha beta"
okc "$RC" 0 "a retired bridge does not fail the run"
ok "retired node reported under its own name, with its marker" \
  'grep -q "^RETIRED beta marker=telegram-retire-20261016$" "$OUT" && ! grep -qE "^(OK|DOWN|UNVERIFIED) beta" "$OUT"'
# The canonical frontend list (#2348) is named as the marker source.
reply beta - retired -
echo "RETIRED=frontends" >> "$TMP/reply/beta"
run "alpha beta"
okc "$RC" 0 "a node retired by its frontend list does not fail the run"
ok "frontend-list retirement is reported as marker=frontends" \
  'grep -q "^RETIRED beta marker=frontends$" "$OUT" && ! grep -qE "^(OK|DOWN|UNVERIFIED) beta" "$OUT"'
ok "healthy node beside it still OK" 'grep -q "^OK alpha" "$OUT"'

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

# ---- degraded recheck (#2047) ----------------------------------------------
# A health-snapshot blip heals within one bridge watchdog cycle. The 2026-09-29
# shape: Telegram getMe missed once, the probe read "degraded" five seconds
# later, and the bridge was healthy a minute after that.
rm -f "$TMP"/reply/*.2 "$TMP"/reply/*.2.unreachable
reply beta /root/ccc-node degraded /root/ccc-node
echo "AVAIL_REASON=health:telegram-degraded" >> "$TMP/reply/beta"
reply alpha /opt/ccc-node yes /opt/ccc-node
cp "$TMP/reply/alpha" "$TMP/reply/beta.2"; sed -i 's#/opt/ccc-node#/root/ccc-node#g' "$TMP/reply/beta.2"
run "alpha beta"
okc "$RC" 0 "a degraded blip that heals on recheck does not page"
ok "healed node is OK and names what it recovered from" \
  'grep -q "^OK beta (/root/ccc-node, recovered-from:health:telegram-degraded)$" "$OUT"'
ok "healed node is not also reported DEGRADED" '! grep -q "^DEGRADED beta" "$OUT"'
ok "recheck skips the doctor" '[ "$(sed -n 2p "$TMP/reply/beta.flags")" = "CCC_FLEET_DOCTOR=0" ]'
ok "healthy peer is probed once" '[ "$(wc -l < "$TMP/reply/alpha.flags")" = 1 ]'
ok "one verdict line per node after a recheck" '[ "$(grep -cE "^(OK|DOWN|DEGRADED|UNVERIFIED|DRIFT) " "$OUT")" = 2 ]'

# The healed verdict still judges the FIRST answer's doctor evidence: the light
# recheck carries none, and it must not launder a drifted node into OK.
printf 'RUNTIME=%s\nAVAIL=degraded\nUNIT=%s\nDOCTOR=1\nAVAIL_REASON=health:telegram-degraded\n' \
  /root/ccc-node /root/ccc-node > "$TMP/reply/beta"
run "beta"
okc "$RC" 1 "a healed node with doctor drift still fails"
ok "drift from the first probe survives the recheck" 'grep -q "^DRIFT beta doctor_exit=1" "$OUT"'

# Lost pid bookkeeping does not heal: it pages after the recheck, with reason.
reply beta /root/ccc-node degraded /root/ccc-node
echo "AVAIL_REASON=pid-bookkeeping-lost" >> "$TMP/reply/beta"
cp "$TMP/reply/beta" "$TMP/reply/beta.2"
run "beta"
okc "$RC" 1 "persistent degraded still pages"
ok "persistent degraded names its reason and the recheck" \
  'grep -q "^DEGRADED beta runtime=/root/ccc-node reason=pid-bookkeeping-lost rechecked=0s$" "$OUT"'

# A recheck that finds the bridge gone reports the current truth.
reply beta.2 /root/ccc-node no /root/ccc-node
run "beta"
okc "$RC" 1 "degraded then down pages"
ok "degraded then down is DOWN" 'grep -q "^DOWN beta" "$OUT" && ! grep -q "^DEGRADED beta" "$OUT"'

# A recheck that cannot reach the node keeps the first answer and says so —
# never OK, never a silent drop.
rm -f "$TMP/reply/beta.2"; : > "$TMP/reply/beta.2.unreachable"
run "beta"
okc "$RC" 1 "degraded with an unreachable recheck pages"
ok "unreachable recheck keeps the first reason" \
  'grep -q "^DEGRADED beta runtime=/root/ccc-node reason=pid-bookkeeping-lost recheck=unverified$" "$OUT"'
rm -f "$TMP/reply/beta.2.unreachable"

# Opt-out pages on the first answer, as before.
reply beta /root/ccc-node degraded /root/ccc-node
cp "$TMP/reply/alpha" "$TMP/reply/beta.2"
OUT="$TMP/out"; RC=0; rm -f "$TMP"/reply/*.seen "$TMP"/reply/*.flags "$TMP"/reply/*.acct
CCC_FLEET_NODES="beta" CCC_FLEET_SSH="$STUB" CCC_FLEET_SELF=_never_ CCC_FLEET_RETRY_DELAY=0 \
  CCC_FLEET_DEGRADED_RECHECK=0 bash "$SC" >"$OUT" 2>&1 || RC=$?
okc "$RC" 1 "recheck disabled pages on the first degraded answer"
ok "recheck disabled probes once" '[ "$(wc -l < "$TMP/reply/beta.flags")" = 1 ]'
ok "reason is reported even without a recheck" 'grep -q "^DEGRADED beta runtime=/root/ccc-node reason=unknown$" "$OUT"'

# Several degraded nodes share ONE wait (the doctor sweep has a 600s budget).
for n in d1 d2 d3; do
  reply "$n" /opt/ccc-node degraded /opt/ccc-node
  reply "$n.2" /opt/ccc-node yes /opt/ccc-node
done
OUT="$TMP/out"; RC=0; rm -f "$TMP"/reply/*.seen "$TMP"/reply/*.flags "$TMP"/reply/*.acct
t0=$SECONDS
CCC_FLEET_NODES="d1 d2 d3" CCC_FLEET_SSH="$STUB" CCC_FLEET_SELF=_never_ CCC_FLEET_RETRY_DELAY=0 \
  CCC_FLEET_DEGRADED_RECHECK_DELAY=2 bash "$SC" >"$OUT" 2>&1 || RC=$?
elapsed=$((SECONDS - t0))
okc "$RC" 0 "three healed nodes do not page"
ok "three degraded nodes share one recheck wait (${elapsed}s)" '[ "$elapsed" -le 4 ]'
ok "all three healed nodes reported OK" '[ "$(grep -c "^OK d[123] (/opt/ccc-node, recovered-from:unknown)$" "$OUT")" = 3 ]'
rm -f "$TMP"/reply/d[123]* "$TMP"/reply/beta.2

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

# ---- persistent memory degradation (#2659) ---------------------------------
# A node's MemPalace index lost 80% of its vectors and ccc-memory-check said
# `mempalace.status=degraded reasons=[index-diverged]` for nine days while every
# doctor sweep passed: the doctor reports it as a 경고, which by contract does
# not change its exit code, and the sweep kept only that exit code. The sweep
# now reads the probe's MEMORY column and fails the node once the degradation
# has lasted CCC_FLEET_MEMORY_CONFIRM consecutive sweeps (default 2), so a
# one-off transient (a refresh in flight) does not page.
reply_mem() { # <node> <memory>
  printf 'RUNTIME=/opt/ccc-node\nAVAIL=yes\nUNIT=/opt/ccc-node\nDOCTOR=0\nDUALDOMAIN=-\nMEMORY=%s\n' "$2" > "$TMP/reply/$1"
}
rm -rf "$TMP/mem-state"
reply_mem mdiv degraded:mempalace.index-diverged
reply_mem mok ok
CCC_FLEET_DOCTOR=1 run "mdiv mok"
okc "$RC" 0 "memory: a first degraded sweep does not page"
ok "memory: the first degraded sweep is visible on the OK line" \
  'grep -q "^OK mdiv (/opt/ccc-node, memory-pending:degraded:mempalace.index-diverged 1/2)$" "$OUT"'
ok "memory: a healthy peer is a plain OK" 'grep -q "^OK mok (/opt/ccc-node)$" "$OUT"'
ok "memory: the streak file is owner-only" '[ "$(stat -c %a "$MEMSTATE")" = 600 ]'
CCC_FLEET_DOCTOR=1 run "mdiv mok"
okc "$RC" 1 "memory: a degradation confirmed on the second sweep fails the run"
ok "memory: the confirmed node is a DRIFT row naming the memory state" \
  'grep -q "^DRIFT mdiv memory=degraded:mempalace.index-diverged sweeps=2/2 runtime=/opt/ccc-node$" "$OUT" && ! grep -q "^OK mdiv" "$OUT"'
ok "memory: the healthy peer is unaffected" 'grep -q "^OK mok (/opt/ccc-node)$" "$OUT"'
# The run's nonzero exit is what telegram-owner-on-failure spools; the row must
# also be counted into the alert title so the owner sees which node.
mem_title=$(python3 - "$ROOT/scripts" "$OUT" "$RC" <<'PY'
import importlib.util, sys
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("agent_cron_fleet_memory", sys.argv[1] + "/agent_cron.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
text = module.build_owner_text("fleet-doctor-sweep", "run-memory", None, "failed",
                               {"exitCode": int(sys.argv[3]), "stdout": open(sys.argv[2]).read(), "stderr": ""})
print(text.splitlines()[0])
PY
)
ok "memory: the confirmed row enters the owner alert title ($mem_title)" \
  '[ "$mem_title" = "agent-cron fleet alert for task fleet-doctor-sweep: DRIFT=1 (mdiv)" ]'
CCC_FLEET_DOCTOR=1 run "mdiv"
ok "memory: a persisting degradation keeps failing, with its count" \
  'test "$RC" = 1 && grep -q "^DRIFT mdiv memory=degraded:mempalace.index-diverged sweeps=3/2 " "$OUT"'
# Recovery clears the streak: a later one-off degradation is pending again.
reply_mem mdiv ok
CCC_FLEET_DOCTOR=1 run "mdiv"
ok "memory: a recovered node is a plain OK" 'test "$RC" = 0 && grep -q "^OK mdiv (/opt/ccc-node)$" "$OUT"'
ok "memory: recovery drops the node from the streak file" '! grep -q "^mdiv	" "$MEMSTATE"'
reply_mem mdiv degraded:mempalace.refresh-stale
CCC_FLEET_DOCTOR=1 run "mdiv"
ok "memory: a one-off transient after recovery does not page" \
  'test "$RC" = 0 && grep -q "^OK mdiv (/opt/ccc-node, memory-pending:degraded:mempalace.refresh-stale 1/2)$" "$OUT"'
# An inspection that could not run neither confirms nor clears the streak.
reply_mem mdiv unknown
CCC_FLEET_DOCTOR=1 run "mdiv"
ok "memory: an unknown memory state never fails the node" \
  'test "$RC" = 0 && grep -q "^OK mdiv (/opt/ccc-node, memory:unknown)$" "$OUT"'
reply_mem mdiv degraded:mempalace.refresh-stale
CCC_FLEET_DOCTOR=1 run "mdiv"
ok "memory: an unknown sweep does not reset the streak" \
  'test "$RC" = 1 && grep -q "^DRIFT mdiv memory=degraded:mempalace.refresh-stale sweeps=2/2 " "$OUT"'
reply_mem merr error:nunchi.db-error
CCC_FLEET_DOCTOR=1 run "merr"; CCC_FLEET_DOCTOR=1 run "merr"
ok "memory: an error state is confirmed like a degraded one" \
  'test "$RC" = 1 && grep -q "^DRIFT merr memory=error:nunchi.db-error sweeps=2/2 " "$OUT"'
# Termux nodes (MemPalace optional) and nodes with nunchi off are never paged,
# however many sweeps report it; neither is a harness without the column.
reply_mem mopt optional
reply_mem moff off
reply_dd mold /opt/ccc-node yes /opt/ccc-node 0 -
for _ in 1 2 3; do CCC_FLEET_DOCTOR=1 run "mopt moff mold"; done
okc "$RC" 0 "memory: optional/off/absent never fail the sweep"
ok "memory: optional, off and absent are plain OK rows" \
  'grep -q "^OK mopt (/opt/ccc-node)$" "$OUT" && grep -q "^OK moff (/opt/ccc-node)$" "$OUT" && grep -q "^OK mold (/opt/ccc-node)$" "$OUT"'
ok "memory: optional/off nodes have no streak" '! grep -qE "^(mopt|moff|mold)	" "$MEMSTATE"'
# Bridge problems outrank memory, and the streak is still counted under them.
printf 'RUNTIME=/opt/ccc-node\nAVAIL=yes\nUNIT=/opt/ccc-node\nDOCTOR=1\nDUALDOMAIN=-\nMEMORY=degraded:mempalace.index-diverged\n' > "$TMP/reply/mboth"
CCC_FLEET_DOCTOR=1 run "mboth"; CCC_FLEET_DOCTOR=1 run "mboth"
ok "memory: doctor drift outranks memory, one row per node" \
  'grep -q "^DRIFT mboth doctor_exit=1 " "$OUT" && [ "$(grep -c " mboth " "$OUT")" = 1 ]'
# Threshold knob: 1 pages on the first degraded sweep, no state needed.
rm -rf "$TMP/mem-state"
reply_mem mnow degraded:mempalace.index-diverged
CCC_FLEET_DOCTOR=1 CCC_FLEET_MEMORY_CONFIRM=1 run "mnow"
ok "memory: CCC_FLEET_MEMORY_CONFIRM=1 pages on the first sweep" \
  'test "$RC" = 1 && grep -q "^DRIFT mnow memory=degraded:mempalace.index-diverged sweeps=1/1 " "$OUT"'
ok "memory: CCC_FLEET_MEMORY_CONFIRM=1 keeps no streak file" '[ ! -e "$MEMSTATE" ]'
# Unusable streak state must never make the sweep quieter: confirm at once.
printf 'x' > "$TMP/mem-not-a-dir"
OUT="$TMP/out"; RC=0; rm -f "$TMP"/reply/*.seen
CCC_FLEET_NODES="mnow" CCC_FLEET_SSH="$STUB" CCC_FLEET_SELF=_never_ CCC_FLEET_RETRY_DELAY=0 \
  CCC_FLEET_DOCTOR=1 CCC_FLEET_MEMORY_STATE="$TMP/mem-not-a-dir/memory.tsv" bash "$SC" >"$OUT" 2>"$TMP/err" || RC=$?
ok "memory: unusable streak state confirms on first sight, loudly" \
  'test "$RC" = 1 && grep -q "^DRIFT mnow memory=degraded:mempalace.index-diverged sweeps=1/1 " "$OUT" && grep -q "memory streak state" "$TMP/err"'
# A symlinked streak file is refused, never followed.
mkdir -p "$TMP/mem-link"; printf 'mnow\t9\t0\n' > "$TMP/mem-link/target"
ln -s "$TMP/mem-link/target" "$TMP/mem-link/memory.tsv"
OUT="$TMP/out"; RC=0; rm -f "$TMP"/reply/*.seen
CCC_FLEET_NODES="mnow" CCC_FLEET_SSH="$STUB" CCC_FLEET_SELF=_never_ CCC_FLEET_RETRY_DELAY=0 \
  CCC_FLEET_DOCTOR=1 CCC_FLEET_MEMORY_STATE="$TMP/mem-link/memory.tsv" bash "$SC" >"$OUT" 2>"$TMP/err" || RC=$?
ok "memory: a symlinked streak file is refused and left untouched" \
  'test "$RC" = 1 && grep -q "sweeps=1/1 " "$OUT" && [ "$(cat "$TMP/mem-link/target")" = "$(printf "mnow\t9\t0")" ]'
# Only the doctor sweep reads the column (the probe sends it only there).
rm -rf "$TMP/mem-state"
run "mnow"
ok "memory: a plain bridge run neither judges memory nor writes streaks" \
  'test "$RC" = 0 && grep -q "^OK mnow (/opt/ccc-node)$" "$OUT" && [ ! -e "$MEMSTATE" ]'
rm -f "$TMP"/reply/m*

# ---- retired nodes are still doctor-checked (#2703) ------------------------
# #2343 made a node with no bridge and a retirement marker answer
# `AVAIL=retired` and stop there, before the doctor block. With 11 of 12 nodes
# retired the 2026-11-06 doctor sweep judged one node; the node of the #2659
# incident was among the 11. A retired node now carries DOCTOR/MEMORY on the
# doctor sweep and is judged on them; the retirement itself still never fails.
reply_ret() { # <node> <doctor> [<memory>] [<doctor-note>]
  printf 'RUNTIME=-\nAVAIL=retired\nRETIRED=frontends\nUNIT=-\nPREPARED=-\nDOCTOR=%s\n' "$2" > "$TMP/reply/$1"
  [ -z "${3:-}" ] || printf 'MEMORY=%s\n' "$3" >> "$TMP/reply/$1"
  [ -z "${4:-}" ] || printf 'DOCTOR_NOTE=%s\n' "$4" >> "$TMP/reply/$1"
}
rm -rf "$TMP/mem-state"
# (a) healthy harness + healthy memory: exactly the pre-#2703 line.
reply_ret rok 0 ok
CCC_FLEET_DOCTOR=1 run "rok"
okc "$RC" 0 "retired doctor: a healthy retired node does not fail the sweep"
ok "retired doctor: a healthy retired node is exactly the RETIRED line" \
  '[ "$(grep " rok" "$OUT")" = "RETIRED rok marker=frontends" ]'
# (b) doctor drift on a retired node is DRIFT, counted into the owner title.
reply_ret rdrift 1 ok
CCC_FLEET_DOCTOR=1 run "rok rdrift"
okc "$RC" 1 "retired doctor: doctor drift on a retired node fails the sweep"
ok "retired doctor: the drift is a DRIFT row naming the retirement, not RETIRED" \
  'grep -q "^DRIFT rdrift doctor_exit=1 retired=frontends$" "$OUT" && ! grep -q "^RETIRED rdrift" "$OUT" && [ "$(grep -c " rdrift" "$OUT")" = 1 ]'
ok "retired doctor: the healthy retired peer is unaffected" 'grep -q "^RETIRED rok marker=frontends$" "$OUT"'
owner_title() { # <task> -> first line of agent-cron's owner text for $OUT/$RC
  python3 - "$ROOT/scripts" "$OUT" "$RC" "$1" <<'PY'
import importlib.util, sys
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location("agent_cron_fleet_retired", sys.argv[1] + "/agent_cron.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
text = module.build_owner_text(sys.argv[4], "run-retired", None, "failed",
                               {"exitCode": int(sys.argv[3]), "stdout": open(sys.argv[2]).read(), "stderr": ""})
print(text.splitlines()[0])
PY
}
ret_title=$(owner_title fleet-doctor-sweep)
ok "retired doctor: the drift enters the owner alert title ($ret_title)" \
  '[ "$ret_title" = "agent-cron fleet alert for task fleet-doctor-sweep: DRIFT=1 (rdrift)" ]'
# A retired DRIFT row stays on the node's telegram pair in change mode: the
# `retired=` note is not a channel token.
ok "retired doctor: the note is not read as a channel by the state tracker" \
  '[ "$(python3 -c "import sys; sys.path.insert(0, sys.argv[1]); import fleet_watch_state as s; print(\" \".join(s.parse_verdicts(open(sys.argv[2]).read())))" "$ROOT/scripts" "$OUT")" = "rok/telegram rdrift/telegram" ]'
# (c) memory degraded on two consecutive sweeps: pending, then DRIFT.
reply_ret rmem 0 degraded:mempalace.index-diverged
CCC_FLEET_DOCTOR=1 run "rmem"
okc "$RC" 0 "retired doctor: a first degraded memory sweep does not page"
ok "retired doctor: the pending degradation is visible on the RETIRED line" \
  '[ "$(grep " rmem" "$OUT")" = "RETIRED rmem marker=frontends memory-pending:degraded:mempalace.index-diverged 1/2" ]'
ok "retired doctor: the streak is counted once per sweep" \
  '[ "$(grep -c "^rmem	1	" "$MEMSTATE")" = 1 ]'
CCC_FLEET_DOCTOR=1 run "rmem"
okc "$RC" 1 "retired doctor: a confirmed memory degradation fails the sweep"
ok "retired doctor: the confirmed degradation is a DRIFT row" \
  '[ "$(grep " rmem" "$OUT")" = "DRIFT rmem memory=degraded:mempalace.index-diverged sweeps=2/2 retired=frontends" ]'
reply_ret rmem 0 ok
CCC_FLEET_DOCTOR=1 run "rmem"
ok "retired doctor: recovered memory clears the streak and the plain line returns" \
  'test "$RC" = 0 && [ "$(grep " rmem" "$OUT")" = "RETIRED rmem marker=frontends" ] && ! grep -q "^rmem	" "$MEMSTATE"'
# A doctor drift outranks memory on a retired node too; the streak still counts.
reply_ret rboth 2 degraded:nunchi.feed-count
CCC_FLEET_DOCTOR=1 run "rboth"
ok "retired doctor: doctor drift outranks pending memory, one row" \
  'test "$RC" = 1 && [ "$(grep " rboth" "$OUT")" = "DRIFT rboth doctor_exit=2 retired=frontends" ] && grep -q "^rboth	1	" "$MEMSTATE"'
# (d) off/optional never count, however many sweeps; unknown is a note only.
reply_ret roff 0 off
reply_ret ropt 0 optional
for _ in 1 2 3; do CCC_FLEET_DOCTOR=1 run "roff ropt"; done
okc "$RC" 0 "retired doctor: off/optional memory never fails the sweep"
ok "retired doctor: off/optional are the plain RETIRED line" \
  'grep -q "^RETIRED roff marker=frontends$" "$OUT" && grep -q "^RETIRED ropt marker=frontends$" "$OUT"'
ok "retired doctor: off/optional keep no streak" '! grep -qE "^(roff|ropt)	" "$MEMSTATE"'
reply_ret runk 0 unknown
CCC_FLEET_DOCTOR=1 run "runk"
ok "retired doctor: an unknown memory check is a note, not a failure" \
  'test "$RC" = 0 && [ "$(grep " runk" "$OUT")" = "RETIRED runk marker=frontends memory:unknown" ]'
# (f) no harness found on the node: never a failure, but said on the line so a
# sweep that inspects nothing is visible.
reply_ret rnone - unknown no-harness
CCC_FLEET_DOCTOR=1 run "rnone"
okc "$RC" 0 "retired doctor: a retired node without a harness does not fail"
ok "retired doctor: the missing harness is named on the RETIRED line" \
  '[ "$(grep " rnone" "$OUT")" = "RETIRED rnone marker=frontends doctor:no-harness" ]'
# An answer that lost its DOCTOR column is an incomplete inspection.
printf 'RUNTIME=-\nAVAIL=retired\nRETIRED=frontends\nUNIT=-\nPREPARED=-\n' > "$TMP/reply/rlost"
CCC_FLEET_DOCTOR=1 run "rlost"
ok "retired doctor: a doctor answer without DOCTOR is UNVERIFIED" \
  'test "$RC" = 1 && grep -q "^UNVERIFIED rlost inspection=incomplete-doctor retired=frontends$" "$OUT"'
# ---- declared harness account (#2773) ---------------------------------------
# A service-account node's harness belongs to that account, not to the root
# login that probes it, so the retired fallback inspected root and reported
# `doctor:unverified-harness` (never a page) every day. The watcher now names
# the account per node; the node gets it as the payload's second line.
rm -f "$TMP"/reply/*.acct
reply_ret racct 0 ok
reply_ret rplain 0 ok
CCC_FLEET_DOCTOR=1 CCC_FLEET_HARNESS_ACCOUNTS="racct=svcacct" run "racct rplain"
ok "harness account: a declared account is sent to its node only" \
  '[ "$(cat "$TMP/reply/racct.acct")" = "CCC_FLEET_HARNESS_ACCOUNT=svcacct" ] && [ "$(cat "$TMP/reply/rplain.acct")" = "CCC_FLEET_HARNESS_ACCOUNT=" ] && [ "$(cat "$TMP/reply/racct.flags")" = "CCC_FLEET_DOCTOR=1" ]'
ok "harness account: the verdicts are unchanged by the declaration" \
  'test "$RC" = 0 && grep -qx "RETIRED racct marker=frontends" "$OUT" && grep -qx "RETIRED rplain marker=frontends" "$OUT"'
CCC_FLEET_DOCTOR=1 CCC_FLEET_HARNESS_ACCOUNTS="racct=svcacct, rplain=svc_1" run "racct rplain"
ok "harness account: comma and space separated pairs" \
  '[ "$(cat "$TMP/reply/rplain.acct")" = "CCC_FLEET_HARNESS_ACCOUNT=svc_1" ]'
# A declaration that cannot be read safely is a failed run, never a silent
# skip: it is the only line between root and a shell command on every node.
for bad in "racct" "racct=" "=svcacct" "racct=Gong" "racct=a;b" 'racct=$(id)' "racct=a racct=b" "racct=-x"; do
  rm -f "$TMP"/reply/*.acct
  CCC_FLEET_DOCTOR=1 CCC_FLEET_HARNESS_ACCOUNTS="$bad" run "racct rplain"
  ok "harness account: invalid declaration '$bad' fails the run before any probe" \
    'test "$RC" = 1 && grep -qx "UNVERIFIED watcher inspection=invalid-harness-accounts" "$OUT" && ! grep -qE "^(OK|RETIRED|DRIFT) " "$OUT" && [ ! -e "$TMP/reply/racct.acct" ]'
done
# A declared account the node cannot inspect is a failure: the operator said
# where the harness is, so "nothing inspected" is no longer an acceptable note.
reply_ret racct unverified unknown unknown-account
CCC_FLEET_DOCTOR=1 CCC_FLEET_HARNESS_ACCOUNTS="racct=svcacct" run "racct"
ok "harness account: an uninspectable declared account is UNVERIFIED with its reason" \
  'test "$RC" = 1 && [ "$(cat "$OUT")" = "UNVERIFIED racct inspection=harness-account:unknown-account retired=frontends" ]'
reply_ret racct unverified unknown
CCC_FLEET_DOCTOR=1 run "racct"
ok "harness account: an unverified reference without a note keeps its old row" \
  'test "$RC" = 1 && [ "$(cat "$OUT")" = "UNVERIFIED racct inspection=harness-reference retired=frontends" ]'
rm -f "$TMP"/reply/racct* "$TMP"/reply/rplain*
# (e) the plain and light runs are unchanged: no doctor flag is sent and the
# line is the RETIRED line, whatever DOCTOR/MEMORY a reply happens to carry.
reply_ret rdrift 1 degraded:mempalace.index-diverged
run "rdrift"
ok "retired doctor: a plain run ignores doctor/memory on a retired node" \
  'test "$RC" = 0 && [ "$(cat "$OUT")" = "RETIRED rdrift marker=frontends" ]'
rm -f "$TMP"/reply/r*.flags
OUT="$TMP/out"; RC=0; rm -f "$TMP"/reply/*.seen
CCC_FLEET_NODES="rdrift" CCC_FLEET_SSH="$STUB" CCC_FLEET_SELF=_never_ CCC_FLEET_RETRY_DELAY=0 \
  CCC_FLEET_DOCTOR=1 bash "$SC" --light >"$OUT" 2>/dev/null || RC=$?
ok "retired doctor: --light sends no doctor flag and prints the RETIRED line only" \
  'test "$RC" = 0 && [ "$(cat "$OUT")" = "RETIRED rdrift marker=frontends" ] && [ "$(cat "$TMP/reply/rdrift.flags")" = "CCC_FLEET_DOCTOR=0" ]'
rm -f "$TMP"/reply/r*

# The remote probe is a quoted heredoc — invisible to bash -n on the script
# itself. Extract and parse it as POSIX sh so a probe typo cannot ship.
probe_body="$TMP/probe-body.sh"
bash "$SC" --print-probe > "$probe_body"
ok "remote probe parses as POSIX sh" '[ -s "$probe_body" ] && bash -n "$probe_body" && { ! command -v dash >/dev/null || dash -n "$probe_body"; }'

# ---- bridge selection with a second telegram_bot process (#1860) -----------
# ccc-matrix-bridge runs the same module with the same --path, so the probe's
# filter matches both. `ps` lists by ascending pid: before the cgroup-based
# selection, a Matrix bridge that started first won `head -1` and the probe
# inspected the wrong process. Verified live on 2026-09-20: 3 of 4 dual-bridge
# nodes were in that state, and gongmyoung paged service-domain=unverified
# daily while its Telegram bridge was healthy.
#
# Run the transmitted probe against a stubbed ps and a fake /proc. The two
# fakes serve from different roots, so the emitted RUNTIME names which process
# was chosen.
sel="$TMP/sel"; mkdir -p "$sel/bin" "$sel/proc/100" "$sel/proc/200"
# Same technique the dual-domain cases below use: rewrite the probe's /proc/
# reads to a fake tree. The production code keeps a literal path.
sed -e "s#/proc/#$sel/proc/#g" "$probe_body" > "$sel/probe.sh"
cat > "$sel/bin/ps" <<'PSEOF'
#!/bin/sh
cat <<'PS'
 1000 100 1 /opt/matrix-tree/bridge/venv/bin/python -m telegram_bot --path /home/x
 1000 200 1 /opt/ccc-node/bridge/venv/bin/python -m telegram_bot --path /home/x
PS
PSEOF
chmod +x "$sel/bin/ps"
printf '0::/user.slice/user-1000.slice/user@1000.service/app.slice/ccc-matrix-bridge.service\n' > "$sel/proc/100/cgroup"
printf '0::/system.slice/ccc-telegram-bridge.service\n' > "$sel/proc/200/cgroup"
printf 'CCC_CHANNEL=matrix\0BOT_DATA_DIR=/home/x/.ccc-matrix\0' > "$sel/proc/100/environ"
printf 'CCC_CHANNEL=telegram\0' > "$sel/proc/200/environ"
sel_out="$sel/out"
PATH="$sel/bin:$PATH" sh "$sel/probe.sh" > "$sel_out" 2>&1
ok "probe selects the telegram bridge, not the lower-pid matrix bridge" \
  'grep -q "^RUNTIME=/opt/ccc-node$" "$sel_out"'
ok "probe does not report the matrix bridge runtime" \
  '! grep -q "^RUNTIME=/opt/matrix-tree$" "$sel_out"'
ok "probe still completes with a second bridge present" \
  'tail -1 "$sel_out" | grep -q "^PROBE_COMPLETE=1$"'

# No cgroup names the unit (Termux/Android): the Matrix process must still be
# excluded by its channel env, even if it has the lower PID.
printf '0::/\n' > "$sel/proc/100/cgroup"
printf '0::/\n' > "$sel/proc/200/cgroup"
PATH="$sel/bin:$PATH" sh "$sel/probe.sh" > "$sel_out" 2>&1
ok "Termux fallback excludes the Matrix process" \
  'grep -q "^RUNTIME=/opt/ccc-node$" "$sel_out"'
ok "fallback still completes" 'tail -1 "$sel_out" | grep -q "^PROBE_COMPLETE=1$"'

# hande frontend (#2380): excluded like Matrix, by unit cgroup and by channel env.
printf '0::/system.slice/ccc-hande-bridge.service\n' > "$sel/proc/100/cgroup"
printf 'CCC_CHANNEL=hande\0BOT_DATA_DIR=/home/x/.ccc-hande\0' > "$sel/proc/100/environ"
PATH="$sel/bin:$PATH" sh "$sel/probe.sh" > "$sel_out" 2>&1
ok "probe skips the hande bridge by cgroup" 'grep -q "^RUNTIME=/opt/ccc-node$" "$sel_out"'
printf '0::/\n' > "$sel/proc/100/cgroup"
PATH="$sel/bin:$PATH" sh "$sel/probe.sh" > "$sel_out" 2>&1
ok "Termux fallback excludes the hande process by env" 'grep -q "^RUNTIME=/opt/ccc-node$" "$sel_out"'
ok "hande exclusion still completes" 'tail -1 "$sel_out" | grep -q "^PROBE_COMPLETE=1$"'

# A single bridge must be unaffected by the new selection path.
cat > "$sel/bin/ps" <<'PSEOF'
#!/bin/sh
cat <<'PS'
 1000 200 1 /opt/ccc-node/bridge/venv/bin/python -m telegram_bot --path /home/x
PS
PSEOF
chmod +x "$sel/bin/ps"
printf '0::/system.slice/ccc-telegram-bridge.service\n' > "$sel/proc/200/cgroup"
PATH="$sel/bin:$PATH" sh "$sel/probe.sh" > "$sel_out" 2>&1
ok "single bridge is selected as before" 'grep -q "^RUNTIME=/opt/ccc-node$" "$sel_out"'

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
echo PROBE_COMPLETE=1
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
touch "$TMP/probe-repo/scripts/ccc-doctor.sh" "$TMP/probe-repo/scripts/ccc-memory-check.sh"
chmod +x "$TMP/probe-repo/scripts/ccc-doctor.sh"
cat > "$TMP/probe-bin/ps" <<EOF
#!$(command -v sh)
case "\$*" in *uid=,pid=,ppid=*) printf '0 42 41 ' ;; *uid=,pid=*) printf '0 42 ' ;; *) printf '0 ' ;; esac
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
case "\$*" in *--status) printf '%s\n' "Bot status: \${PROBE_STATUS:-available}"; [ -z "\${PROBE_STATUS_BODY:-}" ] || printf '%b\n' "\$PROBE_STATUS_BODY" ;; *ccc-memory-check.sh*) [ -z "\${PROBE_MEMORY:-}" ] || cat "\$PROBE_MEMORY"; exit "\${PROBE_MEMORY_RC:-0}" ;; *) exit "\${PROBE_DOCTOR:-0}" ;; esac
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

# The MEMORY column (#2659) is computed node-side from `ccc-memory-check.sh
# --json`, run as the runtime owner with the doctor's own state/cache dirs.
# Index fixtures come from the real repair-status classifier, so the
# flush-lag shape that #2336 modelled as normal (MemPalace `OK` with SQLite a
# little ahead of HNSW) is exactly what a node would report.
mem_fixture() { # <name> <nunchi-status> <mempalace-status|from-repair> [repair-text]
  python3 - "$ROOT/scripts" "$TMP/mem-$1.json" "$2" "$3" "${4:-}" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
import ccc_memory_probe as probe
out, nunchi, mempalace, text = sys.argv[2:6]
doc = {"nunchi": {"status": nunchi, "reasons": ["feed-count"] if nunchi == "degraded" else []}}
if mempalace == "from-repair":
    index = probe.parse_repair_status(text.replace("\\n", "\n"))
    reasons = [] if index["status"] == "ok" else ["index-" + index["status"]]
    doc["mempalace"] = {"status": "degraded" if reasons else "ok", "reasons": reasons, "index": index}
else:
    doc["mempalace"] = {"status": mempalace, "reasons": []}
json.dump(doc, open(out, "w"))
PY
}
mem_probe() { # <fixture-name> [extra env...]
  : > "$TMP/sudo-calls"
  env PATH="$TMP/probe-bin:$PATH" CCC_FLEET_DOCTOR=1 PROBE_MEMORY="$TMP/mem-$1.json" "${@:2}" \
    sh "$TMP/owner-probe.sh" > "$TMP/mem-out"
}
mem_fixture diverged ok from-repair '[drawers]\nsqlite count: 56,690\nhnsw count: 11,392\ndivergence: 45,298\nstatus: DIVERGED\n[closets]'
mem_probe diverged
ok "memory probe: an index-diverged palace is MEMORY=degraded with its reason" \
  'grep -q "^MEMORY=degraded:mempalace.index-diverged$" "$TMP/mem-out" && tail -1 "$TMP/mem-out" | grep -q "^PROBE_COMPLETE=1$"'
ok "memory probe: runs as the runtime owner with the doctor state dirs" \
  'grep -q -- "-n -H -u root -- env CCC_STATE_DIR=$TMP/probe-home/.claude/state CCC_MEMORY_CACHE_DIR=$TMP/probe-home/.claude/hooks/cache timeout 20 bash $TMP/probe-repo/scripts/ccc-memory-check.sh --json" "$TMP/sudo-calls"'
mem_fixture flushlag ok from-repair '[drawers]\nsqlite count: 303,009\nhnsw count: 302,895\ndivergence: 114\nstatus: OK\nnote: HNSW 302,895 / sqlite 303,009 (within flush-lag tolerance)\n[closets]'
mem_probe flushlag
ok "memory probe: modelled flush-lag (OK + small divergence, #2336) is MEMORY=ok" \
  'grep -q "^MEMORY=ok$" "$TMP/mem-out"'
mem_fixture termux ok optional
mem_probe termux
ok "memory probe: Termux (MemPalace optional) is MEMORY=optional" 'grep -q "^MEMORY=optional$" "$TMP/mem-out"'
mem_fixture off off off
mem_probe off
ok "memory probe: nunchi off is MEMORY=off" 'grep -q "^MEMORY=off$" "$TMP/mem-out"'
mem_fixture both degraded from-repair '[drawers]\nsqlite count: 9\nhnsw count: 2\ndivergence: 7\nstatus: DIVERGED'
mem_probe both
ok "memory probe: every degraded component and reason is named" \
  'grep -q "^MEMORY=degraded:nunchi.feed-count,mempalace.index-diverged$" "$TMP/mem-out"'
mem_fixture unavailable unavailable unavailable
mem_probe unavailable
ok "memory probe: an unavailable probe is MEMORY=unknown" 'grep -q "^MEMORY=unknown$" "$TMP/mem-out"'
printf '{"nunchi":{"status":"ok","reasons":[]},"mempalace":{"status":"degraded","reasons":["x y;$(id)`z`"]}}' > "$TMP/mem-hostile.json"
mem_probe hostile
ok "memory probe: reasons are reduced to a safe token" \
  'grep -q "^MEMORY=degraded:mempalace.xyidz$" "$TMP/mem-out"'
printf 'not json' > "$TMP/mem-garbage.json"
mem_probe garbage
ok "memory probe: unparseable output is MEMORY=unknown" 'grep -q "^MEMORY=unknown$" "$TMP/mem-out"'
mem_probe diverged PROBE_MEMORY_RC=1 PROBE_MEMORY=
ok "memory probe: a failed memory check is MEMORY=unknown, and the doctor still counts" \
  'grep -q "^MEMORY=unknown$" "$TMP/mem-out" && grep -q "^DOCTOR=0$" "$TMP/mem-out"'
mv "$TMP/probe-repo/scripts/ccc-memory-check.sh" "$TMP/ccc-memory-check.sh.away"
mem_probe diverged
ok "memory probe: a harness without ccc-memory-check is MEMORY=unknown" \
  'grep -q "^MEMORY=unknown$" "$TMP/mem-out" && ! grep -q "ccc-memory-check" "$TMP/sudo-calls"'
mv "$TMP/ccc-memory-check.sh.away" "$TMP/probe-repo/scripts/ccc-memory-check.sh"
PATH="$TMP/probe-bin:$PATH" sh "$TMP/owner-probe.sh" > "$TMP/mem-out"
ok "memory probe: the plain bridge probe never runs the memory check" '! grep -q "^MEMORY=" "$TMP/mem-out"'
PATH="$TMP/probe-bin:$PATH" PROBE_STATUS=unavailable sh "$TMP/owner-probe.sh" > "$TMP/owner-out"
ok "confirmed unavailable status remains down" 'grep -q "^AVAIL=no$" "$TMP/owner-out"'
PATH="$TMP/probe-bin:$PATH" PROBE_DENIED=1 sh "$TMP/owner-probe.sh" > "$TMP/owner-out"
ok "permission failure is unverified, not down" 'grep -q "^AVAIL=unverified$" "$TMP/owner-out"'
# "degraded" names its source (#2047): lost bookkeeping never heals, a health
# blip does, and the operator response differs.
for reason_case in \
  'pid-bookkeeping-lost|   Process: alive (unmanaged PID(s): 42 (no PID file))\n   Service: degraded (running without pid file; not recoverable by --status/--stop bookkeeping)' \
  'health-stale|   Process: alive (PID: 42)\n   Service: degraded (health stale: last update 9m ago)\n   Telegram: degraded (health stale: last update 9m ago)' \
  'health-missing|   Service: degraded (health missing)' \
  'health-unreadable|   Service: degraded (invalid health file: boom)' \
  'health:telegram-degraded|   Process: alive (PID: 42)\n   Service: degraded\n   Turn occupancy: idle\n   Telegram: degraded (Timed out)\n   Claude: healthy' \
  'health:agent-degraded|   Service: degraded\n   Telegram: healthy\n   Claude: degraded (rate limited)' \
  'health:service-degraded|   Service: degraded\n   Telegram: healthy\n   Claude: healthy'; do
  want=${reason_case%%|*}
  PATH="$TMP/probe-bin:$PATH" PROBE_STATUS=degraded PROBE_STATUS_BODY="${reason_case#*|}" \
    sh "$TMP/owner-probe.sh" > "$TMP/owner-out"
  ok "degraded status reports reason $want" \
    "grep -q '^AVAIL=degraded\$' \"\$TMP/owner-out\" && grep -qx 'AVAIL_REASON=$want' \"\$TMP/owner-out\""
done
PATH="$TMP/probe-bin:$PATH" sh "$TMP/owner-probe.sh" > "$TMP/owner-out"
ok "available status carries no degraded reason" '! grep -q "^AVAIL_REASON=" "$TMP/owner-out"'
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

# A retry job (a Termux node, 2026-10-08): the first `<prep>/job` failed its receipt,
# the preparation was re-run into `<prep>/job2` and the bridge was handed that
# one. Same source sibling, same preparation root — it must pass like `job`.
reply_p dg "$TX/.ccc-node/preparations/main-e707baf-20261008/source" yes - "$TX/.ccc-node/preparations/main-e707baf-20261008/job2"
run "dg"
okc "$RC" 0 "activated retry job (job2) passes"
ok "retry job launch is OK and names the preparation" \
  'grep -q "^OK dg ($TX/.ccc-node/preparations/main-e707baf-20261008/source, prepared:main-e707baf-20261008)$" "$OUT"'
ok "retry job launch is not NONCANONICAL" '! grep -q "^NONCANONICAL dg" "$OUT"'

# The retry name still has to be a job directory: a receipt beside some other
# sibling (here `<prep>/runtime`) does not make the source an activated runtime.
reply_p dg "$TX/.ccc-node/preparations/main-e707baf-20261008/source" yes - "$TX/.ccc-node/preparations/main-e707baf-20261008/runtime"
run "dg"
okc "$RC" 1 "a non-job sibling does not vouch for the source"
ok "non-job sibling is NONCANONICAL" 'grep -q "^NONCANONICAL dg runtime=$TX/.ccc-node/preparations/main-e707baf-20261008/source$" "$OUT"'

# And a retry job does not vouch for a foreign root either.
reply_p dg /work/agent-codebench/ccc-node-pr833 yes - "$TX/.ccc-node/preparations/main-e707baf-20261008/job2"
run "dg"
okc "$RC" 1 "retry job does not vouch for a foreign root"

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
echo "0 41 bash $TMP/prep/source/bridge/start.sh --path $TMP/probe-home --_daemon_supervisor --prepared-runtime $TMP/prep/job"
case "\$*" in *uid=,pid=,ppid=*) printf '0 42 41 ' ;; *uid=,pid=*) printf '0 42 ' ;; *) printf '0 ' ;; esac
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

# A foreign supervisor must never lend its source/job to this worker.
cp "$TMP/prep-bin/ps" "$TMP/prep-ps.original"
for wrong in owner parent project interpreter; do
  case "$wrong" in
    owner) sed 's/0 41 bash/7 41 bash/' "$TMP/prep-ps.original" > "$TMP/prep-bin/ps" ;;
    parent) sed 's/0 41 bash/0 99 bash/' "$TMP/prep-ps.original" > "$TMP/prep-bin/ps" ;;
    project) sed "s#--path $TMP/probe-home --_daemon_supervisor#--path $TMP/foreign --_daemon_supervisor#" "$TMP/prep-ps.original" > "$TMP/prep-bin/ps" ;;
    interpreter) sed "s#$TMP/prep/job/runtime/bin/python#$TMP/foreign/runtime/bin/python#" "$TMP/prep-ps.original" > "$TMP/prep-bin/ps" ;;
  esac
  PATH="$TMP/prep-bin:$TMP/probe-bin:$PATH" sh "$TMP/owner-probe.sh" > "$TMP/prep-out"
  ok "prepared parent binding rejects mismatched $wrong" 'grep -q "^AVAIL=unverified$" "$TMP/prep-out" && grep -q "^RUNTIME=-$" "$TMP/prep-out"'
done
cp "$TMP/prep-ps.original" "$TMP/prep-bin/ps"
printf '{"schema":"ccc.termux-preparation.v1","status":"ready"}\n' > "$TMP/prep/job/receipt.json"
PATH="$TMP/prep-bin:$TMP/probe-bin:$PATH" CCC_FLEET_DOCTOR=1 sh "$TMP/owner-probe.sh" > "$TMP/prep-out"
ok "missing installation reference does not inspect an arbitrary source" 'grep -q "^DOCTOR=unverified$" "$TMP/prep-out"'
mkdir -p "$TMP/install-repo/scripts" "$TMP/install-repo/bridge" "$TMP/install-repo/claude" "$TMP/probe-home/.claude"
touch "$TMP/install-repo/scripts/ccc-doctor.sh" "$TMP/install-repo/bridge/start.sh" "$TMP/install-repo/claude/settings.base.json"
printf '%s\n' "$TMP/install-repo" > "$TMP/probe-home/.claude/self-update.repo"
chmod 600 "$TMP/probe-home/.claude/self-update.repo"
chmod go-w "$TMP" "$TMP/probe-home" "$TMP/probe-home/.claude"
chmod -R go-w "$TMP/install-repo"
# The simulated worker has UID0; use the actual owner for a portable fixture.
# Root-less CI uses its own UID for the prepared worker and direct doctor stub.
if [ "$(id -u)" = 0 ]; then
  : > "$TMP/sudo-calls"
  PATH="$TMP/prep-bin:$TMP/probe-bin:$PATH" CCC_FLEET_DOCTOR=1 sh "$TMP/owner-probe.sh" > "$TMP/prep-out"
  ok "prepared runtime doctor follows the installed harness reference" 'grep -q "^DOCTOR_ROOT=$TMP/install-repo$" "$TMP/prep-out" && grep -q "^DOCTOR=0$" "$TMP/prep-out" && grep -q "$TMP/install-repo/scripts/ccc-doctor.sh" "$TMP/sudo-calls"'
else
  echo 'SKIP: root-owned prepared probe routing fixture (metadata/reference tests run on all users)'
fi
# The reference is a path, not a secret (#2703): world-readable 0644, the shape
# every VPS node carries, is accepted; one others could rewrite is not.
if [ "$(id -u)" = 0 ]; then
  chmod 644 "$TMP/probe-home/.claude/self-update.repo"
  PATH="$TMP/prep-bin:$TMP/probe-bin:$PATH" CCC_FLEET_DOCTOR=1 sh "$TMP/owner-probe.sh" > "$TMP/prep-out"
  ok "a read-only 0644 installation record is accepted" 'grep -q "^DOCTOR_ROOT=$TMP/install-repo$" "$TMP/prep-out" && grep -q "^DOCTOR=0$" "$TMP/prep-out"'
fi
# Other-write only: whether group write (664) is foreign depends on who else
# holds the fixture's group on this host (#2773, sole_member_group); the
# metadata unit tests pin that case.
for unsafe_mode in 646 666; do
  chmod "$unsafe_mode" "$TMP/probe-home/.claude/self-update.repo"
  PATH="$TMP/prep-bin:$TMP/probe-bin:$PATH" CCC_FLEET_DOCTOR=1 sh "$TMP/owner-probe.sh" > "$TMP/prep-out"
  ok "a writable-by-others installation record ($unsafe_mode) is unverified" 'grep -q "^DOCTOR=unverified$" "$TMP/prep-out"'
done

# Interrupted/malformed inspection output cannot be interpreted as DOWN.
printf 'RUNTIME=/opt/ccc-node\n' > "$TMP/reply/partial"
run partial
ok "partial probe response stays UNVERIFIED" 'grep -q "^UNVERIFIED partial " "$OUT" && ! grep -q "^DOWN " "$OUT"'
# Even a complete-looking availability response cannot mask transport failure.
reply_d torn /opt/ccc-node yes /opt/ccc-node 0
printf 'DUALDOMAIN=ok\n' >> "$TMP/reply/torn"
printf '255\n' > "$TMP/reply/torn.rc"
CCC_FLEET_DOCTOR=1 run torn
ok "nonzero transport after availability stays UNVERIFIED" 'test "$RC" = 1 && grep -q "^UNVERIFIED torn inspection=incomplete-probe" "$OUT"'
rm "$TMP/reply/torn.rc"
for tail_fields in '' 'DOCTOR=0' 'DOCTOR=0\nDUALDOMAIN=ok'; do
  reply torn /opt/ccc-node yes /opt/ccc-node
  printf '%b\n' "$tail_fields" >> "$TMP/reply/torn"
  touch "$TMP/reply/torn.incomplete"
  CCC_FLEET_DOCTOR=1 run torn
  ok "truncation before completion stays UNVERIFIED: $tail_fields" 'test "$RC" = 1 && grep -q "^UNVERIFIED torn inspection=incomplete-probe" "$OUT"'
done
rm "$TMP/reply/torn.incomplete"
reply torn /opt/ccc-node yes /opt/ccc-node
CCC_FLEET_DOCTOR=1 run torn
ok "missing requested doctor fields stays UNVERIFIED even with marker" 'test "$RC" = 1 && grep -q "inspection=incomplete-doctor" "$OUT"'
reply_dd torn /opt/ccc-node yes /opt/ccc-node 0 ok
CCC_FLEET_DOCTOR=1 run torn
ok "complete requested doctor inspection passes" 'test "$RC" = 0 && grep -q "^OK torn " "$OUT"'
reply_d badref /opt/ccc-node yes /opt/ccc-node unverified
run badref
ok "unknown harness reference has explicit classification" 'grep -q "^UNVERIFIED badref .*inspection=harness-reference$" "$OUT"'
printf 'RUNTIME=%s\nAVAIL=yes\nUNIT=-\nPREPARED=%s\nCHECKOUT=verified\n' "$TX/.ccc-node/checkouts/12345678" "$TX/.ccc-node/preparations/example" > "$TMP/reply/checkout"
run checkout
ok "proof-backed separate checkout layout is accepted" 'grep -q "^OK checkout .*verified-checkout:example)" "$OUT"'
sed -i 's/CHECKOUT=verified/CHECKOUT=unverified/' "$TMP/reply/checkout"
run checkout
ok "unverified separate checkout is never promoted" 'grep -q "^UNVERIFIED checkout .*inspection=prepared-checkout$" "$OUT"'

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
PATH="$TMP/danso-bin:$TMP/probe-bin:$PATH" DANSO_RC=1 sh "$TMP/owner-probe.sh" > "$TMP/danso-out"
ok "danso degraded names its source" 'grep -qx "AVAIL_REASON=danso-status-degraded" "$TMP/danso-out"'
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

# No ccc bridge and no danso service: still DOWN, as before. HOME is pinned to
# an empty directory: the watcher host itself may carry a retirement marker
# (the scheduled watcher's node does since 2026-10-16), and the probe reads the
# marker from HOME.
mkdir -p "$TMP/empty-bin" "$TMP/empty-home"
cat > "$TMP/empty-bin/ps" <<EOF
#!$(command -v sh)
exit 0
EOF
chmod +x "$TMP/empty-bin/ps"
PATH="$TMP/empty-bin:$TMP/probe-bin:$PATH" HOME="$TMP/empty-home" sh "$TMP/owner-probe.sh" > "$TMP/empty-out"
ok "a node with neither runtime is still DOWN" \
  'grep -q "^AVAIL=no$" "$TMP/empty-out" && grep -q "^RUNTIME=-$" "$TMP/empty-out" && ! grep -q "^KIND=danso$" "$TMP/empty-out"'

# ---- retired on purpose: the probe side (DOC-4014, #2343) ------------------
# The 2026-10-16 fleet retirement left `~/.claude/state/telegram-retire-<date>/`
# (the rollback backup) on every retired node. With no runtime at all, that
# directory turns the answer from DOWN into RETIRED, naming the newest marker.
mkdir -p "$TMP/retired-home/.claude/state/telegram-retire-20261001" \
         "$TMP/retired-home/.claude/state/telegram-retire-20261016"
PATH="$TMP/empty-bin:$TMP/probe-bin:$PATH" HOME="$TMP/retired-home" sh "$TMP/owner-probe.sh" > "$TMP/retired-out"
ok "no runtime plus a retirement marker is RETIRED, not DOWN" \
  'grep -q "^AVAIL=retired$" "$TMP/retired-out" && ! grep -q "^AVAIL=no$" "$TMP/retired-out" && grep -q "^RUNTIME=-$" "$TMP/retired-out"'
ok "the newest marker is named and the probe completes" \
  'grep -q "^RETIRED=telegram-retire-20261016$" "$TMP/retired-out" && [ "$(tail -1 "$TMP/retired-out")" = PROBE_COMPLETE=1 ]'
# A marker file (not a directory) is not a declaration.
mkdir -p "$TMP/file-home/.claude/state"; : > "$TMP/file-home/.claude/state/telegram-retire-20261016"
PATH="$TMP/empty-bin:$TMP/probe-bin:$PATH" HOME="$TMP/file-home" sh "$TMP/owner-probe.sh" > "$TMP/file-out"
ok "a plain file where the marker directory should be is ignored" 'grep -q "^AVAIL=no$" "$TMP/file-out"'
# The marker softens absence only. A bridge that IS running is judged on its
# answer (a TM-4018 rollback leaves the marker behind), so a failing bridge on
# a once-retired node still pages.
PATH="$TMP/probe-bin:$PATH" HOME="$TMP/retired-home" PROBE_STATUS=unavailable sh "$TMP/owner-probe.sh" > "$TMP/retired-running-out"
ok "a running bridge is judged on its answer despite the marker" \
  'grep -q "^AVAIL=no$" "$TMP/retired-running-out" && ! grep -q "^AVAIL=retired$" "$TMP/retired-running-out"'
PATH="$TMP/probe-bin:$PATH" HOME="$TMP/retired-home" sh "$TMP/owner-probe.sh" > "$TMP/retired-running-out"
ok "a healthy bridge is available despite the marker" 'grep -q "^AVAIL=yes$" "$TMP/retired-running-out"'

# ---- retired on purpose: the canonical frontend list (#2348) ----------------
# `~/.claude/frontends` is the declaration ccc-doctor and
# termux-restart-frontends.sh read (#2342/#2346). A node that wrote only that
# list (no backup directory) and leaves out `telegram` is RETIRED, not DOWN.
# fe_home <name> <printf-format>: a HOME holding only that frontend list.
fe_home() { mkdir -p "$TMP/$1/.claude"; printf "$2" > "$TMP/$1/.claude/frontends"; }
probe_empty() { PATH="$TMP/empty-bin:$TMP/probe-bin:$PATH" HOME="$TMP/$1" sh "$TMP/owner-probe.sh" > "$TMP/$1.out"; }
fe_home fe-matrix 'matrix\n'
probe_empty fe-matrix
ok "a frontend list without telegram is RETIRED marker=frontends" \
  'grep -q "^AVAIL=retired$" "$TMP/fe-matrix.out" && grep -q "^RETIRED=frontends$" "$TMP/fe-matrix.out" && grep -q "^RUNTIME=-$" "$TMP/fe-matrix.out" && [ "$(tail -1 "$TMP/fe-matrix.out")" = PROBE_COMPLETE=1 ]'
fe_home fe-telegram 'matrix\ntelegram\n'
probe_empty fe-telegram
ok "a frontend list naming telegram, with no bridge running, is still DOWN" \
  'grep -q "^AVAIL=no$" "$TMP/fe-telegram.out" && ! grep -q "^AVAIL=retired$" "$TMP/fe-telegram.out"'
# Separators are space, tab and comma, as in the other readers; a CRLF list is
# read like ccc-doctor reads it (telegram still named).
fe_home fe-comma 'matrix,telegram\n'
probe_empty fe-comma
ok "comma-separated telegram is a declared frontend" 'grep -q "^AVAIL=no$" "$TMP/fe-comma.out"'
fe_home fe-tab 'hande\tmatrix telegram\n'
probe_empty fe-tab
ok "tab/space-separated telegram is a declared frontend" 'grep -q "^AVAIL=no$" "$TMP/fe-tab.out"'
fe_home fe-crlf 'matrix\r\ntelegram\r\n'
probe_empty fe-crlf
ok "a CRLF list naming telegram is not misread as retired" 'grep -q "^AVAIL=no$" "$TMP/fe-crlf.out"'
# `telegram` only inside a comment is not a declaration of it.
fe_home fe-comment '# telegram retired 2026-10-16\nmatrix # was: telegram\n'
probe_empty fe-comment
ok "telegram named only in a comment is RETIRED marker=frontends" \
  'grep -q "^AVAIL=retired$" "$TMP/fe-comment.out" && grep -q "^RETIRED=frontends$" "$TMP/fe-comment.out"'
# A near-miss name is not telegram (whole-word match).
fe_home fe-near 'matrix telegram-old\n'
probe_empty fe-near
ok "a name that merely contains telegram does not declare it" 'grep -q "^RETIRED=frontends$" "$TMP/fe-near.out"'
# The list is canonical: when it exists it decides over the legacy directory.
# A rolled-back node keeps its backup directory, so a list naming telegram must
# still page a missing bridge; a list without it names itself as the source.
fe_home fe-dir-telegram 'matrix telegram\n'
mkdir -p "$TMP/fe-dir-telegram/.claude/state/telegram-retire-20261016"
probe_empty fe-dir-telegram
ok "a list naming telegram overrides a leftover backup directory (DOWN)" \
  'grep -q "^AVAIL=no$" "$TMP/fe-dir-telegram.out" && ! grep -q "^RETIRED=" "$TMP/fe-dir-telegram.out"'
fe_home fe-dir-matrix 'matrix\n'
mkdir -p "$TMP/fe-dir-matrix/.claude/state/telegram-retire-20261016"
probe_empty fe-dir-matrix
ok "with both markers the canonical list is reported as the source" \
  'grep -q "^RETIRED=frontends$" "$TMP/fe-dir-matrix.out"'
# A directory where the list should be is not a list: the legacy directory
# marker (here: none) decides, so the node stays DOWN.
mkdir -p "$TMP/fe-isdir/.claude/frontends"
probe_empty fe-isdir
ok "a directory named frontends is not a declaration" 'grep -q "^AVAIL=no$" "$TMP/fe-isdir.out"'
# Overrides in the watcher's own environment do not describe the probed node.
PATH="$TMP/empty-bin:$TMP/probe-bin:$PATH" HOME="$TMP/empty-home" CCC_FRONTENDS=matrix \
  CCC_FRONTENDS_FILE="$TMP/fe-matrix/.claude/frontends" sh "$TMP/owner-probe.sh" > "$TMP/fe-env.out"
ok "CCC_FRONTENDS/CCC_FRONTENDS_FILE in the probe environment are ignored" 'grep -q "^AVAIL=no$" "$TMP/fe-env.out"'
# A running bridge still wins over the list, as over the directory marker.
PATH="$TMP/probe-bin:$PATH" HOME="$TMP/fe-matrix" PROBE_STATUS=unavailable sh "$TMP/owner-probe.sh" > "$TMP/fe-running.out"
ok "a running bridge is judged on its answer despite a list without telegram" \
  'grep -q "^AVAIL=no$" "$TMP/fe-running.out" && ! grep -q "^AVAIL=retired$" "$TMP/fe-running.out"'
PATH="$TMP/probe-bin:$PATH" HOME="$TMP/fe-matrix" sh "$TMP/owner-probe.sh" > "$TMP/fe-running.out"
ok "a healthy bridge is available despite a list without telegram" 'grep -q "^AVAIL=yes$" "$TMP/fe-running.out"'

# ---- retired on purpose: the doctor sweep still inspects the node (#2703) ---
# Execute the transmitted probe against fixture homes. With no bridge there is
# no bridge account, so the probing account itself is inspected: its
# ~/.claude, and the harness its operator-owned self-update.repo names (the
# same metadata check a prepared runtime uses), else the self-update default
# checkout locations. The fixture paths are physical: the metadata check
# refuses symlinked components. `/opt/ccc-node` is redirected so the test
# never runs this host's real doctor.
RH="$(cd "$TMP" && pwd -P)/ret-doc"
mkdir -p "$RH"
sed "s#/opt/ccc-node#$RH/opt-ccc-node#g" "$probe_body" > "$TMP/ret-probe.sh"
ret_harness() { # <root>: a minimal checkout the metadata check accepts
  mkdir -p "$1/scripts" "$1/claude" "$1/bridge"
  cat > "$1/scripts/ccc-doctor.sh" <<EOF
printf '%s\n' "doctor root=$1 CCC_DOCTOR_CLAUDE_DIR=\${CCC_DOCTOR_CLAUDE_DIR:-}" >> "$TMP/ret-calls"
exit "\${RET_DOCTOR:-0}"
EOF
  cat > "$1/scripts/ccc-memory-check.sh" <<EOF
printf '%s\n' "memory \$* CCC_STATE_DIR=\${CCC_STATE_DIR:-} CCC_MEMORY_CACHE_DIR=\${CCC_MEMORY_CACHE_DIR:-}" >> "$TMP/ret-calls"
[ -z "\${RET_MEMORY:-}" ] || cat "\$RET_MEMORY"
EOF
  : > "$1/claude/settings.base.json"; : > "$1/bridge/start.sh"
  find "$1" -type d -exec chmod 700 {} +; find "$1" -type f -exec chmod 600 {} +
}
ret_home() { # <home>: retired by its frontend list, owner-only like a real home
  mkdir -p "$1/.claude"; printf 'matrix\n' > "$1/.claude/frontends"; chmod 700 "$1" "$1/.claude"
}
ret_ref() { # <home> <repo> [<mode>]: the operator-owned install reference
  printf '%s\n' "$2" > "$1/.claude/self-update.repo"; chmod "${3:-600}" "$1/.claude/self-update.repo"
}
ret_probe() { # <home> [env...]
  : > "$TMP/ret-calls"
  env PATH="$TMP/empty-bin:$PATH" HOME="$1" "${@:2}" sh "$TMP/ret-probe.sh" > "$TMP/ret-out"
}
ret_harness "$RH/inst"
ret_home "$RH/h-ref"; ret_ref "$RH/h-ref" "$RH/inst"
# A decoy home checkout must not win over the operator's reference.
ret_harness "$RH/h-ref/ccc-node"
ret_probe "$RH/h-ref" CCC_FLEET_DOCTOR=1 RET_MEMORY="$TMP/mem-diverged.json"
ok "retired probe: the doctor sweep still runs the doctor, from the installed reference" \
  'grep -q "^AVAIL=retired$" "$TMP/ret-out" && grep -q "^RETIRED=frontends$" "$TMP/ret-out" && grep -qx "DOCTOR_ROOT=$RH/inst" "$TMP/ret-out" && grep -q "^DOCTOR=0$" "$TMP/ret-out"'
ok "retired probe: the doctor inspects the probing account's own claude dir" \
  'grep -qx "doctor root=$RH/inst CCC_DOCTOR_CLAUDE_DIR=$RH/h-ref/.claude" "$TMP/ret-calls" && ! grep -q "root=$RH/h-ref/ccc-node" "$TMP/ret-calls"'
ok "retired probe: the memory check runs with the doctor's state dirs" \
  'grep -qx "memory --json CCC_STATE_DIR=$RH/h-ref/.claude/state CCC_MEMORY_CACHE_DIR=$RH/h-ref/.claude/hooks/cache" "$TMP/ret-calls" && grep -q "^MEMORY=degraded:mempalace.index-diverged$" "$TMP/ret-out"'
ok "retired probe: the doctor columns precede the completion marker" \
  '[ "$(tail -1 "$TMP/ret-out")" = PROBE_COMPLETE=1 ] && [ "$(grep -c "^DOCTOR=" "$TMP/ret-out")" = 1 ] && [ "$(grep -c "^MEMORY=" "$TMP/ret-out")" = 1 ]'
ret_probe "$RH/h-ref" CCC_FLEET_DOCTOR=1 RET_DOCTOR=1
ok "retired probe: a drifted harness reports its doctor exit" 'grep -q "^DOCTOR=1$" "$TMP/ret-out"'
# (e) without the doctor flag the retired answer is byte-for-byte the old one.
ret_probe "$RH/h-ref"
ok "retired probe: a plain probe is unchanged and runs nothing" \
  '[ "$(cat "$TMP/ret-out")" = "$(printf "RUNTIME=-\nAVAIL=retired\nRETIRED=frontends\nUNIT=-\nPREPARED=-\nPROBE_COMPLETE=1")" ] && [ ! -s "$TMP/ret-calls" ]'
ret_probe "$RH/h-ref" CCC_FLEET_DOCTOR=0
ok "retired probe: CCC_FLEET_DOCTOR=0 is the plain probe" \
  '[ "$(cat "$TMP/ret-out")" = "$(printf "RUNTIME=-\nAVAIL=retired\nRETIRED=frontends\nUNIT=-\nPREPARED=-\nPROBE_COMPLETE=1")" ] && [ ! -s "$TMP/ret-calls" ]'
# The live VPS shape: a root-written, world-readable 0644 reference (#2703).
ret_home "$RH/h-ref644"; ret_ref "$RH/h-ref644" "$RH/inst" 644
ret_probe "$RH/h-ref644" CCC_FLEET_DOCTOR=1
ok "retired probe: a read-only 0644 reference is used" \
  'grep -qx "DOCTOR_ROOT=$RH/inst" "$TMP/ret-out" && grep -q "^DOCTOR=0$" "$TMP/ret-out" && grep -qx "doctor root=$RH/inst CCC_DOCTOR_CLAUDE_DIR=$RH/h-ref644/.claude" "$TMP/ret-calls"'
# A reference that fails the metadata check (writable by others) is not
# replaced by a guess.
ret_home "$RH/h-badref"; ret_ref "$RH/h-badref" "$RH/inst" 646
ret_harness "$RH/h-badref/ccc-node"
ret_probe "$RH/h-badref" CCC_FLEET_DOCTOR=1
ok "retired probe: an unsafe reference runs nothing and says so" \
  'grep -q "^DOCTOR=-$" "$TMP/ret-out" && grep -q "^DOCTOR_NOTE=unverified-reference$" "$TMP/ret-out" && grep -q "^MEMORY=unknown$" "$TMP/ret-out" && [ ! -s "$TMP/ret-calls" ] && [ "$(tail -1 "$TMP/ret-out")" = PROBE_COMPLETE=1 ]'
# No reference: the self-update default checkout (~/ccc-node), then /opt.
ret_home "$RH/h-home"; ret_harness "$RH/h-home/ccc-node"
ret_probe "$RH/h-home" CCC_FLEET_DOCTOR=1
ok "retired probe: without a reference the home checkout is used" \
  'grep -qx "DOCTOR_ROOT=$RH/h-home/ccc-node" "$TMP/ret-out" && grep -q "^DOCTOR=0$" "$TMP/ret-out" && grep -q "^MEMORY=unknown$" "$TMP/ret-out"'
ret_home "$RH/h-opt"; ret_harness "$RH/opt-ccc-node"
ret_probe "$RH/h-opt" CCC_FLEET_DOCTOR=1
ok "retired probe: without a reference or home checkout /opt/ccc-node is used" \
  'grep -qx "DOCTOR_ROOT=$RH/opt-ccc-node" "$TMP/ret-out" && grep -qx "doctor root=$RH/opt-ccc-node CCC_DOCTOR_CLAUDE_DIR=$RH/h-opt/.claude" "$TMP/ret-calls"'
rm -rf "$RH/opt-ccc-node"
# A candidate whose scripts someone else could rewrite is not executed.
ret_home "$RH/h-unsafe"; ret_harness "$RH/h-unsafe/ccc-node"; chmod o+w "$RH/h-unsafe/ccc-node/scripts/ccc-doctor.sh"
ret_probe "$RH/h-unsafe" CCC_FLEET_DOCTOR=1
ok "retired probe: a writable-by-others checkout runs nothing and says so" \
  'grep -q "^DOCTOR=-$" "$TMP/ret-out" && grep -q "^DOCTOR_NOTE=unverified-harness$" "$TMP/ret-out" && [ ! -s "$TMP/ret-calls" ]'
# (f) nothing to inspect: DOCTOR=- with its reason, never a failure.
ret_home "$RH/h-none"
ret_probe "$RH/h-none" CCC_FLEET_DOCTOR=1
ok "retired probe: no harness anywhere is DOCTOR=- no-harness, MEMORY=unknown" \
  'grep -q "^DOCTOR=-$" "$TMP/ret-out" && grep -q "^DOCTOR_NOTE=no-harness$" "$TMP/ret-out" && grep -q "^MEMORY=unknown$" "$TMP/ret-out" && [ "$(tail -1 "$TMP/ret-out")" = PROBE_COMPLETE=1 ]'
# (h) a declared harness account (#2773): the service-account shape. root probes and
# holds the retirement declaration; the harness, its reference and its
# ~/.claude belong to another account. `id`/`getent`/`su`/`sudo` are stubbed
# so the test needs no real account: the declared account gets this test's own
# uid (the fixtures' owner, which the metadata check requires) and the probing
# login a different one, which is what makes the probe switch accounts — via
# `su -` when the login is root (as on the VPS nodes) or `sudo -n` otherwise,
# whichever this host's uid leaves for the stub. Both stubs log and run.
real_id=$(command -v id)
if [ "$("$real_id" -u)" = 0 ]; then login_uid=4242; else login_uid=0; fi
mkdir -p "$TMP/acct-bin"
cat > "$TMP/acct-bin/id" <<EOF
#!$(command -v sh)
case "\$*" in
  '-u hacct') "$real_id" -u ;;
  '-u') echo $login_uid ;;
  '-nu') echo login ;;
  *) exec "$real_id" "\$@" ;;
esac
EOF
cat > "$TMP/acct-bin/getent" <<EOF
#!$(command -v sh)
[ "\$*" = "passwd hacct" ] || exit 2
echo "hacct:x:\$("$real_id" -u):\$("$real_id" -g)::\${ACCT_HOME:-$RH/acct-home}:/bin/sh"
EOF
cat > "$TMP/acct-bin/su" <<EOF
#!$(command -v sh)
[ "\$1 \$2 \$3" = "- hacct -c" ] || { echo "unexpected su \$*" >> "$TMP/acct-su"; exit 97; }
echo "as hacct" >> "$TMP/acct-su"
exec sh -c "\$4"
EOF
cat > "$TMP/acct-bin/sudo" <<EOF
#!$(command -v sh)
[ "\$1 \$2 \$3 \$4 \$5" = "-n -H -u hacct --" ] || { echo "unexpected sudo \$*" >> "$TMP/acct-su"; exit 97; }
echo "as hacct" >> "$TMP/acct-su"
shift 5; exec "\$@"
EOF
chmod +x "$TMP/acct-bin/"*
acct_probe() { # <account> [env...]
  : > "$TMP/ret-calls"; : > "$TMP/acct-su"
  env PATH="$TMP/acct-bin:$TMP/empty-bin:$PATH" HOME="$RH/h-root" CCC_FLEET_DOCTOR=1 \
    CCC_FLEET_HARNESS_ACCOUNT="$1" "${@:2}" sh "$TMP/ret-probe.sh" > "$TMP/ret-out"
}
ret_home "$RH/h-root"
ret_home "$RH/acct-home"; ret_ref "$RH/acct-home" "$RH/inst"
acct_probe hacct RET_MEMORY="$TMP/mem-diverged.json"
ok "harness account probe: the declared account's reference selects the harness" \
  'grep -q "^RETIRED=frontends$" "$TMP/ret-out" && grep -qx "DOCTOR_ROOT=$RH/inst" "$TMP/ret-out" && grep -q "^DOCTOR=0$" "$TMP/ret-out"'
ok "harness account probe: doctor and memory check run as that account on its ~/.claude" \
  'grep -qx "doctor root=$RH/inst CCC_DOCTOR_CLAUDE_DIR=$RH/acct-home/.claude" "$TMP/ret-calls" && grep -qx "memory --json CCC_STATE_DIR=$RH/acct-home/.claude/state CCC_MEMORY_CACHE_DIR=$RH/acct-home/.claude/hooks/cache" "$TMP/ret-calls" && [ "$(cat "$TMP/acct-su")" = "$(printf "as hacct\nas hacct")" ]'
ok "harness account probe: the memory column comes from that account" \
  'grep -q "^MEMORY=degraded:mempalace.index-diverged$" "$TMP/ret-out" && [ "$(tail -1 "$TMP/ret-out")" = PROBE_COMPLETE=1 ]'
# Without a reference the account's own ~/ccc-node is the candidate, not root's.
ret_home "$RH/acct-noref"; ret_harness "$RH/acct-noref/ccc-node"; ret_harness "$RH/h-root/ccc-node"
acct_probe hacct ACCT_HOME="$RH/acct-noref"
ok "harness account probe: without a reference the account's home checkout is used" \
  'grep -qx "DOCTOR_ROOT=$RH/acct-noref/ccc-node" "$TMP/ret-out" && ! grep -q "root=$RH/h-root/ccc-node" "$TMP/ret-calls"'
rm -rf "$RH/h-root/ccc-node"
# Every way the declared account cannot be inspected is DOCTOR=unverified (a
# page), named, and runs nothing.
acct_fail() { # <label> <note> <account> [env...]
  acct_probe "${@:3}"
  ok "harness account probe: $1 is DOCTOR=unverified $2 and runs nothing" \
    'grep -q "^DOCTOR=unverified$" "$TMP/ret-out" && grep -qx "DOCTOR_NOTE='"$2"'" "$TMP/ret-out" && grep -q "^MEMORY=unknown$" "$TMP/ret-out" && [ ! -s "$TMP/ret-calls" ] && [ ! -s "$TMP/acct-su" ] && [ "$(tail -1 "$TMP/ret-out")" = PROBE_COMPLETE=1 ]'
}
acct_fail "an unknown account" unknown-account nosuch
acct_fail "a malformed account name" invalid-account 'a;b'
acct_fail "an account name starting with a dash" invalid-account '-x'
acct_fail "a home that is not a plain absolute path" unknown-account hacct ACCT_HOME="relative/home"
acct_fail "a home with shell metacharacters" unknown-account hacct ACCT_HOME="$RH/it's"
ret_home "$RH/acct-badref"; ret_ref "$RH/acct-badref" "$RH/inst" 646
acct_fail "an other-writable reference" unverified-reference hacct ACCT_HOME="$RH/acct-badref"
ret_home "$RH/acct-none"
acct_fail "an account with no harness" no-harness hacct ACCT_HOME="$RH/acct-none"
# An empty declaration is the undeclared probe: the probing account itself.
ret_harness "$RH/h-root/ccc-node"
ret_probe "$RH/h-root" CCC_FLEET_DOCTOR=1 CCC_FLEET_HARNESS_ACCOUNT=
ok "harness account probe: no declaration keeps inspecting the probing account" \
  'grep -qx "DOCTOR_ROOT=$RH/h-root/ccc-node" "$TMP/ret-out" && grep -qx "doctor root=$RH/h-root/ccc-node CCC_DOCTOR_CLAUDE_DIR=$RH/h-root/.claude" "$TMP/ret-calls"'
rm -rf "$RH/h-root/ccc-node"
# (g) the #2659 incident shape end to end: a retired node whose MemPalace index
# is diverged, probed by the watcher itself (CCC_FLEET_SELF runs the real
# probe locally), pages on the second doctor sweep.
ret_home "$RH/self"; ret_harness "$RH/self/ccc-node"
rm -rf "$TMP/mem-state"
self_sweep() {
  OUT="$TMP/out"; RC=0; : > "$TMP/ret-calls"
  env PATH="$TMP/empty-bin:$PATH" HOME="$RH/self" RET_MEMORY="$TMP/mem-diverged.json" \
    CCC_FLEET_NODES=rself CCC_FLEET_SELF=rself CCC_FLEET_SSH="$STUB" CCC_FLEET_RETRY_DELAY=0 \
    CCC_FLEET_DOCTOR=1 bash "$SC" >"$OUT" 2>&1 || RC=$?
}
self_sweep
ok "retired end to end: the first index-diverged sweep is pending, not a page" \
  'test "$RC" = 0 && [ "$(cat "$OUT")" = "RETIRED rself marker=frontends memory-pending:degraded:mempalace.index-diverged 1/2" ] && grep -q "^doctor root=$RH/self/ccc-node " "$TMP/ret-calls"'
self_sweep
ok "retired end to end: the second index-diverged sweep pages as DRIFT" \
  'test "$RC" = 1 && [ "$(cat "$OUT")" = "DRIFT rself memory=degraded:mempalace.index-diverged sweeps=2/2 retired=frontends" ]'
ret_title=$(owner_title fleet-doctor-sweep)
ok "retired end to end: the page names the node in the owner alert title ($ret_title)" \
  '[ "$ret_title" = "agent-cron fleet alert for task fleet-doctor-sweep: DRIFT=1 (rself)" ]'
rm -rf "$TMP/mem-state"

# ---- runtime owner is not the service manager ------------------------------
# Execute the POSIX probe: a system service may use User=gongmyoung without
# needing a user unit, user bus, or linger. Keep legacy user-service coverage.
mkdir -p "$TMP/domain-bin" "$TMP/domain-repo/.git" "$TMP/domain-home" "$TMP/domain-proc/42"
cat > "$TMP/domain-bin/ps" <<EOF
#!$(command -v sh)
case "\$*" in *uid=,pid=,ppid=*) printf '1000 42 41 ' ;; *uid=,pid=*) printf '1000 42 ' ;; *) printf '1000 ' ;; esac
if [ "\${UNKNOWN_ROOT:-0}" = 1 ]; then
  echo '/unrecognized/python -m telegram_bot --path $TMP/domain-home'
else
  echo '$TMP/domain-repo/bridge/venv/bin/python -m telegram_bot --path $TMP/domain-home'
fi
EOF
cat > "$TMP/domain-bin/id" <<EOF
#!$(command -v sh)
case "\$1" in
 -nu) echo gongmyoung ;;
 -u) case "\${2:-}" in gongmyoung|1000) echo 1000 ;; *) echo 0 ;; esac ;;
 *) exit 0 ;;
esac
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
  *)
     if [ "\${FETCH_DENIED:-0}" = 1 ]; then
       case "\$*" in *".git/FETCH_HEAD"*) echo '$TMP/domain-repo/.git/FETCH_HEAD' ;; esac
     fi
     [ "\${GIT_DENIED:-0}" = 0 ] || echo '$TMP/domain-repo/.git/objects/unwritable' ;;
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
domain_probe UNIT_USER=1000
ok "numeric systemd User resolves to the runtime UID" 'grep -q "^DUALDOMAIN=ok$" "$TMP/domain-out"'
domain_probe FETCH_DENIED=1
ok "unwritable existing FETCH_HEAD is still drift" 'grep -q "git-access-denied" "$TMP/domain-out"'
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

# ---- change-based alerting: --state-file (#2086) ----------------------------
# A daily watch that failed every day for a chronic issue hid a new outage for
# hours. With a state file the run pages only on NEW/STILL/RECOVERED events and
# its report leads with rows that name the node.
STATE="$TMP/state/fleet-watch.json"
run_state() { # <nodes> [extra env...]
  OUT="$TMP/out"; RC=0
  rm -f "$TMP"/reply/*.seen "$TMP"/reply/*.flags "$TMP"/reply/*.acct
  env CCC_FLEET_NODES="$1" CCC_FLEET_SSH="$STUB" CCC_FLEET_SELF=_never_ \
    CCC_FLEET_RETRY_DELAY=0 CCC_FLEET_MATRIX=0 CCC_FLEET_HANDE=0 "${@:2}" \
    bash "$SC" --state-file "$STATE" >"$OUT" 2>"$TMP/err" || RC=$?
}
rm -f "$TMP"/reply/beta.2
reply alpha /opt/ccc-node yes /opt/ccc-node
reply beta /opt/ccc-node degraded /opt/ccc-node
echo "AVAIL_REASON=health-stale" >> "$TMP/reply/beta"
run_state "alpha beta" CCC_FLEET_WATCH_NOW=2026-10-01T00:00:00Z
okc "$RC" 0 "state mode: a first abnormal answer does not page"
ok "state mode: first abnormal answer is PENDING with its channel" \
  'grep -q "^PENDING DEGRADED beta channel=telegram runtime=/opt/ccc-node reason=health-stale confirm=1/2$" "$OUT"'
ok "state mode: OK rows are dropped, a summary remains" \
  '! grep -q "^OK " "$OUT" && grep -q "^SUMMARY checked=2 ok=1 .*page=no$" "$OUT"'
ok "state mode: the degraded recheck is off (confirmation replaces it)" \
  '[ "$(wc -l < "$TMP/reply/beta.flags")" = 1 ]'
ok "state mode: state file is owner-only" '[ "$(stat -c %a "$STATE")" = 600 ]'
run_state "alpha beta" CCC_FLEET_WATCH_NOW=2026-10-01T00:15:00Z
okc "$RC" 1 "state mode: a confirmed new abnormal pair pages"
ok "state mode: the report leads with a NEW row naming the node" \
  '[ "$(head -1 "$OUT")" = "NEW DEGRADED beta channel=telegram runtime=/opt/ccc-node reason=health-stale since=2026-10-01T00:00:00Z" ]'
run_state "alpha beta" CCC_FLEET_WATCH_NOW=2026-10-01T00:30:00Z
okc "$RC" 0 "state mode: a known issue stays quiet"
ok "state mode: the known issue is still listed" 'grep -q "^KNOWN DEGRADED beta channel=telegram" "$OUT"'
# The chronic beta issue must not hide a new one on alpha.
reply alpha /opt/ccc-node no /opt/ccc-node
run_state "alpha beta" CCC_FLEET_WATCH_NOW=2026-10-01T00:45:00Z
run_state "alpha beta" CCC_FLEET_WATCH_NOW=2026-10-01T01:00:00Z
okc "$RC" 1 "state mode: a new issue pages beside a known one"
ok "state mode: NEW row first, its relay marker (#2674), then KNOWN" \
  '[ "$(sed -n 1p "$OUT")" = "NEW DOWN alpha channel=telegram since=2026-10-01T00:45:00Z" ] && [ "$(sed -n 2p "$OUT")" = "@fleet-alert opens=fw:alpha:telegram:20261001T004500Z" ] && sed -n 3p "$OUT" | grep -q "^KNOWN DEGRADED beta "'
reply alpha /opt/ccc-node yes /opt/ccc-node
run_state "alpha beta" CCC_FLEET_WATCH_NOW=2026-10-01T01:15:00Z
run_state "alpha beta" CCC_FLEET_WATCH_NOW=2026-10-01T01:30:00Z
okc "$RC" 1 "state mode: a confirmed recovery of an alerted node is reported"
ok "state mode: RECOVERED names the node and its old verdict" \
  'grep -q "^RECOVERED alpha channel=telegram was=DOWN since=2026-10-01T00:45:00Z for=0h45m$" "$OUT"'
# A pair retired on purpose closes its alert once, then stays quiet (#2343):
# the 2026-10-16 shape, 11 pairs alerted DOWN and then declared retired.
reply alpha /opt/ccc-node no /opt/ccc-node
run_state "alpha beta" CCC_FLEET_WATCH_NOW=2026-10-01T01:35:00Z
run_state "alpha beta" CCC_FLEET_WATCH_NOW=2026-10-01T01:40:00Z
okc "$RC" 1 "state mode: the pair is alerted DOWN before its retirement"
reply alpha - retired -
echo "RETIRED=telegram-retire-20261001" >> "$TMP/reply/alpha"
run_state "alpha beta" CCC_FLEET_WATCH_NOW=2026-10-01T01:41:00Z
okc "$RC" 0 "state mode: a first retired answer does not page"
ok "state mode: retirement is a recovery in progress" \
  'grep -q "^RECOVERING alpha channel=telegram was=DOWN now=RETIRED confirm=1/2$" "$OUT"'
run_state "alpha beta" CCC_FLEET_WATCH_NOW=2026-10-01T01:42:00Z
okc "$RC" 1 "state mode: a confirmed retirement closes the alert, once"
ok "state mode: RECOVERED says the pair is retired, not restored" \
  'grep -q "^RECOVERED alpha channel=telegram was=DOWN now=RETIRED since=2026-10-01T01:35:00Z for=0h07m$" "$OUT"'
run_state "alpha beta" CCC_FLEET_WATCH_NOW=2026-10-01T01:43:00Z
okc "$RC" 0 "state mode: a retired pair then stays quiet"
ok "state mode: retired pairs are counted, not listed" \
  '! grep -q " alpha " "$OUT" && grep -q "^SUMMARY checked=2 ok=0 retired=1 " "$OUT"'
reply alpha /opt/ccc-node yes /opt/ccc-node
# An explicitly requested recheck still works in state mode.
run_state "beta" CCC_FLEET_WATCH_NOW=2026-10-01T01:45:00Z CCC_FLEET_DEGRADED_RECHECK=1
ok "state mode: CCC_FLEET_DEGRADED_RECHECK=1 is honoured" '[ "$(wc -l < "$TMP/reply/beta.flags")" = 2 ]'
# The env form selects the same mode.
OUT="$TMP/out"; RC=0; rm -f "$TMP"/reply/*.seen
CCC_FLEET_NODES="beta" CCC_FLEET_SSH="$STUB" CCC_FLEET_SELF=_never_ CCC_FLEET_RETRY_DELAY=0 \
  CCC_FLEET_WATCH_STATE="$STATE" CCC_FLEET_WATCH_NOW=2026-10-01T02:00:00Z bash "$SC" >"$OUT" 2>&1 || RC=$?
ok "state mode: CCC_FLEET_WATCH_STATE selects change mode" 'grep -q "^KNOWN DEGRADED beta " "$OUT"'

# A corrupt state file warns and starts over; it never crashes the watch.
printf '{not json' > "$STATE"
run_state "beta" CCC_FLEET_WATCH_NOW=2026-10-01T02:15:00Z
okc "$RC" 0 "state mode: corrupt state restarts from empty state"
ok "state mode: corrupt state is reported on stderr" 'grep -q "not valid JSON" "$TMP/err"'
ok "state mode: corrupt state is replaced" 'python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$STATE"'

# State that cannot be written falls back to the raw verdicts, loudly.
printf 'x' > "$TMP/not-a-dir"
OUT="$TMP/out"; RC=0; rm -f "$TMP"/reply/*.seen
CCC_FLEET_NODES="alpha beta" CCC_FLEET_SSH="$STUB" CCC_FLEET_SELF=_never_ CCC_FLEET_RETRY_DELAY=0 \
  bash "$SC" --state-file "$TMP/not-a-dir/state.json" >"$OUT" 2>"$TMP/err" || RC=$?
okc "$RC" 1 "state mode: unusable state pages"
ok "state mode: unusable state names the watcher, then abnormal rows before OK rows" \
  '[ "$(sed -n 1p "$OUT")" = "UNVERIFIED watcher state-file=unusable" ] && sed -n 2p "$OUT" | grep -q "^DEGRADED beta " && sed -n 3p "$OUT" | grep -q "^OK alpha "'

# Without the option nothing changes, and a mistyped option is refused.
run "alpha beta"
ok "plain mode output is unchanged" 'grep -q "^OK alpha (/opt/ccc-node)$" "$OUT" && ! grep -qE "^(NEW|PENDING|SUMMARY) " "$OUT"'
OUT="$TMP/out"; RC=0
bash "$SC" --state-flie "$STATE" >"$OUT" 2>&1 || RC=$?
okc "$RC" 2 "an unknown option is refused rather than ignored"
OUT="$TMP/out"; RC=0
bash "$SC" --state-file >"$OUT" 2>&1 || RC=$?
okc "$RC" 2 "--state-file without a path is refused"

# ---- --light cadence mode (#2086) -------------------------------------------
# Never the doctor, fewer transport retries, and a run deadline.
reply alpha /opt/ccc-node yes /opt/ccc-node
OUT="$TMP/out"; RC=0; rm -f "$TMP"/reply/*.seen "$TMP"/reply/*.flags "$TMP"/reply/*.acct
CCC_FLEET_NODES="alpha" CCC_FLEET_SSH="$STUB" CCC_FLEET_SELF=_never_ CCC_FLEET_DOCTOR=1 \
  bash "$SC" --light >"$OUT" 2>"$TMP/err" || RC=$?
okc "$RC" 0 "light mode: a healthy node passes"
ok "light mode: the doctor is never requested" '[ "$(cat "$TMP/reply/alpha.flags")" = "CCC_FLEET_DOCTOR=0" ]'
ok "light mode: an ignored doctor request is said out loud" 'grep -q "CCC_FLEET_DOCTOR ignored" "$TMP/err"'
: > "$TMP/calls"
OUT="$TMP/out"; RC=0
CCC_FLEET_NODES="ghost3" CCC_FLEET_SSH="$FLAKY" CCC_FLEET_SELF=_never_ CCC_FLEET_RETRY_DELAY=0 \
  bash "$SC" --light >"$OUT" 2>&1 || RC=$?
ok "light mode: one transport retry by default" '[ "$(grep -c "^ghost3$" "$TMP/calls")" = 2 ]'
# The deadline is checked against bash $SECONDS, which is whole wall-clock
# seconds truncated (time(NULL) - start), not elapsed time: it can read 1
# a few milliseconds after start if a second boundary passes. With
# DEADLINE=1 that skipped slow1 too on 6 of 60 local runs under ordinary
# load (#2116, merge-queue eviction of #2113). DEADLINE=3 keeps slow1
# inside the deadline unless startup takes >2s, and the 4s probe guarantees
# slow2 lands past it.
SLOW="$TMP/ssh-slow"
cat > "$SLOW" <<SLOWEOF
#!$(command -v bash)
cat >/dev/null
sleep 4
printf 'RUNTIME=/opt/ccc-node\nAVAIL=yes\nUNIT=/opt/ccc-node\nPROBE_COMPLETE=1\n'
SLOWEOF
chmod +x "$SLOW"
OUT="$TMP/out"; RC=0
CCC_FLEET_NODES="slow1 slow2" CCC_FLEET_SSH="$SLOW" CCC_FLEET_SELF=_never_ CCC_FLEET_DEADLINE=3 \
  bash "$SC" --light >"$OUT" 2>&1 || RC=$?
okc "$RC" 1 "light mode: nodes past the deadline are not silently skipped"
ok "light mode: the node probed in time is judged, the rest are UNVERIFIED" \
  'grep -q "^OK slow1 " "$OUT" && grep -q "^UNVERIFIED slow2 inspection=deadline-3s$" "$OUT"'

python3 "$ROOT/scripts/fleet_watch_state_test.py"
okc "$?" 0 "change-based alert state transitions and state-file safety"
python3 "$ROOT/scripts/fleet_watch_metadata_test.py"
okc "$?" 0 "strict metadata and actual Git provenance regressions"
python3 "$ROOT/scripts/fleet_watch_permissions_test.py"
okc "$?" 0 "actual unprivileged Git permission regressions"

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
