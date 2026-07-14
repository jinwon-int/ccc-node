#!/usr/bin/env bash
# Tests for bridge-watchdog.sh — debounce window, stale/live PID handling,
# restart branches, and interpreter portability (#450).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WD="$HERE/bridge-watchdog.sh"
pass=0; fail=0
# ok: eval a shell condition string. okc: assert a captured exit code equals 0
# (passing rc as an argument keeps it visible to static analysis).
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = 0 ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $2 (rc=$1)"; fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

STARTSTUB="$TMP/start.sh"
MARKER="$TMP/restart.marker"
cat > "$STARTSTUB" <<STUB
#!/usr/bin/env bash
echo "restart args: \$*" >> "$MARKER"
exit 0
STUB
chmod +x "$STARTSTUB"

PID_FILE="$TMP/bot.pid"
LOG="$TMP/wd.log"

run_wd() { # runs the watchdog against the stub start.sh; prints exit code
  rm -f "$MARKER"
  local rc=0
  HOME="$TMP" \
  BRIDGE_WATCHDOG_LOG="$LOG" \
  BRIDGE_WATCHDOG_PID_FILE="$PID_FILE" \
  BRIDGE_WATCHDOG_START="$STARTSTUB" \
  BRIDGE_WATCHDOG_GRACE_SECONDS=90 \
  BRIDGE_WATCHDOG_PROCESS_MATCH="__ccc_wd_no_such_process_zzz__" \
  bash "$WD" || rc=$?
  printf '%s' "$rc"
}

dead_pid() { # print a pid guaranteed not to be alive
  sleep 1 & local d=$!
  kill "$d" 2>/dev/null
  wait "$d" 2>/dev/null || true
  printf '%s' "$d"
}

# ---- portability ------------------------------------------------------------
ok "shebang is env-based (runs on VPS + Termux)" '[ "$(head -1 "$WD")" = "#!/usr/bin/env bash" ]'
ok "no hardcoded Termux interpreter path remains" '! grep -q "com.termux/files/usr/bin/bash" "$WD"'
ok "uses set -uo pipefail" 'grep -q "set -uo pipefail" "$WD"'

# ---- alive: live pid in bot.pid -> skip, no restart -------------------------
printf '%s' "$$" > "$PID_FILE"   # the test runner pid: definitely alive
okc "$(run_wd)" "alive pid: exits 0"
ok  "alive pid: does NOT restart" '[ ! -f "$MARKER" ]'

# ---- debounce: dead pid but fresh bot.pid -> skip this tick -----------------
printf '%s' "$(dead_pid)" > "$PID_FILE"   # mtime = now (fresh)
okc "$(run_wd)" "fresh dead pid: exits 0 (debounced)"
ok  "fresh dead pid: does NOT restart (races a fresh start)" '[ ! -f "$MARKER" ]'
ok  "fresh dead pid: logs the debounce skip" 'grep -q "skipping this tick" "$LOG"'

# ---- stale: dead pid + old bot.pid -> restart -------------------------------
printf '%s' "$(dead_pid)" > "$PID_FILE"
touch -d '2000-01-01 00:00:00' "$PID_FILE"   # age >> GRACE_SECONDS
okc "$(run_wd)" "stale dead pid: exits 0"
ok  "stale dead pid: restarts via start.sh" '[ -f "$MARKER" ]'
ok  "stale dead pid: restart passes --daemon" 'grep -q -- "--daemon" "$MARKER"'

# ---- missing pid file -> restart --------------------------------------------
rm -f "$PID_FILE"
okc "$(run_wd)" "missing pid file: exits 0"
ok  "missing pid file: restarts via start.sh" '[ -f "$MARKER" ]'

# ---- start.sh absent -> logs, does not crash --------------------------------
rm -f "$PID_FILE" "$MARKER"
missing_rc=0
HOME="$TMP" BRIDGE_WATCHDOG_LOG="$LOG" BRIDGE_WATCHDOG_PID_FILE="$PID_FILE" \
  BRIDGE_WATCHDOG_START="$TMP/does-not-exist.sh" \
  BRIDGE_WATCHDOG_PROCESS_MATCH="__ccc_wd_no_such_process_zzz__" \
  bash "$WD" || missing_rc=$?
okc "$missing_rc" "missing start.sh: exits 0 (no crash)"
ok  "missing start.sh: logs not-found" 'grep -q "not found/executable" "$LOG"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
