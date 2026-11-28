#!/usr/bin/env bash
# Tests for the idle-gated restart commands of the Termux-native A2A worker
# harness (#2791 stage 2b): `restart-worker --if-idle` and
# `restart-supervisor --if-idle`.
#
# Same style as a2a-termux-native-worker-health.test.sh: the real harness and
# health script run against stubs.
#   * A2A_PYTHON_HARNESS -> a bash mock; `run` execs a Python fake worker whose
#     argv names $WORKER_ROOT/dist/worker.js, prints "registered with …", and
#     spawns a handler-like child when a trigger file appears.
#   * Mock curl (tunnel UP) and ssh (-N blocks) on PATH.
#   * Lock/log paths under $TMP.
# The idle gate itself is also unit-tested against a mock /proc tree
# (A2A_NATIVE_WORKER_PROC_ROOT).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HARNESS="$ROOT/scripts/a2a-termux-native-worker.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
REAL_SETSID="$(command -v setsid || echo /data/data/com.termux/files/usr/bin/setsid)"
OWNED_PIDS=()
cleanup_test() {
    trap - EXIT
    bash "$HARNESS" stop >/dev/null 2>&1 || true
    local pid f
    for f in "$TMP"/child-*.pid; do
        [[ -s "$f" ]] && OWNED_PIDS+=("$(cat "$f")")
    done
    for pid in "${OWNED_PIDS[@]}"; do
        [[ -n "$pid" ]] && kill -KILL "$pid" 2>/dev/null || true
    done
    jobs -p | xargs -r kill -KILL 2>/dev/null || true
    rm -rf "$TMP"
}
trap cleanup_test EXIT

ok() {
    if eval "$2"; then
        pass=$((pass+1))
    else
        fail=$((fail+1))
        echo "FAIL: $1"
        echo "  cond: $2"
        [[ -n "${out:-}" ]] && printf '  out: %s\n' "$out"
    fi
}

mkdir -p "$TMP/bin" "$TMP/worker-root/dist"
WORKER_JS="$TMP/worker-root/dist/worker.js"
: > "$WORKER_JS"

cat > "$TMP/mock-worker.py" <<'PY'
import os
import subprocess
import sys
import time

tmp = os.environ["A2A_TEST_TMP"]
if not os.path.exists(os.path.join(tmp, "no-register")):
    print("worker pid=%d registered with mock-broker" % os.getpid(), flush=True)
while True:
    trigger = os.path.join(tmp, "spawn-child")
    if os.path.exists(trigger):
        os.unlink(trigger)
        child = subprocess.Popen(["sleep", "3600"])
        with open(os.path.join(tmp, "child-%d.pid" % child.pid), "w") as f:
            f.write(str(child.pid))
    time.sleep(0.1)
PY

cat > "$TMP/mock-python-harness.sh" <<EOF
#!/usr/bin/env bash
case "\${1:-}" in
    check) exit 0 ;;
    run)   exec python3 "$TMP/mock-worker.py" "$WORKER_JS" ;;
    *)     exit 2 ;;
esac
EOF
chmod +x "$TMP/mock-python-harness.sh"

printf '#!/usr/bin/env bash\nexit 0\n' > "$TMP/bin/curl"
cat > "$TMP/bin/ssh" <<'EOF'
#!/usr/bin/env bash
[[ " $* " == *" -N "* ]] && exec sleep 3600
exit 255
EOF
chmod +x "$TMP/bin/curl" "$TMP/bin/ssh"

ENVF="$TMP/canonical.env"
cat > "$ENVF" <<EOF
A2A_TUNNEL_SSH_TARGET=fake-target
A2A_WORKER_ROOT=$TMP/worker-root
EOF

export A2A_TEST_TMP="$TMP"
export A2A_SUPERVISOR_LOCK_DIR="$TMP"
export A2A_SUPERVISOR_LOG_DIR="$TMP"
export A2A_SUPERVISOR_LOCK="$TMP/sup.lock"
export A2A_SUPERVISOR_LOG="$TMP/sup.log"
export A2A_SUPERVISOR_HEALTH_LOG="$TMP/health.log"
export A2A_PYTHON_HARNESS="$TMP/mock-python-harness.sh"
export A2A_NATIVE_WORKER_RESTART_SETTLE=1
export A2A_NATIVE_WORKER_RESTART_TIMEOUT=40
export PATH="$TMP/bin:$PATH"
PIDFILE="$A2A_SUPERVISOR_LOCK.pid"
: > "$A2A_SUPERVISOR_LOG"; : > "$A2A_SUPERVISOR_HEALTH_LOG"

