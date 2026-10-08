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
echo "restart args: \$* channel=\${CCC_CHANNEL:-<unset>}" >> "$MARKER"
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

# ---- start lock (#970): another start in flight -> skip, no pile-on ----------
rm -f "$PID_FILE" "$MARKER"
LOCKF="$TMP/wd.lock"
run_wd_locked() {
  rm -f "$MARKER"
  local rc=0
  HOME="$TMP" \
  BRIDGE_WATCHDOG_LOG="$LOG" \
  BRIDGE_WATCHDOG_PID_FILE="$PID_FILE" \
  BRIDGE_WATCHDOG_START="$STARTSTUB" \
  BRIDGE_WATCHDOG_LOCK="$LOCKF" \
  BRIDGE_WATCHDOG_PROCESS_MATCH="__ccc_wd_no_such_process_zzz__" \
  bash "$WD" || rc=$?
  printf '%s' "$rc"
}
exec 9>"$LOCKF"
flock 9   # simulate a concurrent tick mid-start (dependency build in flight)
okc "$(run_wd_locked)" "lock held: exits 0"
ok  "lock held: does NOT start another start.sh" '[ ! -f "$MARKER" ]'
ok  "lock held: logs the lock skip" 'grep -q "lock held" "$LOG"'
flock -u 9
exec 9>&-
okc "$(run_wd_locked)" "lock released: next tick proceeds"
ok  "lock released: restarts normally" '[ -f "$MARKER" ]'

# ---- start.sh absent -> logs, does not crash --------------------------------
rm -f "$PID_FILE" "$MARKER"
missing_rc=0
HOME="$TMP" BRIDGE_WATCHDOG_LOG="$LOG" BRIDGE_WATCHDOG_PID_FILE="$PID_FILE" \
  BRIDGE_WATCHDOG_START="$TMP/does-not-exist.sh" \
  BRIDGE_WATCHDOG_PROCESS_MATCH="__ccc_wd_no_such_process_zzz__" \
  bash "$WD" || missing_rc=$?
okc "$missing_rc" "missing start.sh: exits 0 (no crash)"
ok  "missing start.sh: logs not-found" 'grep -q "not found/executable" "$LOG"'

# ---- channel filter (#2176): a Matrix frontend is not the Telegram bridge ----
# The Matrix frontend runs the same `python -m telegram_bot --path $HOME`
# command line, so the pgrep fallback used to count it as "Telegram is up" and
# never restarted a dead Telegram bridge. Spawn real stand-in processes whose
# cmdline carries a unique marker and whose environ carries the channel, so the
# watchdog reads them exactly as it reads a live bridge.
FAKE_MARK="__ccc_wd_fakebridge_$$_${RANDOM}__"
FAKE_PIDS=()
FAKE_LAST=""
# A single process per stand-in: python3 keeps the marker in its own cmdline
# (`bash -c 'sleep …'` forked a sleep that outlived the kill). The pid goes to
# a global, not stdout, so the caller's FAKE_PIDS sees it (no subshell).
spawn_fake() { # $1 = matrix | telegram ; sets FAKE_LAST
  if [ "$1" = matrix ]; then
    env CCC_CHANNEL=matrix python3 -c 'import time; time.sleep(300)' "$FAKE_MARK" >/dev/null 2>&1 &
  else
    env -u CCC_CHANNEL python3 -c 'import time; time.sleep(300)' "$FAKE_MARK" >/dev/null 2>&1 &
  fi
  FAKE_LAST=$!
  FAKE_PIDS+=("$FAKE_LAST")
}
reap_fakes() {
  local p
  for p in "${FAKE_PIDS[@]}"; do kill "$p" 2>/dev/null; wait "$p" 2>/dev/null; done
  FAKE_PIDS=()
}
trap 'reap_fakes; rm -rf "$TMP"' EXIT
run_wd_match() { # like run_wd, but the fallback matches the stand-in processes
  rm -f "$MARKER"
  local rc=0
  HOME="$TMP" \
  BRIDGE_WATCHDOG_LOG="$LOG" \
  BRIDGE_WATCHDOG_PID_FILE="$PID_FILE" \
  BRIDGE_WATCHDOG_START="$STARTSTUB" \
  BRIDGE_WATCHDOG_LOCK="$TMP/wd-chan.lock" \
  BRIDGE_WATCHDOG_GRACE_SECONDS=90 \
  BRIDGE_WATCHDOG_PROCESS_MATCH="$FAKE_MARK" \
  BRIDGE_WATCHDOG_PROC_ROOT="${WD_PROC_ROOT:-/proc}" \
  bash "$WD" || rc=$?
  printf '%s' "$rc"
}
stale_pidfile() { printf '%s' "$(dead_pid)" > "$PID_FILE"; touch -d '2000-01-01 00:00:00' "$PID_FILE"; }

