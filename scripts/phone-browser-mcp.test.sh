#!/usr/bin/env bash
# Tests for phone-browser-mcp.sh (#2522) with fake ssh / phone adb / npx:
# stdio pass-through and clean stdout, per-session forward + tunnel and their
# cleanup (normal exit, MCP failure, SIGTERM, tunnel failure), USB-debugging
# guard, Chrome foregrounding when the socket is missing, bounded ssh
# failures, MCP selection/extra args, keep-awake loop, input validation;
# #2529: startup orphan sweep (dead wrapper + state -> master closed, recorded
# forward removed; live wrapper preserved; no state -> tunnel only; PID reuse;
# other hosts / symlinks untouched; unreachable phone keeps state for retry),
# SIGKILL watcher (reaper reclaims, holds no stdio, stops on normal exit).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SUT="$HERE/phone-browser-mcp.sh"
pass=0; fail=0
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = "$2" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $3 (rc=$1 want=$2)"; fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
LOG="$TMP/log"; PH="$TMP/phone"; mkdir -p "$PH" "$TMP/remote-bin"
command -v python3 >/dev/null 2>&1 || { echo "FAIL: python3 is required (fake ControlMaster sockets)"; exit 1; }

# Real AF_UNIX socket files: the sweep only touches sockets, like ssh's -S ctl.
cat > "$TMP/mksock" <<'EOF'
#!/usr/bin/env bash
exec python3 -c 'import socket,sys; socket.socket(socket.AF_UNIX).bind(sys.argv[1])' "$1"
EOF
chmod +x "$TMP/mksock"

# Fake phone adb (runs "on the phone" via the fake ssh).
cat > "$TMP/remote-bin/adb" <<'EOF'
#!/usr/bin/env bash
[ "${1:-}" = -s ] && shift 2
case "$*" in
  "shell grep -c chrome_devtools_remote /proc/net/unix") cat "$FAKE_PH/sock" ;;
  "shell settings get global adb_enabled") cat "$FAKE_PH/usb" ;;
  "shell input keyevent KEYCODE_WAKEUP") echo WAKE >> "$FAKE_LOG" ;;
  "shell am start -n "*) echo AMSTART >> "$FAKE_LOG"; [ -e "$FAKE_PH/sock-after-start" ] && echo 1 > "$FAKE_PH/sock" ;;
  "forward tcp:0 localabstract:chrome_devtools_remote") echo FWD-ADD >> "$FAKE_LOG"; cat "$FAKE_PH/fwd-port" 2>/dev/null || echo 34567 ;;
  "forward --remove tcp:"*) echo "FWD-REMOVE ${3:-}" >> "$FAKE_LOG" ;;
  *) echo "ADB-UNKNOWN $*" >> "$FAKE_LOG"; exit 1 ;;
esac
EOF
chmod +x "$TMP/remote-bin/adb"

cat > "$TMP/ssh" <<'EOF'
#!/usr/bin/env bash
ctl=""; op=""; master=0; fwd=""; nflag=0
args=("$@"); n=${#args[@]}; i=0
while [ $i -lt $n ]; do
  case "${args[$i]}" in
    -S) i=$((i+1)); ctl="${args[$i]}" ;;
    -O) i=$((i+1)); op="${args[$i]}" ;;
    -L) i=$((i+1)); fwd="${args[$i]}" ;;
    -o) i=$((i+1)) ;;
    -M) master=1 ;;
    -n) nflag=1 ;;
    -f|-N) ;;
    *) break ;;
  esac
  i=$((i+1))
done
host="${args[$i]:-}"; i=$((i+1)); cmd="${args[*]:$i}"
case "$op" in
  exit) echo "SSH-EXIT $host ${ctl##*/}" >> "$FAKE_LOG"; [ -S "$ctl" ] || exit 255; rm -f "$ctl"; exit 0 ;;
  check) [ -e "$ctl" ]; exit $? ;;
esac
[ -e "$FAKE_PH/ssh-down" ] && exit 255
if [ "$master" = 1 ]; then
  echo "SSH-MASTER $host $fwd n=$nflag" >> "$FAKE_LOG"
  [ -e "$FAKE_PH/master-fail" ] && exit 255
  "$FAKE_MKSOCK" "$ctl"; exit 0
