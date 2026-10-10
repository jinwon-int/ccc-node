#!/usr/bin/env bash
# shellcheck disable=SC2034  # out/rc are read via eval inside ok()
# Tests for termux-restart-frontends.sh — hermetic: fake HOME, a fake
# bridge-current.sh (records its call, exits FAKE_TG_RC), a fake `sv` that
# "restarts" by rewriting health.json for a live sleep process and reports
# uptime from the restart time, second-scale windows. No Termux, no runit,
# nothing real restarted.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/termux-restart-frontends.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
TMP="$(ccc_test_tmpdir)" || exit 1
cleanup() { [ -n "${LIVE1:-}" ] && kill "$LIVE1" 2>/dev/null; [ -n "${LIVE2:-}" ] && kill "$LIVE2" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT
export HOME="$TMP/home"; mkdir -p "$HOME/.claude/state" "$HOME/.ccc-node/scripts" "$HOME/.ccc-node/preparations/gen-A" "$HOME/.ccc-node/preparations/gen-B" "$HOME/.ccc-matrix"
unset CCC_BRIDGE_RESTART_DEADLINE_EPOCH SVDIR PREFIX
export CCC_MATRIX_SURVIVE_SECONDS=2 CCC_MATRIX_START_SECONDS=3 CCC_MATRIX_POLL_SECONDS=1 CCC_MATRIX_BUSY_POLL_SECONDS=1
export CCC_MATRIX_MIN_WINDOW_SECONDS=2 CCC_TERMUX_FOLLOWER_WAIT_SECONDS=1
LOG="$HOME/.claude/state/restart-frontends.log"
FAKE="$TMP/fake.env"; export FAKE
CALLS="$TMP/calls"; export CALLS

# two live processes to play Matrix pids (health.json pid must be alive)
sleep 600 & LIVE1=$!
sleep 600 & LIVE2=$!
DEAD=$(bash -c 'echo $$'); # that shell has exited: a dead pid

# --- fake bridge-current.sh: records args, exits FAKE_TG_RC
cat > "$HOME/.ccc-node/scripts/bridge-current.sh" <<'SH'
#!/usr/bin/env bash
printf 'bridge-current %s\n' "$*" >> "$CALLS"
. "$FAKE"; exit "${FAKE_TG_RC:-0}"
SH

# --- fake sv in PATH: `sv restart <svc>` rewrites health.json (new process on
# FAKE_NEW_GEN, pid FAKE_NEW_PID, state FAKE_STATE) unless FAKE_RESTART_NOOP=1;
# `sv status <svc>` prints runit's line with pid FAKE_SV_PID (default: the
# health pid) and uptime = now - last restart.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/sv" <<'SH'
#!/usr/bin/env bash
. "$FAKE"
printf 'sv %s\n' "$*" >> "$CALLS"
case "$1" in
  restart)
    [ "${FAKE_RESTART_NOOP:-0}" = 1 ] && exit 0
    date +%s > "$FAKE_STARTED_FILE"
    write_health "$FAKE_NEW_PID" "$(date +%s)" "${FAKE_STATE:-available}" "$FAKE_NEW_GEN" 0 ;;
  status)
    started=$(cat "$FAKE_STARTED_FILE" 2>/dev/null || echo 0)
    pid="${FAKE_SV_PID:-$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["process"]["pid"])' "$HOME/.ccc-matrix/health.json" 2>/dev/null)}"
    printf 'run: %s: (pid %s) %ss; run: log: (pid 1) 999999s\n' "$2" "$pid" "$(( $(date +%s) - started ))" ;;
esac
SH
chmod +x "$TMP/bin/sv" "$HOME/.ccc-node/scripts/bridge-current.sh"
export PATH="$TMP/bin:$PATH"

