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
# the harness setup.sh never installs this itself.
#
# The cron entry runs through `bash -lc` so the login profile PATH is loaded;
# refresh-memory.sh shells out to python3/jq/curl/wiki-agent, which a bare cron
# PATH (especially on Termux, which has no /usr/bin) would not resolve.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
REFRESH="${CCC_REFRESH_MEMORY_CMD:-$CLAUDE_DIR/hooks/refresh-memory.sh}"
SCHEDULE="${CCC_MEMORY_REFRESH_CRON:-*/30 * * * *}"
LOG="${CCC_MEMORY_REFRESH_CRON_LOG:-$STATE_DIR/refresh-memory.cron.log}"
CRONTAB="${CCC_CRONTAB_CMD:-crontab}"
MARKER="# ccc-node:memory-refresh"
APPLY=0
REMOVE=0

usage() {
  cat <<EOF
Usage: install-memory-refresh-cron.sh [--dry-run|--apply] [--remove] [--schedule SPEC]

Installs (or removes) a crontab entry that runs refresh-memory.sh to keep the
memory prefetch snapshot warm on idle nodes. Defaults to dry-run; --apply is
required to change the crontab. Idempotent: re-running replaces the single
"$MARKER" line.

Options:
  --dry-run        Show the resulting crontab without changing it (default).
  --apply          Write the crontab change.
  --remove         Remove the managed entry (with --apply) instead of adding it.
  --schedule SPEC  Cron schedule (5 fields). Default: "$SCHEDULE".

Env overrides: CCC_CLAUDE_DIR, CCC_STATE_DIR, CCC_REFRESH_MEMORY_CMD,
CCC_MEMORY_REFRESH_CRON, CCC_MEMORY_REFRESH_CRON_LOG, CCC_CRONTAB_CMD.
EOF
}

need_val() { [ -n "${2:-}" ] || { echo "$1 requires a value" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) APPLY=0 ;;
    --apply) APPLY=1 ;;
    --remove) REMOVE=1 ;;
    --schedule) need_val "$1" "${2:-}"; SCHEDULE="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if ! command -v "${CRONTAB%% *}" >/dev/null 2>&1; then
  echo "crontab command not found ('$CRONTAB'); cannot manage cron on this node" >&2
  exit 3
fi

# The warming command. bash -lc loads the login PATH so python3/jq/curl/wiki-agent resolve.
CRON_LINE="$SCHEDULE bash -lc 'CCC_CLAUDE_DIR=\"$CLAUDE_DIR\" \"$REFRESH\"' >> \"$LOG\" 2>&1  $MARKER"

current="$("$CRONTAB" -l 2>/dev/null || true)"
without_marker="$(printf '%s\n' "$current" | grep -vF "$MARKER" || true)"

if [ "$REMOVE" = 1 ]; then
  desired="$without_marker"
  action="remove"
else
  desired="$(printf '%s\n%s' "$without_marker" "$CRON_LINE" | sed '/^$/d')"
  action="install"
fi

if [ "$APPLY" = 1 ]; then
  # Ensure the log directory exists before cron fires. The cron line redirects
  # to "$LOG" (under STATE_DIR); if that dir is absent, /bin/sh fails to open the
  # redirect and the warming refresh never runs. refresh-memory.sh creates it
  # internally, but that is too late — the redirect is set up first.
  if [ "$REMOVE" != 1 ]; then
    mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
  fi
  printf '%s\n' "$desired" | "$CRONTAB" -
  echo "memory-refresh cron: ${action} done (schedule: ${SCHEDULE})"
else
  echo "[dry-run] would ${action} memory-refresh cron (schedule: ${SCHEDULE}); pass --apply to write"
  echo "[dry-run] resulting crontab:"
  printf '%s\n' "$desired" | sed 's/^/  /'
fi
