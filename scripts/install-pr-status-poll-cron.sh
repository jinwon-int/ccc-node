#!/usr/bin/env bash
# Install a periodic PR/issue status poll cron for ccc-node (ccc-node#962).
#
# ccc-pr-status-poll.sh only tracks whatever this node's operator lists in
# ~/.claude/pr-status-poll.repos ("<owner/repo> <author>" per line) — an
# EMPTY/missing file means "not configured yet", not "intentionally off"; this
# installer does not create that file, only the cron entry that would run it.
#
# Consistent with install-memory-refresh-cron.sh / install-agent-cron-systemd.sh:
# SAFE BY DEFAULT (dry-run unless --apply), idempotent (a single marker-tagged
# entry), never prints secrets, and the harness setup.sh never installs this
# itself. The managed entry carries a `gen=h_<sha256:12>` stamp over this
# script plus the shared cron-installer lib (#1081, inputs owned by
# ccc_installer_gen_inputs) so ccc-doctor can tell when the installed entry
# was rendered by older code.
#
# The managed unit is a BEGIN/END block (unified removal strategy, #1077):
# scripts/lib/installer-cron-common.sh owns block parsing and the whole
# install/remove flow; legacy bare marker lines are migrated into a block on
# the next apply.
#
# The cron entry runs through `bash -lc` so the login profile PATH is loaded;
# ccc-pr-status-poll.sh shells out to gh/jq, which a bare cron PATH (especially
# on Termux, which has no /usr/bin) would not resolve.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SELF_DIR/install-pr-status-poll-cron.sh"
CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
POLL="${CCC_PR_STATUS_POLL_CMD:-$CLAUDE_DIR/hooks/ccc-pr-status-poll.sh}"
SCHEDULE="${CCC_PR_STATUS_POLL_CRON:-*/17 * * * *}"
LOG="${CCC_PR_STATUS_POLL_CRON_LOG:-$STATE_DIR/pr-status-poll.cron.log}"
CRONTAB="${CCC_CRONTAB_CMD:-crontab}"
MARKER="# ccc-node:pr-status-poll"
BLOCK_BEGIN="# ccc-node:pr-status-poll:begin"
BLOCK_END="# ccc-node:pr-status-poll:end"
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
Usage: install-pr-status-poll-cron.sh [--dry-run|--apply] [--remove] [--schedule SPEC]

Installs (or removes) a crontab entry that runs ccc-pr-status-poll.sh so a PR
or issue this node's bridge identity opened gets its state changes (CI done,
closed, merged) noticed and pushed to the owner notification spool, instead
of only being noticed if/when a session happens to re-check it manually.
Defaults to dry-run; --apply is required to change the crontab. Idempotent:
re-running replaces the managed "$BLOCK_BEGIN" ..
"$BLOCK_END" block (and migrates any legacy bare "$MARKER" line into it).

This installer does NOT create ~/.claude/pr-status-poll.repos — that
operator-owned allowlist decides what gets tracked and is left alone here,
same as ~/.claude/self-update.services is for ccc-self-update.sh.

Options:
  --dry-run        Show the resulting crontab without changing it (default).
  --apply          Write the crontab change.
  --remove         Remove the managed entry (with --apply) instead of adding it.
  --schedule SPEC  Cron schedule (5 fields). Default: "$SCHEDULE".

Env overrides: CCC_CLAUDE_DIR, CCC_STATE_DIR, CCC_PR_STATUS_POLL_CMD,
CCC_PR_STATUS_POLL_CRON, CCC_PR_STATUS_POLL_CRON_LOG, CCC_CRONTAB_CMD.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) APPLY=0 ;;
    --apply) APPLY=1 ;;
    --remove) REMOVE=1 ;;
    --schedule) ccc_cron_need_val "$1" "${2:-}"; SCHEDULE="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

CRON_LINE="$SCHEDULE bash -lc 'CCC_CLAUDE_DIR=\"$CLAUDE_DIR\" \"$POLL\" run' >> \"$LOG\" 2>&1  $MARKER gen=$GEN"

if [ "$APPLY" = 1 ] && [ "$REMOVE" != 1 ]; then
  # Same redirect-first failure mode as install-memory-refresh-cron.sh: the
  # cron line appends to "$LOG" (under STATE_DIR); create the directory now,
  # not when the job first fires.
  mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
fi

ccc_cron_installer_finish \
  --label "pr-status-poll" \
  --marker "$MARKER" --begin "$BLOCK_BEGIN" --end "$BLOCK_END" \
  --crontab "$CRONTAB" --state-dir "$STATE_DIR" --self "$SELF" --gen "$GEN" \
  --apply "$APPLY" --remove "$REMOVE" --schedule-desc "$SCHEDULE" \
  --body "$CRON_LINE" -- \
  --apply --schedule "$SCHEDULE"
