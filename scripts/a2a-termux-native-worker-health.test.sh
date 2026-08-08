#!/usr/bin/env bash
# Tests for the Termux-native A2A worker health check.
#
# We drive the real health script but replace its underlying dependencies
# with predictable stubs:
#   * A2A_PYTHON_HARNESS -> a bash mock that returns rc=0/rc=2 on `check`.
#   * A mock curl on PATH so tunnel state is deterministic.
#   * A2A_SUPERVISOR_LOCK / LOG paths under $TMP so nothing touches $HOME/.a2a.
#
# The cap-detector cases spawn a `sleep 3600` renamed via `-a` argv0 to
# something matching CANONICAL_SIG or LEGACY_SIG, so pgrep -f picks them up
# without needing a real supervisor to be running.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HEALTH="$ROOT/scripts/a2a-termux-native-worker-health.sh"
HARNESS="$ROOT/scripts/a2a-termux-native-worker.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
# Fixtures supply every CCC_* input this suite needs; ambient harness variables
# from a live node must not reach them (#1023).
ccc_test_reset_hook_env

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
# Resolve real setsid BEFORE we prepend $TMP/bin (with mock setsid) to PATH,
# so the fake-supervisor helpers can detach properly (ppid=1) while the
# health check under test still sees the mock.
REAL_SETSID="$(command -v setsid || echo /data/data/com.termux/files/usr/bin/setsid)"
OWNED_PIDS=()
cleanup_test() {
    trap - EXIT
    bash "$HARNESS" stop >/dev/null 2>&1 || true
    local pid
    for pid in "${OWNED_PIDS[@]}"; do
        [[ -n "$pid" ]] && kill -KILL "$pid" 2>/dev/null || true
    done
    for pid in "${OWNED_PIDS[@]}"; do
        [[ -n "$pid" ]] && wait "$pid" 2>/dev/null || true
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
    fi
}

# Mock Python harness — `check` OK by default.
cat > "$TMP/mock-python-harness.sh" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
    check) exit 0 ;;
    run)   exec sleep 3600 ;;
    *)     exit 2 ;;
esac
EOF
chmod +x "$TMP/mock-python-harness.sh"

# Failing Python mock — used for env-validation-failure path.
cat > "$TMP/mock-python-harness-fail.sh" <<'EOF'
#!/usr/bin/env bash
echo "mock: forcing check failure" >&2
exit 2
EOF
chmod +x "$TMP/mock-python-harness-fail.sh"

# Mock curl — includes an HTTP-error mode that succeeds unless callers use -f.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/curl" <<'EOF'
#!/usr/bin/env bash
case "${A2A_TEST_CURL_MODE:-down}" in
    up) exit 0 ;;
    http-error)
        [[ " $* " == *" -f"* || " $* " == *" -fs"* ]] && exit 22
        exit 0
        ;;
    *) exit 7 ;;
esac
EOF
chmod +x "$TMP/bin/curl"

# Tunnel invocations block like a live ssh process; reachability probes return
# the requested deterministic result.
cat > "$TMP/bin/ssh" <<'EOF'
#!/usr/bin/env bash
if [[ " $* " == *" -N "* ]]; then
    exec sleep 3600
fi
[[ "${A2A_TEST_SSH_OK:-0}" == "1" ]] && exit 0
exit 255
EOF
chmod +x "$TMP/bin/ssh"

# Mock setsid records every invocation.  In spawn mode it delegates to the real
# binary; in noop mode it reproduces the corrupt 227-byte test stub incident by
# returning rc=0 without starting anything.
cat > "$TMP/bin/setsid" <<EOF
#!/usr/bin/env bash
printf 'setsid %s\n' "\$*" >> "$TMP/setsid-invocations.log"
[[ "\${A2A_TEST_SETSID_MODE:-noop}" == "spawn" ]] && exec "$REAL_SETSID" "\$@"
exit 0
EOF
chmod +x "$TMP/bin/setsid"

