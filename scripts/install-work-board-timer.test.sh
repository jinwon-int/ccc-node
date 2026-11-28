#!/usr/bin/env bash
# Hermetic tests for scripts/install-work-board-timer.sh (#2914): dry-run by
# default, the unit contents, input gates, apply and uninstall against a temp
# unit folder with a stub systemctl.
# shellcheck disable=SC2034  # test variables are read inside the eval'd ok() conditions
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
I="$DIR/install-work-board-timer.sh"
ROOT="$(cd "$DIR/.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

UNITS="$TMP/units"; LOG="$TMP/systemctl.log"
mkdir -p "$TMP/bin" "$TMP/home/.claude/state" "$TMP/home/.claude/hooks/lib"
write_exec_stub "$TMP/bin/systemctl" <<'EOF'
printf '%s\n' "$*" >> "$LOG"
EOF
export LOG
inst() { HOME="$TMP/home" CCC_STATE_DIR="$TMP/home/.claude/state" CCC_SYSTEMD_DIR="$UNITS" \
           CCC_SYSTEMCTL="$TMP/bin/systemctl" CCC_WORK_BOARD_ORG='' bash "$I" "$@"; }
ST="$TMP/home/.claude/state"

# 1. dry-run is the default and changes nothing
out="$(inst --org example-org)"; rc=$?
ok "dry-run exits 0, prints both units and writes nothing" \
  '[ "$rc" = 0 ] && grep -q "^# dry-run: would write $UNITS/ccc-work-board.service" <<<"$out" && grep -q "^# dry-run: would write $UNITS/ccc-work-board.timer" <<<"$out" && [ ! -e "$UNITS" ] && [ ! -e "$LOG" ]'
ok "timer fires at 05:30 Asia/Seoul and catches up after downtime" \
  'grep -qx "OnCalendar=\*-\*-\* 05:30:00 Asia/Seoul" <<<"$out" && grep -qx "Persistent=true" <<<"$out"'
ok "service runs the board from this checkout with org, page, state and result file" \
  'grep -qx "ExecStart=/usr/bin/env python3 $ROOT/scripts/ccc-work-board.py run --org example-org --page pages/current/work-board.md --state-dir $ST --result-file $ST/ccc-work-board.result" <<<"$out"'
ok "service is a capped oneshot with HOME and PATH pinned" \
  'grep -qx "Type=oneshot" <<<"$out" && grep -qx "TimeoutStartSec=1500" <<<"$out" && grep -qx "Environment=HOME=$TMP/home" <<<"$out" && grep -q "^Environment=PATH=$TMP/home/.local/bin:" <<<"$out"'
ok "no notify line when the notifier is absent, and no blank line left behind" \
  '! grep -q "ExecStopPost" <<<"$out" && ! sed -n "/^\[Service\]/,/^PrivateTmp/p" <<<"$out" | grep -q "^$"'
: > "$TMP/home/.claude/hooks/lib/detached_jobs.py"
out="$(inst --org example-org)"
ok "with the notifier present, one line per run: info on success, notice on failure" \
  'grep -qx "ExecStopPost=-/usr/bin/env python3 $TMP/home/.claude/hooks/lib/detached_jobs.py notify --unit ccc-work-board --exit \${EXIT_STATUS} --tier info --fail-tier notice --result-file $ST/ccc-work-board.result" <<<"$out"'
out="$(inst --org example-org --page pages/team/x/board.md --on-calendar '*-*-* 06:00:00 Asia/Seoul')"
ok "page and calendar can be overridden" \
  'grep -q -- "--page pages/team/x/board.md " <<<"$out" && grep -qx "OnCalendar=\*-\*-\* 06:00:00 Asia/Seoul" <<<"$out"'

out="$(inst --org example-org --schedule-nodes alpha,beta-2,hub --self-node hub)"
ok "stage 3 schedule collection is passed through when asked" \
  'grep -q -- "--result-file $ST/ccc-work-board.result --schedule-nodes alpha,beta-2,hub --self-node hub$" <<<"$out"'
