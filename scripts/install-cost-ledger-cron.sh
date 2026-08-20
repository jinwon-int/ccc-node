#!/usr/bin/env bash
# Install a daily cost-ledger cron for ccc-node (#1205, TM-2370 P0-d).
#
# scripts/cost-ledger.py aggregates one KST day of Claude Code transcript usage
# into a per-model token/cost row. It is read-only over the transcripts and
# writes one idempotent line per (date, node), so a re-run for the same day
# replaces rather than duplicates — which is what makes a missed night
# recoverable by hand without corrupting the ledger.
#
# The entry has to come from an installer rather than a hand-added crontab
# line: doctor's cron-drift check only knows about managed markers, so an
# unmanaged line both escapes drift detection and trips the warning that
# something unmanaged is scheduled. Going through the shared driver also earns
# the gen stamp (#1081) and the install record that lets self-update reapply
# this entry when the rendering changes.
#
# Consistent with the other cron installers: SAFE BY DEFAULT (dry-run unless
# --apply), idempotent BEGIN/END block (#1077), never prints secrets, and
# setup.sh never installs it on its own.
#
# Nodes with no Claude Code transcripts (daegyo is Codex-primary, gongmyoung
# has no harness tree) are not a special case for this installer: the entry can
# be installed there and cost-ledger.py skips cleanly with ok/skipped rather
# than failing, so the fleet keeps one uniform managed unit.
#
# Runs through `bash -lc` for the login PATH — cost-ledger.py needs a python3
# that a bare cron PATH does not resolve on Termux, which has no /usr/bin.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SELF_DIR/install-cost-ledger-cron.sh"
CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
LEDGER_CMD="${CCC_COST_LEDGER_CMD:-$SELF_DIR/cost-ledger.py}"
LEDGER_OUT="${CCC_COST_LEDGER_FILE:-$STATE_DIR/cost-ledger.jsonl}"
# 03:29 KST: after midnight so "yesterday" is closed, and off the :00/:30 marks
# every other scheduled job lands on. Early enough that the row exists before
# the 04:45 self-update window touches anything.
SCHEDULE="${CCC_COST_LEDGER_CRON:-29 3 * * *}"
LOG="${CCC_COST_LEDGER_CRON_LOG:-$STATE_DIR/cost-ledger.cron.log}"
CRONTAB="${CCC_CRONTAB_CMD:-crontab}"
MARKER="# ccc-node:cost-ledger"
BLOCK_BEGIN="# ccc-node:cost-ledger:begin"
BLOCK_END="# ccc-node:cost-ledger:end"
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
Usage: install-cost-ledger-cron.sh [--dry-run|--apply] [--remove] [--schedule SPEC]

Installs (or removes) a crontab entry that runs cost-ledger.py once a day and
appends one row per (date, node) to the local ledger, so per-model token and
cost usage is recorded where it happened instead of being reconstructed later
from transcripts that may have been pruned.

Defaults to dry-run; --apply is required to change the crontab. Idempotent:
re-running replaces the managed "$BLOCK_BEGIN" ..
"$BLOCK_END" block (and migrates any legacy bare "$MARKER" line into it).

The ledger row carries est_cost_usd only for models with a published price in
cost-ledger.py's table; anything else is recorded with tokens and a null cost
rather than a guessed one.

Options:
  --dry-run        Show the resulting crontab without changing it (default).
  --apply          Write the crontab change.
  --remove         Remove the managed entry (with --apply) instead of adding it.
  --schedule SPEC  Cron schedule (5 fields). Default: "$SCHEDULE".

Env overrides: CCC_CLAUDE_DIR, CCC_STATE_DIR, CCC_COST_LEDGER_CMD,
CCC_COST_LEDGER_FILE, CCC_COST_LEDGER_CRON, CCC_COST_LEDGER_CRON_LOG,
CCC_CRONTAB_CMD.
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

# CCC_STATE_DIR rides in the entry rather than being left to the cron
# environment: cost-ledger.py falls back to it for the fleet node name when
# CCC_NODE is unset, and `bash -lc` under cron exports neither. Without it a
# node whose identity lives only in state/node.txt would be recorded under its
# hostname, which disagrees with the fleet name on at least one node.
CRON_LINE="$SCHEDULE bash -lc 'CCC_STATE_DIR=\"$STATE_DIR\" python3 \"$LEDGER_CMD\" --out \"$LEDGER_OUT\"' >> \"$LOG\" 2>&1  $MARKER gen=$GEN"

if [ "$APPLY" = 1 ] && [ "$REMOVE" != 1 ]; then
  # The entry appends to "$LOG" under STATE_DIR; create the directory now
  # rather than when the job first fires (same as the sibling installers).
  mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
fi

ccc_cron_installer_finish \
  --label "cost-ledger" \
  --marker "$MARKER" --begin "$BLOCK_BEGIN" --end "$BLOCK_END" \
  --crontab "$CRONTAB" --state-dir "$STATE_DIR" --self "$SELF" --gen "$GEN" \
  --apply "$APPLY" --remove "$REMOVE" --schedule-desc "$SCHEDULE" \
  --body "$CRON_LINE" -- \
  --apply --schedule "$SCHEDULE"
