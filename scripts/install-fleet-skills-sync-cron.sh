#!/usr/bin/env bash
# Install the daily fleet-skills sync cron for ccc-node (#1079 follow-up).
#
# ccc-fleet-skills-sync.py installs approved private fleet skills from an
# exact commit of the fleet-skills repo. Until now every node carried a
# HAND-WRITTEN `# ccc-node:fleet-skills-sync` line (no gen stamp, no install
# record), so ccc-doctor reported it as an unknown unmanaged marker on all 12
# nodes and nothing could re-render it when the command shape changed. This
# installer owns that line the same way the other cron installers own theirs.
#
# Consistent with install-pr-status-poll-cron.sh / install-memory-refresh-cron.sh:
# SAFE BY DEFAULT (dry-run unless --apply), idempotent (a single marker-tagged
# entry inside a BEGIN/END block, #1077), never prints secrets, and the harness
# setup.sh never installs this itself. The managed entry carries a
# `gen=h_<sha256:12>` stamp over this script plus the shared cron-installer lib
# (#1081, inputs owned by ccc_installer_gen_inputs) so ccc-doctor can tell when
# the installed entry was rendered by older code. A legacy bare marker line is
# migrated into the block on the next apply.
#
# The cron entry runs through `bash -lc` so the login profile PATH is loaded;
# the sync resolves the exact fleet-skills HEAD with `git ls-remote` at fire
# time (the sync tool refuses anything but a 40-char SHA), then applies it.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SELF_DIR/install-fleet-skills-sync-cron.sh"
CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
SYNC="${CCC_FLEET_SKILLS_SYNC_CMD:-$CLAUDE_DIR/hooks/ccc-fleet-skills-sync.py}"
REPO_URL="${CCC_FLEET_SKILLS_REPO:-https://github.com/jinwon-int/fleet-skills.git}"
REPO_BRANCH="${CCC_FLEET_SKILLS_BRANCH:-main}"
SCHEDULE="${CCC_FLEET_SKILLS_SYNC_CRON:-0 5 * * *}"
LOG="${CCC_FLEET_SKILLS_SYNC_CRON_LOG:-$STATE_DIR/fleet-skills-sync.cron.log}"
CRONTAB="${CCC_CRONTAB_CMD:-crontab}"
MARKER="# ccc-node:fleet-skills-sync"
BLOCK_BEGIN="# ccc-node:fleet-skills-sync:begin"
BLOCK_END="# ccc-node:fleet-skills-sync:end"
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
Usage: install-fleet-skills-sync-cron.sh [--dry-run|--apply] [--remove] [--schedule SPEC]

Installs (or removes) a crontab entry that resolves the exact HEAD of the
fleet-skills repo and runs ccc-fleet-skills-sync.py apply --ref <sha> so
approved private fleet skills reach this node on a schedule. Defaults to
dry-run; --apply is required to change the crontab. Idempotent: re-running
replaces the managed "$BLOCK_BEGIN" ..
"$BLOCK_END" block (and migrates any legacy bare "$MARKER" line into it).

Options:
  --dry-run        Show the resulting crontab without changing it (default).
  --apply          Write the crontab change.
  --remove         Remove the managed entry (with --apply) instead of adding it.
  --schedule SPEC  Cron schedule (5 fields). Default: "$SCHEDULE".

Env overrides: CCC_CLAUDE_DIR, CCC_STATE_DIR, CCC_FLEET_SKILLS_SYNC_CMD,
CCC_FLEET_SKILLS_REPO, CCC_FLEET_SKILLS_BRANCH, CCC_FLEET_SKILLS_SYNC_CRON,
CCC_FLEET_SKILLS_SYNC_CRON_LOG, CCC_CRONTAB_CMD.
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

# The body is single-quoted for `bash -lc`; \$S is resolved at fire time, not
# at install time, so the entry never pins a stale ref. An empty ls-remote
# (offline) short-circuits instead of handing the sync an empty --ref.
CRON_LINE="$SCHEDULE bash -lc 'S=\$(git ls-remote \"$REPO_URL\" \"$REPO_BRANCH\" | cut -f1); [ -n \"\$S\" ] && CCC_CLAUDE_DIR=\"$CLAUDE_DIR\" python3 \"$SYNC\" apply --ref \"\$S\"' >> \"$LOG\" 2>&1  $MARKER gen=$GEN"

if [ "$APPLY" = 1 ] && [ "$REMOVE" != 1 ]; then
  # Same redirect-first failure mode as the sibling installers: the cron line
  # appends to "$LOG" (under STATE_DIR); create the directory now.
  mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
fi

ccc_cron_installer_finish \
  --label "fleet-skills-sync" \
  --marker "$MARKER" --begin "$BLOCK_BEGIN" --end "$BLOCK_END" \
  --crontab "$CRONTAB" --state-dir "$STATE_DIR" --self "$SELF" --gen "$GEN" \
  --apply "$APPLY" --remove "$REMOVE" --schedule-desc "$SCHEDULE" \
  --body "$CRON_LINE" -- \
  --apply --schedule "$SCHEDULE"
