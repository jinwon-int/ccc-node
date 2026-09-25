#!/usr/bin/env bash
# Hermetic tests for install-timed-test-deadline-scan-cron.sh.
# Uses a stub crontab (CCC_CRONTAB_CMD) backed by a temp file, so no real
# crontab is touched and the suite is platform-independent (Linux + Termux).
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALLER="$ROOT/scripts/install-timed-test-deadline-scan-cron.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/ccc-deadline-cron-test.XXXXXX")"
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

pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
# Entry lines are the only stamped lines; the #1077 BEGIN/END block markers
# carry the lane marker as a substring and must not be counted as entries.
marker_count() { grep -E "# ccc-node:timed-test-deadline-scan gen=h_" "$FAKE_CRON" 2>/dev/null | wc -l | tr -d ' '; }
block_count() { grep -cF "# ccc-node:timed-test-deadline-scan:begin" "$FAKE_CRON" 2>/dev/null | head -1; }

# dry-run does not mutate the crontab
out="$(bash "$INSTALLER" --dry-run 2>&1)"; rc=$?
ok "dry-run exits 0" '[ "$rc" = 0 ]'
ok "dry-run announces install" 'printf "%s" "$out" | grep -q "would install"'
ok "dry-run does not write crontab" '[ "$(marker_count)" = 0 ]'

# apply installs exactly one entry line inside one managed block (#1077)
out="$(bash "$INSTALLER" --apply 2>&1)"; rc=$?
ok "apply exits 0" '[ "$rc" = 0 ]'
ok "apply installs one marker line" '[ "$(marker_count)" = 1 ]'
ok "apply wraps the entry in a managed block" '[ "$(block_count)" = 1 ] && grep -qF "# ccc-node:timed-test-deadline-scan:end" "$FAKE_CRON"'
ok "installed line carries default schedule" 'grep -qF "20 9 * * *" "$FAKE_CRON"'
ok "installed line loads login PATH via bash -lc" 'grep -qF "bash -lc" "$FAKE_CRON"'
ok "installed line invokes the scanner" 'grep -qF "timed_test_deadline_scan.py" "$FAKE_CRON"'

# The repo list is referenced, never baked in as a repo enumeration (#1867):
# an installer re-run that quietly drops baked state is the failure this lane
# is built to avoid, so the crontab line must carry a path, not a repo list.
ok "cron line points at the allowlist file" 'grep -qF "timed-test-deadline-scan.repos" "$FAKE_CRON"'
ok "cron line carries no literal owner/repo pair" '! grep -qE "\-\-repo [A-Za-z0-9._-]+/[A-Za-z0-9._-]+" "$FAKE_CRON"'

# Exit-code contract: doctor/notification lanes branch on it without parsing.
ok "cron line requests nonzero exit on findings" 'grep -qF -- "--exit-nonzero-on-findings" "$FAKE_CRON"'
ok "cron line pins an explicit mode" 'grep -qF -- "--mode \"expired\"" "$FAKE_CRON"'

# Owner notice (#1870 잔여 2번): findings that only reach the cron log went
# unread (ccc-node#1913, 2026-09-25), so the default line asks the scanner to
# spool a high-confidence owner notice, rendered explicitly.
ok "cron line notifies the owner at high confidence by default" 'grep -qF -- "--notify \"high\"" "$FAKE_CRON"'

# The installer must not create the operator-owned allowlist.
ok "installer does not create the repo allowlist" '[ ! -e "$CCC_CLAUDE_DIR/timed-test-deadline-scan.repos" ]'

# generation stamp (#1081): content hash of installer + shared rendering libs
# shellcheck source=/dev/null
. "$ROOT/scripts/lib/installer-gen-stamp.sh"
# shellcheck disable=SC2034  # want_gen is read via eval inside ok()
want_gen="$(ccc_installer_gen_stamp_auto "$INSTALLER")"
ok "installed line carries gen stamp" 'grep -qE "# ccc-node:timed-test-deadline-scan gen=h_[0-9a-f]{12}$" "$FAKE_CRON"'
ok "gen stamp matches installer content" 'grep -qF "gen=$want_gen" "$FAKE_CRON"'
ok "BEGIN/END block markers stay unstamped (exact-match parsed)" '! grep -qE "timed-test-deadline-scan:(begin|end) gen=" "$FAKE_CRON"'

# idempotent: re-apply keeps a single line
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "re-apply stays idempotent (one line)" '[ "$(marker_count)" = 1 ]'
ok "re-apply keeps the same gen stamp" 'grep -qF "gen=$want_gen" "$FAKE_CRON"'

# custom schedule replaces, still single line
bash "$INSTALLER" --apply --schedule "5 * * * *" >/dev/null 2>&1
ok "custom schedule still single line" '[ "$(marker_count)" = 1 ]'
ok "custom schedule applied" 'grep -qF "5 * * * *" "$FAKE_CRON"'
ok "old schedule removed" '! grep -qF "20 9 * * *" "$FAKE_CRON"'

# relative mode is selectable and lands in the rendered line
bash "$INSTALLER" --apply --mode relative >/dev/null 2>&1
ok "relative mode applied" 'grep -qF -- "--mode \"relative\"" "$FAKE_CRON"'
ok "relative mode still single line" '[ "$(marker_count)" = 1 ]'