# ---- 1. Argument handling ---------------------------------------------------
out="$(bash "$HARNESS" restart-worker --env-file "$ENVF" 2>&1)"; rc=$?
ok "restart-worker without --if-idle refuses (rc=2)" \
    '[ "$rc" = 2 ] && grep -q "requires --if-idle" <<<"$out"'
out="$(bash "$HARNESS" restart-supervisor 2>&1)"; rc=$?
ok "restart-supervisor without --if-idle refuses (rc=2)" \
    '[ "$rc" = 2 ] && grep -q "requires --if-idle" <<<"$out"'
out="$(bash "$HARNESS" status --if-idle 2>&1)"; rc=$?
ok "--if-idle is rejected on other commands" '[ "$rc" = 2 ] && grep -q "apply only to restart" <<<"$out"'
out="$(A2A_NATIVE_WORKER_BUSY_MAX_SECONDS=0 bash "$HARNESS" restart-worker --if-idle 2>&1)"; rc=$?
ok "busy-max 0 is rejected (rc=2)" '[ "$rc" = 2 ] && grep -q "A2A_NATIVE_WORKER_BUSY_MAX_SECONDS must be" <<<"$out"'
out="$(A2A_NATIVE_WORKER_RESTART_TIMEOUT=x bash "$HARNESS" restart-worker --if-idle 2>&1)"; rc=$?
ok "non-numeric restart timeout is rejected (rc=2)" '[ "$rc" = 2 ] && grep -q "RESTART_TIMEOUT must be" <<<"$out"'

# ---- 2. No supervisor -> rc=3, nothing spawned ------------------------------
rm -f "$PIDFILE"
out="$(bash "$HARNESS" restart-worker --if-idle --env-file "$ENVF" 2>&1)"; rc=$?
ok "no supervisor: restart-worker rc=3" '[ "$rc" = 3 ] && grep -q "result=no-supervisor" <<<"$out"'
out="$(bash "$HARNESS" restart-supervisor --if-idle --env-file "$ENVF" 2>&1)"; rc=$?
ok "no supervisor: restart-supervisor rc=3 and spawns nothing" \
    '[ "$rc" = 3 ] && grep -q "result=no-supervisor" <<<"$out" && [ ! -s "$PIDFILE" ]'

# ---- 3. Idle gate against a mock /proc tree ---------------------------------
# shellcheck source=scripts/a2a-termux-native-worker.sh
. "$HARNESS"
MOCK_PROC="$TMP/mock-proc"
# mock_proc_add <pid> <ppid> <age_seconds>: stat whose starttime is <age> ago
# on the same CLOCK_BOOTTIME the gate reads. The comm holds ") " to pin the
# rsplit parsing.
mock_proc_add() {
    mkdir -p "$MOCK_PROC/$1"
    python3 - "$1" "$2" "$3" "$MOCK_PROC/$1/stat" <<'PY'
import os, sys, time
pid, ppid, age, path = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
tick = os.sysconf("SC_CLK_TCK")
start = int((time.clock_gettime(time.CLOCK_BOOTTIME) - age) * tick)
open(path, "w").write("%s (odd) name) S %s %s\n" % (pid, ppid, " ".join(["0"] * 17 + [str(start), "0", "0"])))
PY
}
probe() { A2A_NATIVE_WORKER_PROC_ROOT="$MOCK_PROC" worker_busy_probe "$@"; }

rm -rf "$MOCK_PROC"; mock_proc_add 100 1 500; mock_proc_add 200 1 5
out="$(probe 100 1800)"; rc=$?
ok "mock /proc: worker without children is idle" '[ "$rc" = 0 ] && [ "$out" = "state=idle children=0" ]'
mock_proc_add 101 100 30
out="$(probe 100 1800)"; rc=$?
ok "mock /proc: a young handler child is busy (rc=10)" \
    '[ "$rc" = 10 ] && grep -q "^state=busy children=1 youngest=3[0-9]s$" <<<"$out"'
