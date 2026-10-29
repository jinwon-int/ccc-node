#!/usr/bin/env bash
# Install the timed-test RELATIVE scan cron for ccc-node (ccc-node#2544).
#
# scripts/timed_test_deadline_scan.py --mode relative reports timed tests
# written only in relative terms ("며칠", "1~2주", "T+24h") with no absolute
# KST datetime — the owner rule (2026-09-11) is broken at write time, so the
# expired scan can never catch them. The 2026-10-27 fleet audit found two real
# ones this way (a soak of "수일분", unjudged 7 days; an observation of "며칠",
# unjudged 9 days), and no node ran relative mode at all, because the only way
# to install it was the deadline installer's --mode relative, which REPLACED
# the expired entry. This installer is what makes it run alongside expired.
#
# WHY A SEPARATE INSTALLER — same three reasons as the follow-up installer
# (#2324), see install-timed-followup-scan-cron.sh:
#   - One installer renders one managed entry. A second --mode through the
#     deadline installer would replace its `expired` entry, the one the owner
#     rule depends on.
#   - Install records (installer-gen-stamp.sh) are keyed by installer file
#     basename, so two entries from one installer would overwrite each
#     other's record and a self-update replay would restore only one.
#   - The marker `# ccc-node:timed-relative-scan` is not a prefix-extension of
#     `# ccc-node:timed-test-deadline-scan` (or of the follow-up marker) in
#     either direction: the shared block parser and ccc-doctor match markers
#     as substrings.
# install-timed-test-deadline-scan-cron.sh --mode relative now delegates here
# and leaves the expired entry alone.
#
# WHAT GETS SCANNED is the same operator-owned list the deadline installer
# reads, ~/.claude/timed-test-deadline-scan.repos (one `owner/name` per line).
# This installer does NOT create it; a missing or empty list makes the scanner
# exit 3 ("not configured") rather than report a reassuring zero.
#
# Consistent with the other timed-test installers: SAFE BY DEFAULT (dry-run
# unless --apply), idempotent (a single marker-tagged entry in a BEGIN/END
# block, #1077), never prints secrets, and setup.sh never installs this
# itself. The managed entry carries a `gen=h_<sha256:12>` stamp (#1081) so
# ccc-doctor can tell when it was rendered by older code, and every option is
# materialized in the install record so a self-update replay keeps it.
#
# OWNER NOTICE: the rendered line passes `--notify high`. The measured false
# positive shapes (#2544: "당분간", effort estimates, process durations,
# quoted rule text, judged checkboxes, outside authors) are demoted to low by
# the scanner, so they stay in the log and out of the notice. Relative
# notices keep their own dedup state (notify-relative.json).
#
# The cron entry runs through `bash -lc` so the login profile PATH is loaded;
# the scanner shells out to `gh`, which a bare cron PATH (especially on Termux,
# which has no /usr/bin) would not resolve.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SELF_DIR/install-timed-relative-scan-cron.sh"
CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
SCAN_CMD="${CCC_TIMED_RELATIVE_SCAN_CMD:-$SELF_DIR/timed_test_deadline_scan.py}"
# Same allowlist file as the deadline installer: one list of repos to watch.
REPOS="${CCC_TIMED_RELATIVE_SCAN_REPOS:-$CLAUDE_DIR/timed-test-deadline-scan.repos}"
NOTIFY="${CCC_TIMED_RELATIVE_SCAN_NOTIFY:-high}"
# Once a day at 09:25 KST: a relative-only wording does not get more urgent by
# the hour, and 09:25 sits between the deadline scan (09:20) and the
# follow-up scan (09:35), which call `gh` for the same repos.
SCHEDULE="${CCC_TIMED_RELATIVE_SCAN_CRON:-25 9 * * *}"
LOG="${CCC_TIMED_RELATIVE_SCAN_CRON_LOG:-$STATE_DIR/timed-relative-scan.cron.log}"
CRONTAB="${CCC_CRONTAB_CMD:-crontab}"
MARKER="# ccc-node:timed-relative-scan"
BLOCK_BEGIN="# ccc-node:timed-relative-scan:begin"
BLOCK_END="# ccc-node:timed-relative-scan:end"
APPLY=0
REMOVE=0

