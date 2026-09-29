#!/usr/bin/env bash
# Tests for bridge/start.sh --restart (atomic stop→start→verify) and the
# service-systemd.sh is-managed probe it uses. Hermetic: fake HOME, fake
# CCC_SYSTEMD_DIR, stubbed systemctl, owned fake bot/provider processes and a
# CCC_BRIDGE_RESTART_SPAWN fake start command — the real bridge on this node is
# never probed, signaled, or started.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
START="$HERE/start.sh"
SSD="$HERE/service-systemd.sh"
pass=0; fail=0
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = "$2" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $3 (rc=$1 want=$2)"; fi; }

TMP="$(mktemp -d)"
# Fixture stubs name the build host's resolved bash, not `#!/usr/bin/env bash`
# which cannot resolve on Termux (#1153; same root as #472/#663).
. "$HERE/../claude/hooks/lib/test-stub.sh"
SPAWNED_PIDS="$TMP/spawned.pids"
: > "$SPAWNED_PIDS"
SELF_PARENT=""
cleanup() {
    # Only ever kill pids we spawned AND that are still plain `sleep`
    # processes — never a blind kill of pid-file contents.
    local p
    while IFS= read -r p; do
        [ -n "$p" ] || continue
        [ -r "/proc/$p/cmdline" ] || continue
        if tr '\0' ' ' < "/proc/$p/cmdline" | grep -q '^sleep '; then
            kill "$p" 2>/dev/null || true
        fi
    done < "$SPAWNED_PIDS"
    if [ -n "$SELF_PARENT" ] && kill -0 "$SELF_PARENT" 2>/dev/null; then
        local self_args
        self_args="$(tr '\0' ' ' < "/proc/$SELF_PARENT/cmdline" 2>/dev/null || true)"
        case "$self_args" in
            *"$TMP/fake-provider-self"*) kill "$SELF_PARENT" 2>/dev/null || true ;;
        esac
    fi
    rm -rf "$TMP"
}
trap cleanup EXIT

OUT="$TMP/out"; RC=0
run() { RC=0; "$@" >"$OUT" 2>&1 || RC=$?; }

# systemctl stubs: one where every call (incl. is-active) succeeds, one where
# is-active reports inactive (rc 3, like real systemctl).
SC_OK="$TMP/systemctl-ok"
write_exec_stub "$SC_OK" <<'SH'
exit 0
SH
SC_INACTIVE="$TMP/systemctl-inactive"
write_exec_stub "$SC_INACTIVE" <<'SH'
case " $* " in *" is-active "*) exit 3 ;; esac
exit 0
SH

SD_EMPTY="$TMP/sd-empty"; mkdir -p "$SD_EMPTY"          # no unit => not managed
SD_MANAGED="$TMP/sd-managed"; mkdir -p "$SD_MANAGED"
touch "$SD_MANAGED/ccc-telegram-bridge.service"

new_project() { # <name> <token>  -> sets PROJ, BD, HOMEDIR
    PROJ="$TMP/$1"; BD="$PROJ/.telegram_bot"
    mkdir -p "$BD"
    echo "TELEGRAM_BOT_TOKEN=$2" > "$BD/.env"
    HOMEDIR="$TMP/home-$1"; mkdir -p "$HOMEDIR"
}

write_health() { # <bot_data_dir>
    cat > "$1/health.json" <<EOF
{"updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
 "service": {"state": "available", "reason": ""},
 "telegram": {"state": "healthy"},
 "agent": {"state": "healthy", "provider": "claude"}}
EOF
}

# ---- restart refuses when systemd manages the bridge (exit 3, untouched) ----
new_project managed "123456:TEST-restart-managed"
# Spawn detached (subshell parent exits immediately) so the sleeper is adopted
# by init and never lingers as a zombie child of this test shell.
OLD="$( ( sleep 300 >/dev/null 2>&1 & echo $! ) )"
echo "$OLD" >> "$SPAWNED_PIDS"
echo "$OLD" > "$BD/bot.pid"
run env HOME="$HOMEDIR" CCC_SYSTEMD_DIR="$SD_MANAGED" CCC_SYSTEMCTL="$SC_OK" \
    bash "$START" --path "$PROJ" --restart
okc "$RC" 3 "restart exits 3 when systemd unit is active"
ok "systemd hint names the service manager" \
   'grep -q "managed by systemd" "$OUT" && grep -q "systemctl" "$OUT" && grep -q "restart ccc-telegram-bridge.service" "$OUT"'
