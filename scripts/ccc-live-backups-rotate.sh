#!/usr/bin/env bash
# ccc-live-backups-rotate — prune ccc-node live-backup directories to the
# KEEP newest snapshots. Versioned successor of the out-of-band 2026-07-19
# script that 12 nodes cron daily (#973).
#
# Install: setup.sh copies this to ~/.ccc-node/scripts/ccc-live-backups-rotate.sh.
# Cron (already registered on fleet nodes):
#   30 4 * * * $HOME/.ccc-node/scripts/ccc-live-backups-rotate.sh \
#     >> "$HOME/.claude/state/live-backups-rotate.cron.log" 2>&1  # ccc-node:live-backups-rotate
# Do NOT redirect to /dev/null: a missing/failing script must stay
# discoverable. The script itself appends one body-free line per run to
# ${CCC_STATE_DIR:-~/.claude/state}/live-backups-rotate.log and exits non-zero
# when any prune fails.
#
# Env: CCC_LIVE_BACKUPS_KEEP (default 5), CCC_STATE_DIR,
#      CCC_LIVE_BACKUPS_ROOTS (tests only — space-separated root list
#      overriding the default /root, $HOME, /home/* scan).
set -u

KEEP="${CCC_LIVE_BACKUPS_KEEP:-5}"
case "$KEEP" in
  ''|*[!0-9]*) KEEP=5 ;;
esac
STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
LOG="$STATE_DIR/live-backups-rotate.log"
ROOTS="${CCC_LIVE_BACKUPS_ROOTS:-/root/ccc-node-live-backups ${HOME:-/root}/ccc-node-live-backups /home/*/ccc-node-live-backups}"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
mkdir -p "$STATE_DIR" 2>/dev/null

pruned=0
failed=0
seen=" "
# Intentional word splitting: ROOTS may hold a glob (/home/*/...) and a
# test-injected list. Candidates that do not exist are skipped below.
for lb in $ROOTS; do
  [ -d "$lb" ] || continue
  # De-duplicate (e.g. /root == $HOME on root-homed nodes processed the same
  # tree twice in the legacy script).
  case "$seen" in *" $lb "*) continue ;; esac
  seen="$seen$lb "
  while IFS= read -r d; do
    # Only ever remove a direct child directory of the backup root.
    case "$d" in
      "$lb"/*/) [ -d "$d" ] || continue ;;
      *) continue ;;
    esac
    if rm -rf -- "$d"; then
      pruned=$((pruned + 1))
    else
      failed=$((failed + 1))
    fi
  done < <(ls -1dt "$lb"/*/ 2>/dev/null | tail -n +$((KEEP + 1)))
done

printf '%s pruned=%d failed=%d keep=%d\n' "$(ts)" "$pruned" "$failed" "$KEEP" >> "$LOG" 2>/dev/null
[ "$failed" -eq 0 ]
