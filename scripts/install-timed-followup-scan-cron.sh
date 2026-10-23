#!/usr/bin/env bash
# Install the timed-test FOLLOW-UP scan cron for ccc-node (ccc-node#2324).
#
# scripts/timed_test_deadline_scan.py --mode followup reports follow-ups that
# a verdict promised at an absolute KST datetime which has passed with no
# comment after it (`followup-overdue`). The #2321 audit found 21 such issues,
# 12 of them already closed: a verdict-then-close or a PR auto-close leaves the
# promised re-measurement with nowhere to go. This installer is what makes the
# follow-up scan run without somebody remembering to.
#
# WHY A SEPARATE INSTALLER (owner decision 2026-10-23, #2324):
#   - Followup mode reports ONLY followup findings. Re-applying
#     install-timed-test-deadline-scan-cron.sh with --mode followup would
#     REPLACE its `expired` entry, and the expired scan is the one the owner
#     rule (2026-09-11) depends on.
#   - Install records (installer-gen-stamp.sh) are keyed by installer file
#     basename, one record per installer. Two entries rendered by one
#     installer would overwrite each other's record, and a self-update replay
#     would restore only the last one applied (#2475 class).
#   - The marker `# ccc-node:timed-followup-scan` is deliberately NOT a
#     prefix-extension of `# ccc-node:timed-test-deadline-scan`: the shared
#     block parser and ccc-doctor both match markers as substrings, so a
#     `...deadline-scan-followup` marker would be stripped by the deadline
#     installer's --remove/re-apply and counted under its doctor row.
#
# WHAT GETS SCANNED is the same operator-owned list the deadline installer
# reads, ~/.claude/timed-test-deadline-scan.repos (one `owner/name` per line).
# This installer does NOT create it; a missing or empty list makes the scanner
# exit 3 ("not configured") rather than report a reassuring zero.
#
# Consistent with install-timed-test-deadline-scan-cron.sh: SAFE BY DEFAULT
# (dry-run unless --apply), idempotent (a single marker-tagged entry in a
# BEGIN/END block, #1077), never prints secrets, and setup.sh never installs
# this itself. The managed entry carries a `gen=h_<sha256:12>` stamp (#1081)
# so ccc-doctor can tell when it was rendered by older code, and every option
# is materialized in the install record so a self-update replay keeps it.
#
# OWNER NOTICE: the rendered line passes `--notify high`, so a high-confidence
# finding is queued as a short owner-only notice in the bridge push spool.
# Followup notices keep their own dedup state (notify-followup.json), so they
# never suppress or re-trigger the expired scan's notices.
#
# The cron entry runs through `bash -lc` so the login profile PATH is loaded;
# the scanner shells out to `gh`, which a bare cron PATH (especially on Termux,
# which has no /usr/bin) would not resolve.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SELF_DIR/install-timed-followup-scan-cron.sh"
CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
SCAN_CMD="${CCC_TIMED_FOLLOWUP_SCAN_CMD:-$SELF_DIR/timed_test_deadline_scan.py}"
# Same allowlist file as the deadline installer: one list of repos to watch.
REPOS="${CCC_TIMED_FOLLOWUP_SCAN_REPOS:-$CLAUDE_DIR/timed-test-deadline-scan.repos}"
# 45 days is the window the #2321 audit needed to catch the closed issues
# whose promised follow-up went missing (docs/timed-test-deadline-scan.md).
CLOSED_DAYS="${CCC_TIMED_FOLLOWUP_SCAN_CLOSED_DAYS:-45}"
NOTIFY="${CCC_TIMED_FOLLOWUP_SCAN_NOTIFY:-high}"
# Once a day at 09:35 KST: a follow-up is promised for a day, not a minute, so
# one daytime run is enough, and it lands after the deadline scan's 09:20 run
# rather than on top of it (both call `gh` for the same repos).
SCHEDULE="${CCC_TIMED_FOLLOWUP_SCAN_CRON:-35 9 * * *}"
LOG="${CCC_TIMED_FOLLOWUP_SCAN_CRON_LOG:-$STATE_DIR/timed-followup-scan.cron.log}"
CRONTAB="${CCC_CRONTAB_CMD:-crontab}"
MARKER="# ccc-node:timed-followup-scan"
BLOCK_BEGIN="# ccc-node:timed-followup-scan:begin"
BLOCK_END="# ccc-node:timed-followup-scan:end"
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
Usage: install-timed-followup-scan-cron.sh [--dry-run|--apply] [--remove]
                                           [--schedule SPEC]
                                           [--include-closed-days N]
                                           [--notify LEVEL]

