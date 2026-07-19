#!/usr/bin/env bash
# Tests for bridge/start.sh --restart (atomic stop→start→verify) and the
# service-systemd.sh is-managed probe it uses. Hermetic: fake HOME, fake
# CCC_SYSTEMD_DIR, stubbed systemctl, fake bot processes (plain sleepers) and
# a CCC_BRIDGE_RESTART_SPAWN fake start command — the real bridge on this
# node is never probed, signaled, or started.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
START="$HERE/start.sh"
SSD="$HERE/service-systemd.sh"
pass=0; fail=0
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = "$2" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $3 (rc=$1 want=$2)"; fi; }

TMP="$(mktemp -d)"
SPAWNED_PIDS="$TMP/spawned.pids"
: > "$SPAWNED_PIDS"
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
    rm -rf "$TMP"
}
trap cleanup EXIT

OUT="$TMP/out"; RC=0
run() { RC=0; "$@" >"$OUT" 2>&1 || RC=$?; }

# systemctl stubs: one where every call (incl. is-active) succeeds, one where
# is-active reports inactive (rc 3, like real systemctl).
SC_OK="$TMP/systemctl-ok"
printf '#!/usr/bin/env bash\nexit 0\n' > "$SC_OK"; chmod +x "$SC_OK"
SC_INACTIVE="$TMP/systemctl-inactive"
printf '#!/usr/bin/env bash\ncase " $* " in *" is-active "*) exit 3 ;; esac\nexit 0\n' > "$SC_INACTIVE"
chmod +x "$SC_INACTIVE"

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

# ---- foreground restart: replaces the PID and verifies availability ---------
new_project fg "123456:TEST-restart-fg"
OLD="$( ( sleep 300 >/dev/null 2>&1 & echo $! ) )"
echo "$OLD" >> "$SPAWNED_PIDS"
echo "$OLD" > "$BD/bot.pid"
CALLS="$TMP/fg-spawn.calls"
FAKE_FG="$TMP/fake-start-fg"
cat > "$FAKE_FG" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$CALLS"
echo \$\$ > "$BD/bot.pid"
cat > "$BD/health.json" <<HEOF
{"updated_at": "\$(date -u +%Y-%m-%dT%H:%M:%SZ)",
 "service": {"state": "available", "reason": ""},
 "telegram": {"state": "healthy"},
 "agent": {"state": "healthy", "provider": "claude"}}
HEOF
exec sleep 300
EOF
chmod +x "$FAKE_FG"
run env HOME="$HOMEDIR" CCC_SYSTEMD_DIR="$SD_EMPTY" CCC_SYSTEMCTL="$SC_OK" \
    CCC_BRIDGE_RESTART_SPAWN="$FAKE_FG" \
    CCC_BRIDGE_RESTART_STOP_TIMEOUT=5 CCC_BRIDGE_RESTART_READY_TIMEOUT=15 \
    bash "$START" --path "$PROJ" --restart
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
ok "spawn used the project path" 'grep -q -- "--path $PROJ" "$CALLS"'
ok "foreground spawn did not pass --daemon" '! grep -q -- "--daemon" "$CALLS"'
kill "$NEW" 2>/dev/null

# ---- daemon restart (-d): dispatches --daemon through the spawn seam --------
new_project dm "123456:TEST-restart-dm"
DCALLS="$TMP/dm-spawn.calls"
FAKE_DM="$TMP/fake-start-dm"
cat > "$FAKE_DM" <<EOF
#!/usr/bin/env bash
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
chmod +x "$FAKE_DM"
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
cat > "$FAKE_SLOW" <<EOF
#!/usr/bin/env bash
echo \$\$ > "$BD/bot.pid"
exec sleep 300
EOF
chmod +x "$FAKE_SLOW"
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
kill "$SNEW" 2>/dev/null

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
printf '#!/usr/bin/env bash\ntouch "%s"\nexit 0\n' "$STUCK_CALLS" > "$FAKE_STUCK"
chmod +x "$FAKE_STUCK"
run env HOME="$HOMEDIR" CCC_SYSTEMD_DIR="$SD_EMPTY" CCC_SYSTEMCTL="$SC_OK" \
    BASH_ENV="$STUBENV" CCC_TEST_UNKILLABLE_PID="$UNKILLABLE" \
    CCC_BRIDGE_RESTART_SPAWN="$FAKE_STUCK" \
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