ok "managed restart leaves the process untouched" 'kill -0 "$OLD" 2>/dev/null'
ok "managed restart leaves the pid file untouched" '[ "$(cat "$BD/bot.pid")" = "$OLD" ]'
kill "$OLD" 2>/dev/null

# ---- service-systemd.sh is-managed probe ------------------------------------
run env CCC_SYSTEMD_DIR="$SD_MANAGED" CCC_SYSTEMCTL="$SC_OK" bash "$SSD" is-managed
okc "$RC" 0 "is-managed: unit file + active => managed"
run env CCC_SYSTEMD_DIR="$SD_MANAGED" CCC_SYSTEMCTL="$SC_INACTIVE" bash "$SSD" is-managed
okc "$RC" 1 "is-managed: unit file but inactive => not managed (conservative)"
run env CCC_SYSTEMD_DIR="$SD_EMPTY" CCC_SYSTEMCTL="$SC_OK" bash "$SSD" is-managed
okc "$RC" 1 "is-managed: no unit file => not managed"

# ---- self-restart: refuse before stop, including inactive systemd drift -----
# Reproduce the nosuk topology from #706: a systemd unit exists but is inactive,
# while a detached provider process serves the bot and invokes --restart -d
# through an in-turn Bash child. The restart driver must return before killing
# its ancestor or dispatching a replacement.
new_project self "123456:TEST-restart-self"
SELF_OUT="$TMP/self.out"
SELF_RC="$TMP/self.rc"
SELF_SPAWNED="$TMP/self-spawned"
FAKE_SELF_SPAWN="$TMP/fake-start-self"
write_exec_stub "$FAKE_SELF_SPAWN" <<EOF
touch "$SELF_SPAWNED"
exit 0
EOF
FAKE_SELF_PROVIDER="$TMP/fake-provider-self"
write_exec_stub "$FAKE_SELF_PROVIDER" <<EOF
echo "\$\$" > "$BD/bot.pid"
HOME="$HOMEDIR" CCC_SYSTEMD_DIR="$SD_MANAGED" CCC_SYSTEMCTL="$SC_INACTIVE" \\
    CCC_BRIDGE_RESTART_SPAWN="$FAKE_SELF_SPAWN" \\
    bash "$START" --path "$PROJ" --restart -d >"$SELF_OUT" 2>&1
printf '%s\\n' "\$?" > "$SELF_RC"
sleep 300 &
keeper=\$!
echo "\$keeper" >> "$SPAWNED_PIDS"
wait "\$keeper"
EOF
bash "$FAKE_SELF_PROVIDER" &
SELF_PARENT=$!
for _ in $(seq 1 100); do
    [ -s "$SELF_RC" ] && break
    sleep 0.05
done
SELF_RESULT="$(cat "$SELF_RC" 2>/dev/null || echo missing)"
okc "$SELF_RESULT" 5 "in-bridge restart exits 5 before destructive stop"
ok "self-restart leaves the serving ancestor alive" \
   'kill -0 "$SELF_PARENT" 2>/dev/null'
ok "self-restart leaves the pid file untouched" \
   '[ "$(cat "$BD/bot.pid")" = "$SELF_PARENT" ]'
ok "self-restart does not dispatch a replacement" '[ ! -e "$SELF_SPAWNED" ]'
ok "self-restart explains the process-tree refusal" \
   'grep -q "inside the target bridge process tree" "$SELF_OUT" && grep -q "refused-before-stop" "$SELF_OUT"'
ok "self-restart gives external systemd and unmanaged recovery lanes" \
   'grep -q "systemctl.*restart ccc-telegram-bridge.service" "$SELF_OUT" && grep -q "shell outside the bridge tree" "$SELF_OUT"'
kill "$SELF_PARENT" 2>/dev/null || true
wait "$SELF_PARENT" 2>/dev/null || true
SELF_PARENT=""

# ---- invalid prepared runtime must fail before stopping the old bot ---------
new_project prepared-invalid "123456:TEST-prepared-invalid"
OLD="$( ( sleep 300 >/dev/null 2>&1 & echo $! ) )"
echo "$OLD" >> "$SPAWNED_PIDS"
echo "$OLD" > "$BD/bot.pid"
run env HOME="$HOMEDIR" CCC_SYSTEMD_DIR="$SD_EMPTY" CCC_SYSTEMCTL="$SC_OK" \
    bash "$START" --path "$PROJ" --prepared-runtime "$TMP/missing-runtime" --restart