fi
[ "$nflag" = 1 ] || echo "SSH-WITHOUT-n $cmd" >> "$FAKE_LOG"
PATH="$FAKE_REMOTE_BIN:$PATH" bash -c "$cmd" </dev/null
EOF
chmod +x "$TMP/ssh"

# Fake npx: records argv, echoes stdin lines (stdio pass-through), optional sleep/rc.
cat > "$TMP/npx" <<'EOF'
#!/usr/bin/env bash
echo "NPX $*" >> "$FAKE_LOG"
echo $$ > "$FAKE_PH/npx.pid"
while IFS= read -r line || [ -n "$line" ]; do echo "ECHO:$line"; done
[ -n "${FAKE_NPX_SLEEP:-}" ] && sleep "$FAKE_NPX_SLEEP"
exit "${FAKE_NPX_RC:-0}"
EOF
chmod +x "$TMP/npx"

PORT_BASE=$(( 43000 + $$ % 2000 ))
reset() { : > "$LOG"; rm -f "$PH"/*; echo 1 > "$PH/sock"; echo 1 > "$PH/usb"; rm -rf "$TMP/state"; }
envs=(FAKE_LOG="$LOG" FAKE_PH="$PH" FAKE_REMOTE_BIN="$TMP/remote-bin" FAKE_MKSOCK="$TMP/mksock"
      PHONE_BROWSER_SSH="$TMP/ssh" PHONE_BROWSER_NPX="$TMP/npx"
      PHONE_BROWSER_STATE_DIR="$TMP/state" PHONE_BROWSER_PORT_BASE="$PORT_BASE"
      PHONE_BROWSER_SOCKET_WAIT=2)
# shellcheck disable=SC2034  # OUT/ERR are read via eval inside ok()
run() { # <stdin-text> <extra env...> -- <args...>
  local input="$1"; shift; local extra=()
  while [ "$1" != -- ]; do extra+=("$1"); shift; done; shift
  OUT="$(printf '%s' "$input" | env "${envs[@]}" "${extra[@]}" bash "$SUT" "$@" 2>"$TMP/err")"; RC=$?
  ERR="$(cat "$TMP/err")"
}
cnt() { grep -c "$1" "$LOG" 2>/dev/null || true; }
# Session files (<host>.<pid>.<port>.ctl/.state) left in the state dir.
state_files() { # <host>
  local f
  for f in "$TMP/state/$1".*.ctl "$TMP/state/$1".*.state; do
    [[ "${f##*/}" =~ ^$1\.[0-9]+\.[0-9]+\.(ctl|state)$ ]] && echo "${f##*/}"
  done
  return 0
}
# PIDs of reapers (`--reap-after <host> ...`) still running, by exact argv.
reapers_of() { # <host> [wrapper-pid]
  local f pid args
  for f in /proc/[0-9]*/cmdline; do
    pid="${f#/proc/}"; pid="${pid%/cmdline}"
    args="$(tr '\0' ' ' 2>/dev/null <"$f")" || continue
    case "$args" in *"$SUT --reap-after $1 ${2:+$2 }"*) echo "$pid" ;; esac
  done
}
dead_pid() { bash -c 'exit 0' & local p=$!; wait "$p"; echo "$p"; }
mkstate() { printf 'host=%s\nserial=127.0.0.1:5555\npp=%s\n' "${3:-ph}" "$2" > "$1"; }

# ── validation ───────────────────────────────────────────────────────────────
reset
run "" -- ; okc "$RC" 2 "no args rc 2"
run "" -- --help; okc "$RC" 0 "--help rc 0"
run "" -- '-oProxyCommand=x'; okc "$RC" 2 "option-looking host refused"
run "" -- ph --mcp firefox; okc "$RC" 2 "unknown --mcp refused"
run "" -- ph --bogus; okc "$RC" 2 "unknown arg refused"
run "" PHONE_BROWSER_KEEP_AWAKE=x -- ph; okc "$RC" 2 "bad keep-awake refused"
run "" PHONE_BROWSER_SWEEP_BUDGET=x -- ph; okc "$RC" 2 "bad sweep budget refused"
run "" PHONE_BROWSER_WATCH_INTERVAL=1s -- ph; okc "$RC" 2 "bad watch interval refused"
ok "validation touched nothing" '[ ! -s "$LOG" ]'

