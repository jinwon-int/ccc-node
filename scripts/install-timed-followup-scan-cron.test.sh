#!/usr/bin/env bash
# Hermetic tests for install-timed-followup-scan-cron.sh (#2324).
# Uses a stub crontab (CCC_CRONTAB_CMD) backed by a temp file, so no real
# crontab is touched and the suite is platform-independent (Linux + Termux).
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALLER="$ROOT/scripts/install-timed-followup-scan-cron.sh"
DEADLINE_INSTALLER="$ROOT/scripts/install-timed-test-deadline-scan-cron.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/ccc-followup-cron-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

export FAKE_CRON="$TMP/crontab.txt"
: > "$FAKE_CRON"

# Stub crontab: `-l` prints the file, `-` overwrites it from stdin.
# Shebang must resolve to a real path: this sandbox has no /usr/bin/env.
STUB="$TMP/crontab-stub.sh"
BASH_BIN="$(command -v bash)"
cat > "$STUB" <<STUBEOF
#!$BASH_BIN
f="\${FAKE_CRON:?}"
case "\${1:-}" in
  -l) [ -s "\$f" ] && cat "\$f" || exit 1 ;;
  -)  cat > "\$f" ;;
  *)  exit 2 ;;
esac
STUBEOF
chmod +x "$STUB"
export CCC_CRONTAB_CMD="$STUB"
export CCC_CLAUDE_DIR="$TMP/claude"
export CCC_STATE_DIR="$CCC_CLAUDE_DIR/state"
mkdir -p "$CCC_CLAUDE_DIR/state"
# Operator env must not leak into the defaults under test.
unset CCC_TIMED_FOLLOWUP_SCAN_CMD CCC_TIMED_FOLLOWUP_SCAN_REPOS CCC_TIMED_FOLLOWUP_SCAN_CLOSED_DAYS \
  CCC_TIMED_FOLLOWUP_SCAN_NOTIFY CCC_TIMED_FOLLOWUP_SCAN_CRON CCC_TIMED_FOLLOWUP_SCAN_CRON_LOG \
  CCC_TIMED_TEST_SCAN_MODE CCC_TIMED_TEST_SCAN_NOTIFY CCC_TIMED_TEST_SCAN_CRON

pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
# Entry lines are the only stamped lines; BEGIN/END block markers carry the
# lane marker as a substring and must not be counted as entries.
marker_count() { grep -E "# ccc-node:timed-followup-scan gen=h_" "$FAKE_CRON" 2>/dev/null | wc -l | tr -d ' '; }
block_count() { grep -cF "# ccc-node:timed-followup-scan:begin" "$FAKE_CRON" 2>/dev/null | head -1; }
deadline_count() { grep -E "# ccc-node:timed-test-deadline-scan gen=h_" "$FAKE_CRON" 2>/dev/null | wc -l | tr -d ' '; }

# dry-run does not mutate the crontab
out="$(bash "$INSTALLER" --dry-run 2>&1)"; rc=$?
ok "dry-run exits 0" '[ "$rc" = 0 ]'
ok "dry-run announces install" 'printf "%s" "$out" | grep -q "would install timed-followup-scan"'
ok "dry-run does not write crontab" '[ "$(marker_count)" = 0 ] && [ ! -s "$FAKE_CRON" ]'
ok "dry-run writes no install record" '[ ! -e "$CCC_STATE_DIR/install-timed-followup-scan-cron.json" ]'
out="$(bash "$INSTALLER" 2>&1)"; rc=$?
ok "no flag at all is a dry-run" '[ "$rc" = 0 ] && printf "%s" "$out" | grep -q "\[dry-run\]" && [ ! -s "$FAKE_CRON" ]'

# apply installs exactly one entry line inside one managed block (#1077)
out="$(bash "$INSTALLER" --apply 2>&1)"; rc=$?
ok "apply exits 0" '[ "$rc" = 0 ]'
ok "apply installs one marker line" '[ "$(marker_count)" = 1 ]'
ok "apply wraps the entry in a managed block" '[ "$(block_count)" = 1 ] && grep -qF "# ccc-node:timed-followup-scan:end" "$FAKE_CRON"'
ok "installed line carries the once-a-day daytime default schedule" 'grep -qF "35 9 * * *" "$FAKE_CRON"'
ok "installed line loads login PATH via bash -lc" 'grep -qF "bash -lc" "$FAKE_CRON"'
ok "installed line invokes the scanner" 'grep -qF "timed_test_deadline_scan.py" "$FAKE_CRON"'
ok "installed line runs followup mode" 'grep -qF -- "--mode followup" "$FAKE_CRON"'
ok "installed line includes closed issues for 45 days by default" 'grep -qF -- "--include-closed-days 45 " "$FAKE_CRON"'
ok "installed line notifies the owner at high confidence by default" 'grep -qF -- "--notify \"high\"" "$FAKE_CRON"'
ok "installed line requests nonzero exit on findings" 'grep -qF -- "--exit-nonzero-on-findings" "$FAKE_CRON"'
ok "installed line logs to its own cron log" 'grep -qF "$CCC_STATE_DIR/timed-followup-scan.cron.log" "$FAKE_CRON"'
ok "log directory exists after apply" '[ -d "$CCC_STATE_DIR" ]'