okc "$RC" 6 "invalid prepared runtime fails before stop"
ok "invalid preparation leaves previous process alive" 'kill -0 "$OLD" 2>/dev/null'
ok "invalid preparation preserves PID" '[ "$(cat "$BD/bot.pid")" = "$OLD" ]'
kill "$OLD" 2>/dev/null

# ---- foreground restart: replaces the PID and verifies availability ---------
new_project fg "123456:TEST-restart-fg"
PREPARED="$TMP/prepared job"; mkdir -p "$PREPARED/runtime/bin"
# Candidate preflight is synthetic; serving verification runs the real stdlib
# checker using the test host's Python, without provider or package operations.
EXPECTED="$TMP/expected.json"
python3 - "$HERE" "$PREPARED/runtime" "$EXPECTED" <<'PYFIX'
import json, sys
from pathlib import Path
Path(sys.argv[3]).write_text(json.dumps(dict(schema="ccc.prepared-runtime.v1", status="ready",
    source_dir=sys.argv[1], runtime_dir=sys.argv[2], dependency_fingerprint="b"*64,
    source_seal={"sha256":"a"*64,"files":1,"bytes":1})))
PYFIX
REAL_PYTHON="$(command -v python3)"
write_exec_stub "$PREPARED/runtime/bin/python" <<EOF
case " \$* " in
  *prepared_runtime.py*) cat "$EXPECTED" ;;
  *prepared_serving.py*) exec "$REAL_PYTHON" "\$@" ;;
  *) exit 98 ;;
esac
EOF
OLD="$( ( sleep 300 >/dev/null 2>&1 & echo $! ) )"
echo "$OLD" >> "$SPAWNED_PIDS"
echo "$OLD" > "$BD/bot.pid"
CALLS="$TMP/fg-spawn.calls"
FAKE_FG="$TMP/fake-start-fg"
write_exec_stub "$FAKE_FG" <<EOF
printf '%s\n' "\$*" >> "$CALLS"
echo \$\$ > "$BD/bot.pid"
"$REAL_PYTHON" - "$EXPECTED" "$BD/health.json" "\$\$" <<'PYFIX'
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
expected=json.loads(Path(sys.argv[1]).read_text())
now=datetime.now(timezone.utc).isoformat()
generation=dict(schema="ccc.runtime-generation.v1", observed_at=now,
    source_dir=expected["source_dir"], source_seal=expected["source_seal"],
    python_prefix=expected["runtime_dir"], python_executable=expected["runtime_dir"]+"/bin/python",
    dependency_fingerprint=expected["dependency_fingerprint"], collection_errors=[])
if os.environ.get("CCC_TEST_WRONG_GENERATION") == "1":
    generation["python_prefix"]="/wrong/runtime"
    generation["python_executable"]="/wrong/runtime/bin/python"
    # Mutate the backing validation fixture too: the parent must use its pinned
    # pre-stop report rather than reloading a conveniently matching identity.
    expected["runtime_dir"]="/wrong/runtime"
    Path(sys.argv[1]).write_text(json.dumps(expected))
Path(sys.argv[2]).write_text(json.dumps(dict(schema_version=1, updated_at=now,
    process={"pid":int(sys.argv[3]),"started_at":now}, runtime_generation=generation,
    service={"state":"available"},telegram={"state":"healthy"},agent={"state":"healthy"})))
PYFIX
exec sleep 300
EOF
run env HOME="$HOMEDIR" CCC_SYSTEMD_DIR="$SD_EMPTY" CCC_SYSTEMCTL="$SC_OK" \
    CCC_BRIDGE_RESTART_SPAWN="$FAKE_FG" \
    CCC_BRIDGE_RESTART_STOP_TIMEOUT=5 CCC_BRIDGE_RESTART_READY_TIMEOUT=15 \
    bash "$START" --path "$PROJ" --prepared-runtime "$PREPARED" --restart
