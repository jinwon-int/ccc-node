#!/usr/bin/env bash
# Hermetic tests for scripts/install-spans-view-timer.sh (#2858 item 4):
# dry-run by default, the unit contents, input gates, apply and uninstall
# against a temp unit folder with a stub systemctl.
# shellcheck disable=SC2034  # test variables are read inside the eval'd ok() conditions
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
I="$DIR/install-spans-view-timer.sh"
ROOT="$(cd "$DIR/.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

UNITS="$TMP/units"; LOG="$TMP/systemctl.log"; OUT="$TMP/uploads/spans"
mkdir -p "$TMP/uploads" "$TMP/bin" "$TMP/home/.claude/state"
write_exec_stub "$TMP/bin/systemctl" <<'EOF'
printf '%s\n' "$*" >> "$LOG"
EOF
export LOG
inst() { HOME="$TMP/home" CCC_STATE_DIR="$TMP/home/.claude/state" CCC_SYSTEMD_DIR="$UNITS" \
           CCC_SYSTEMCTL="$TMP/bin/systemctl" CCC_FLEET_NODES='' CCC_FLEET_SELF='' bash "$I" "$@"; }

# 1. dry-run is the default and changes nothing
out="$(inst --out-dir "$OUT" --nodes "alpha beta" --self alpha)"; rc=$?
ok "dry-run exits 0, prints both units and writes nothing" \
  '[ "$rc" = 0 ] && grep -q "^# dry-run: would write $UNITS/ccc-spans-view.service" <<<"$out" && grep -q "^# dry-run: would write $UNITS/ccc-spans-view.timer" <<<"$out" && [ ! -e "$UNITS" ] && [ ! -e "$LOG" ] && [ ! -e "$OUT" ]'
ok "timer fires at 04:30 Asia/Seoul and catches up after downtime" \
  'grep -qx "OnCalendar=\*-\*-\* 04:30:00 Asia/Seoul" <<<"$out" && grep -qx "Persistent=true" <<<"$out"'
ok "service runs the collector from this checkout with the out-dir" \
  'grep -qx "ExecStart=/usr/bin/env bash $ROOT/scripts/spans-view-collect.sh --out-dir $OUT" <<<"$out"'
ok "service is a capped oneshot" 'grep -qx "Type=oneshot" <<<"$out" && grep -qx "TimeoutStartSec=900" <<<"$out"'
ok "service may write only the state folder and the out-dir's parent" \
  'grep -qx "ProtectSystem=strict" <<<"$out" && grep -qx "ReadWritePaths=$TMP/home/.claude/state $TMP/uploads" <<<"$out" && grep -qx "PrivateTmp=true" <<<"$out" && grep -qx "NoNewPrivileges=true" <<<"$out"'
ok "node list and self name are passed as environment" \
  'grep -qx "Environment=\"CCC_FLEET_NODES=alpha beta\"" <<<"$out" && grep -qx "Environment=CCC_FLEET_SELF=alpha" <<<"$out"'
ok "state folder and HOME are pinned" \
  'grep -qx "Environment=CCC_STATE_DIR=$TMP/home/.claude/state" <<<"$out" && grep -qx "Environment=HOME=$TMP/home" <<<"$out"'
out="$(inst --out-dir "$OUT" --nodes "alpha")"
ok "without --self no self line is written" '! grep -q "CCC_FLEET_SELF" <<<"$out"'
inst --out-dir "$OUT" >/dev/null 2>&1; rc=$?
ok "--nodes is required: the collector has no built-in node list" '[ "$rc" = 2 ]'

# 2. input gates: every value lands in a unit file
bad=0
for d in "relative/spans" "/tmp/with space" "/tmp/a;b" '/tmp/$(id)' "/tmp/../etc" "/tmp/a%b" ""; do
  inst --out-dir "$d" --nodes alpha >/dev/null 2>&1; [ $? = 2 ] || { bad=$((bad+1)); echo "  accepted out-dir: $d"; }