# ── happy path: chrome-devtools ──────────────────────────────────────────────
reset
run $'{"jsonrpc":"2.0","id":1}\nsecond' -- ph
okc "$RC" 0 "chrome-devtools run rc 0"
ok "stdin passes through to the MCP" 'grep -qx "ECHO:{\"jsonrpc\":\"2.0\",\"id\":1}" <<<"$OUT" && grep -qx "ECHO:second" <<<"$OUT"'
ok "stdout carries only MCP output" '! grep -qv "^ECHO:" <<<"$OUT"'
ok "launcher messages go to stderr" 'grep -q "Chrome DevTools → http://127.0.0.1:" <<<"$ERR"'
ok "per-session phone forward via tcp:0" '[ "$(cnt FWD-ADD)" = 1 ]'
ok "tunnel to the phone forward port" 'grep -qE "SSH-MASTER ph 127\.0\.0\.1:[0-9]+:127\.0\.0\.1:34567 n=1" "$LOG"'
# shellcheck disable=SC2034  # LP is read via eval inside ok()
LP="$(sed -nE 's/.*SSH-MASTER ph 127\.0\.0\.1:([0-9]+):.*/\1/p' "$LOG" | head -1)"
ok "pinned chrome-devtools-mcp with browserUrl + privacy flags" 'grep -qx "NPX -y chrome-devtools-mcp@1.12.0 --browserUrl http://127.0.0.1:$LP --no-usage-statistics --no-performance-crux" "$LOG"'
ok "screen woken once" '[ "$(cnt WAKE)" = 1 ]'
ok "cleanup: own forward removed" 'grep -qx "FWD-REMOVE tcp:34567" "$LOG"'
ok "cleanup: own tunnel closed" 'grep -q "SSH-EXIT ph" "$LOG" && [ -z "$(state_files ph)" ]'
ok "cleanup: no reaper left behind" '[ -z "$(reapers_of ph)" ]'
ok "every remote ssh ran with -n" '! grep -q "SSH-WITHOUT-n" "$LOG"'

# ── playwright + extra args ──────────────────────────────────────────────────
reset
run "" -- ph --mcp playwright -- --isolated
okc "$RC" 0 "playwright run rc 0"
ok "pinned playwright with cdp-endpoint + extra args" 'grep -qE "^NPX -y @playwright/mcp@0\.0\.83 --cdp-endpoint http://127\.0\.0\.1:[0-9]+ --isolated$" "$LOG"'

# ── MCP failure rc passes through, still cleaned ─────────────────────────────
reset
run "" FAKE_NPX_RC=7 -- ph
okc "$RC" 7 "MCP rc passes through"
ok "cleanup after MCP failure" 'grep -q "FWD-REMOVE tcp:34567" "$LOG" && grep -q "SSH-EXIT ph" "$LOG"'

# ── USB debugging guard / foregrounding ──────────────────────────────────────
reset; echo 0 > "$PH/sock"; echo 0 > "$PH/usb"
run "" -- ph
okc "$RC" 6 "USB debugging off → rc 6"
ok "usb-off message names the setting" 'grep -q "USB debugging is off" <<<"$ERR"'
ok "usb-off: no forward, no MCP, no settings write" '[ "$(cnt FWD-ADD)" = 0 ] && [ "$(cnt NPX)" = 0 ] && ! grep -q "settings put" "$LOG"'
reset; echo 0 > "$PH/sock"; : > "$PH/sock-after-start"
run "" -- ph
ok "socket missing → wake + foreground Chrome, then run" '[ "$RC" = 0 ] && [ "$(cnt AMSTART)" = 1 ] && [ "$(cnt NPX)" = 1 ]'
reset; echo 0 > "$PH/sock"
run "" -- ph
okc "$RC" 6 "socket never appears → rc 6"
ok "no forward when socket never appeared" '[ "$(cnt FWD-ADD)" = 0 ]'