NEW="$(cat "$BD/bot.pid" 2>/dev/null)"
[ -n "$NEW" ] && echo "$NEW" >> "$SPAWNED_PIDS"
okc "$RC" 0 "foreground restart exits 0 on verified-available"
ok "old process was stopped" '! kill -0 "$OLD" 2>/dev/null'
ok "new process is alive and differs from old" \
   '[ -n "$NEW" ] && [ "$NEW" != "$OLD" ] && kill -0 "$NEW" 2>/dev/null'
ok "restart reports the old PID" 'grep -q "old PID: $OLD" "$OUT"'
ok "restart reports the new PID" 'grep -q "new PID: $NEW" "$OUT"'
ok "restart prints the availability health summary" \
   'grep -q "Bot status: available" "$OUT" && grep -q "Restart verified" "$OUT"'
ok "restart forwards prepared directory to child" 'grep -q -- "--prepared-runtime $PREPARED" "$CALLS"'
ok "spawn used the project path" 'grep -q -- "--path $PROJ" "$CALLS"'
ok "foreground spawn did not pass --daemon" '! grep -q -- "--daemon" "$CALLS"'
kill "$NEW" 2>/dev/null

# A fresh generic "available" status from a different generation must not
# promote the prepared candidate, even if its backing receipt changes.
run env HOME="$HOMEDIR" CCC_SYSTEMD_DIR="$SD_EMPTY" CCC_SYSTEMCTL="$SC_OK" \
    CCC_BRIDGE_RESTART_SPAWN="$FAKE_FG" CCC_TEST_WRONG_GENERATION=1 \
    CCC_BRIDGE_RESTART_STOP_TIMEOUT=5 CCC_BRIDGE_RESTART_READY_TIMEOUT=2 \
    bash "$START" --path "$PROJ" --prepared-runtime "$PREPARED" --restart
WRONG="$(cat "$BD/bot.pid" 2>/dev/null)"
[ -n "$WRONG" ] && echo "$WRONG" >> "$SPAWNED_PIDS"
okc "$RC" 4 "wrong prepared serving generation cannot pass restart"
ok "wrong generation explains missing verification despite generic availability" \
   'grep -q "Selected prepared generation was not verified" "$OUT" && grep -q "Bot status: available" "$OUT" && ! grep -q "Restart verified" "$OUT"'
ok "failed generation observation leaves candidate for explicit recovery" \
   '[ -n "$WRONG" ] && kill -0 "$WRONG" 2>/dev/null'
kill "$WRONG" 2>/dev/null || true

# ---- daemon restart (-d): dispatches --daemon through the spawn seam --------
new_project dm "123456:TEST-restart-dm"
DCALLS="$TMP/dm-spawn.calls"
FAKE_DM="$TMP/fake-start-dm"
write_exec_stub "$FAKE_DM" <<EOF
printf '%s\n' "\$*" >> "$DCALLS"
sleep 300 &
child=\$!
echo "\$child" > "$BD/bot.pid"
echo "\$child" >> "$SPAWNED_PIDS"
cat > "$BD/health.json" <<HEOF
{"updated_at": "\$(date -u +%Y-%m-%dT%H:%M:%SZ)",
 "service": {"state": "available", "reason": ""},
 "telegram": {"state": "healthy"},
 "agent": {"state": "healthy", "provider": "claude"}}
HEOF
exit 0
EOF
run env HOME="$HOMEDIR" CCC_SYSTEMD_DIR="$SD_EMPTY" CCC_SYSTEMCTL="$SC_OK" \
    CCC_BRIDGE_RESTART_SPAWN="$FAKE_DM" \
    CCC_BRIDGE_RESTART_STOP_TIMEOUT=5 CCC_BRIDGE_RESTART_READY_TIMEOUT=15 \
    bash "$START" --path "$PROJ" --restart -d
DNEW="$(cat "$BD/bot.pid" 2>/dev/null)"
okc "$RC" 0 "daemon restart exits 0 on verified-available"
ok "daemon restart passed --daemon to the start path" 'grep -q -- "--daemon" "$DCALLS"'
ok "daemon restart left a live verified process" \
   '[ -n "$DNEW" ] && kill -0 "$DNEW" 2>/dev/null'
ok "daemon restart reports the new PID" 'grep -q "new PID: $DNEW" "$OUT"'
kill "$DNEW" 2>/dev/null