Installs (or removes) a crontab entry that runs
timed_test_deadline_scan.py --mode followup, so a follow-up promised by a
verdict whose KST datetime has passed with nothing posted after it gets
noticed, including on issues that were closed right after the verdict.

This is separate from install-timed-test-deadline-scan-cron.sh and leaves its
entry alone: run both to watch deadlines and follow-ups.

Defaults to dry-run; --apply is required to change the crontab. Idempotent:
re-running replaces the managed "$BLOCK_BEGIN" ..
"$BLOCK_END" block (and migrates any legacy bare "$MARKER" line into it).

This installer does NOT create the repo allowlist
($REPOS) — the same operator-owned file the
deadline scan reads. Until it exists the scan exits 3 (not configured).

Options:
  --dry-run                 Show the resulting crontab without changing it (default).
  --apply                   Write the crontab change.
  --remove                  Remove the managed entry (with --apply) instead of adding it.
  --schedule SPEC           Cron schedule (5 fields). Default: "$SCHEDULE".
  --include-closed-days N   Also scan issues closed within the last N days
                            (non-negative integer; 0 = open issues only).
                            Default: $CLOSED_DAYS.
  --notify LEVEL            Owner notice via the bridge push spool for findings at
                            this confidence or above: high (default), low, or off.

Env overrides: CCC_CLAUDE_DIR, CCC_STATE_DIR, CCC_TIMED_FOLLOWUP_SCAN_CMD,
CCC_TIMED_FOLLOWUP_SCAN_REPOS, CCC_TIMED_FOLLOWUP_SCAN_CLOSED_DAYS,
CCC_TIMED_FOLLOWUP_SCAN_NOTIFY, CCC_TIMED_FOLLOWUP_SCAN_CRON,
CCC_TIMED_FOLLOWUP_SCAN_CRON_LOG, CCC_CRONTAB_CMD.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) APPLY=0 ;;
    --apply) APPLY=1 ;;
    --remove) REMOVE=1 ;;
    --schedule) ccc_cron_need_val "$1" "${2:-}"; SCHEDULE="$2"; shift ;;
    --include-closed-days) ccc_cron_need_val "$1" "${2:-}"; CLOSED_DAYS="$2"; shift ;;
    --notify) ccc_cron_need_val "$1" "${2:-}"; NOTIFY="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# Digits only (no sign, no blanks): the value lands unquoted-safe in the cron
# line and the scanner rejects a negative N with exit 2 on every tick, which
# would only show up in the log. Leading zeros are normalized ("045" -> 45)
# so the rendered line and the install record never carry an octal-looking
# value.
case "$CLOSED_DAYS" in
  ''|*[!0-9]*)
    echo "invalid --include-closed-days: $CLOSED_DAYS (expected a non-negative integer)" >&2
    exit 2 ;;
esac
CLOSED_DAYS="$((10#$CLOSED_DAYS))"
case "$NOTIFY" in
  off|high|low) ;;
  *) echo "unknown --notify: $NOTIFY (expected off, high or low)" >&2; exit 2 ;;
esac

# --exit-nonzero-on-findings keeps the deadline lane's exit-code contract:
# 1 findings, 3 repo list missing or empty, 4 partial scan. --mode and
# --notify are rendered explicitly so the crontab line says what it does.
CRON_LINE="$SCHEDULE bash -lc 'python3 \"$SCAN_CMD\" --repos-file \"$REPOS\" --mode followup --include-closed-days $CLOSED_DAYS --notify \"$NOTIFY\" --exit-nonzero-on-findings' >> \"$LOG\" 2>&1  $MARKER gen=$GEN"

if [ "$APPLY" = 1 ] && [ "$REMOVE" != 1 ]; then
  # The cron line appends to "$LOG" (under STATE_DIR); create the directory
  # now, not when the job first fires (install-memory-refresh-cron.sh class).
  mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
fi

ccc_cron_installer_finish \
  --label "timed-followup-scan" \
  --marker "$MARKER" --begin "$BLOCK_BEGIN" --end "$BLOCK_END" \
  --crontab "$CRONTAB" --state-dir "$STATE_DIR" --self "$SELF" --gen "$GEN" \
  --apply "$APPLY" --remove "$REMOVE" --schedule-desc "$SCHEDULE" \
  --body "$CRON_LINE" -- \
  --apply --schedule "$SCHEDULE" --include-closed-days "$CLOSED_DAYS" --notify "$NOTIFY"
