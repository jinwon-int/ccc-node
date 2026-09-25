#!/usr/bin/env bash
# Install a daily timed-test deadline scan cron for ccc-node (ccc-node#1870).
#
# The owner rule (2026-09-11) requires an absolute KST end datetime on every
# timed test. Writing it down and waking up at it are separate problems: the
# 2026-09-21 sweep found seven issues whose deadline had passed unjudged, the
# oldest silent for nine days. scripts/timed_test_deadline_scan.py finds them;
# this installer is what makes it run without somebody remembering to.
#
# WHAT GETS SCANNED lives in ~/.claude/timed-test-deadline-scan.repos, one
# `owner/name` per line — operator-owned, same contract as
# ~/.claude/pr-status-poll.repos. This installer does NOT create it. A missing
# or empty list makes the scanner exit 3 ("not configured") rather than report
# a reassuring zero, so an unconfigured node is visibly unconfigured.
#
# Keeping the repo list in a file rather than baked into the cron line is
# deliberate (#1867): that issue is about an installer re-run silently
# dropping baked-in env, which cost nosuk nine days of piri drafting. The less
# state lives in the crontab line, the less a re-run can quietly lose. Adding a
# repository here never requires re-running this installer.
#
# Consistent with install-cost-ledger-cron.sh / install-pr-status-poll-cron.sh:
# SAFE BY DEFAULT (dry-run unless --apply), idempotent (a single marker-tagged
# entry in a BEGIN/END block, #1077), never prints secrets, and setup.sh never
# installs this itself. The managed entry carries a `gen=h_<sha256:12>` stamp
# (#1081) so ccc-doctor can tell when it was rendered by older code.
#
# OWNER NOTICE (#1870 잔여 2번): the rendered line passes `--notify high`, so a
# high-confidence finding is also queued as a short owner-only notice in the
# bridge push spool (~/.claude/state/telegram-spool) — the same channel
# agent-cron, ccc-self-update.sh and ccc-pr-status-poll.sh use. Until then the
# findings only reached the cron log, and on 2026-09-25 that log caught
# ccc-node#1913 expired-unjudged with nobody reading it. The scanner dedups an
# unchanged finding set (reminder every 3 days). `--notify off` restores the
# log-only behaviour. An install record written before this option existed
# carries no --notify in its argv, so a self-update replay renders the new
# default (high).
#
# The cron entry runs through `bash -lc` so the login profile PATH is loaded;
# the scanner shells out to `gh`, which a bare cron PATH (especially on Termux,
# which has no /usr/bin) would not resolve.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SELF_DIR/install-timed-test-deadline-scan-cron.sh"
CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
SCAN_CMD="${CCC_TIMED_TEST_SCAN_CMD:-$SELF_DIR/timed_test_deadline_scan.py}"
REPOS="${CCC_TIMED_TEST_SCAN_REPOS:-$CLAUDE_DIR/timed-test-deadline-scan.repos}"
MODE="${CCC_TIMED_TEST_SCAN_MODE:-expired}"
NOTIFY="${CCC_TIMED_TEST_SCAN_NOTIFY:-high}"
# 09:20 KST daily: late enough that an overnight deadline has actually passed,
# early enough that a finding still has a working day attached to it.
SCHEDULE="${CCC_TIMED_TEST_SCAN_CRON:-20 9 * * *}"
LOG="${CCC_TIMED_TEST_SCAN_CRON_LOG:-$STATE_DIR/timed-test-deadline-scan.cron.log}"
CRONTAB="${CCC_CRONTAB_CMD:-crontab}"
MARKER="# ccc-node:timed-test-deadline-scan"
BLOCK_BEGIN="# ccc-node:timed-test-deadline-scan:begin"
BLOCK_END="# ccc-node:timed-test-deadline-scan:end"
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
Usage: install-timed-test-deadline-scan-cron.sh [--dry-run|--apply] [--remove]
                                                [--schedule SPEC] [--mode MODE]
                                                [--notify LEVEL]