out="$(inst --org example-org)"
ok "schedule collection stays off by default" '! grep -q -- "--schedule-nodes" <<<"$out"'
inst --org example-org --schedule-nodes 'alpha beta' >/dev/null 2>&1; rs1=$?
inst --org example-org --schedule-nodes 'alpha,;x' >/dev/null 2>&1; rs2=$?
inst --org example-org --schedule-nodes alpha --self-node 'h b' >/dev/null 2>&1; rs3=$?
inst --org example-org --self-node hub >/dev/null 2>&1; rs4=$?
ok "bad schedule node lists and a lone --self-node are refused" '[ "$rs1$rs2$rs3$rs4" = 2222 ]'

# 2. input gates: every value lands in a unit file
inst >/dev/null 2>&1; rc0=$?
inst --org 'bad org' >/dev/null 2>&1; rc1=$?
inst --org example-org --page ../x.md >/dev/null 2>&1; rc2=$?
inst --org example-org --page pages/x.txt >/dev/null 2>&1; rc3=$?
inst --org example-org --on-calendar $'05:30\nExecStartPre=/bin/x' >/dev/null 2>&1; rc4=$?
inst --org example-org --service-name "../x" >/dev/null 2>&1; rc5=$?
inst --org example-org --state-dir "/tmp/a b" >/dev/null 2>&1; rc6=$?
inst --bogus >/dev/null 2>&1; rc7=$?
inst --org 'a;b' >/dev/null 2>&1; rc8=$?
ok "missing org, bad org, page, calendar, service name, state dir and unknown flag are refused" \
  '[ "$rc0$rc1$rc2$rc3$rc4$rc5$rc6$rc7$rc8" = 222222222 ]'
ok "no refused run touched systemd" '[ ! -e "$UNITS" ] && [ ! -e "$LOG" ]'

# 3. --apply
inst --apply --org example-org --state-dir "$TMP/no-state" >/dev/null 2>&1; rc=$?
ok "apply refuses a state folder that does not exist" '[ "$rc" = 3 ] && [ ! -e "$UNITS" ] && [ ! -e "$LOG" ]'
out="$(inst --apply --org example-org)"; rc=$?
ok "apply writes both units 0644 and enables the timer" \
  '[ "$rc" = 0 ] && [ "$(stat -c %a "$UNITS/ccc-work-board.service")" = 644 ] && [ "$(stat -c %a "$UNITS/ccc-work-board.timer")" = 644 ] && [ "$(cat "$LOG")" = "daemon-reload
enable --now ccc-work-board.timer" ]'
ok "installed service content matches the dry-run" \
  'diff <(inst --org example-org | awk "/^# dry-run: would write .*service/{on=1;next} /^# dry-run/{on=0} on") "$UNITS/ccc-work-board.service" >/dev/null'
before="$(cat "$UNITS/ccc-work-board.service")"
inst --apply --org example-org >/dev/null
ok "apply is idempotent" '[ "$(cat "$UNITS/ccc-work-board.service")" = "$before" ]'

# 4. --uninstall
: > "$LOG"
out="$(inst --uninstall)"; rc=$?
ok "uninstall dry-run changes nothing" '[ "$rc" = 0 ] && [ -e "$UNITS/ccc-work-board.timer" ] && [ ! -s "$LOG" ] && grep -q "would remove" <<<"$out"'
out="$(inst --uninstall --apply)"; rc=$?
ok "uninstall --apply disables the timer and removes both units" \
  '[ "$rc" = 0 ] && [ ! -e "$UNITS/ccc-work-board.service" ] && [ ! -e "$UNITS/ccc-work-board.timer" ] && [ "$(cat "$LOG")" = "disable --now ccc-work-board.timer
daemon-reload" ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