# ── ssh failures ─────────────────────────────────────────────────────────────
reset; : > "$PH/ssh-down"
run "" -- ph
okc "$RC" 3 "unreachable phone → rc 3"
reset; : > "$PH/master-fail"
run "" -- ph
okc "$RC" 3 "tunnel failure → rc 3"
ok "stops after 3 master attempts" '[ "$(cnt SSH-MASTER)" = 3 ]'
ok "forward removed even when tunnel failed" 'grep -q "FWD-REMOVE tcp:34567" "$LOG" && [ "$(cnt NPX)" = 0 ]'

# ── SIGTERM cleans up ────────────────────────────────────────────────────────
# A FIFO held open by fd 7 keeps the MCP's stdin alive; exact PIDs only (no
# pkill/pgrep -f, which can match the test runner's own command line).
reset
FIFO="$TMP/stdin.fifo"; mkfifo "$FIFO"
env "${envs[@]}" bash "$SUT" ph <"$FIFO" >/dev/null 2>&1 &
lpid=$!
exec 7>"$FIFO"
for _ in $(seq 1 50); do [ -s "$PH/npx.pid" ] && break; sleep 0.1; done
ok "launcher running before TERM" 'kill -0 "$lpid" 2>/dev/null && [ -s "$PH/npx.pid" ]'
kill -TERM "$lpid" 2>/dev/null
for _ in $(seq 1 50); do kill -0 "$lpid" 2>/dev/null || break; sleep 0.1; done
exec 7>&-
wait "$lpid" 2>/dev/null
ok "TERM: forward removed + tunnel closed" 'grep -q "FWD-REMOVE tcp:34567" "$LOG" && grep -q "SSH-EXIT ph" "$LOG"'
# shellcheck disable=SC2034  # npid is read via eval inside ok()
npid="$(cat "$PH/npx.pid" 2>/dev/null)"
sleep 0.2
ok "TERM: MCP child stopped" '[ -n "$npid" ] && ! kill -0 "$npid" 2>/dev/null'

# ── keep-awake ───────────────────────────────────────────────────────────────
reset
run "" PHONE_BROWSER_KEEP_AWAKE=1 FAKE_NPX_SLEEP=2.6 -- ph
ok "keep-awake re-wakes while MCP runs" '[ "$RC" = 0 ] && [ "$(cnt WAKE)" -ge 3 ]'

# ── #2529 (a) orphan sweep: dead wrapper + state; host / symlink isolation ───
reset; mkdir -p "$TMP/state"; S="$TMP/state"
D="$(dead_pid)"
"$TMP/mksock" "$S/ph.$D.40001.ctl"; mkstate "$S/ph.$D.40001.state" 45678
mkstate "$S/ph.$D.0.state" 45680                      # killed before its tunnel
"$TMP/mksock" "$S/ph2.$D.40002.ctl"; mkstate "$S/ph2.$D.40002.state" 45679 ph2
"$TMP/mksock" "$S/ph.x.$D.40003.ctl"                  # host "ph.x", not "ph"
"$TMP/mksock" "$TMP/elsewhere.sock"; ln -s "$TMP/elsewhere.sock" "$S/ph.$D.40004.ctl"
: > "$S/ph.$D.40006.ctl"                              # not a socket
run "" -- ph
okc "$RC" 0 "sweep run rc 0"
ok "(a) orphan master closed" 'grep -qx "SSH-EXIT ph ph.$D.40001.ctl" "$LOG"'
ok "(a) orphan's recorded forward removed" 'grep -qx "FWD-REMOVE tcp:45678" "$LOG"'
ok "(a) orphan socket + state deleted" '[ ! -e "$S/ph.$D.40001.ctl" ] && [ ! -e "$S/ph.$D.40001.state" ]'
ok "(a) state-only orphan: forward removed, state deleted" 'grep -qx "FWD-REMOVE tcp:45680" "$LOG" && [ ! -e "$S/ph.$D.0.state" ]'
ok "(a) sweep ran before this session's forward" '[ "$(grep -n "FWD-REMOVE tcp:45678" "$LOG" | cut -d: -f1)" -lt "$(grep -n FWD-ADD "$LOG" | cut -d: -f1)" ]'
ok "(a) own session still cleaned" 'grep -qx "FWD-REMOVE tcp:34567" "$LOG" && [ "$(cnt FWD-REMOVE)" = 3 ]'
ok "(a) other host's session untouched" '[ -S "$S/ph2.$D.40002.ctl" ] && [ -f "$S/ph2.$D.40002.state" ] && ! grep -q "tcp:45679\|SSH-EXIT ph2" "$LOG"'
ok "(a) dotted host prefix not mistaken for ours" '[ -S "$S/ph.x.$D.40003.ctl" ] && ! grep -q "ph.x.$D" "$LOG"'
ok "(a) symlinked ctl never followed" '[ -L "$S/ph.$D.40004.ctl" ] && [ -S "$TMP/elsewhere.sock" ] && ! grep -q "40004" "$LOG"'
ok "(a) non-socket ctl left alone" '[ -f "$S/ph.$D.40006.ctl" ] && ! grep -q "40006" "$LOG"'
ok "(a) sweep messages on stderr" 'grep -q "orphan sweep: wrapper $D" <<<"$ERR" && grep -q "phone forward tcp:45678 removed" <<<"$ERR"'
ok "(f) sweep wrote nothing to stdout" '[ -z "$OUT" ]'

