#!/usr/bin/env bash
# Tests for phone-browser-mcp.sh (#2522) with fake ssh / phone adb / npx:
# stdio pass-through and clean stdout, per-session forward + tunnel and their
# cleanup (normal exit, MCP failure, SIGTERM, tunnel failure), USB-debugging
# guard, Chrome foregrounding when the socket is missing, bounded ssh
# failures, MCP selection/extra args, keep-awake loop, input validation.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SUT="$HERE/phone-browser-mcp.sh"
pass=0; fail=0
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = "$2" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $3 (rc=$1 want=$2)"; fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
LOG="$TMP/log"; PH="$TMP/phone"; mkdir -p "$PH" "$TMP/remote-bin"

# Fake phone adb (runs "on the phone" via the fake ssh).
cat > "$TMP/remote-bin/adb" <<'EOF'
#!/usr/bin/env bash
[ "${1:-}" = -s ] && shift 2
case "$*" in
  "shell grep -c chrome_devtools_remote /proc/net/unix") cat "$FAKE_PH/sock" ;;
  "shell settings get global adb_enabled") cat "$FAKE_PH/usb" ;;
  "shell input keyevent KEYCODE_WAKEUP") echo WAKE >> "$FAKE_LOG" ;;
  "shell am start -n "*) echo AMSTART >> "$FAKE_LOG"; [ -e "$FAKE_PH/sock-after-start" ] && echo 1 > "$FAKE_PH/sock" ;;
  "forward tcp:0 localabstract:chrome_devtools_remote") echo FWD-ADD >> "$FAKE_LOG"; echo 34567 ;;
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
  exit) echo "SSH-EXIT $host" >> "$FAKE_LOG"; rm -f "$ctl"; exit 0 ;;
  check) [ -e "$ctl" ]; exit $? ;;
esac
[ -e "$FAKE_PH/ssh-down" ] && exit 255
if [ "$master" = 1 ]; then
  echo "SSH-MASTER $host $fwd n=$nflag" >> "$FAKE_LOG"
  [ -e "$FAKE_PH/master-fail" ] && exit 255
  : > "$ctl"; exit 0
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
envs=(FAKE_LOG="$LOG" FAKE_PH="$PH" FAKE_REMOTE_BIN="$TMP/remote-bin"
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

# ── validation ───────────────────────────────────────────────────────────────
reset
run "" -- ; okc "$RC" 2 "no args rc 2"
run "" -- --help; okc "$RC" 0 "--help rc 0"
run "" -- '-oProxyCommand=x'; okc "$RC" 2 "option-looking host refused"
run "" -- ph --mcp firefox; okc "$RC" 2 "unknown --mcp refused"
run "" -- ph --bogus; okc "$RC" 2 "unknown arg refused"
run "" PHONE_BROWSER_KEEP_AWAKE=x -- ph; okc "$RC" 2 "bad keep-awake refused"
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
ok "cleanup: own tunnel closed" 'grep -q "SSH-EXIT ph" "$LOG" && [ -z "$(ls -A "$TMP/state" 2>/dev/null)" ]'
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

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