# ---- readiness timeout: nonzero with a clear reason -------------------------
new_project slow "123456:TEST-restart-slow"
FAKE_SLOW="$TMP/fake-start-slow"
write_exec_stub "$FAKE_SLOW" <<EOF
echo \$\$ > "$BD/bot.pid"
exec sleep 300
EOF
run env HOME="$HOMEDIR" CCC_SYSTEMD_DIR="$SD_EMPTY" CCC_SYSTEMCTL="$SC_OK" \
    CCC_BRIDGE_RESTART_SPAWN="$FAKE_SLOW" \
    CCC_BRIDGE_RESTART_STOP_TIMEOUT=5 CCC_BRIDGE_RESTART_READY_TIMEOUT=2 \
    bash "$START" --path "$PROJ" --restart
SNEW="$(cat "$BD/bot.pid" 2>/dev/null)"
[ -n "$SNEW" ] && echo "$SNEW" >> "$SPAWNED_PIDS"
okc "$RC" 4 "never-available restart exits 4"
ok "timeout reason is explicit" 'grep -q "not-available-within-timeout" "$OUT"'
ok "timed-out restart leaves the new process running (reported, not killed)" \
   '[ -n "$SNEW" ] && kill -0 "$SNEW" 2>/dev/null && grep -q "left running" "$OUT"'
# #1868: the timeout is machine-distinguishable and names who serves now.
ok "timeout emits a timeout outcome with the live-but-unavailable process" \
   'grep "^ccc-restart-outcome: " "$OUT" | sed "s/^ccc-restart-outcome: //" | jq -e --argjson p "$SNEW" \
      ".candidate == \"timeout\" and .candidate_exit == 4 and .candidate_window == 2 and .recovery == \"not-configured\" and .serving == \"alive\" and .serving_pid == \$p" >/dev/null'
kill "$SNEW" 2>/dev/null

# ---- #1868: invalid available window is refused before anything stops -------
new_project badwin "123456:TEST-restart-badwin"
BW_OLD="$( ( sleep 300 >/dev/null 2>&1 & echo $! ) )"
echo "$BW_OLD" >> "$SPAWNED_PIDS"
echo "$BW_OLD" > "$BD/bot.pid"
for bad in abc 0 3601 090; do
    run env HOME="$HOMEDIR" CCC_SYSTEMD_DIR="$SD_EMPTY" CCC_SYSTEMCTL="$SC_OK" \
        CCC_BRIDGE_RESTART_SPAWN="$TMP/never-spawned" CCC_BRIDGE_RESTART_READY_TIMEOUT="$bad" \
        bash "$START" --path "$PROJ" --restart
    okc "$RC" 6 "invalid ready window '$bad' is refused before stop"
done
run env HOME="$HOMEDIR" CCC_SYSTEMD_DIR="$SD_EMPTY" CCC_SYSTEMCTL="$SC_OK" \
    CCC_BRIDGE_RESTART_SPAWN="$TMP/never-spawned" CCC_BRIDGE_RESTART_RECOVERY_READY_TIMEOUT=-5 \
    bash "$START" --path "$PROJ" --restart
okc "$RC" 6 "invalid recovery window is refused before stop"
ok "invalid window names the variable" 'grep -q "CCC_BRIDGE_RESTART_RECOVERY_READY_TIMEOUT must be an integer" "$OUT"'
ok "invalid window leaves the serving process untouched" \
   'kill -0 "$BW_OLD" 2>/dev/null && [ "$(cat "$BD/bot.pid")" = "$BW_OLD" ]'
kill "$BW_OLD" 2>/dev/null

