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

pass=0; fail=0
TMP="$(mktemp -d)"
# Resolve real setsid BEFORE we prepend $TMP/bin (with mock setsid) to PATH,
# so the fake-supervisor helpers can detach properly (ppid=1) while the
# health check under test still sees the mock.
REAL_SETSID="$(command -v setsid || echo /data/data/com.termux/files/usr/bin/setsid)"
trap 'trap - EXIT; jobs -p | xargs -r kill -KILL 2>/dev/null; rm -rf "$TMP"' EXIT

# Pre-flight: kill any leftover fake-supervisor processes from previous test
# runs so the cap-detector tests below aren't polluted by orphaned sleeps.
pkill -KILL -f 'exec -a "bash .*a2a-termux-native-worker\.sh supervise' 2>/dev/null || true
pkill -KILL -f 'exec -a "bash .*native-worker-supervisor\.sh' 2>/dev/null || true

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

# Mock curl — flips based on A2A_TEST_CURL_OK.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/curl" <<'EOF'
#!/usr/bin/env bash
[[ "${A2A_TEST_CURL_OK:-0}" == "1" ]] && exit 0
exit 7
EOF
chmod +x "$TMP/bin/curl"

# Mock setsid so --self-heal spawns are inspectable and don't leak processes.
cat > "$TMP/bin/setsid" <<EOF
#!/usr/bin/env bash
# Record the invocation and return immediately.  We just want to prove the
# health script tried to spawn a supervisor; we don't need to run it.
printf 'setsid %s\n' "\$*" >> "$TMP/setsid-invocations.log"
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
export A2A_SUPERVISOR_LOCK="$TMP/sup.lock"
export A2A_SUPERVISOR_LOG="$TMP/sup.log"
export A2A_SUPERVISOR_HEALTH_LOG="$TMP/health.log"
export A2A_PYTHON_HARNESS="$TMP/mock-python-harness.sh"
export A2A_TEST_CURL_OK=0
export PATH="$TMP/bin:$PATH"
PIDFILE="$A2A_SUPERVISOR_LOCK.pid"

# ---- 1. Usage / arg-parsing paths ------------------------------------------

out="$(bash "$HEALTH" 2>&1)"; rc=$?
ok "no args prints usage rc=2" '[ "$rc" = 2 ] && grep -q "Usage:" <<<"$out"'

out="$(bash "$HEALTH" --help 2>&1)"; rc=$?
ok "--help prints usage rc=2" '[ "$rc" = 2 ] && grep -q "self-heal" <<<"$out"'

out="$(bash "$HEALTH" --env-file /no/such/file 2>&1)"; rc=$?
ok "missing env file fails rc=2" '[ "$rc" = 2 ] && grep -q "env file not found" <<<"$out"'

out="$(bash "$HEALTH" --env-file "$ENVF" --bogus 2>&1)"; rc=$?
ok "unknown arg fails rc=2" '[ "$rc" = 2 ] && grep -q "unknown arg" <<<"$out"'

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
ok "no supervisor + --no-self-heal rc=0" '[ "$rc" = 0 ] && grep -q "DOWN no supervisor" <<<"$out"'

# ---- 4. No supervisor, --self-heal spawns via setsid -----------------------

: > "$TMP/setsid-invocations.log"
rm -f "$PIDFILE"
out="$(bash "$HEALTH" --env-file "$ENVF" --self-heal $BIG_CAP 2>&1)"; rc=$?
ok "no supervisor + --self-heal returns rc=0" '[ "$rc" = 0 ] && grep -q "STARTED" <<<"$out"'
ok "self-heal invoked setsid on the harness" \
    'grep -q "setsid.*-f bash .*a2a-termux-native-worker\.sh supervise" "$TMP/setsid-invocations.log"'

# ---- 5. Supervisor running, tunnel DOWN -> rc=3 ----------------------------

# Fake a running supervisor by writing our own PID to the PID file.  We're
# alive so kill -0 succeeds, current_supervisor_pid returns our PID.
echo "$$" > "$PIDFILE"
export A2A_TEST_CURL_OK=0
out="$(bash "$HEALTH" --env-file "$ENVF" --no-self-heal $BIG_CAP 2>&1)"; rc=$?
ok "supervisor up + tunnel DOWN returns rc=3" '[ "$rc" = 3 ] && grep -q "tunnel DOWN" <<<"$out"'

# ---- 6. Supervisor running, tunnel UP -> rc=0 OK ---------------------------

export A2A_TEST_CURL_OK=1
out="$(bash "$HEALTH" --env-file "$ENVF" --no-self-heal $BIG_CAP 2>&1)"; rc=$?
ok "supervisor up + tunnel UP returns rc=0" '[ "$rc" = 0 ] && grep -qE "^OK sup=[0-9]+" <<<"$out"'
ok "OK line reports cap=N/99" 'grep -qE "cap=[0-9]+/99" <<<"$out"'

# ---- 7. --json emits one-line JSON summary ---------------------------------

export A2A_TEST_CURL_OK=1
echo "$$" > "$PIDFILE"
out="$(bash "$HEALTH" --env-file "$ENVF" --no-self-heal --json --quiet $BIG_CAP 2>&1)"; rc=$?
ok "json output is one line" '[ "$rc" = 0 ] && [ "$(printf %s "$out" | wc -l)" -le 1 ]'
ok "json carries schema + action=ok" \
    'grep -q "\"schema\":\"a2a-native-worker-health.v1\"" <<<"$out" && grep -q "\"action\":\"ok\"" <<<"$out"'

# ---- 8. --quiet suppresses OK output but keeps rc=0 ------------------------

echo "$$" > "$PIDFILE"
export A2A_TEST_CURL_OK=1
out="$(bash "$HEALTH" --env-file "$ENVF" --no-self-heal --quiet $BIG_CAP 2>&1)"; rc=$?
ok "--quiet suppresses OK output" '[ "$rc" = 0 ] && [ -z "$out" ]'

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
    '[ "$rc" = 0 ] && ! grep -q "MANUAL SWEEP" <<<"$out"'

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
for _ in $(seq 1 20); do
    n=$(pgrep -f 'native-worker-supervisor\.sh' 2>/dev/null | wc -l)
    [[ "$n" -ge 2 ]] && break
    sleep 0.1
done
out="$(bash "$HEALTH" --env-file "$ENVF" --no-self-heal --max-supervisors 1 2>&1)"; rc=$?
ok "legacy supervisor pile-up trips cap (ND-1236)" \
    '[ "$rc" = 4 ] && grep -q "MANUAL SWEEP" <<<"$out"'
kill -KILL "$LEG1" "$LEG2" 2>/dev/null || true

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
