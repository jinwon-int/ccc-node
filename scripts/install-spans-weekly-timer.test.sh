#!/usr/bin/env bash
# Hermetic tests for scripts/install-spans-weekly-timer.sh (#2899): dry-run by
# default, the unit contents, input gates, apply and uninstall against a temp
# unit folder with a stub systemctl. Nothing here runs the weekly summary.
# shellcheck disable=SC2034  # test variables are read inside the eval'd ok() conditions
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
I="$DIR/install-spans-weekly-timer.sh"
ROOT="$(cd "$DIR/.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

UNITS="$TMP/units"; LOG="$TMP/systemctl.log"; ST="$TMP/home/.claude/state"
mkdir -p "$TMP/bin" "$ST" "$TMP/home/.claude/hooks/lib"
write_exec_stub "$TMP/bin/systemctl" <<'EOF'
printf '%s\n' "$*" >> "$LOG"
EOF
export LOG
inst() { HOME="$TMP/home" CCC_STATE_DIR="$ST" CCC_SYSTEMD_DIR="$UNITS" CCC_SYSTEMCTL="$TMP/bin/systemctl" \
           CCC_FLEET_NODES='' CCC_SPANS_WEEKLY_PAGE='' bash "$I" "$@"; }

# 1. dry-run is the default and changes nothing
out="$(inst --nodes "alpha beta")"; rc=$?
ok "dry-run exits 0, prints both units and writes nothing" \
  '[ "$rc" = 0 ] && grep -q "^# dry-run: would write $UNITS/ccc-spans-weekly.service" <<<"$out" && grep -q "^# dry-run: would write $UNITS/ccc-spans-weekly.timer" <<<"$out" && [ ! -e "$UNITS" ] && [ ! -e "$LOG" ]'
ok "timer fires Monday 05:00 Asia/Seoul (after the 04:30 view) and catches up after downtime" \
  'grep -qx "OnCalendar=Mon \*-\*-\* 05:00:00 Asia/Seoul" <<<"$out" && grep -qx "Persistent=true" <<<"$out"'
ok "service runs the weekly summary from this checkout on the services page" \
  'grep -qx "ExecStart=/usr/bin/env python3 $ROOT/scripts/spans-weekly.py --page pages/services/span-rollup.md --state-dir $ST --result-file $ST/ccc-spans-weekly.result" <<<"$out"'
ok "service never passes --dry-run and is a capped oneshot" \
  '! grep -q -- "--dry-run" <<<"$(grep ^ExecStart <<<"$out")" && grep -qx "Type=oneshot" <<<"$out" && grep -qx "TimeoutStartSec=1500" <<<"$out"'
ok "node list, state folder and HOME are passed as environment" \
  'grep -qx "Environment=\"CCC_FLEET_NODES=alpha beta\"" <<<"$out" && grep -qx "Environment=CCC_STATE_DIR=$ST" <<<"$out" && grep -qx "Environment=HOME=$TMP/home" <<<"$out"'
ok "no notify line when the notifier is absent, and no blank line left behind" \
  '! grep -q "ExecStopPost" <<<"$out" && ! grep -qx "" <<<"$(sed -n "/^\[Service\]/,/^PrivateTmp/p" <<<"$out")"'
: > "$TMP/home/.claude/hooks/lib/detached_jobs.py"
out="$(inst --nodes alpha --page pages/services/other.md)"
ok "with the notifier present a failed run is sent at once" \
  'grep -qx "ExecStopPost=-/usr/bin/env python3 $TMP/home/.claude/hooks/lib/detached_jobs.py notify --unit ccc-spans-weekly --exit \${EXIT_STATUS} --tier info --fail-tier notice --result-file $ST/ccc-spans-weekly.result" <<<"$out"'
ok "--page reaches the command line" 'grep -q -- "--page pages/services/other.md " <<<"$out"'
inst >/dev/null 2>&1; rc=$?
ok "--nodes is required: a node that never answered must still be listed" '[ "$rc" = 2 ]'

# 2. input gates: every value lands in a unit file
inst --nodes $'alpha\tbeta' >/dev/null 2>&1; rc0=$?
inst --nodes "alpha be/ta" >/dev/null 2>&1; rc1=$?
inst --nodes alpha --page "../etc/x.md" >/dev/null 2>&1; rc2=$?
inst --nodes alpha --page "pages/a b.md" >/dev/null 2>&1; rc3=$?
inst --nodes alpha --on-calendar $'Mon 05:00\nExecStartPre=/bin/x' >/dev/null 2>&1; rc4=$?
inst --nodes alpha --service-name "../x" >/dev/null 2>&1; rc5=$?
inst --nodes alpha --state-dir "/tmp/a b" >/dev/null 2>&1; rc6=$?
(cd "$DIR" && inst --nodes "alpha *") >/dev/null 2>&1; rc7=$?
inst --bogus >/dev/null 2>&1; rc8=$?
ok "bad node, page, calendar, service name, state dir and unknown flag are refused (exit 2)" \
  '[ "$rc0$rc1$rc2$rc3$rc4$rc5$rc6$rc7$rc8" = 222222222 ]'
ok "no refused run touched systemd" '[ ! -e "$UNITS" ] && [ ! -e "$LOG" ]'

# 3. --apply
inst --apply --nodes alpha --state-dir "$TMP/no-state" >/dev/null 2>&1; rc=$?
ok "apply refuses a state folder that does not exist and installs nothing" '[ "$rc" = 3 ] && [ ! -e "$UNITS" ] && [ ! -e "$LOG" ]'
out="$(inst --apply --nodes "alpha beta")"; rc=$?
ok "apply writes both units 0644 and enables the timer" \
  '[ "$rc" = 0 ] && [ "$(stat -c %a "$UNITS/ccc-spans-weekly.service")" = 644 ] && [ "$(stat -c %a "$UNITS/ccc-spans-weekly.timer")" = 644 ] && [ "$(cat "$LOG")" = "daemon-reload
enable --now ccc-spans-weekly.timer" ]'
ok "installed service content matches the dry-run" \
  'diff <(inst --nodes "alpha beta" | awk "/^# dry-run: would write .*service/{on=1;next} /^# dry-run/{on=0} on") "$UNITS/ccc-spans-weekly.service" >/dev/null'
ok "apply does not run the weekly summary" '[ ! -e "$ST/ccc-spans-weekly.result" ]'

# 4. --uninstall
: > "$LOG"
out="$(inst --uninstall)"; rc=$?
ok "uninstall dry-run changes nothing" '[ "$rc" = 0 ] && [ -e "$UNITS/ccc-spans-weekly.timer" ] && [ ! -s "$LOG" ] && grep -q "would remove" <<<"$out"'
out="$(inst --uninstall --apply)"; rc=$?
ok "uninstall --apply disables the timer and removes both units" \
  '[ "$rc" = 0 ] && [ ! -e "$UNITS/ccc-spans-weekly.service" ] && [ ! -e "$UNITS/ccc-spans-weekly.timer" ] && [ "$(cat "$LOG")" = "disable --now ccc-spans-weekly.timer
daemon-reload" ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