# Minimal env — the mock python `check` doesn't read anything.
ENVF="$TMP/canonical.env"
cat > "$ENVF" <<EOF
A2A_TUNNEL_SSH_TARGET=fake-target
A2A_WORKER_ROOT=$TMP/worker-root
EOF
mkdir -p "$TMP/worker-root/dist"

# Wire everything to test-local dirs.
export A2A_SUPERVISOR_LOCK_DIR="$TMP"
export A2A_SUPERVISOR_LOG_DIR="$TMP"
export A2A_SUPERVISOR_LOCK="$TMP/sup-\"quoted\\path.lock"
export A2A_SUPERVISOR_LOG="$TMP/sup.log"
export A2A_SUPERVISOR_HEALTH_LOG="$TMP/health.log"
export A2A_PYTHON_HARNESS="$TMP/mock-python-harness.sh"
export A2A_TEST_CURL_MODE=down
export A2A_TEST_SETSID_MODE=noop
export PATH="$TMP/bin:$PATH"
PIDFILE="$A2A_SUPERVISOR_LOCK.pid"

start_owned_supervisor() {
    rm -f "$PIDFILE"
    bash "$HARNESS" supervise --env-file "$ENVF" </dev/null >/dev/null 2>&1 &
    local pid=$!
    OWNED_PIDS+=("$pid")
    for _ in $(seq 1 50); do
        [[ -s "$PIDFILE" ]] && break
        sleep 0.1
    done
    [[ -s "$PIDFILE" ]] || return 1
    printf '%s' "$pid"
}

remember_pidfile_owner() {
    local pid
    read -r pid _ < "$PIDFILE" || return 1
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    OWNED_PIDS+=("$pid")
}

stop_owned_supervisor() {
    bash "$HARNESS" stop >/dev/null 2>&1 || true
    rm -f "$PIDFILE"
}

# ---- 1. Usage / arg-parsing paths ------------------------------------------

out="$(bash "$HEALTH" 2>&1)"; rc=$?
ok "no args prints usage rc=2" '[ "$rc" = 2 ] && grep -q "Usage:" <<<"$out"'

out="$(bash "$HEALTH" --help 2>&1)"; rc=$?
ok "--help prints usage rc=2" '[ "$rc" = 2 ] && grep -q "self-heal" <<<"$out"'

out="$(bash "$HEALTH" --env-file /no/such/file 2>&1)"; rc=$?
ok "missing env file fails rc=2" '[ "$rc" = 2 ] && grep -q "env file not found" <<<"$out"'

out="$(bash "$HEALTH" --env-file "$ENVF" --bogus 2>&1)"; rc=$?
ok "unknown arg fails rc=2" '[ "$rc" = 2 ] && grep -q "unknown arg" <<<"$out"'

for bad_opt in \
    "--max-supervisors nope" \
    "--max-supervisors 0" \
    "--tunnel-down-restart-after -1" \
    "--tunnel-restart-cooldown 999999999"; do
    # shellcheck disable=SC2086  # deliberate option/value pair expansion
    out="$(bash "$HEALTH" --env-file "$ENVF" $bad_opt 2>&1)"; rc=$?
    ok "invalid numeric option '$bad_opt' fails rc=2" \
        '[ "$rc" = 2 ] && grep -q "must be an integer" <<<"$out"'
done

# ---- 2. Env validation surfaces from the harness ---------------------------

A2A_PYTHON_HARNESS="$TMP/mock-python-harness-fail.sh" \
    bash "$HEALTH" --env-file "$ENVF" --no-self-heal >/dev/null 2>&1
rc=$?
ok "env validation failure returns rc=2" '[ "$rc" = 2 ]'

# Sections 3–8 need to be isolated from any legitimate supervisor process the
# host may actually be running (this test lives on the same nodes we deploy
# to, so `pgrep -f a2a-termux-native-worker.sh supervise` can be non-empty
# even under a healthy singleton).  We pass a generous --max-supervisors so
# the cap check never fires until section 9 exercises it deliberately.
BIG_CAP="--max-supervisors 99"

# ---- 3. No supervisor, --no-self-heal (read-only path) ---------------------