Installs (or removes) a crontab entry that runs timed_test_deadline_scan.py so
a timed test whose KST end datetime has passed gets noticed, instead of only
being noticed if a session happens to sweep the issue tracker by hand.

Defaults to dry-run; --apply is required to change the crontab. Idempotent:
re-running replaces the managed "$BLOCK_BEGIN" ..
"$BLOCK_END" block (and migrates any legacy bare "$MARKER" line into it).

This installer does NOT create the repo allowlist
($REPOS) — that operator-owned file decides
what gets scanned and is left alone here, same as
~/.claude/pr-status-poll.repos is for ccc-pr-status-poll.sh. Until it exists
the scan exits 3 (not configured), which is on purpose: an unconfigured scan
must not look like a clean one.

Options:
  --dry-run        Show the resulting crontab without changing it (default).
  --apply          Write the crontab change.
  --remove         Remove the managed entry (with --apply) instead of adding it.
  --schedule SPEC  Cron schedule (5 fields). Default: "$SCHEDULE".
  --mode MODE      Scan mode: expired (default) or relative.
  --notify LEVEL   Owner notice via the bridge push spool for findings at this
                   confidence or above: high (default), low, or off (log only).

Env overrides: CCC_CLAUDE_DIR, CCC_STATE_DIR, CCC_TIMED_TEST_SCAN_CMD,
CCC_TIMED_TEST_SCAN_REPOS, CCC_TIMED_TEST_SCAN_MODE, CCC_TIMED_TEST_SCAN_NOTIFY,
CCC_TIMED_TEST_SCAN_CRON, CCC_TIMED_TEST_SCAN_CRON_LOG, CCC_CRONTAB_CMD.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) APPLY=0 ;;
    --apply) APPLY=1 ;;
    --remove) REMOVE=1 ;;
    --schedule) ccc_cron_need_val "$1" "${2:-}"; SCHEDULE="$2"; shift ;;
    --mode) ccc_cron_need_val "$1" "${2:-}"; MODE="$2"; shift ;;
    --notify) ccc_cron_need_val "$1" "${2:-}"; NOTIFY="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

case "$MODE" in
  expired|relative) ;;
  *) echo "unknown --mode: $MODE (expected expired or relative)" >&2; exit 2 ;;
esac
case "$NOTIFY" in
  off|high|low) ;;
  *) echo "unknown --notify: $NOTIFY (expected off, high or low)" >&2; exit 2 ;;
esac

# --exit-nonzero-on-findings is intentional: it lets ccc-doctor and any future
# notification lane branch on the exit code without parsing the report. Exit 1
# means findings, 3 means the repo list is missing or empty. --notify is
# rendered explicitly even at its default so the crontab line says what it does.
CRON_LINE="$SCHEDULE bash -lc 'python3 \"$SCAN_CMD\" --repos-file \"$REPOS\" --mode \"$MODE\" --notify \"$NOTIFY\" --exit-nonzero-on-findings' >> \"$LOG\" 2>&1  $MARKER gen=$GEN"

if [ "$APPLY" = 1 ] && [ "$REMOVE" != 1 ]; then
  # Same redirect-first failure mode as install-memory-refresh-cron.sh: the
  # cron line appends to "$LOG" (under STATE_DIR); create the directory now,
  # not when the job first fires.
  mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
fi

ccc_cron_installer_finish \
  --label "timed-test-deadline-scan" \
  --marker "$MARKER" --begin "$BLOCK_BEGIN" --end "$BLOCK_END" \
  --crontab "$CRONTAB" --state-dir "$STATE_DIR" --self "$SELF" --gen "$GEN" \
  --apply "$APPLY" --remove "$REMOVE" --schedule-desc "$SCHEDULE" \
  --body "$CRON_LINE" -- \
  --apply --schedule "$SCHEDULE" --mode "$MODE" --notify "$NOTIFY"