out="$(probe 100 20)"; rc=$?
ok "mock /proc: a child older than busy-max counts as hung, not busy" \
    '[ "$rc" = 0 ] && grep -q "^state=idle children=0 hung=1 hung_oldest=" <<<"$out"'
mock_proc_add 102 101 3
out="$(probe 100 20)"; rc=$?
ok "mock /proc: a young grandchild under a hung child is still busy" \
    '[ "$rc" = 10 ] && grep -q "children=1 youngest=[0-9]s hung=1" <<<"$out"'
out="$(probe 200 1800)"; rc=$?
ok "mock /proc: unrelated young processes do not make another pid busy" '[ "$rc" = 0 ]'
out="$(probe 999 1800)"; rc=$?
ok "mock /proc: unreadable worker stat is unknown (rc=11, fail-closed)" \
    '[ "$rc" = 11 ] && grep -q "state=unknown" <<<"$out"'

# ---- 4. Real supervisor + fake worker ---------------------------------------
# A canonical supervisor already running on this host (a live Termux node)
# would make sup_count=2; skip the live-process sections rather than lie.
if pgrep -f 'a2a-termux-native-worker\.sh[[:space:]]+supervise' >/dev/null 2>&1; then
    echo "SKIP: a supervise process already runs on this host; live restart sections skipped"
    echo "----"; echo "PASS=$pass FAIL=$fail"; [ "$fail" = 0 ]; exit
fi

sup_pid() { local p; read -r p _ < "$PIDFILE" 2>/dev/null || return 1; printf '%s' "$p"; }
worker_of() { # <sup_pid>: the fake worker child pid
    supervisor_worker_records "$1" "$WORKER_JS" | awk 'NR==1 {print $1}'
}
wait_registered_worker() { # <sup_pid> <not_pid>: echo the new worker pid
    local w
    for _ in $(seq 1 150); do
        w=$(worker_of "$1")
        if [[ -n "$w" && "$w" != "$2" ]] && grep -q "worker pid=$w registered with" "$A2A_SUPERVISOR_HEALTH_LOG"; then
            printf '%s' "$w"; return 0
        fi
        sleep 0.1
    done
    return 1
}
start_supervisor() {
    rm -f "$PIDFILE"
    "$REAL_SETSID" -f bash "$HARNESS" supervise --env-file "$ENVF" \
        </dev/null >>"$A2A_SUPERVISOR_HEALTH_LOG" 2>&1
    for _ in $(seq 1 50); do [[ -s "$PIDFILE" ]] && break; sleep 0.1; done
    sup_pid
}
spawn_child() { # ask the fake worker to start a handler-like child
    local before; before=$(compgen -G "$TMP/child-*.pid" | wc -l)
    touch "$TMP/spawn-child"
    for _ in $(seq 1 50); do
        [[ "$(compgen -G "$TMP/child-*.pid" | wc -l)" -gt "$before" ]] && return 0
        sleep 0.1
    done
    return 1
}

SUP=$(start_supervisor)
OWNED_PIDS+=("$SUP")
# shellcheck disable=SC2034  # W1 is read by the eval-based ok() assertions.
W1=$(wait_registered_worker "$SUP" "")
ok "fixture: canonical supervisor runs a registered fake worker" '[ -n "$SUP" ] && [ -n "$W1" ]'

# 4a. dry-run: gate only, nothing signaled.
out="$(bash "$HARNESS" restart-worker --if-idle --dry-run 2>&1)"; rc=$?
ok "dry-run on an idle worker reports would-restart and signals nothing" \
    '[ "$rc" = 0 ] && grep -q "result=would-restart worker_pid=$W1 " <<<"$out" && kill -0 "$W1" && [ "$(worker_of "$SUP")" = "$W1" ]'
ok "--env-file defaults to the running supervisor's own" 'grep -q "sup=$SUP" <<<"$out"'

# 4b. busy: a fresh handler child blocks both commands; nothing is signaled.
spawn_child
out="$(bash "$HARNESS" restart-worker --if-idle 2>&1)"; rc=$?
ok "busy worker: restart-worker exits 10 with the reason" \
    '[ "$rc" = 10 ] && grep -q "result=busy worker_pid=$W1 state=busy children=1 " <<<"$out" && grep -q "not restarted" <<<"$out"'