rm -f "$PIDFILE"
out="$(bash "$HEALTH" --env-file "$ENVF" --no-self-heal $BIG_CAP 2>&1)"; rc=$?
ok "no supervisor + --no-self-heal fails closed rc=3" \
    '[ "$rc" = 3 ] && grep -q "DOWN no supervisor" <<<"$out"'

# A stale PID record naming an unrelated live process must be ignored, and
# `stop` must never signal that process.
sleep 3600 >/dev/null 2>&1 &
STALE_PID=$!
OWNED_PIDS+=("$STALE_PID")
printf '%s 1\n' "$STALE_PID" > "$PIDFILE"
out="$(bash "$HARNESS" stop 2>&1)"; rc=$?
ok "stale/reused PID record is not signaled" \
    '[ "$rc" = 0 ] && kill -0 "$STALE_PID" 2>/dev/null && grep -q "no supervisor" <<<"$out"'
rm -f "$PIDFILE"

# ---- 4. No supervisor, --self-heal spawns via setsid -----------------------

: > "$TMP/setsid-invocations.log"
rm -f "$PIDFILE"
out="$(bash "$HEALTH" --env-file "$ENVF" --self-heal $BIG_CAP 2>&1)"; rc=$?
ok "no-op setsid cannot claim a successful self-heal" \
    '[ "$rc" = 5 ] && grep -q "no canonical supervisor" <<<"$out"'
ok "self-heal invoked setsid on the harness" \
    'grep -q "setsid.*-f bash .*a2a-termux-native-worker\.sh supervise" "$TMP/setsid-invocations.log"'

export A2A_TEST_SETSID_MODE=spawn
out="$(bash "$HEALTH" --env-file "$ENVF" --self-heal $BIG_CAP 2>&1)"; rc=$?
ok "real setsid + canonical PID identity self-heals" \
    '[ "$rc" = 0 ] && grep -q "STARTED" <<<"$out" && [ "$(wc -w < "$PIDFILE")" = 2 ]'
remember_pidfile_owner || true

# ---- 5. Supervisor running, tunnel DOWN -> rc=3 ----------------------------

export A2A_TEST_CURL_MODE=down
out="$(bash "$HEALTH" --env-file "$ENVF" --no-self-heal $BIG_CAP 2>&1)"; rc=$?
ok "supervisor up + tunnel DOWN returns rc=3" '[ "$rc" = 3 ] && grep -q "tunnel DOWN" <<<"$out"'

export A2A_TEST_CURL_MODE=http-error
out="$(bash "$HEALTH" --env-file "$ENVF" --no-self-heal $BIG_CAP 2>&1)"; rc=$?
ok "HTTP 5xx-style curl response is DOWN because -f is required" \
    '[ "$rc" = 3 ] && grep -q "tunnel DOWN" <<<"$out"'

# ---- 6. Supervisor running, tunnel UP -> rc=0 OK ---------------------------

export A2A_TEST_CURL_MODE=up
out="$(bash "$HEALTH" --env-file "$ENVF" --no-self-heal $BIG_CAP 2>&1)"; rc=$?
ok "supervisor up + tunnel UP returns rc=0" '[ "$rc" = 0 ] && grep -qE "^OK sup=[0-9]+" <<<"$out"'
ok "OK line reports cap=N/99" 'grep -qE "cap=[0-9]+/99" <<<"$out"'

# ---- 7. --json emits one-line JSON summary ---------------------------------

export A2A_TEST_CURL_MODE=up
out="$(bash "$HEALTH" --env-file "$ENVF" --no-self-heal --json --quiet $BIG_CAP 2>&1)"; rc=$?
ok "json output is one line" '[ "$rc" = 0 ] && [ "$(printf %s "$out" | wc -l)" -le 1 ]'
ok "json carries schema + action=ok" \
    'grep -q "\"schema\":\"a2a-native-worker-health.v1\"" <<<"$out" && grep -q "\"action\":\"ok\"" <<<"$out"'
ok "json safely round-trips quoted/backslash lock path" \
    'python3 -c '\''import json,sys; assert json.loads(sys.stdin.read())["lock"] == sys.argv[1]'\'' "$A2A_SUPERVISOR_LOCK" <<<"$out"'