# an unknown mode is rejected before touching the crontab
# shellcheck disable=SC2034  # before is read via eval inside ok()
before="$(cat "$FAKE_CRON")"
# shellcheck disable=SC2034  # out/rc are read via eval inside ok()
out="$(bash "$INSTALLER" --apply --mode nonsense 2>&1)"; rc=$?
ok "unknown mode exits 2" '[ "$rc" = 2 ]'
ok "unknown mode is named in the error" 'printf "%s" "$out" | grep -q "unknown --mode"'
ok "unknown mode leaves the crontab untouched" '[ "$(cat "$FAKE_CRON")" = "$before" ]'

# --notify is selectable (off restores log-only) and lands in the rendered line
bash "$INSTALLER" --apply --notify off >/dev/null 2>&1
ok "notify off applied" 'grep -qF -- "--notify \"off\"" "$FAKE_CRON"'
ok "notify off still single line" '[ "$(marker_count)" = 1 ]'
CCC_TIMED_TEST_SCAN_NOTIFY=low bash "$INSTALLER" --apply >/dev/null 2>&1
ok "notify level env override applied" 'grep -qF -- "--notify \"low\"" "$FAKE_CRON"'

# an unknown notify level is rejected before touching the crontab
# shellcheck disable=SC2034  # before is read via eval inside ok()
before="$(cat "$FAKE_CRON")"
# shellcheck disable=SC2034  # out/rc are read via eval inside ok()
out="$(bash "$INSTALLER" --apply --notify loud 2>&1)"; rc=$?
ok "unknown notify exits 2" '[ "$rc" = 2 ]'
ok "unknown notify is named in the error" 'printf "%s" "$out" | grep -q "unknown --notify"'
ok "unknown notify leaves the crontab untouched" '[ "$(cat "$FAKE_CRON")" = "$before" ]'

# install record (#1081 phase 2): replay material for self-update
bash "$INSTALLER" --apply --schedule "5 * * * *" --mode expired >/dev/null 2>&1
# shellcheck disable=SC2034  # REC is read via eval inside ok()
REC="$CCC_CLAUDE_DIR/state/install-timed-test-deadline-scan-cron.json"
ok "apply writes an install record" '[ -f "$REC" ]'
ok "record carries schema/marker/gen" 'jq -e ".schema==\"ccc.install-record.v1\" and .marker==\"# ccc-node:timed-test-deadline-scan\" and .gen==\"$want_gen\"" "$REC" >/dev/null'
ok "record argv materializes schedule, mode and notify" 'jq -e ".argv == [\"--apply\",\"--schedule\",\"5 * * * *\",\"--mode\",\"expired\",\"--notify\",\"high\"]" "$REC" >/dev/null'
ok "record is owner-only" '[ "$(stat -c %a "$REC")" = 600 ]'

# A record written before --notify existed replays (self-update step 5) with
# the old argv; the installer default must then turn the notice on.
bash "$INSTALLER" --apply --schedule "5 * * * *" --mode expired >/dev/null 2>&1
ok "pre-notify replay argv renders the high default" 'grep -qF -- "--notify \"high\"" "$FAKE_CRON"'

# a pre-existing unrelated cron line is preserved
printf '0 4 * * * echo keepme\n' >> "$FAKE_CRON"
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "unrelated cron line preserved" 'grep -qF "echo keepme" "$FAKE_CRON"'
ok "still one marker line after preserve" '[ "$(marker_count)" = 1 ]'

# remove takes the marker line out, keeps the unrelated one
bash "$INSTALLER" --apply --remove >/dev/null 2>&1
ok "remove deletes marker line" '[ "$(marker_count)" = 0 ]'
ok "remove deletes the block markers too" '! grep -qF "# ccc-node:timed-test-deadline-scan:begin" "$FAKE_CRON"'
ok "remove keeps unrelated line" 'grep -qF "echo keepme" "$FAKE_CRON"'
ok "remove drops the install record (no resurrection via re-apply)" '[ ! -f "$REC" ]'

# legacy migration (#1077): a bare stamped pre-#1077 line is folded into a block
printf '%s\n' '20 9 * * * bash -lc '"'"'old'"'"'  # ccc-node:timed-test-deadline-scan gen=h_000000000000' '0 4 * * * echo keepme2' > "$FAKE_CRON"
bash "$INSTALLER" --apply >/dev/null 2>&1
ok "legacy bare marker line is migrated into a block" '[ "$(marker_count)" = 1 ] && [ "$(block_count)" = 1 ]'
ok "legacy line content replaced (old gen gone)" '! grep -qF "gen=h_000000000000" "$FAKE_CRON"'
ok "migration preserves unrelated lines" 'grep -qF "echo keepme2" "$FAKE_CRON"'

# corrupt managed block fails closed (#1077)
printf '%s\n' '# ccc-node:timed-test-deadline-scan:begin' '20 9 * * * dangling  # ccc-node:timed-test-deadline-scan gen=h_000000000000' > "$FAKE_CRON"
# shellcheck disable=SC2034  # out is read via eval inside ok()
out="$(bash "$INSTALLER" --apply 2>&1)"
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
ok "corrupt managed block exits 4" '[ "$rc" = 4 ]'
ok "corrupt managed block is reported" 'printf "%s" "$out" | grep -q "corrupt managed schedule block"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
