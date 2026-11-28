#!/usr/bin/env bash
# Install the Termux DNS health watch cron (ccc-node#2937). Opt-in, Termux
# (Android) nodes only: it watches for the Android system resolver failing
# while raw DNS to a public resolver works — the 2026-11-28 incident where the
# Tailscale app's in-VPN DNS stalled and every app's name lookup failed for
# ~5 hours. Detect + alert only; it never reconnects the VPN.
#
# The cron line runs scripts/termux-dns-health-watch.sh from THIS checkout
# every 5 minutes; the watch writes its own per-run log line and state under
# CCC_STATE_DIR and pushes one alert (and one recovery notice) to the push
# spool. The cron log only collects unexpected stderr plus the exit code.
#
# Same shape as the sibling cron installers: dry-run by default, BEGIN/END
# block (#1077), gen stamp over script + shared cron lib (#1081), install
# record, --remove.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SELF_DIR/install-termux-dns-health-watch-cron.sh"
CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
WATCH="${CCC_DNS_WATCH_CMD:-$SELF_DIR/termux-dns-health-watch.sh}"
SCHEDULE="${CCC_DNS_WATCH_CRON:-2-59/5 * * * *}"
LOG="${CCC_DNS_WATCH_CRON_LOG:-$STATE_DIR/dns-health-watch.cron.log}"
CRONTAB="${CCC_CRONTAB_CMD:-crontab}"
MARKER="# ccc-node:dns-health-watch"
BLOCK_BEGIN="# ccc-node:dns-health-watch:begin"
BLOCK_END="# ccc-node:dns-health-watch:end"
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
Usage: install-termux-dns-health-watch-cron.sh [--dry-run|--apply] [--remove] [--schedule SPEC]

Installs (or removes) a 5-minute crontab entry that runs
scripts/termux-dns-health-watch.sh from this checkout (Termux nodes only) and
appends its exit code to the log. Defaults to dry-run; --apply is required to
change the crontab. Idempotent: re-running replaces the managed
"$BLOCK_BEGIN" .. "$BLOCK_END" block (and migrates any legacy bare
"$MARKER" line into it).

Options:
  --dry-run        Show the resulting crontab without changing it (default).
  --apply          Write the crontab change.
  --remove         Remove the managed entry (with --apply) instead of adding it.
  --schedule SPEC  Cron schedule (5 fields). Default: "$SCHEDULE".

Env overrides: CCC_CLAUDE_DIR, CCC_STATE_DIR, CCC_DNS_WATCH_CMD,
CCC_DNS_WATCH_CRON, CCC_DNS_WATCH_CRON_LOG, CCC_CRONTAB_CMD.
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

CRON_LINE="$SCHEDULE bash -lc 'CCC_STATE_DIR=\"$STATE_DIR\" bash \"$WATCH\"; echo \"dns-health-watch rc=\$?\"' >> \"$LOG\" 2>&1  $MARKER gen=$GEN"

if [ "$APPLY" = 1 ] && [ "$REMOVE" != 1 ]; then
  # Same redirect-first failure mode as the sibling installers: the cron line
  # appends to "$LOG" (under STATE_DIR); create the directory now.
  mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
fi

ccc_cron_installer_finish \
  --label "dns-health-watch" \
  --marker "$MARKER" --begin "$BLOCK_BEGIN" --end "$BLOCK_END" \
  --crontab "$CRONTAB" --state-dir "$STATE_DIR" --self "$SELF" --gen "$GEN" \
  --apply "$APPLY" --remove "$REMOVE" --schedule-desc "$SCHEDULE" \
  --body "$CRON_LINE" -- \
  --apply --schedule "$SCHEDULE"