# ---- 8. --quiet suppresses OK output but keeps rc=0 ------------------------

export A2A_TEST_CURL_MODE=up
out="$(bash "$HEALTH" --env-file "$ENVF" --no-self-heal --quiet $BIG_CAP 2>&1)"; rc=$?
ok "--quiet suppresses OK output" '[ "$rc" = 0 ] && [ -z "$out" ]'
stop_owned_supervisor

# ---- 9. Supervisor-count cap violation -> rc=4 (ND-1236) -------------------

# Spawn a fake supervisor-looking process (argv0 matches CANONICAL_SIG),
# then invoke health with --max-supervisors=1 while the singleton PIDFILE
# already looks occupied.  With 2 canonical-looking pids, the cap check
# should fire and refuse to self-heal.
rm -f "$PIDFILE"

start_fake_supervisor() {
    # `exec -a <argv0>` renames the process so pgrep -f matches.  `setsid -f`
    # detaches the process so its ppid=1 — required because the health
    # checker filters supervisors by ppid=1 (a real canonical supervisor is
    # setsid-detached).  Without setsid the fake would be a child of the
    # test shell (ppid=test-shell-pid) and wouldn't count.
    # Marker file lets us map argv0 -> spawned PID for later cleanup, since
    # setsid detaches the process out of $! tracking.
    local marker="$TMP/fake-sup-$$-$RANDOM.pid"
    # Use REAL_SETSID (resolved before mock injection) so the fake actually
    # detaches — the mock setsid on PATH just records invocations.
    "$REAL_SETSID" -f bash -c "echo \$\$ > $marker; exec -a 'bash /path/a2a-termux-native-worker.sh supervise --env-file /x' sleep 3600" \
        </dev/null >/dev/null 2>&1
    # Wait briefly for the marker to appear.
    for _ in $(seq 1 20); do
        [[ -s "$marker" ]] && break
        sleep 0.1
    done
    cat "$marker" 2>/dev/null
}
FAKE1=$(start_fake_supervisor)
FAKE2=$(start_fake_supervisor)
OWNED_PIDS+=("$FAKE1" "$FAKE2")

# Give the shells a moment to actually exec sleep.
for _ in $(seq 1 20); do
    n=$(pgrep -f 'a2a-termux-native-worker\.sh[[:space:]]+supervise' 2>/dev/null | wc -l)
    [[ "$n" -ge 2 ]] && break
    sleep 0.1
done

out="$(bash "$HEALTH" --env-file "$ENVF" --no-self-heal --max-supervisors 1 2>&1)"; rc=$?
ok "cap violation returns rc=4" '[ "$rc" = 4 ] && grep -q "MANUAL SWEEP REQUIRED" <<<"$out"'
ok "cap violation cites ND-1236" 'grep -q "ND-1236" <<<"$out"'

# Cap violation must NOT self-heal.
: > "$TMP/setsid-invocations.log"
bash "$HEALTH" --env-file "$ENVF" --self-heal --max-supervisors 1 >/dev/null 2>&1 || true
ok "cap violation blocks self-heal (setsid not invoked)" \
    '[ ! -s "$TMP/setsid-invocations.log" ]'

# Raising the cap defuses the check.
out="$(bash "$HEALTH" --env-file "$ENVF" --no-self-heal --max-supervisors 5 2>&1)"; rc=$?
ok "raising --max-supervisors clears cap violation" \
    '[ "$rc" = 3 ] && ! grep -q "MANUAL SWEEP" <<<"$out"'

kill -KILL "$FAKE1" "$FAKE2" 2>/dev/null || true

# ---- 10. Legacy-script pattern is ALSO caught by the cap detector ---------

