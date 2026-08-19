#!/usr/bin/env bash
# Install a periodic memory-cache warming cron for ccc-node.
#
# The memory prefetch snapshot (~/.claude/hooks/cache/wiki.txt + honcho.txt) is
# normally refreshed only by the SessionStart hook (load-memory -> detached
# refresh-memory). On nodes that idle between Claude sessions (bridge / A2A
# hosts) the snapshot can go stale, so the FIRST session after a long idle
# injects a stale snapshot before the next background refresh catches up. This
# installer adds a crontab entry that runs refresh-memory.sh on a schedule to
# keep the snapshot warm.
#
# Consistent with install-agent-cron-systemd.sh: SAFE BY DEFAULT (dry-run unless
# --apply), idempotent (a single marker-tagged line), never prints secrets, and
# the harness setup.sh never installs this itself. The managed entry carries a
# `gen=h_<sha256:12>` stamp over this script plus the shared cron-installer lib
# (#1081, inputs owned by ccc_installer_gen_inputs) so ccc-doctor can tell when
# the installed entry was rendered by older code.
#
# The managed unit is a BEGIN/END block (unified removal strategy, #1077):
# scripts/lib/installer-cron-common.sh owns block parsing and the whole
# install/remove flow; legacy bare marker lines are migrated into a block on
# the next apply.
#
# The cron entry runs through `bash -lc` so the login profile PATH is loaded;
# refresh-memory.sh shells out to python3/jq/curl/wiki-agent, which a bare cron
# PATH (especially on Termux, which has no /usr/bin) would not resolve.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SELF="$ROOT/scripts/install-memory-refresh-cron.sh"
CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
REFRESH="${CCC_REFRESH_MEMORY_CMD:-$CLAUDE_DIR/hooks/refresh-memory.sh}"
SCHEDULE="${CCC_MEMORY_REFRESH_CRON:-*/30 * * * *}"
LOG="${CCC_MEMORY_REFRESH_CRON_LOG:-$STATE_DIR/refresh-memory.cron.log}"
CRONTAB="${CCC_CRONTAB_CMD:-crontab}"
MARKER="# ccc-node:memory-refresh"
BLOCK_BEGIN="# ccc-node:memory-refresh:begin"
BLOCK_END="# ccc-node:memory-refresh:end"
APPLY=0
REMOVE=0

# Shared installer libs (#1081, #1077): gen stamps + records, and the common
# crontab install/remove driver.
GEN_STAMP_LIB="$ROOT/scripts/lib/installer-gen-stamp.sh"
CRON_COMMON_LIB="$ROOT/scripts/lib/installer-cron-common.sh"
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
Usage: install-memory-refresh-cron.sh [--dry-run|--apply] [--remove] [--schedule SPEC]

Installs (or removes) a crontab entry that runs refresh-memory.sh to keep the
memory prefetch snapshot warm on idle nodes. Defaults to dry-run; --apply is
required to change the crontab. Idempotent: re-running replaces the managed
"$BLOCK_BEGIN" .. "$BLOCK_END" block
(and migrates any legacy bare "$MARKER" line into it).

Options:
  --dry-run        Show the resulting crontab without changing it (default).
  --apply          Write the crontab change.
  --remove         Remove the managed entry (with --apply) instead of adding it.
  --schedule SPEC  Cron schedule (5 fields). Default: "$SCHEDULE".

Env overrides: CCC_CLAUDE_DIR, CCC_STATE_DIR, CCC_REFRESH_MEMORY_CMD,
CCC_MEMORY_REFRESH_CRON, CCC_MEMORY_REFRESH_CRON_LOG, CCC_CRONTAB_CMD.
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

# The warming command. bash -lc loads the login PATH so python3/jq/curl/wiki-agent resolve.
CRON_LINE="$SCHEDULE bash -lc 'CCC_CLAUDE_DIR=\"$CLAUDE_DIR\" \"$REFRESH\"' >> \"$LOG\" 2>&1  $MARKER gen=$GEN"

if [ "$APPLY" = 1 ] && [ "$REMOVE" != 1 ]; then
  # Ensure the log directory exists before cron fires. The cron line redirects
  # to "$LOG" (under STATE_DIR); if that dir is absent, /bin/sh fails to open
  # the redirect and the warming refresh never runs. refresh-memory.sh creates
  # it internally, but that is too late — the redirect is set up first.
  mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
fi

ccc_cron_installer_finish \
  --label "memory-refresh" \
  --marker "$MARKER" --begin "$BLOCK_BEGIN" --end "$BLOCK_END" \
  --crontab "$CRONTAB" --state-dir "$STATE_DIR" --self "$SELF" --gen "$GEN" \
  --apply "$APPLY" --remove "$REMOVE" --schedule-desc "$SCHEDULE" \
  --body "$CRON_LINE" -- \
  --apply --schedule "$SCHEDULE"