write_health() { # <pid> <started_epoch> <state> <gen> <active_requests>
  python3 - "$HOME/.ccc-matrix/health.json" "$@" <<'PY'
import json, sys
from datetime import datetime, timezone
path, pid, started, state, gen, active = sys.argv[1:7]
json.dump({
  "process": {"mode": "foreground", "pid": int(pid), "started_at": datetime.fromtimestamp(int(started), timezone.utc).isoformat().replace("+00:00", "Z")},
  "service": {"state": state, "reason": ""},
  "runtime_generation": {"python_prefix": f"/x/.ccc-node/preparations/{gen}/job/runtime"},
  "workload": {"active_requests": int(active)},
}, open(path, "w"))
PY
}
export -f write_health
point() { ln -sfn "preparations/$1" "$HOME/.ccc-node/bridge-current"; }
setfake() { # key=value ... → FAKE env file (sourced by the fakes)
  : > "$FAKE"; echo "FAKE_STARTED_FILE=$TMP/started" >> "$FAKE"
  for kv in "$@"; do echo "$kv" >> "$FAKE"; done
}
reset() { : > "$CALLS"; : > "$LOG"; rm -f "$TMP/started"; }
run() { bash "$SCRIPT" "$@" 2>&1; }

# ---------------------------------------------------------------- dry-run
reset; point gen-A; write_health "$LIVE1" "$(( $(date +%s) - 500 ))" available gen-A 0
setfake "FAKE_SV_PID=$LIVE1"; echo "$(( $(date +%s) - 500 ))" > "$TMP/started"
out="$(run --dry-run)"; rc=$?
ok "dry-run no-op when the live process runs the pointer's generation" '[ "$rc" = 0 ] && [[ "$out" == *"bridge-current=gen-A matrix_running=gen-A"* ]] && [[ "$out" == *"-> no-op"* ]]'
ok "dry-run reports sv pid/uptime and the survival window" '[[ "$out" == *"sv_pid=$LIVE1"* ]] && [[ "$out" == *"survive=2s"* ]]'
ok "dry-run touches nothing" '! grep -q "sv restart" "$CALLS" && ! grep -q bridge-current "$CALLS"'
point gen-B
out="$(run --dry-run)"; rc=$?
ok "dry-run: pointer moved → would restart then verify" '[ "$rc" = 0 ] && [[ "$out" == *"-> would-restart-matrix-then-verify"* ]]'
write_health "$DEAD" "$(( $(date +%s) - 500 ))" available gen-B 0
out="$(run --dry-run)"
ok "dry-run: stale health.json of a dead pid counts as not running" '[[ "$out" == *"matrix_running=none"* ]] && [[ "$out" == *"would-restart"* ]]'

# ---------------------------------------------------------------- default run: up to date
reset; point gen-A; write_health "$LIVE1" "$(( $(date +%s) - 500 ))" available gen-A 0; setfake
out="$(run)"; rc=$?
ok "up to date: Telegram restarted, Matrix untouched, exit 0" '[ "$rc" = 0 ] && grep -q "bridge-current --path $HOME --restart -d" "$CALLS" && ! grep -q "sv restart" "$CALLS" && grep -q "matrix up to date (gen-A)" "$LOG"'

# ---------------------------------------------------------------- Telegram failure propagates
reset; setfake "FAKE_TG_RC=7"
out="$(run)"; rc=$?
ok "Telegram restart rc propagates (7) when Matrix is fine" '[ "$rc" = 7 ] && grep -q "telegram restart rc=7" "$LOG"'

# ---------------------------------------------------------------- restart + survives
reset; point gen-B; write_health "$LIVE1" "$(( $(date +%s) - 500 ))" available gen-A 0
setfake "FAKE_NEW_PID=$LIVE2" "FAKE_NEW_GEN=gen-B"
t0=$(date +%s); out="$(run)"; rc=$?; el=$(( $(date +%s) - t0 ))
ok "pointer moved: sv restart issued, new process verified for the window, exit 0" '[ "$rc" = 0 ] && grep -q "sv restart ccc-matrix-bridge" "$CALLS" && grep -q "matrix verified: gen=gen-B pid=$LIVE2 uptime=" "$LOG"'
ok "survival window was actually held (>= 2 s)" '[ "$el" -ge 2 ]'
ok "the restart log names the transition" 'grep -q "matrix restart issued: running=gen-A want=gen-B" "$LOG"'