done
ok "an out-dir that is not a plain absolute path is refused (exit 2)" '[ "$bad" = 0 ]'
inst --out-dir "$OUT" --nodes $'alpha\tbeta' >/dev/null 2>&1; rc0=$?
inst --out-dir "$OUT" --nodes "alpha be/ta" >/dev/null 2>&1; rc1=$?
inst --out-dir "$OUT" --nodes alpha --self 'a b' >/dev/null 2>&1; rc2=$?
inst --out-dir "$OUT" --nodes alpha --on-calendar $'04:30\nExecStartPre=/bin/x' >/dev/null 2>&1; rc3=$?
inst --out-dir "$OUT" --nodes alpha --service-name "../x" >/dev/null 2>&1; rc4=$?
inst --out-dir "$OUT" --nodes alpha --state-dir "/tmp/a b" >/dev/null 2>&1; rc5=$?
(cd "$DIR" && inst --out-dir "$OUT" --nodes "alpha *") >/dev/null 2>&1; rc8=$?
inst --bogus >/dev/null 2>&1; rc6=$?
inst >/dev/null 2>&1; rc7=$?
ok "bad node, calendar, service name, state dir, unknown flag and missing --out-dir are refused" \
  '[ "$rc0$rc1$rc2$rc3$rc4$rc5$rc6$rc7$rc8" = 222222222 ]'
ok "no refused run touched systemd" '[ ! -e "$UNITS" ] && [ ! -e "$LOG" ]'

# 3. --apply
inst --apply --out-dir "$TMP/no-such-parent/spans" --nodes alpha >/dev/null 2>&1; rc=$?
ok "apply refuses an out-dir whose parent is missing and installs nothing" '[ "$rc" = 3 ] && [ ! -e "$UNITS" ] && [ ! -e "$LOG" ]'
inst --apply --out-dir "$OUT" --nodes alpha --state-dir "$TMP/no-state" >/dev/null 2>&1; rc=$?
ok "apply refuses a state folder that does not exist (the unit could not start)" '[ "$rc" = 3 ] && [ ! -e "$UNITS" ] && [ ! -e "$LOG" ]'
ln -s "$TMP/home" "$TMP/uploads/link"
inst --apply --out-dir "$TMP/uploads/link" --nodes alpha >/dev/null 2>&1; rc=$?
ok "apply refuses a symlinked out-dir" '[ "$rc" = 3 ] && [ ! -e "$UNITS" ]'
out="$(inst --apply --out-dir "$OUT" --nodes "alpha beta")"; rc=$?
ok "apply writes both units 0644 and enables the timer" \
  '[ "$rc" = 0 ] && [ "$(stat -c %a "$UNITS/ccc-spans-view.service")" = 644 ] && [ "$(stat -c %a "$UNITS/ccc-spans-view.timer")" = 644 ] && [ "$(cat "$LOG")" = "daemon-reload
enable --now ccc-spans-view.timer" ]'
ok "apply does not create the out-dir or run the collector" '[ ! -e "$OUT" ]'
ok "installed service content matches the dry-run" \
  'diff <(inst --out-dir "$OUT" --nodes "alpha beta" | awk "/^# dry-run: would write .*service/{on=1;next} /^# dry-run/{on=0} on") "$UNITS/ccc-spans-view.service" >/dev/null'
before="$(cat "$UNITS/ccc-spans-view.service")"
inst --apply --out-dir "$OUT" --nodes "alpha beta" >/dev/null
ok "apply is idempotent" '[ "$(cat "$UNITS/ccc-spans-view.service")" = "$before" ]'

# 4. --uninstall
: > "$LOG"
out="$(inst --uninstall)"; rc=$?
ok "uninstall dry-run changes nothing" '[ "$rc" = 0 ] && [ -e "$UNITS/ccc-spans-view.timer" ] && [ ! -s "$LOG" ] && grep -q "would remove" <<<"$out"'
mkdir -p "$OUT"; echo page > "$OUT/index.html"
out="$(inst --uninstall --apply)"; rc=$?
ok "uninstall --apply disables the timer, removes both units and keeps the pages" \
  '[ "$rc" = 0 ] && [ ! -e "$UNITS/ccc-spans-view.service" ] && [ ! -e "$UNITS/ccc-spans-view.timer" ] && [ -s "$OUT/index.html" ] && [ "$(cat "$LOG")" = "disable --now ccc-spans-view.timer
daemon-reload" ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