# ---- #1868: recovery window derivation (pure helper, sourced seam) -----------
recovery_window() { # <candidate-rc> [env assignments...]
    local rc="$1"; shift
    env -u CCC_BRIDGE_RESTART_READY_TIMEOUT -u CCC_BRIDGE_RESTART_RECOVERY_READY_TIMEOUT \
        -u CCC_BRIDGE_RESTART_DEADLINE_EPOCH HOME="$TMP/home-win" "$@" bash -c '
        CCC_START_SH_LIB_ONLY=1 . "$1" --path "$2" >/dev/null
        restart_resolve_windows >/dev/null || exit 6
        restart_recovery_window "$3"' _ "$START" "$TMP/win-project" "$rc"
}
mkdir -p "$TMP/win-project"
okc "$(recovery_window 4)" 180 "timeout recovery default is max(2x90, 180) = 180"
okc "$(recovery_window 2)" 90 "start-error recovery keeps the candidate window"
okc "$(recovery_window 4 CCC_BRIDGE_RESTART_READY_TIMEOUT=120)" 240 "timeout recovery doubles a larger candidate window"
okc "$(recovery_window 4 CCC_BRIDGE_RESTART_READY_TIMEOUT=30)" 180 "timeout recovery never drops below 180"
okc "$(recovery_window 2 CCC_BRIDGE_RESTART_RECOVERY_READY_TIMEOUT=300)" 300 "explicit recovery window applies to any failure"
NOW="$(date -u +%s)"
w="$(recovery_window 4 CCC_BRIDGE_RESTART_DEADLINE_EPOCH="$((NOW + 60 + 120))")"
ok "outer deadline shrinks the timeout recovery window (got $w)" '[ "$w" -ge 118 ] && [ "$w" -le 120 ]'
okc "$(recovery_window 4 CCC_BRIDGE_RESTART_DEADLINE_EPOCH="$((NOW + 10))")" 90 "outer deadline never shrinks below the candidate window"
okc "$(recovery_window 4 CCC_BRIDGE_RESTART_DEADLINE_EPOCH="$((NOW + 3600))")" 180 "a roomy outer deadline changes nothing"

# ---- #1868: candidate + recovery outcomes (real finish path, stubbed lifecycle)
# Lifecycle predicates are fixtures; finish_prepared_restart_failure decides the
# recovery window, the exit code and the outcome record.
FIN_LIVE="$( ( sleep 300 >/dev/null 2>&1 & echo $! ) )"
echo "$FIN_LIVE" >> "$SPAWNED_PIDS"
finish_case() { # <candidate-rc> <recovery-rc> <verify-rc> <serving: dead|alive|available>
    mkdir -p "$TMP/fin-project"
    rm -f "$TMP/fin-project/recovery.env"
    run env -u CCC_BRIDGE_RESTART_READY_TIMEOUT -u CCC_BRIDGE_RESTART_RECOVERY_READY_TIMEOUT \
        -u CCC_BRIDGE_RESTART_DEADLINE_EPOCH HOME="$TMP/home-fin" \
        FIN_RECOVERY_RC="$2" FIN_VERIFY_RC="$3" FIN_SERVING="$4" FIN_LIVE="$FIN_LIVE" \
        bash -c '
        CCC_START_SH_LIB_ONLY=1 . "$1" --path "$2" >/dev/null
        restart_resolve_windows >/dev/null
        TRANSITION_RUN=fixture
        RECOVERY_SOURCE="$2/old"
        RECOVERY_RUNTIME="$2/job"
        RESTART_OLD_PID=3999999
        transition_phase() { :; }
        bash() { printf "%s\n" "$CCC_BRIDGE_RESTART_READY_TIMEOUT" > "$PROJECT_ROOT/recovery.env"; printf "%s\n" "${CCC_BRIDGE_RESTART_SUPPRESS_OUTCOME:-}" > "$PROJECT_ROOT/recovery.quiet"; return "$FIN_RECOVERY_RC"; }
        verify_previous_serving() { return "$FIN_VERIFY_RC"; }
        find_project_bot_pids() { :; }
        read_pid() { [ "$FIN_SERVING" = dead ] || echo "$FIN_LIVE"; }
        render_status_from_health() { [ "$FIN_SERVING" = available ] && echo "Bot status: available" || echo "Bot status: starting"; }
        finish_prepared_restart_failure "$3"' _ "$START" "$TMP/fin-project" "$1"
    # shellcheck disable=SC2034  # FIN_OUTCOME is read via eval inside ok()
    FIN_OUTCOME="$(grep "^ccc-restart-outcome: " "$OUT" | sed "s/^ccc-restart-outcome: //")"
}
finish_case 4 4 1 dead
okc "$RC" 8 "timeout candidate + timeout recovery exits 8"
okc "$(cat "$TMP/fin-project/recovery.env")" 180 "timed-out candidate hands the recovery a 180s window"
okc "$(cat "$TMP/fin-project/recovery.quiet")" 1 "nested recovery run is told not to emit its own outcome line"
ok "double timeout outcome is explicit and reports the service DOWN" \
   'jq -e ".schema == \"ccc.restart-outcome.v1\" and .candidate == \"timeout\" and .candidate_window == 90 and .recovery == \"timeout\" and .recovery_exit == 4 and .recovery_window == 180 and .serving == \"dead\" and .serving_pid == null and .previous_pid == 3999999 and .previous_alive == false" <<<"$FIN_OUTCOME" >/dev/null'