# ---------------------------------------------------------------- restart, process never comes up (E2: old behaviour said success)
reset; point gen-B; write_health "$LIVE1" "$(( $(date +%s) - 500 ))" available gen-A 0
setfake "FAKE_RESTART_NOOP=1"
out="$(run)"; rc=$?
ok "no new process after the restart → exit 3, not success" '[ "$rc" = 3 ] && grep -q "matrix NOT up after 3s" "$LOG" && grep -q "NOT healthy (rc=3)" "$LOG"'

# ---------------------------------------------------------------- restart, new process runs the WRONG generation
reset; point gen-B; write_health "$LIVE1" "$(( $(date +%s) - 500 ))" available gen-A 0
setfake "FAKE_NEW_PID=$LIVE2" "FAKE_NEW_GEN=gen-A"
out="$(run)"; rc=$?
ok "new process on a different generation than the pointer → exit 3" '[ "$rc" = 3 ] && grep -q "gen=gen-A want=gen-B" "$LOG"'

# ---------------------------------------------------------------- restart, crash loop: runit pid differs from the health pid
reset; point gen-B; write_health "$LIVE1" "$(( $(date +%s) - 500 ))" available gen-A 0
setfake "FAKE_NEW_PID=$LIVE2" "FAKE_NEW_GEN=gen-B" "FAKE_SV_PID=424242"
out="$(run)"; rc=$?
ok "supervised pid != health pid inside the window (crash/restart) → exit 3" '[ "$rc" = 3 ] && grep -q "supervised pid changed $LIVE2 -> 424242" "$LOG"'

# ---------------------------------------------------------------- restart, process dies inside the window
reset; point gen-B; write_health "$LIVE1" "$(( $(date +%s) - 500 ))" available gen-A 0
sleep 600 & DYING=$!
setfake "FAKE_NEW_PID=$DYING" "FAKE_NEW_GEN=gen-B"
( sleep 1; kill "$DYING" 2>/dev/null ) &
out="$(run)"; rc=$?
ok "new process dies inside the window → exit 3" '[ "$rc" = 3 ] && grep -q "matrix pid $DYING died inside" "$LOG"'

# ---------------------------------------------------------------- restart, up but not available
reset; point gen-B; write_health "$LIVE1" "$(( $(date +%s) - 500 ))" available gen-A 0
setfake "FAKE_NEW_PID=$LIVE2" "FAKE_NEW_GEN=gen-B" "FAKE_STATE=degraded"
out="$(run)"; rc=$?
ok "process survives but service.state != available → exit 3" '[ "$rc" = 3 ] && grep -q "service.state=degraded" "$LOG"'

# ---------------------------------------------------------------- busy until the deadline → follower detached, exit 0
reset; point gen-B; write_health "$LIVE1" "$(( $(date +%s) - 500 ))" available gen-A 3
setfake "FAKE_NEW_PID=$LIVE2" "FAKE_NEW_GEN=gen-B"
out="$(CCC_BRIDGE_RESTART_DEADLINE_EPOCH=$(( $(date +%s) + 31 )) run)"; rc=$?
ok "busy Matrix until the deadline: no restart, follower detached, exit 0" '[ "$rc" = 0 ] && ! grep -q "sv restart" "$CALLS" && grep -q "matrix busy until deadline (running=gen-A want=gen-B)" "$LOG" && grep -q "follower detached (waits up to 1s" "$LOG"'
sleep 3  # let the 1 s follower give up (still busy) so it does not outlive the test

# ---------------------------------------------------------------- window truncated by the deadline
reset; point gen-B; write_health "$LIVE1" "$(( $(date +%s) - 500 ))" available gen-A 0
setfake "FAKE_NEW_PID=$LIVE2" "FAKE_NEW_GEN=gen-B"
out="$(CCC_MATRIX_SURVIVE_SECONDS=30 CCC_BRIDGE_RESTART_DEADLINE_EPOCH=$(( $(date +%s) + 33 )) run)"; rc=$?
ok "survival window truncated to what the deadline leaves (floor = min window)" '[ "$rc" = 0 ] && grep -q "survival window truncated to [23]s" "$LOG" && grep -q "matrix verified" "$LOG"'