# ── (c) socket without a state file: tunnel only, phone forwards untouched ───
reset; mkdir -p "$TMP/state"; D="$(dead_pid)"
"$TMP/mksock" "$S/ph.$D.40011.ctl"
run "" -- ph
ok "(c) stateless orphan tunnel closed + socket removed" 'grep -qx "SSH-EXIT ph ph.$D.40011.ctl" "$LOG" && [ ! -e "$S/ph.$D.40011.ctl" ]'
ok "(c) no forward removed except our own" '[ "$(cnt FWD-REMOVE)" = 1 ] && grep -qx "FWD-REMOVE tcp:34567" "$LOG"'
ok "(c) logged that phone forwards were left alone" 'grep -q "no state file, phone forwards left alone" <<<"$ERR"'

# ── (d) PID reuse: live PID that is not a phone-browser-mcp wrapper ──────────
reset; mkdir -p "$TMP/state"
sleep 30 & SP=$!
"$TMP/mksock" "$S/ph.$SP.40021.ctl"; mkstate "$S/ph.$SP.40021.state" 45690
run "" -- ph
ok "(d) reused PID → session reclaimed" 'grep -qx "SSH-EXIT ph ph.$SP.40021.ctl" "$LOG" && grep -qx "FWD-REMOVE tcp:45690" "$LOG" && [ -z "$(ls "$S" | grep "\.$SP\.")" ]'
kill "$SP" 2>/dev/null; wait "$SP" 2>/dev/null

# ── unreachable phone: tunnel closed, state kept, next launch retries ────────
reset; mkdir -p "$TMP/state"; D="$(dead_pid)"
"$TMP/mksock" "$S/ph.$D.40031.ctl"; mkstate "$S/ph.$D.40031.state" 45700
mkstate "$S/ph.$D.40032.state" 45701
: > "$PH/ssh-down"
run "" -- ph
okc "$RC" 3 "unreachable phone still rc 3 after the sweep"
ok "unreachable: tunnel closed, both states kept" '[ ! -e "$S/ph.$D.40031.ctl" ] && [ -f "$S/ph.$D.40031.state" ] && [ -f "$S/ph.$D.40032.state" ] && [ -z "$(ls "$S" | grep "\.reap\.")" ]'
ok "unreachable: one ssh attempt for forwards, then skipped" '[ "$(grep -c "kept for the next launch" <<<"$ERR")" = 2 ]'
rm -f "$PH/ssh-down"; : > "$LOG"
run "" -- ph
ok "retry: kept forwards removed on the next launch" 'grep -qx "FWD-REMOVE tcp:45700" "$LOG" && grep -qx "FWD-REMOVE tcp:45701" "$LOG" && [ -z "$(state_files ph)" ]'