ok "double failure says no live bridge in plain text" 'grep -q "NO live bridge process" "$OUT" && grep -q "Previously serving PID 3999999: dead" "$OUT"'
finish_case 4 4 1 alive
ok "double failure distinguishes a still-starting bridge" \
   'jq -e --argjson p "$FIN_LIVE" ".serving == \"alive\" and .serving_pid == \$p" <<<"$FIN_OUTCOME" >/dev/null && grep -q "alive but NOT available" "$OUT"'
finish_case 2 2 1 available
okc "$RC" 8 "start-error candidate + start-error recovery exits 8"
okc "$(cat "$TMP/fin-project/recovery.env")" 90 "start-error candidate keeps the 90s recovery window"
ok "start-error outcome is distinguishable from timeout" \
   'jq -e ".candidate == \"start-error\" and .candidate_exit == 2 and .recovery == \"start-error\" and .serving == \"available\"" <<<"$FIN_OUTCOME" >/dev/null'
finish_case 4 0 1 alive
okc "$RC" 8 "unverified recovery still exits 8"
ok "unverified recovery is not reported as a timeout" 'jq -e ".recovery == \"unverified\" and .recovery_exit == 4" <<<"$FIN_OUTCOME" >/dev/null'
finish_case 4 0 0 available
okc "$RC" 7 "verified recovery exits 7"
ok "recovered outcome keeps the candidate timeout cause" \
   'jq -e ".candidate == \"timeout\" and .recovery == \"recovered\" and .recovery_exit == 0 and .serving == \"available\"" <<<"$FIN_OUTCOME" >/dev/null'
kill "$FIN_LIVE" 2>/dev/null

# ---- stop refusal: report + refuse to start on top --------------------------
# BASH_ENV seam: make one fake pid report alive to kill -0 and immune to
# signals (simulating an unkillable process), and shrink sleep so the --stop
# escalation loop stays fast. No real process is involved at all.
new_project stuck "123456:TEST-restart-stuck"
UNKILLABLE=4000000
echo "$UNKILLABLE" > "$BD/bot.pid"
STUBENV="$TMP/stub-env.sh"
cat > "$STUBENV" <<'EOF'
kill() {
    local a
    for a in "$@"; do
        if [ -n "${CCC_TEST_UNKILLABLE_PID:-}" ] && [ "$a" = "$CCC_TEST_UNKILLABLE_PID" ]; then
            return 0
        fi
    done
    command kill "$@"
}
sleep() { command sleep 0.05; }
EOF
STUCK_CALLS="$TMP/stuck-spawn.calls"
FAKE_STUCK="$TMP/fake-start-stuck"
write_exec_stub "$FAKE_STUCK" <<EOF
touch "$STUCK_CALLS"
exit 0
EOF
run env HOME="$HOMEDIR" CCC_SYSTEMD_DIR="$SD_EMPTY" CCC_SYSTEMCTL="$SC_OK" \
    BASH_ENV="$STUBENV" CCC_TEST_UNKILLABLE_PID="$UNKILLABLE" \
    CCC_BRIDGE_RESTART_SPAWN="$FAKE_STUCK" \
    CCC_BRIDGE_STOP_GRACE_SECONDS=1 \
    CCC_BRIDGE_RESTART_STOP_TIMEOUT=2 CCC_BRIDGE_RESTART_READY_TIMEOUT=2 \
    bash "$START" --path "$PROJ" --restart
okc "$RC" 1 "stop-refusing process makes restart exit 1"
ok "stop failure reason is explicit" \
   'grep -q "stop-failed" "$OUT" && grep -q "refuses to exit" "$OUT"'
ok "stop failure names the surviving PID" 'grep -q "$UNKILLABLE" "$OUT"'
ok "no new instance is started after a failed stop" '[ ! -e "$STUCK_CALLS" ]'

# ---- restart without --path is rejected before touching anything ------------
# (-u PROJECT_ROOT: when this test itself runs under the bridge, PROJECT_ROOT
# is exported in the environment and would silently supply a project path.)
run env -u PROJECT_ROOT HOME="$TMP/home-nopath" bash "$START" --restart
ok "restart without --path fails with usage error" \
   '[ "$RC" != 0 ] && grep -q "specify project path" "$OUT"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
