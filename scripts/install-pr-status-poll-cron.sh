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
# line), never prints secrets, and the harness setup.sh never installs this
# itself. The managed line carries a `gen=h_<sha256:12>` stamp of this
# script's content (#1081) so ccc-doctor can tell when the installed entry was
# rendered by an older installer.
#
# The cron entry runs through `bash -lc` so the login profile PATH is loaded;
# ccc-pr-status-poll.sh shells out to gh/jq, which a bare cron PATH (especially
# on Termux, which has no /usr/bin) would not resolve.
set -euo pipefail

CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
POLL="${CCC_PR_STATUS_POLL_CMD:-$CLAUDE_DIR/hooks/ccc-pr-status-poll.sh}"
SCHEDULE="${CCC_PR_STATUS_POLL_CRON:-*/17 * * * *}"
LOG="${CCC_PR_STATUS_POLL_CRON_LOG:-$STATE_DIR/pr-status-poll.cron.log}"
CRONTAB="${CCC_CRONTAB_CMD:-crontab}"
MARKER="# ccc-node:pr-status-poll"
APPLY=0
REMOVE=0

# Generation stamp (#1081): content hash of this script, pinned into the
# managed cron line so drift between the installed entry and the current
# installer is detectable.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEN_STAMP_LIB="$SELF_DIR/lib/installer-gen-stamp.sh"
if [ ! -r "$GEN_STAMP_LIB" ]; then
  echo "shared gen-stamp library is missing: $GEN_STAMP_LIB" >&2
  exit 4
fi
# shellcheck source=/dev/null
. "$GEN_STAMP_LIB"
GEN="$(ccc_installer_gen_stamp "$SELF_DIR/install-pr-status-poll-cron.sh")"

usage() {
  cat <<EOF
Usage: install-pr-status-poll-cron.sh [--dry-run|--apply] [--remove] [--schedule SPEC]

Installs (or removes) a crontab entry that runs ccc-pr-status-poll.sh so a PR
or issue this node's bridge identity opened gets its state changes (CI done,
closed, merged) noticed and pushed to the owner notification spool, instead
of only being noticed if/when a session happens to re-check it manually.
Defaults to dry-run; --apply is required to change the crontab. Idempotent:
re-running replaces the single "$MARKER" line.

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

CRON_LINE="$SCHEDULE bash -lc 'CCC_CLAUDE_DIR=\"$CLAUDE_DIR\" \"$POLL\" run' >> \"$LOG\" 2>&1  $MARKER gen=$GEN"

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
  if [ "$REMOVE" != 1 ]; then
    mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
  fi
  printf '%s\n' "$desired" | "$CRONTAB" -
  echo "pr-status-poll cron: ${action} done (schedule: ${SCHEDULE})"
else
  echo "[dry-run] would ${action} pr-status-poll cron (schedule: ${SCHEDULE}); pass --apply to write"
  echo "[dry-run] resulting crontab:"
  printf '%s\n' "$desired" | sed 's/^/  /'
fi
