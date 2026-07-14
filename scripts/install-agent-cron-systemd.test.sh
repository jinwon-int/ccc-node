#!/usr/bin/env bash
# Tests for install-agent-cron-systemd.sh — hermetic: dry-run golden content,
# --apply into a temp CCC_SYSTEMD_DIR with a stubbed systemctl, idempotency,
# --user dir resolution, and validation. No real systemd contact. (#457)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SC="$HERE/install-agent-cron-systemd.sh"
pass=0; fail=0
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = "$2" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $3 (rc=$1 want=$2)"; fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Stub systemctl: record every invocation, never touch real systemd.
CALLS="$TMP/systemctl.calls"
STUB="$TMP/systemctl"
cat > "$STUB" <<STUBEOF
#!/usr/bin/env bash
echo "\$*" >> "$CALLS"
exit 0
STUBEOF
chmod +x "$STUB"

OUT="$TMP/out"; RC=0
run() { RC=0; "$@" >"$OUT" 2>&1 || RC=$?; }

# ---- dry-run (default): golden content, no files written -------------------
run env CCC_SYSTEMD_DIR="$TMP/sd" CCC_SYSTEMCTL="$STUB" bash "$SC"
okc "$RC" 0 "dry-run exits 0"
ok "dry-run announces service write" 'grep -q "would write .*ccc-agent-cron.service" "$OUT"'
ok "dry-run announces timer write"   'grep -q "would write .*ccc-agent-cron.timer" "$OUT"'
ok "dry-run emits oneshot service"   'grep -q "Type=oneshot" "$OUT"'
ok "dry-run ExecStart is scheduler tick" 'grep -q "agent-cron.sh scheduler --execute --json" "$OUT"'
ok "dry-run emits OnCalendar timer"  'grep -q "OnCalendar=" "$OUT"'
ok "dry-run writes NO files" '[ ! -e "$TMP/sd" ]'
ok "dry-run calls NO systemctl" '[ ! -f "$CALLS" ]'

# ---- --apply: writes units into temp dir, drives stubbed systemctl ---------
run env CCC_SYSTEMD_DIR="$TMP/sd" CCC_SYSTEMCTL="$STUB" bash "$SC" --apply
okc "$RC" 0 "apply exits 0"
ok "apply wrote service file" '[ -f "$TMP/sd/ccc-agent-cron.service" ]'
ok "apply wrote timer file"   '[ -f "$TMP/sd/ccc-agent-cron.timer" ]'
ok "service file has ExecStart" 'grep -q "agent-cron.sh scheduler --execute --json" "$TMP/sd/ccc-agent-cron.service"'
ok "timer file has OnCalendar"  'grep -q "OnCalendar=" "$TMP/sd/ccc-agent-cron.timer"'
ok "units are mode 0644" '[ "$(stat -c %a "$TMP/sd/ccc-agent-cron.service")" = "644" ]'
ok "apply ran daemon-reload" 'grep -q "daemon-reload" "$CALLS"'
ok "apply enabled the timer"  'grep -q "enable --now ccc-agent-cron.timer" "$CALLS"'
ok "apply restarted the timer" 'grep -q "restart ccc-agent-cron.timer" "$CALLS"'

# ---- idempotency: re-apply overwrites cleanly, same content ----------------
# shellcheck disable=SC2034  # $before is consumed via eval in ok()
before="$(cat "$TMP/sd/ccc-agent-cron.service")"
run env CCC_SYSTEMD_DIR="$TMP/sd" CCC_SYSTEMCTL="$STUB" bash "$SC" --apply
okc "$RC" 0 "re-apply exits 0"
ok "re-apply keeps identical service content" '[ "$before" = "$(cat "$TMP/sd/ccc-agent-cron.service")" ]'

# ---- --no-enable --no-restart: unit written, no enable/restart -------------
rm -f "$CALLS"
run env CCC_SYSTEMD_DIR="$TMP/sd2" CCC_SYSTEMCTL="$STUB" bash "$SC" --apply --no-enable --no-restart
okc "$RC" 0 "no-enable/no-restart exits 0"
ok "still daemon-reload" 'grep -q "daemon-reload" "$CALLS"'
ok "no enable call" '! grep -q "enable" "$CALLS"'
ok "no restart call" '! grep -q "restart" "$CALLS"'

# ---- --user: SYSTEMD_DIR resolves under HOME, systemctl gets --user --------
rm -f "$CALLS"
run env HOME="$TMP/home" CCC_SYSTEMCTL="$STUB" bash "$SC" --apply --user
okc "$RC" 0 "user apply exits 0"
ok "user unit under HOME/.config/systemd/user" '[ -f "$TMP/home/.config/systemd/user/ccc-agent-cron.service" ]'
ok "user mode passes --user to systemctl" 'grep -q -- "--user .*daemon-reload" "$CALLS"'

# ---- validation: bad service name ------------------------------------------
run env CCC_SYSTEMD_DIR="$TMP/sd3" CCC_SYSTEMCTL="$STUB" bash "$SC" --apply --service-name 'bad name!'
okc "$RC" 2 "invalid service name exits 2"

# ---- custom --on-calendar propagates ---------------------------------------
run env CCC_SYSTEMD_DIR="$TMP/sd4" CCC_SYSTEMCTL="$STUB" bash "$SC" --apply --on-calendar '*-*-* 04:00:00' --no-enable --no-restart
ok "custom OnCalendar honored" 'grep -q "OnCalendar=\*-\*-\* 04:00:00" "$TMP/sd4/ccc-agent-cron.timer"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