# Shared installer libs (#1081, #1077): gen stamps + records, and the common
# crontab install/remove driver.
GEN_STAMP_LIB="$SELF_DIR/lib/installer-gen-stamp.sh"
CRON_COMMON_LIB="$SELF_DIR/lib/installer-cron-common.sh"
for lib in "$GEN_STAMP_LIB" "$CRON_COMMON_LIB"; do
  if [ ! -r "$lib" ]; then
    echo "shared installer library is missing: $lib" >&2
    exit 4
  fi
  # shellcheck source=/dev/null
  . "$lib"
done
GEN="$(ccc_installer_gen_stamp_auto "$SELF")"

usage() {
  cat <<EOF
Usage: install-timed-relative-scan-cron.sh [--dry-run|--apply] [--remove]
                                           [--schedule SPEC] [--notify LEVEL]

Installs (or removes) a crontab entry that runs
timed_test_deadline_scan.py --mode relative, so a timed test written only in
relative terms ("며칠", "1~2주", "T+24h") with no absolute KST datetime gets
noticed.

This is separate from install-timed-test-deadline-scan-cron.sh and leaves its
expired entry alone: run both to watch deadlines and relative-only wording.

Defaults to dry-run; --apply is required to change the crontab. Idempotent:
re-running replaces the managed "$BLOCK_BEGIN" ..
"$BLOCK_END" block (and migrates any legacy bare "$MARKER" line into it).

This installer does NOT create the repo allowlist
($REPOS) — the same operator-owned file the
deadline scan reads. Until it exists the scan exits 3 (not configured).

Options:
  --dry-run        Show the resulting crontab without changing it (default).
  --apply          Write the crontab change.
  --remove         Remove the managed entry (with --apply) instead of adding it.
  --schedule SPEC  Cron schedule (5 fields). Default: "$SCHEDULE".
  --notify LEVEL   Owner notice via the bridge push spool for findings at this
                   confidence or above: high (default), low, or off (log only).

Env overrides: CCC_CLAUDE_DIR, CCC_STATE_DIR, CCC_TIMED_RELATIVE_SCAN_CMD,
CCC_TIMED_RELATIVE_SCAN_REPOS, CCC_TIMED_RELATIVE_SCAN_NOTIFY,
CCC_TIMED_RELATIVE_SCAN_CRON, CCC_TIMED_RELATIVE_SCAN_CRON_LOG, CCC_CRONTAB_CMD.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) APPLY=0 ;;
    --apply) APPLY=1 ;;
    --remove) REMOVE=1 ;;
    --schedule) ccc_cron_need_val "$1" "${2:-}"; SCHEDULE="$2"; shift ;;
    --notify) ccc_cron_need_val "$1" "${2:-}"; NOTIFY="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

case "$NOTIFY" in
  off|high|low) ;;
  *) echo "unknown --notify: $NOTIFY (expected off, high or low)" >&2; exit 2 ;;
esac

# --exit-nonzero-on-findings keeps the deadline lane's exit-code contract:
# 1 findings, 3 repo list missing or empty, 4 partial scan. --mode and
# --notify are rendered explicitly so the crontab line says what it does.
CRON_LINE="$SCHEDULE bash -lc 'python3 \"$SCAN_CMD\" --repos-file \"$REPOS\" --mode relative --notify \"$NOTIFY\" --exit-nonzero-on-findings' >> \"$LOG\" 2>&1  $MARKER gen=$GEN"

if [ "$APPLY" = 1 ] && [ "$REMOVE" != 1 ]; then
  # The cron line appends to "$LOG" (under STATE_DIR); create the directory
  # now, not when the job first fires (install-memory-refresh-cron.sh class).
  mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
fi

ccc_cron_installer_finish \
  --label "timed-relative-scan" \
  --marker "$MARKER" --begin "$BLOCK_BEGIN" --end "$BLOCK_END" \
  --crontab "$CRONTAB" --state-dir "$STATE_DIR" --self "$SELF" --gen "$GEN" \
  --apply "$APPLY" --remove "$REMOVE" --schedule-desc "$SCHEDULE" \
  --body "$CRON_LINE" -- \
  --apply --schedule "$SCHEDULE" --notify "$NOTIFY"
