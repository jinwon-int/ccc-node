#!/usr/bin/env bash
# Install the daily Hermes-style skill-autosave sweep as a crontab entry.
#
# ccc-skill-autosave.sh closes the gap that SessionEnd hooks never fire for
# Telegram-bridge / SDK sessions: it refreshes the skill-candidate report,
# drafts skills from recent transcripts through skill-review.sh, and queues an
# owner-only Telegram notification when drafts await approval.
#
# Consistent with install-memory-refresh-cron.sh: SAFE BY DEFAULT (dry-run
# unless --apply), idempotent (a single marker-tagged line), never prints
# secrets, and the harness setup.sh never installs this itself.
#
# The cron entry runs through `bash -lc` so the login profile PATH is loaded;
# the sweep shells out to jq/find and skill-review.sh shells out to `claude`,
# which a bare cron PATH (especially on Termux) would not resolve.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
AUTOSAVE="${CCC_SKILL_AUTOSAVE_CMD:-$CLAUDE_DIR/hooks/ccc-skill-autosave.sh}"
# Default: daily 20:45 UTC (05:45 KST) — before the operator's morning review.
SCHEDULE="${CCC_SKILL_AUTOSAVE_CRON:-45 20 * * *}"
LOG="${CCC_SKILL_AUTOSAVE_CRON_LOG:-$STATE_DIR/skill-autosave.cron.log}"
CRONTAB="${CCC_CRONTAB_CMD:-crontab}"
MARKER="# ccc-node:skill-autosave"
APPLY=0
REMOVE=0

usage() {
  cat <<EOF
Usage: install-skill-autosave-cron.sh [--dry-run|--apply] [--remove] [--schedule SPEC]

Installs (or removes) a crontab entry that runs ccc-skill-autosave.sh daily:
refresh skill candidates, draft skills from recent transcripts (Telegram
bridge sessions included), and queue an owner Telegram notification when
drafts await approval. Defaults to dry-run; --apply is required to change the
crontab. Idempotent: re-running replaces the single "$MARKER" line.

Options:
  --dry-run        Show the resulting crontab without changing it (default).
  --apply          Write the crontab change.
  --remove         Remove the managed entry (with --apply) instead of adding it.
  --schedule SPEC  Cron schedule (5 fields). Default: "$SCHEDULE".

Env overrides: CCC_CLAUDE_DIR, CCC_STATE_DIR, CCC_SKILL_AUTOSAVE_CMD,
CCC_SKILL_AUTOSAVE_CRON, CCC_SKILL_AUTOSAVE_CRON_LOG, CCC_CRONTAB_CMD.
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

if [ ! -f "$AUTOSAVE" ] && [ -f "$ROOT/scripts/ccc-skill-autosave.sh" ]; then
  AUTOSAVE="$ROOT/scripts/ccc-skill-autosave.sh"
fi

CRON_LINE="$SCHEDULE bash -lc 'CCC_CLAUDE_DIR=\"$CLAUDE_DIR\" \"$AUTOSAVE\" run' >> \"$LOG\" 2>&1  $MARKER"

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
  printf '%s\n' "$desired" | "$CRONTAB" -
  echo "skill-autosave cron: ${action} done (schedule: ${SCHEDULE})"
else
  echo "[dry-run] would ${action} skill-autosave cron (schedule: ${SCHEDULE}); pass --apply to write"
  echo "[dry-run] resulting crontab:"
  printf '%s\n' "$desired" | sed 's/^/  /'
fi
