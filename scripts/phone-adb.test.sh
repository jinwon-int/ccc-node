#!/usr/bin/env bash
# Tests for phone-adb.sh (#2517) — fake ssh/adb on PATH-free seams
# (PHONE_ADB_SSH / PHONE_ADB_ADB): tunnel open + reuse, version guard,
# refused subcommands, host/serial validation, ssh-mode quoting round-trip
# (no remote command injection), bounded ssh-failure retries, close/status,
# and stale-state recovery.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SUT="$HERE/phone-adb.sh"
pass=0; fail=0
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = "$2" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $3 (rc=$1 want=$2)"; fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
LOG="$TMP/log"

# ── fakes ────────────────────────────────────────────────────────────────────
# Fake remote adb: prints one argument per line (so quoting is observable).
mkdir -p "$TMP/remote-bin"
cat > "$TMP/remote-bin/adb" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = version ]; then echo "Android Debug Bridge version ${FAKE_REMOTE_VER:-1.0.41}"; exit 0; fi
for a in "$@"; do printf 'ARG<%s>\n' "$a"; done
EOF
chmod +x "$TMP/remote-bin/adb"

cat > "$TMP/ssh" <<'EOF'
#!/usr/bin/env bash
# Fake ssh: ControlMaster check/exit/open via a ctl file; remote commands run
# through bash -c with the fake remote adb first on PATH (like a login shell).
ctl=""; op=""; master=0; fwd=""
args=("$@"); n=${#args[@]}; i=0
while [ $i -lt $n ]; do
  a="${args[$i]}"
  case "$a" in
    -S) i=$((i+1)); ctl="${args[$i]}" ;;
    -O) i=$((i+1)); op="${args[$i]}" ;;
    -M) master=1 ;;
    -L) i=$((i+1)); fwd="${args[$i]}" ;;
    -o) i=$((i+1)) ;;
    -f|-N|-t) ;;
    *) break ;;
  esac
  i=$((i+1))
done
host="${args[$i]:-}"; i=$((i+1)); cmd="${args[*]:$i}"
case "$op" in
  check) [ -e "$ctl" ]; exit $? ;;
  exit) echo "SSH-EXIT $host" >> "$FAKE_LOG"; rm -f "$ctl"; exit 0 ;;
esac
if [ "$master" = 1 ]; then
  echo "SSH-MASTER $host $fwd" >> "$FAKE_LOG"
  [ "${FAKE_MASTER_RC:-0}" = 0 ] || exit "$FAKE_MASTER_RC"
  : > "$ctl"; exit 0
fi
echo "SSH-CMD $host $cmd" >> "$FAKE_LOG"
PATH="$FAKE_REMOTE_BIN:$PATH" bash -c "$cmd"
EOF
chmod +x "$TMP/ssh"

cat > "$TMP/adb" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = version ]; then echo "Android Debug Bridge version ${FAKE_LOCAL_VER:-1.0.41}"; exit 0; fi
echo "ADB $*" >> "$FAKE_LOG"
echo "local-adb-ok"
EOF
chmod +x "$TMP/adb"

PORT_BASE=$(( 42000 + $$ % 2000 ))
# shellcheck disable=SC2034  # OUT is read via eval inside ok()
run() { # <env...> -- <args...>  → $OUT, $RC
  local envs=()
  while [ "$1" != -- ]; do envs+=("$1"); shift; done; shift
  OUT="$(env FAKE_LOG="$LOG" FAKE_REMOTE_BIN="$TMP/remote-bin" \
    PHONE_ADB_SSH="$TMP/ssh" PHONE_ADB_ADB="$TMP/adb" \
    PHONE_ADB_STATE_DIR="$TMP/state" PHONE_ADB_PORT_BASE="$PORT_BASE" \
    "${envs[@]}" bash "$SUT" "$@" 2>&1)"; RC=$?
}
count() { grep -c "$1" "$LOG" 2>/dev/null || true; }
reset() { : > "$LOG"; rm -rf "$TMP/state"; }

# ── usage / validation ───────────────────────────────────────────────────────
reset
run -- ; okc "$RC" 2 "no args → usage rc 2"
run -- --help; okc "$RC" 0 "--help rc 0"
run -- ph; okc "$RC" 2 "host without adb args → rc 2"
run -- '-oProxyCommand=touch' shell id; okc "$RC" 2 "option-looking host refused"
run -- 'a b' shell id; okc "$RC" 2 "host with space refused"
run -- ph --serial 'x;y' shell id; okc "$RC" 2 "bad serial refused"
ok "validation never reached ssh/adb" '[ ! -s "$LOG" ]'

# ── refused subcommands ──────────────────────────────────────────────────────
run PHONE_ADB_MODE=tunnel -- ph kill-server; okc "$RC" 2 "kill-server refused"
run PHONE_ADB_MODE=tunnel -- ph start-server; okc "$RC" 2 "start-server refused"
run PHONE_ADB_MODE=tunnel -- ph disconnect; okc "$RC" 2 "bare disconnect refused"
run PHONE_ADB_MODE=tunnel -- ph reconnect; okc "$RC" 2 "bare reconnect refused"
run PHONE_ADB_MODE=tunnel -- ph -s other shell id; okc "$RC" 2 "adb global -s refused"
run PHONE_ADB_MODE=tunnel -- ph -P 5037 devices; okc "$RC" 2 "adb global -P refused"
ok "refusals never reached ssh/adb" '[ ! -s "$LOG" ]'