# The repo list is the deadline scan's allowlist, referenced by path (#1867).
ok "cron line points at the shared allowlist file" 'grep -qF -- "--repos-file \"$CCC_CLAUDE_DIR/timed-test-deadline-scan.repos\"" "$FAKE_CRON"'
ok "cron line carries no literal owner/repo pair" '! grep -qE "\-\-repo [A-Za-z0-9._-]+/[A-Za-z0-9._-]+" "$FAKE_CRON"'
ok "installer does not create the repo allowlist" '[ ! -e "$CCC_CLAUDE_DIR/timed-test-deadline-scan.repos" ]'

# The rendered scanner arguments must be accepted by the real scanner parser.
# shellcheck disable=SC2034  # parse_rc is read via eval inside ok()
parse_rc="$(python3 - "$ROOT" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + "/scripts")
import timed_test_deadline_scan as scan
args = scan._parse_args(["--repos-file", "/nonexistent", "--mode", "followup",
                         "--include-closed-days", "45", "--notify", "high",
                         "--exit-nonzero-on-findings"])
print(0 if (args.mode, args.include_closed_days, args.notify) == ("followup", 45, "high") else 1)
PY
)"
ok "rendered arguments parse in the scanner" '[ "$parse_rc" = 0 ]'

# generation stamp (#1081): content hash of installer + shared rendering libs
# shellcheck source=/dev/null
. "$ROOT/scripts/lib/installer-gen-stamp.sh"
# shellcheck disable=SC2034  # want_gen is read via eval inside ok()
want_gen="$(ccc_installer_gen_stamp_auto "$INSTALLER")"
ok "gen inputs include the shared cron lib" 'ccc_installer_gen_inputs "$INSTALLER" | grep -q "lib/installer-cron-common.sh$"'
ok "installed line carries gen stamp" 'grep -qE "# ccc-node:timed-followup-scan gen=h_[0-9a-f]{12}$" "$FAKE_CRON"'
ok "gen stamp matches installer content" 'grep -qF "gen=$want_gen" "$FAKE_CRON"'
ok "BEGIN/END block markers stay unstamped (exact-match parsed)" '! grep -qE "timed-followup-scan:(begin|end) gen=" "$FAKE_CRON"'

# idempotent: re-apply keeps a single line
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "re-apply stays idempotent (one line)" '[ "$(marker_count)" = 1 ] && [ "$(block_count)" = 1 ]'

# custom schedule replaces, still single line
bash "$INSTALLER" --apply --schedule "5 10 * * *" >/dev/null 2>&1
ok "custom schedule still single line" '[ "$(marker_count)" = 1 ]'
ok "custom schedule applied" 'grep -qF "5 10 * * *" "$FAKE_CRON"'
ok "old schedule removed" '! grep -qF "35 9 * * *" "$FAKE_CRON"'

# --include-closed-days: accepted values land in the line; bad ones are refused
bash "$INSTALLER" --apply --include-closed-days 0 >/dev/null 2>&1
ok "closed days 0 (open issues only) applied" 'grep -qF -- "--include-closed-days 0 " "$FAKE_CRON"'
bash "$INSTALLER" --apply --include-closed-days 090 >/dev/null 2>&1
ok "closed days leading zeros normalized" 'grep -qF -- "--include-closed-days 90 " "$FAKE_CRON"'
CCC_TIMED_FOLLOWUP_SCAN_CLOSED_DAYS=30 bash "$INSTALLER" --apply >/dev/null 2>&1
ok "closed days env override applied" 'grep -qF -- "--include-closed-days 30 " "$FAKE_CRON"'
for bad in -1 abc 4.5 "1 2" "+3"; do
  # shellcheck disable=SC2034  # before/out/rc are read via eval inside ok()
  before="$(cat "$FAKE_CRON")"
  # shellcheck disable=SC2034
  out="$(bash "$INSTALLER" --apply --include-closed-days "$bad" 2>&1)"; rc=$?
  ok "closed days '$bad' exits 2" '[ "$rc" = 2 ]'
  ok "closed days '$bad' is named in the error" 'printf "%s" "$out" | grep -q "invalid --include-closed-days"'
  ok "closed days '$bad' leaves the crontab untouched" '[ "$(cat "$FAKE_CRON")" = "$before" ]'