# ── (b) live wrapper preserved, (e) SIGKILL watcher, (f) reaper stdio ────────
reset; echo 34001 > "$PH/fwd-port"
FIFO="$TMP/stdin2.fifo"; mkfifo "$FIFO"
env "${envs[@]}" PHONE_BROWSER_WATCH_INTERVAL=0.2 bash "$SUT" ph <"$FIFO" >/dev/null 2>"$TMP/errA" &
apid=$!
exec 7>"$FIFO"
for _ in $(seq 1 50); do [ -s "$PH/npx.pid" ] && break; sleep 0.1; done
ACTL=""; for f in "$S/ph.$apid".*.ctl; do [ -S "$f" ] && ACTL="${f##*/}"; done; ASTEM="${ACTL%.ctl}"
ok "(b) wrapper A running with ctl + state" '[ -n "$ACTL" ] && [ -S "$S/$ACTL" ] && [ -f "$S/$ASTEM.state" ] && grep -qx "pp=34001" "$S/$ASTEM.state"'
# shellcheck disable=SC2034  # read via eval inside ok()
RPID="$(reapers_of ph "$apid" | head -1)"
ok "(e) wrapper A has a reaper" '[ -n "$RPID" ]'
ok "(f) reaper stdin/stdout are /dev/null" '[ "$(readlink "/proc/$RPID/fd/0")" = /dev/null ] && [ "$(readlink "/proc/$RPID/fd/1")" = /dev/null ]'
if command -v setsid >/dev/null 2>&1; then
  ok "(e) reaper runs in its own session" '[ "$(ps -o sid= -p "$RPID" | tr -d " ")" != "$(ps -o sid= -p "$apid" | tr -d " ")" ]'
fi
echo 34002 > "$PH/fwd-port"
run "" PHONE_BROWSER_WATCH_INTERVAL=0.2 -- ph
okc "$RC" 0 "(b) second wrapper rc 0 while A runs"
ok "(b) live wrapper's tunnel + state preserved" '[ -S "$S/$ACTL" ] && [ -f "$S/$ASTEM.state" ] && ! grep -q "SSH-EXIT ph $ACTL" "$LOG" && ! grep -q "tcp:34001" "$LOG"'
ok "(b) second wrapper cleaned only its own" 'grep -qx "FWD-REMOVE tcp:34002" "$LOG" && [ "$(cnt FWD-REMOVE)" = 1 ]'
# shellcheck disable=SC2034  # read via eval inside ok()
BPID="$(sed -nE 's/^SSH-EXIT ph ph\.([0-9]+)\..*/\1/p' "$LOG" | head -1)"
ok "(e) normal exit stopped the second wrapper's reaper" '[ -n "$BPID" ] && [ -z "$(reapers_of ph "$BPID")" ]'
: > "$LOG"
kill -KILL "$apid"; wait "$apid" 2>/dev/null
for _ in $(seq 1 50); do [ -e "$S/$ASTEM.state" ] || [ -e "$S/$ACTL" ] || break; sleep 0.1; done
ok "(e) SIGKILLed wrapper: reaper closed its master" 'grep -qx "SSH-EXIT ph $ACTL" "$LOG" && [ ! -e "$S/$ACTL" ]'
ok "(e) SIGKILLed wrapper: reaper removed its forward + state" 'grep -qx "FWD-REMOVE tcp:34001" "$LOG" && [ ! -e "$S/$ASTEM.state" ] && [ "$(cnt FWD-REMOVE)" = 1 ]'
ok "(e) reaper logged to reaper.log, not the MCP stdio" 'grep -q "wrapper $apid died without cleanup" "$S/reaper.log" && ! grep -q "died without cleanup" "$TMP/errA"'
for _ in $(seq 1 20); do [ -z "$(reapers_of ph "$apid")" ] && break; sleep 0.1; done
ok "(e) reaper exited after reclaiming" '[ -z "$(reapers_of ph "$apid")" ]'
exec 7>&-

# ── watcher disabled ─────────────────────────────────────────────────────────
reset
env "${envs[@]}" PHONE_BROWSER_WATCH_INTERVAL=0 bash "$SUT" ph <"$FIFO" >/dev/null 2>&1 &
apid=$!
exec 7>"$FIFO"
for _ in $(seq 1 50); do [ -s "$PH/npx.pid" ] && break; sleep 0.1; done
ok "PHONE_BROWSER_WATCH_INTERVAL=0 starts no reaper" 'kill -0 "$apid" 2>/dev/null && [ -z "$(reapers_of ph "$apid")" ]'
exec 7>&-
wait "$apid" 2>/dev/null

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