# ---------------------------------------------------------------- --matrix-only
reset; point gen-B; write_health "$LIVE1" "$(( $(date +%s) - 500 ))" available gen-A 0
setfake "FAKE_NEW_PID=$LIVE2" "FAKE_NEW_GEN=gen-B"
out="$(run --matrix-only 5)"; rc=$?
ok "--matrix-only restarts and verifies without touching Telegram" '[ "$rc" = 0 ] && ! grep -q bridge-current "$CALLS" && grep -q "matrix verified: gen=gen-B" "$LOG"'

# ---------------------------------------------------------------- --verify (read-only judgement of the current process)
reset; point gen-B; write_health "$LIVE2" "$(( $(date +%s) - 500 ))" available gen-B 0
setfake; echo "$(( $(date +%s) - 500 ))" > "$TMP/started"
out="$(run --verify 2)"; rc=$?
ok "--verify passes a long-running process on the pointer's generation" '[ "$rc" = 0 ] && [[ "$out" == *"matrix verified: gen=gen-B pid=$LIVE2"* ]] && ! grep -q "sv restart" "$CALLS"'
write_health "$DEAD" "$(( $(date +%s) - 500 ))" available gen-B 0
out="$(run --verify 2)"; rc=$?
ok "--verify fails a dead process" '[ "$rc" = 3 ]'
rm -f "$HOME/.ccc-matrix/health.json"
out="$(run --verify 2)"; rc=$?
ok "--verify without health.json → exit 3" '[ "$rc" = 3 ] && [[ "$out" == *"no health.json"* ]]'

# ---------------------------------------------------------------- recovery arming + rollback (#2175 C remnant)
GA="$HOME/.ccc-node/preparations/gen-A"; GB="$HOME/.ccc-node/preparations/gen-B"
mkdir -p "$GA/source/bridge" "$GA/job/runtime/bin" "$GB/source/bridge" "$GB/job/runtime/bin" "$HOME/.telegram_bot"
printf '#!/usr/bin/env bash\nprintf "genA-start %%s\\n" "$*" >> "$CALLS"; exit 0\n' > "$GA/source/bridge/start.sh"
for f in "$GA/job/runtime/bin/python" "$GB/source/bridge/start.sh" "$GB/job/runtime/bin/python"; do printf '#!/usr/bin/env bash\nexit 0\n' > "$f"; done
chmod +x "$GA/source/bridge/start.sh" "$GA/job/runtime/bin/python" "$GB/source/bridge/start.sh" "$GB/job/runtime/bin/python"
tg_health() { # <pid> <gen>: the live Telegram process and the generation it serves
  python3 - "$HOME/.telegram_bot/health.json" "$1" "$HOME/.ccc-node/preparations/$2" <<'PY'
import json, sys
path, pid, gen = sys.argv[1:4]
json.dump({"process": {"pid": int(pid), "started_at": "2026-10-10T00:00:00Z"},
           "runtime_generation": {"source_dir": f"{gen}/source/bridge", "python_prefix": f"{gen}/job/runtime"}}, open(path, "w"))
PY
}

# live Telegram on gen-A, pointer moved to gen-B → recovery armed to gen-A
reset; point gen-B; tg_health "$LIVE1" gen-A
write_health "$LIVE1" "$(( $(date +%s) - 500 ))" available gen-B 0; setfake
out="$(run)"; rc=$?
ok "pointer != live generation: recovery args name the live generation" '[ "$rc" = 0 ] && grep -q -- "bridge-current --path $HOME --restart -d --recovery-source $(readlink -f "$GA")/source/bridge --recovery-runtime $(readlink -f "$GA")/job" "$CALLS"'
ok "arming is logged" 'grep -q "recovery armed: candidate=gen-B previous=gen-A" "$LOG"'
ok "a successful restart leaves the pointer on the candidate" '[ "$(readlink "$HOME/.ccc-node/bridge-current")" = "preparations/gen-B" ] && ! compgen -G "$HOME/.ccc-node/bridge-current.rolledback-*" >/dev/null'