# Same shape as #9 but argv0 mimics ~/.hermes/scripts/native-worker-supervisor.sh
# so we prove a pre-migration node running BOTH scripts trips the detector.
rm -f "$PIDFILE"
start_fake_legacy() {
    # Same ppid=1 detachment requirement as start_fake_supervisor.
    local marker="$TMP/fake-leg-$$-$RANDOM.pid"
    "$REAL_SETSID" -f bash -c "echo \$\$ > $marker; exec -a 'bash /root/.hermes/scripts/native-worker-supervisor.sh' sleep 3600" \
        </dev/null >/dev/null 2>&1
    for _ in $(seq 1 20); do
        [[ -s "$marker" ]] && break
        sleep 0.1
    done
    cat "$marker" 2>/dev/null
}
LEG1=$(start_fake_legacy)
LEG2=$(start_fake_legacy)
OWNED_PIDS+=("$LEG1" "$LEG2")
for _ in $(seq 1 20); do
    n=$(pgrep -f 'native-worker-supervisor\.sh' 2>/dev/null | wc -l)
    [[ "$n" -ge 2 ]] && break
    sleep 0.1
done
out="$(bash "$HEALTH" --env-file "$ENVF" --no-self-heal --max-supervisors 1 2>&1)"; rc=$?
ok "legacy supervisor pile-up trips cap (ND-1236)" \
    '[ "$rc" = 4 ] && grep -q "MANUAL SWEEP" <<<"$out"'
kill -KILL "$LEG1" "$LEG2" 2>/dev/null || true

# ---- 11. tunnel-down controlled recovery (#810) ----------------------------
# A supervisor up + tunnel DOWN is the ND-1236 stuck state.  Under --self-heal
# the checker restarts it, but only after N down cycles, when the ssh target is
# reachable, and outside the restart cooldown.  The ssh/setsid mocks above let
# us run real canonical supervisors while keeping every child test-owned.

STREAK_FILE="$A2A_SUPERVISOR_LOCK.tunnel_down_streak"
TS_FILE="$A2A_SUPERVISOR_LOCK.tunnel_restart_ts"

fresh_fake_sup() { start_owned_supervisor; }

export A2A_TEST_CURL_MODE=down   # tunnel DOWN for 11a–11e

# 11a. streak below threshold → defer (rc=3), no respawn.
rm -f "$STREAK_FILE" "$TS_FILE"; : > "$TMP/setsid-invocations.log"
fake=$(fresh_fake_sup); export A2A_TEST_SSH_OK=1
OWNED_PIDS+=("$fake")
out="$(bash "$HEALTH" --env-file "$ENVF" --self-heal --tunnel-down-restart-after 3 $BIG_CAP 2>&1)"; rc=$?
ok "tunnel-down below threshold defers (rc=3)" '[ "$rc" = 3 ] && grep -q "deferring restart" <<<"$out"'
ok "deferral does not respawn (no setsid)" '[ ! -s "$TMP/setsid-invocations.log" ]'
ok "deferral records streak=1" '[ "$(cat "$STREAK_FILE" 2>/dev/null)" = 1 ]'
stop_owned_supervisor

# 11b. streak at threshold + reachable → controlled restart (rc=0, setsid).
rm -f "$TS_FILE"; printf '2\n' > "$STREAK_FILE"; : > "$TMP/setsid-invocations.log"
fake=$(fresh_fake_sup); export A2A_TEST_SSH_OK=1
OWNED_PIDS+=("$fake")
out="$(bash "$HEALTH" --env-file "$ENVF" --self-heal --tunnel-down-restart-after 3 $BIG_CAP 2>&1)"; rc=$?
ok "tunnel-down at threshold + reachable restarts (rc=0)" '[ "$rc" = 0 ] && grep -q "restarted supervisor" <<<"$out"'
ok "recovery respawns via setsid supervise" 'grep -q "setsid.*supervise" "$TMP/setsid-invocations.log"'
ok "recovery clears the streak" '[ ! -f "$STREAK_FILE" ]'
ok "recovery records a restart timestamp" '[ -s "$TS_FILE" ]'
remember_pidfile_owner || true
stop_owned_supervisor

