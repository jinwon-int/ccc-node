#!/usr/bin/env bash
# Install (or remove) the daily t2-starvation-observe cron (#2024 acceptance).
#
# scripts/t2-starvation-observe.sh reads each broker's sqlite read-only and
# appends one JSON line per run to the local observation log: T1 counts from
# the local broker DB, T2 counts over SSH from gwakga's. The line feeds the
# #2024 skills-intake T2-starvation acceptance analysis; the script
# self-expires after 2026-09-08 00:00 KST and exits 0 afterwards, so the
# entry can outlive the observation window without noise.
#
# SEOSEO-ONLY BY PRECONDITION, not by node list: the observer needs a local
# T1 broker DB and SSH reachability to the gwakga broker, and the installer
# refuses to install anywhere both are absent. On every other node it is a
# documented no-op (exit 0), so a fleet-wide self-update reapply stays clean
# and the one node that matters keeps a managed, drift-checked entry instead
# of the hand-added bare line this installer migrates (the #1077/#1081
# managed-block pattern: gen stamp, install record, legacy bare-marker
# migration).
#
# Consistent with the other cron installers: SAFE BY DEFAULT (dry-run unless
# --apply), idempotent BEGIN/END block, never prints secrets, and setup.sh
# never installs it on its own.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SELF_DIR/install-t2-starvation-observe-cron.sh"
CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
OBSERVE_CMD="${CCC_T2_OBSERVE_CMD:-$SELF_DIR/t2-starvation-observe.sh}"
OBSERVE_LOG="${CCC_T2_OBSERVE_LOG:-$STATE_DIR/t2-starvation-observe.log}"
T1_DB="${CCC_T2_OBSERVE_T1_DB:-/var/lib/a2a-broker/state.sqlite}"
T2_HOST="${CCC_T2_OBSERVE_T2_HOST:-gwakga}"
# 09:00 KST: each run closes a full UTC day window (the script windows on
# now-24h), early enough to land before the day's triage reads the sample.
SCHEDULE="${CCC_T2_OBSERVE_CRON:-0 9 * * *}"
LOG="$OBSERVE_LOG"
CRONTAB="${CCC_CRONTAB_CMD:-crontab}"
MARKER="# ccc-node:t2-starvation-observe"
BLOCK_BEGIN="# ccc-node:t2-starvation-observe:begin"
BLOCK_END="# ccc-node:t2-starvation-observe:end"
APPLY=0
REMOVE=0

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
Usage: install-t2-starvation-observe-cron.sh [--dry-run|--apply] [--remove] [--schedule SPEC]

Installs (or removes) the daily t2-starvation-observe entry (#2024). The
observer is read-only over both brokers' sqlite (T1 local, T2 via SSH to
gwakga) and appends one JSON sample per run until its self-expiry.

The installer is a no-op on nodes without the T1 broker DB
("$T1_DB") or without SSH reachability to "$T2_HOST" — the
observer cannot run there, so nothing is scheduled.

Options:
  --dry-run        Show the resulting crontab without changing it (default).
  --apply          Write the crontab change (preconditions are checked first).
  --remove         Remove the managed entry (with --apply) instead of adding it.
  --schedule SPEC  Cron schedule (5 fields). Default: "$SCHEDULE".

Env overrides: CCC_CLAUDE_DIR, CCC_STATE_DIR, CCC_T2_OBSERVE_CMD,
CCC_T2_OBSERVE_LOG, CCC_T2_OBSERVE_T1_DB, CCC_T2_OBSERVE_T2_HOST,
CCC_T2_OBSERVE_CRON, CCC_CRONTAB_CMD.
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

CRON_LINE="$SCHEDULE bash '$OBSERVE_CMD' >> \"$OBSERVE_LOG\" 2>&1  $MARKER gen=$GEN"

if [ "$APPLY" = 1 ] && [ "$REMOVE" != 1 ]; then
  # Preconditions before touching the crontab: no T1 broker DB or no T2 SSH
  # path means the observer cannot produce samples here. Exit 0 (not an
  # error) so fleet-wide reapplies stay clean on the 11 nodes that are not
  # seoseo.
  if [ ! -r "$T1_DB" ]; then
    echo "skip: no local T1 broker DB at $T1_DB — t2-starvation-observe only runs where a local broker lives"
    exit 0
  fi
  if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$T2_HOST" true 2>/dev/null; then
    echo "skip: T2 host '$T2_HOST' unreachable — t2-starvation-observe requires gwakga SSH access"
    exit 0
  fi
  # The entry appends to "$LOG" under STATE_DIR; create the directory now
  # rather than when the job first fires (same as the sibling installers).
  mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
fi

ccc_cron_installer_finish \
  --label "t2-starvation-observe" \
  --marker "$MARKER" --begin "$BLOCK_BEGIN" --end "$BLOCK_END" \
  --crontab "$CRONTAB" --state-dir "$STATE_DIR" --self "$SELF" --gen "$GEN" \
  --apply "$APPLY" --remove "$REMOVE" --schedule-desc "$SCHEDULE" \
  --body "$CRON_LINE" -- \
  --apply --schedule "$SCHEDULE"