ok "busy worker is left running under the same supervisor" 'kill -0 "$W1" && [ "$(worker_of "$SUP")" = "$W1" ]'
out="$(bash "$HARNESS" restart-supervisor --if-idle 2>&1)"; rc=$?
ok "busy worker: restart-supervisor exits 10 and keeps the supervisor" \
    '[ "$rc" = 10 ] && grep -q "result=busy" <<<"$out" && [ "$(sup_pid)" = "$SUP" ] && kill -0 "$SUP"'
out="$(bash "$HARNESS" restart-worker --if-idle --dry-run 2>&1)"; rc=$?
ok "busy worker: dry-run exits 10 too" '[ "$rc" = 10 ]'

# 4c. unknown idle state (unreadable tree) fails closed.
mkdir -p "$TMP/empty-proc"
out="$(A2A_NATIVE_WORKER_PROC_ROOT="$TMP/empty-proc" bash "$HARNESS" restart-worker --if-idle 2>&1)"; rc=$?
ok "unreadable process tree: rc=11, worker untouched" \
    '[ "$rc" = 11 ] && grep -q "fail-closed" <<<"$out" && kill -0 "$W1"'

# 4d. the child outlives busy-max -> hung, does not block; restart verified.
sleep 1.2
out="$(A2A_NATIVE_WORKER_BUSY_MAX_SECONDS=1 bash "$HARNESS" restart-worker --if-idle 2>&1)"; rc=$?
W2=$(worker_of "$SUP")
ok "hung child past busy-max: restart-worker succeeds (rc=0)" \
    '[ "$rc" = 0 ] && grep -q "result=ok old_pid=$W1 new_pid=$W2 sup=$SUP registered=yes sup_count=1 workers=1 tunnel=UP rc=0" <<<"$out"'
ok "only the worker was restarted: same supervisor, old worker gone" \
    '[ "$(sup_pid)" = "$SUP" ] && kill -0 "$SUP" && ! kill -0 "$W1" 2>/dev/null && [ -n "$W2" ] && [ "$W2" != "$W1" ]'
ok "the hung child is reported in the log, not killed" \
    'grep -q "restart-worker: SIGTERM worker pid=$W1 .*hung=1" "$A2A_SUPERVISOR_LOG" && kill -0 "$(cat "$TMP"/child-*.pid | head -n1)"'

# 4e. a relaunched worker that never registers is reported unverified (rc=30).
touch "$TMP/no-register"
out="$(A2A_NATIVE_WORKER_RESTART_TIMEOUT=3 bash "$HARNESS" restart-worker --if-idle 2>&1)"; rc=$?
ok "no registration within the timeout: rc=30 unverified" \
    '[ "$rc" = 30 ] && grep -q "result=unverified reason=no-registered-worker" <<<"$out"'
rm -f "$TMP/no-register"
for _ in $(seq 1 150); do
    W3=$(worker_of "$SUP"); [[ -n "$W3" && "$W3" != "$W2" ]] && break; sleep 0.1
done

# 4f. restart-supervisor: dry-run, then a verified stop + respawn.
out="$(bash "$HARNESS" restart-supervisor --if-idle --dry-run 2>&1)"; rc=$?
ok "restart-supervisor dry-run signals nothing" \
    '[ "$rc" = 0 ] && grep -q "result=would-restart sup=$SUP " <<<"$out" && [ "$(sup_pid)" = "$SUP" ]'
out="$(bash "$HARNESS" restart-supervisor --if-idle 2>&1)"
# shellcheck disable=SC2034  # rc is read by the eval-based ok() assertion.
rc=$?
NEW_SUP=$(sup_pid)
OWNED_PIDS+=("$NEW_SUP")
ok "restart-supervisor respawns and verifies (rc=0)" \
    '[ "$rc" = 0 ] && grep -q "result=ok old_sup=$SUP new_sup=$NEW_SUP worker_pid=[0-9]* registered=yes sup_count=1 workers=1 tunnel=UP rc=0" <<<"$out"'
ok "the old supervisor is gone and a new one holds the lock" \
    '[ -n "$NEW_SUP" ] && [ "$NEW_SUP" != "$SUP" ] && ! kill -0 "$SUP" 2>/dev/null'

bash "$HARNESS" stop >/dev/null 2>&1 || true

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