if [ -r "/proc/$$/environ" ] && command -v python3 >/dev/null 2>&1; then
  # Telegram down (stale bot.pid), only a Matrix frontend alive -> restart.
  stale_pidfile
  spawn_fake matrix; sleep 0.3
  okc "$(run_wd_match)" "matrix-only alive: exits 0"
  ok  "matrix-only alive: Telegram is judged down and restarted (#2176)" '[ -f "$MARKER" ]'

  # A Telegram-channel bridge alive alongside Matrix -> still healthy.
  stale_pidfile
  spawn_fake telegram; sleep 0.3
  okc "$(run_wd_match)" "telegram alive next to matrix: exits 0"
  ok  "telegram alive next to matrix: does NOT restart" '[ ! -f "$MARKER" ]'
  reap_fakes

  # bot.pid recycled onto the live Matrix frontend -> not proof of Telegram.
  spawn_fake matrix; sleep 0.3
  printf '%s' "$FAKE_LAST" > "$PID_FILE"; touch -d '2000-01-01 00:00:00' "$PID_FILE"
  okc "$(run_wd_match)" "bot.pid on matrix pid: exits 0"
  ok  "bot.pid on matrix pid: Telegram is judged down and restarted" '[ -f "$MARKER" ]'

  # Unreadable environ -> conservative telegram reading, never a false "down".
  stale_pidfile
  mkdir -p "$TMP/empty-proc"
  WD_PROC_ROOT="$TMP/empty-proc"
  okc "$(run_wd_match)" "unreadable environ: exits 0"
  ok  "unreadable environ: treated as telegram, does NOT restart" '[ ! -f "$MARKER" ]'
  unset WD_PROC_ROOT
  reap_fakes

  # Watchdog launched with a Matrix shell's environment (e.g. a crond started
  # there): start.sh must still be called for the Telegram channel.
  stale_pidfile
  CCC_CHANNEL=matrix SESSION_STORE_PATH=/nonexistent/sessions.json run_wd_match >/dev/null
  ok  "matrix caller env: restart still attempted" '[ -f "$MARKER" ]'
  ok  "matrix caller env: start.sh gets no inherited CCC_CHANNEL" 'grep -q "channel=<unset>" "$MARKER"'
else
  echo "SKIP: channel filter cases (needs readable /proc/<pid>/environ and python3)"
fi

# ---- start.sh exit code is logged, not masked by $(ts) -----------------------
FAILSTUB="$TMP/start-fail.sh"
printf '#!/usr/bin/env bash\nexit 3\n' > "$FAILSTUB"; chmod +x "$FAILSTUB"
rm -f "$PID_FILE"; : > "$LOG"
HOME="$TMP" BRIDGE_WATCHDOG_LOG="$LOG" BRIDGE_WATCHDOG_PID_FILE="$PID_FILE" \
  BRIDGE_WATCHDOG_START="$FAILSTUB" BRIDGE_WATCHDOG_LOCK="$TMP/wd-rc.lock" \
  BRIDGE_WATCHDOG_PROCESS_MATCH="__ccc_wd_no_such_process_zzz__" \
  bash "$WD" >/dev/null 2>&1
ok  "failed start: logs the real start.sh exit code" 'grep -q "start.sh exit=3" "$LOG"'

# ---- unset HOME (cron/systemd context) -> still runs -------------------------
# Regression (#869 sweep): every default below `set -u` dereferenced $HOME, so
# a watchdog started by cron/systemd without HOME died on "unbound variable"
# before it could create its log dir -- silently disabling supervision.
#
# The $HOME-defaulted overrides MUST NOT be set here: `${VAR:-word}` never
# evaluates its default when VAR is set, so passing BRIDGE_WATCHDOG_LOG /
# PID_FILE / START (as the fixtures above do) hides the very bug this pins.
# Point HOME's fallback at a scratch dir instead, so the HOME-less run cannot
# touch this host's real bridge state or the fixtures asserted above. Runs
# last for the same reason.
NOHOME_ROOT="$TMP/nohome-root"; mkdir -p "$NOHOME_ROOT"
# shellcheck disable=SC2034  # consumed inside the quoted ok() assertions below
nohome_out="$(env -u HOME \
  CCC_WATCHDOG_HOME_FALLBACK="$NOHOME_ROOT" \
  BRIDGE_WATCHDOG_PROCESS_MATCH="__ccc_wd_no_such_process_zzz__" \
  bash "$WD" 2>&1)"
# shellcheck disable=SC2034  # consumed inside the quoted ok() assertions below
nohome_rc=$?
ok "unset HOME: no unbound-variable abort" '! grep -q "unbound variable" <<<"$nohome_out"'
ok "unset HOME: reached the watchdog body" '[ "$nohome_rc" = 0 ]'
ok "unset HOME: used the fallback root, not the real home" '[ -d "$NOHOME_ROOT/.hermes/logs" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