# same, but start.sh reports 7 (candidate failed, previous restored) → pointer rolled back, Matrix untouched
reset; point gen-B; tg_health "$LIVE1" gen-A
write_health "$LIVE1" "$(( $(date +%s) - 500 ))" available gen-A 0; setfake "FAKE_TG_RC=7"
out="$(run)"; rc=$?
ok "rc 7 with recovery armed → exit 7 and pointer back on the restored generation" '[ "$rc" = 7 ] && [ "$(readlink "$HOME/.ccc-node/bridge-current")" = "preparations/gen-A" ]'
ok "the failed target is recorded" 'compgen -G "$HOME/.ccc-node/bridge-current.rolledback-*" >/dev/null && grep -qx "preparations/gen-B" "$HOME"/.ccc-node/bridge-current.rolledback-*'
ok "rollback is logged and the Matrix step sees an up-to-date pointer (no restart)" 'grep -q "pointer rolled back preparations/gen-B -> preparations/gen-A" "$LOG" && ! grep -q "sv restart" "$CALLS" && grep -q "matrix up to date (gen-A)" "$LOG"'
rm -f "$HOME"/.ccc-node/bridge-current.rolledback-*

# rc 7 WITHOUT recovery armed (live == pointer) → no rollback, status propagates as before
reset; point gen-A; tg_health "$LIVE1" gen-A
write_health "$LIVE1" "$(( $(date +%s) - 500 ))" available gen-A 0; setfake "FAKE_TG_RC=7"
out="$(run)"; rc=$?
ok "live generation == pointer: no recovery args" '[ "$rc" = 7 ] && grep -q -- "bridge-current --path $HOME --restart -d$" "$CALLS"'
ok "no rollback without an armed recovery" '[ "$(readlink "$HOME/.ccc-node/bridge-current")" = "preparations/gen-A" ] && ! compgen -G "$HOME/.ccc-node/bridge-current.rolledback-*" >/dev/null && ! grep -q "rolled back" "$LOG"'

# dead Telegram pid / disabled / incomplete previous pair → not armed
reset; point gen-B; tg_health "$DEAD" gen-A; write_health "$LIVE1" "$(( $(date +%s) - 500 ))" available gen-B 0; setfake
out="$(run)"
ok "a dead Telegram pid in health.json arms nothing" '! grep -q -- "--recovery-source" "$CALLS"'
reset; tg_health "$LIVE1" gen-A
out="$(CCC_TERMUX_RECOVERY=0 run)"
ok "CCC_TERMUX_RECOVERY=0 arms nothing" '! grep -q -- "--recovery-source" "$CALLS"'
reset; chmod -x "$GA/job/runtime/bin/python"
out="$(run)"
ok "a previous pair without a runtime is not armed (logged)" '! grep -q -- "--recovery-source" "$CALLS" && grep -q "recovery not armed" "$LOG"'
chmod +x "$GA/job/runtime/bin/python"

# ---------------------------------------------------------------- launcher resolution
reset; point gen-A; tg_health "$LIVE1" gen-A; write_health "$LIVE1" "$(( $(date +%s) - 500 ))" available gen-A 0; setfake
mv "$HOME/.ccc-node/scripts/bridge-current.sh" "$TMP/bridge-current.sh.keep"
out="$(CCC_TERMUX_LAUNCH_FLOCK=0 run)"; rc=$?
ok "without a node bridge-current.sh the repo termux-bridge-current.sh launches the serving generation" '[ "$rc" = 0 ] && grep -q -- "genA-start --prepared-runtime $(readlink -e "$GA/job") --path $HOME --restart -d" "$CALLS" && ! grep -q "^bridge-current" "$CALLS"'
reset
printf '#!/usr/bin/env bash\nprintf "custom %%s\\n" "$*" >> "$CALLS"; exit 0\n' > "$TMP/custom-launcher.sh"
out="$(CCC_TERMUX_LAUNCHER="$TMP/custom-launcher.sh" run)"; rc=$?
ok "CCC_TERMUX_LAUNCHER overrides the launcher" '[ "$rc" = 0 ] && grep -q "^custom --path $HOME --restart -d" "$CALLS"'
mv "$TMP/bridge-current.sh.keep" "$HOME/.ccc-node/scripts/bridge-current.sh"
rm -f "$HOME/.telegram_bot/health.json"

# ---------------------------------------------------------------- usage
out="$(run --bogus)"; rc=$?
ok "unknown argument → exit 2" '[ "$rc" = 2 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