done
# shellcheck disable=SC2034
out="$(bash "$INSTALLER" --apply --include-closed-days 2>&1)"; rc=$?
ok "closed days without a value exits 2" '[ "$rc" = 2 ]'

# --notify is selectable (off restores log-only) and lands in the rendered line
bash "$INSTALLER" --apply --notify off >/dev/null 2>&1
ok "notify off applied" 'grep -qF -- "--notify \"off\"" "$FAKE_CRON"'
ok "notify off still single line" '[ "$(marker_count)" = 1 ]'
CCC_TIMED_FOLLOWUP_SCAN_NOTIFY=low bash "$INSTALLER" --apply >/dev/null 2>&1
ok "notify level env override applied" 'grep -qF -- "--notify \"low\"" "$FAKE_CRON"'
# shellcheck disable=SC2034
before="$(cat "$FAKE_CRON")"
# shellcheck disable=SC2034
out="$(bash "$INSTALLER" --apply --notify loud 2>&1)"; rc=$?
ok "unknown notify exits 2" '[ "$rc" = 2 ]'
ok "unknown notify is named in the error" 'printf "%s" "$out" | grep -q "unknown --notify"'
ok "unknown notify leaves the crontab untouched" '[ "$(cat "$FAKE_CRON")" = "$before" ]'
# shellcheck disable=SC2034
out="$(bash "$INSTALLER" --apply --mode expired 2>&1)"; rc=$?
ok "--mode is not an option here (followup is fixed)" '[ "$rc" = 2 ] && printf "%s" "$out" | grep -q "unknown arg: --mode"'

# install record (#1081 phase 2): every option materialized for self-update replay
bash "$INSTALLER" --apply --schedule "5 10 * * *" --include-closed-days 60 >/dev/null 2>&1
# shellcheck disable=SC2034  # REC is read via eval inside ok()
REC="$CCC_STATE_DIR/install-timed-followup-scan-cron.json"
ok "apply writes its own install record" '[ -f "$REC" ]'
ok "record carries schema/marker/gen" 'jq -e ".schema==\"ccc.install-record.v1\" and .marker==\"# ccc-node:timed-followup-scan\" and .gen==\"$want_gen\"" "$REC" >/dev/null'
ok "record argv materializes schedule, closed days and notify" 'jq -e ".argv == [\"--apply\",\"--schedule\",\"5 10 * * *\",\"--include-closed-days\",\"60\",\"--notify\",\"high\"]" "$REC" >/dev/null'
ok "record is owner-only" '[ "$(stat -c %a "$REC")" = 600 ]'
# Replaying the recorded argv (what self-update step 5 does) renders the same line.
# shellcheck disable=SC2034
line_before="$(grep -F "# ccc-node:timed-followup-scan gen=" "$FAKE_CRON")"
mapfile -t replay < <(jq -r '.argv[]' "$REC")
CCC_TIMED_FOLLOWUP_SCAN_CLOSED_DAYS=7 CCC_TIMED_FOLLOWUP_SCAN_NOTIFY=off bash "$INSTALLER" "${replay[@]}" >/dev/null 2>&1
ok "record replay renders the identical line despite different env" '[ "$(grep -F "# ccc-node:timed-followup-scan gen=" "$FAKE_CRON")" = "$line_before" ]'