# 11c. ssh target unreachable → never restart (rc=3), even past threshold.
rm -f "$TS_FILE"; printf '5\n' > "$STREAK_FILE"; : > "$TMP/setsid-invocations.log"
fake=$(fresh_fake_sup); export A2A_TEST_SSH_OK=0
OWNED_PIDS+=("$fake")
out="$(bash "$HEALTH" --env-file "$ENVF" --self-heal --tunnel-down-restart-after 3 $BIG_CAP 2>&1)"; rc=$?
ok "unreachable target does not restart (rc=3)" '[ "$rc" = 3 ]'
ok "unreachable → no setsid respawn" '[ ! -s "$TMP/setsid-invocations.log" ]'
stop_owned_supervisor

# 11d. cooldown active → defer even past threshold + reachable.
printf '9\n' > "$STREAK_FILE"; date +%s > "$TS_FILE"; : > "$TMP/setsid-invocations.log"
fake=$(fresh_fake_sup); export A2A_TEST_SSH_OK=1
OWNED_PIDS+=("$fake")
out="$(bash "$HEALTH" --env-file "$ENVF" --self-heal --tunnel-down-restart-after 3 --tunnel-restart-cooldown 3600 $BIG_CAP 2>&1)"; rc=$?
ok "restart cooldown defers restart (rc=3)" '[ "$rc" = 3 ] && grep -q "cooldown" <<<"$out"'
ok "cooldown → no setsid respawn" '[ ! -s "$TMP/setsid-invocations.log" ]'
stop_owned_supervisor

# 11e. --no-tunnel-recovery keeps the legacy rc=3 flag and writes no state.
rm -f "$STREAK_FILE" "$TS_FILE"; : > "$TMP/setsid-invocations.log"
fake=$(fresh_fake_sup); export A2A_TEST_SSH_OK=1
OWNED_PIDS+=("$fake")
out="$(bash "$HEALTH" --env-file "$ENVF" --self-heal --no-tunnel-recovery $BIG_CAP 2>&1)"; rc=$?
ok "--no-tunnel-recovery flags rc=3 (legacy behavior)" '[ "$rc" = 3 ] && grep -q "self-heal cannot fix" <<<"$out"'
ok "--no-tunnel-recovery writes no streak file" '[ ! -f "$STREAK_FILE" ]'
stop_owned_supervisor

# 11f. tunnel UP clears a pending streak.
printf '2\n' > "$STREAK_FILE"
fake=$(fresh_fake_sup); export A2A_TEST_CURL_MODE=up
OWNED_PIDS+=("$fake")
# shellcheck disable=SC2034  # out is consumed by the eval-based ok() assertion.
out="$(bash "$HEALTH" --env-file "$ENVF" --self-heal $BIG_CAP 2>&1)"
# shellcheck disable=SC2034  # rc is consumed by the eval-based ok() assertion.
rc=$?
ok "tunnel UP returns rc=0 and clears the streak" '[ "$rc" = 0 ] && [ ! -f "$STREAK_FILE" ]'
stop_owned_supervisor
export A2A_TEST_CURL_MODE=down

# ---- 12. Worker-root matching treats paths as data, never regex ------------
SPECIAL_ROOT="$TMP/worker.[x]\\literal"
mkdir -p "$SPECIAL_ROOT/dist" "$TMP/worker.x\\literal/dist"
python3 -c 'import time; time.sleep(3600)' "$SPECIAL_ROOT/dist/worker.js" &
EXACT_WORKER_PID=$!
python3 -c 'import time; time.sleep(3600)' "$TMP/worker.x\\literal/dist/workerXjs" &
DECOY_WORKER_PID=$!
OWNED_PIDS+=("$EXACT_WORKER_PID" "$DECOY_WORKER_PID")
sleep 0.2
# shellcheck disable=SC2034  # count is consumed by the eval-based ok() assertion.
count=$(bash -c 'source "$1"; count_workers_under "$2"' _ "$HEALTH" "$SPECIAL_ROOT")
ok "special-character worker root counts exact argv only" '[ "$count" = 1 ]'
kill -KILL "$EXACT_WORKER_PID" "$DECOY_WORKER_PID" 2>/dev/null || true
wait "$EXACT_WORKER_PID" "$DECOY_WORKER_PID" 2>/dev/null || true

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