# ── tunnel mode: open, then reuse ────────────────────────────────────────────
reset
run PHONE_ADB_MODE=tunnel -- ph shell getprop ro.product.model
okc "$RC" 0 "tunnel run rc 0"
# shellcheck disable=SC2034  # P is read via eval inside ok()
P="$(cat "$TMP/state/ph.port" 2>/dev/null)"
ok "port recorded in range" '[ -n "$P" ] && [ "$P" -ge "$PORT_BASE" ]'
ok "master forwards to the phone adb server" 'grep -q "SSH-MASTER ph 127.0.0.1:$P:127.0.0.1:5037" "$LOG"'
ok "phone server prepared (start-server + self-loop connect)" 'grep -q "SSH-CMD ph adb start-server" "$LOG" && grep -q "adb connect 127.0.0.1:5555" "$LOG"'
ok "local adb aimed at tunnel with default serial" 'grep -qx "ADB -H 127.0.0.1 -P $P -s 127.0.0.1:5555 shell getprop ro.product.model" "$LOG"'
ok "state dir is private" '[ "$(stat -c %a "$TMP/state" 2>/dev/null || stat -f %Lp "$TMP/state")" = 700 ]'
run PHONE_ADB_MODE=tunnel -- ph --serial 10.0.0.9:5555 shell id
okc "$RC" 0 "second run rc 0"
okc "$(count SSH-MASTER)" 1 "tunnel reused (one master)"
ok "custom serial passed" 'grep -qx "ADB -H 127.0.0.1 -P $P -s 10.0.0.9:5555 shell id" "$LOG"'
run PHONE_ADB_MODE=tunnel -- ph disconnect 10.0.0.9:5555
okc "$RC" 0 "targeted disconnect allowed"

# ── status / close / stale state ─────────────────────────────────────────────
run PHONE_ADB_MODE=tunnel -- ph --status
ok "status up" '[ "$RC" = 0 ] && grep -q "tunnel=up port=$P" <<<"$OUT"'
run PHONE_ADB_MODE=tunnel -- ph --close
ok "close exits master and clears state" '[ "$RC" = 0 ] && grep -q "SSH-EXIT ph" "$LOG" && [ ! -e "$TMP/state/ph.ctl" ] && [ ! -e "$TMP/state/ph.port" ]'
run PHONE_ADB_MODE=tunnel -- ph --status
ok "status down after close" 'grep -q "tunnel=down" <<<"$OUT"'
reset; mkdir -p "$TMP/state"; echo 1 > "$TMP/state/ph.port"   # port file, no master
run PHONE_ADB_MODE=tunnel -- ph shell id
ok "stale port file → reopened" '[ "$RC" = 0 ] && [ "$(count SSH-MASTER)" = 1 ] && [ "$(cat "$TMP/state/ph.port")" != 1 ]'

# ── version guard ────────────────────────────────────────────────────────────
reset
run PHONE_ADB_MODE=tunnel FAKE_REMOTE_VER=1.0.40 -- ph shell id
okc "$RC" 5 "version mismatch rc 5"
okc "$(count SSH-MASTER)" 0 "no tunnel on mismatch"
ok "local adb not run on mismatch" '! grep -q "^ADB " "$LOG"'

# ── bounded ssh failures ─────────────────────────────────────────────────────
reset
run PHONE_ADB_MODE=tunnel FAKE_MASTER_RC=255 -- ph shell id
okc "$RC" 3 "master failure rc 3"
okc "$(count SSH-MASTER)" 3 "stops after 3 ssh failures"

# ── ssh mode: quoting round-trip, no injection ───────────────────────────────
reset
run PHONE_ADB_MODE=ssh -- ph shell 'echo a  b' '$(touch '"$TMP"'/pwned)' "it's"
okc "$RC" 0 "ssh mode rc 0"
ok "args arrive intact" 'grep -qx "ARG<-s>" <<<"$OUT" && grep -qx "ARG<127.0.0.1:5555>" <<<"$OUT" && grep -qx "ARG<echo a  b>" <<<"$OUT" && grep -qF "ARG<\$(touch $TMP/pwned)>" <<<"$OUT" && grep -qx "ARG<it'"'"'s>" <<<"$OUT"'
ok "no remote command injection" '[ ! -e "$TMP/pwned" ]'
ok "ssh mode opens no tunnel" '[ "$(count SSH-MASTER)" = 0 ] && ! grep -q "^ADB " "$LOG"'
run PHONE_ADB_MODE=ssh -- ph push /etc/hostname /data/local/tmp/x
okc "$RC" 4 "push refused in ssh mode"
run PHONE_ADB_MODE=ssh -- ph install app.apk
okc "$RC" 4 "install refused in ssh mode"
run PHONE_ADB_MODE=ssh -- ph --status
ok "ssh-mode status lists devices remotely" '[ "$RC" = 0 ] && grep -q "mode=ssh" <<<"$OUT" && grep -q "SSH-CMD ph adb devices" "$LOG"'

# ── auto mode picks ssh when no local adb ────────────────────────────────────
reset
run PHONE_ADB_ADB="$TMP/no-such-adb" -- ph shell id
ok "auto → ssh mode without local adb" '[ "$RC" = 0 ] && [ "$(count SSH-MASTER)" = 0 ] && grep -q "SSH-CMD ph adb -s 127.0.0.1:5555 shell id" "$LOG"'

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