# --- coexistence with the deadline-scan installer (#2324) --------------------
# The two lanes share a scanner and an allowlist but must never touch each
# other's managed block: markers are matched as substrings by the shared block
# parser, so neither marker may be a prefix of the other.
: > "$FAKE_CRON"
rm -f "$CCC_STATE_DIR"/install-timed-*.json
bash "$DEADLINE_INSTALLER" --apply >/dev/null 2>&1
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "both lanes installed side by side" '[ "$(deadline_count)" = 1 ] && [ "$(marker_count)" = 1 ]'
ok "deadline entry still runs expired mode" 'grep -F "# ccc-node:timed-test-deadline-scan gen=" "$FAKE_CRON" | grep -qF -- "--mode \"expired\""'
ok "each installer keeps its own install record" '[ -f "$CCC_STATE_DIR/install-timed-test-deadline-scan-cron.json" ] && [ -f "$CCC_STATE_DIR/install-timed-followup-scan-cron.json" ]'
# shellcheck disable=SC2034  # read via eval inside ok()
M_FOLLOW="# ccc-node:timed-followup-scan"
# shellcheck disable=SC2034  # read via eval inside ok()
M_DEAD="# ccc-node:timed-test-deadline-scan"
ok "neither marker is a substring of the other" '[ "${M_DEAD#*"$M_FOLLOW"}" = "$M_DEAD" ] && [ "${M_FOLLOW#*"$M_DEAD"}" = "$M_FOLLOW" ]'
ok "markers match what the installers render" 'grep -qxF "MARKER=\"$M_FOLLOW\"" "$INSTALLER" && grep -qxF "MARKER=\"$M_DEAD\"" "$DEADLINE_INSTALLER"'
bash "$DEADLINE_INSTALLER" --apply --schedule "20 9 * * *" >/dev/null 2>&1
ok "re-applying the deadline installer keeps the followup block" '[ "$(marker_count)" = 1 ] && [ "$(block_count)" = 1 ] && [ "$(deadline_count)" = 1 ]'
bash "$INSTALLER" --apply --schedule "40 9 * * *" >/dev/null 2>&1
ok "re-applying the followup installer keeps the deadline block" '[ "$(deadline_count)" = 1 ] && grep -qF "20 9 * * *" "$FAKE_CRON"'
bash "$INSTALLER" --apply --remove >/dev/null 2>&1
ok "removing followup leaves the deadline block intact" '[ "$(marker_count)" = 0 ] && [ "$(deadline_count)" = 1 ] && grep -qF "# ccc-node:timed-test-deadline-scan:begin" "$FAKE_CRON" && grep -qF "# ccc-node:timed-test-deadline-scan:end" "$FAKE_CRON"'
ok "removing followup leaves the deadline record" '[ -f "$CCC_STATE_DIR/install-timed-test-deadline-scan-cron.json" ] && [ ! -f "$CCC_STATE_DIR/install-timed-followup-scan-cron.json" ]'
bash "$INSTALLER" --apply >/dev/null 2>&1
bash "$DEADLINE_INSTALLER" --apply --remove >/dev/null 2>&1
ok "removing deadline leaves the followup block intact" '[ "$(deadline_count)" = 0 ] && [ "$(marker_count)" = 1 ] && [ "$(block_count)" = 1 ] && grep -qF "# ccc-node:timed-followup-scan:end" "$FAKE_CRON"'

# a pre-existing unrelated cron line is preserved; remove keeps it
printf '0 4 * * * echo keepme\n' >> "$FAKE_CRON"
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "unrelated cron line preserved" 'grep -qF "echo keepme" "$FAKE_CRON"'
bash "$INSTALLER" --apply --remove >/dev/null 2>&1
ok "remove deletes marker line" '[ "$(marker_count)" = 0 ]'
ok "remove deletes the block markers too" '! grep -qF "# ccc-node:timed-followup-scan:begin" "$FAKE_CRON"'
ok "remove keeps unrelated line" 'grep -qF "echo keepme" "$FAKE_CRON"'
ok "remove drops the install record (no resurrection via re-apply)" '[ ! -f "$CCC_STATE_DIR/install-timed-followup-scan-cron.json" ]'

# legacy migration (#1077): a bare stamped line is folded into a block
printf '%s\n' '35 9 * * * bash -lc '"'"'old'"'"'  # ccc-node:timed-followup-scan gen=h_000000000000' '0 4 * * * echo keepme2' > "$FAKE_CRON"
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "legacy bare marker line is migrated into a block" '[ "$(marker_count)" = 1 ] && [ "$(block_count)" = 1 ]'
ok "legacy line content replaced (old gen gone)" '! grep -qF "gen=h_000000000000" "$FAKE_CRON"'
ok "migration preserves unrelated lines" 'grep -qF "echo keepme2" "$FAKE_CRON"'

# corrupt managed block fails closed (#1077)
printf '%s\n' '# ccc-node:timed-followup-scan:begin' '35 9 * * * dangling  # ccc-node:timed-followup-scan gen=h_000000000000' > "$FAKE_CRON"
# shellcheck disable=SC2034
out="$(bash "$INSTALLER" --apply 2>&1)"
# shellcheck disable=SC2034
rc=$?
ok "corrupt managed block exits 4" '[ "$rc" = 4 ]'
ok "corrupt managed block is reported" 'printf "%s" "$out" | grep -q "corrupt managed schedule block"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
