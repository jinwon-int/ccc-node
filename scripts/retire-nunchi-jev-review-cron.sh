#!/usr/bin/env bash
# Retire the nunchi Jev kind-review cron line on this node (#2905).
#
# `claude/hooks/nunchi/jev-review.py` is the opt-in advisory consumer described
# in docs/memory-pipeline-repair.md. It was installed by hand as a crontab line
# (no installer renders it, so no `--remove` existed). Measured on 2026-11-25
# it had run roughly 9,300 times per node while the narrow external boundary
# (technical-topic screen, sensitive-topic rejection, identifier redaction,
# closed vocabulary) let almost nothing through: three Jev attempts across the
# whole fleet. Its results are advisory only (`~/.nunchi/jev-review.db`), so
# turning it off changes no memory collection.
#
# What this does:
#   - finds ACTIVE crontab lines that run `hooks/nunchi/jev-review.py`;
#   - with --apply, comments each one out behind the marker
#     `#jev-review-retired-YYYYMMDD# ` (the line itself is kept, so the exact
#     original can be restored), after saving the old crontab to a private
#     backup under the state dir;
#   - with --restore --apply, removes that marker again (rollback).
# Already-commented lines (for example an older `#unified-…#` disable) are left
# alone. The review database and log are never touched: they stay as evidence.
#
# SAFE BY DEFAULT: dry-run unless --apply. Idempotent. Prints no secrets (the
# matched lines carry only paths and the NUNCHI_JEV_REVIEW flag).
#
# Exit codes: 0 ok (including "nothing to do"), 2 usage, 3 crontab command
# missing, 4 backup or crontab write failed.
set -euo pipefail

CRONTAB="${CCC_CRONTAB_CMD:-crontab}"
CLAUDE_DIR="${CCC_CLAUDE_DIR:-$HOME/.claude}"
STATE_DIR="${CCC_STATE_DIR:-$CLAUDE_DIR/state}"
STAMP="${CCC_JEV_REVIEW_RETIRE_DATE:-$(date +%Y%m%d)}"
NEEDLE="hooks/nunchi/jev-review.py"
MARKER_RE='^#jev-review-retired-[0-9]{8}# '
APPLY=0
MODE=retire

usage() {
  cat <<EOF
Usage: retire-nunchi-jev-review-cron.sh [--dry-run|--apply] [--restore] [--status]

Comments out the crontab line(s) that run $NEEDLE behind the marker
"#jev-review-retired-<date># ". Defaults to dry-run; --apply writes the crontab.

Options:
  --dry-run   Show what would change (default).
  --apply     Write the change. The previous crontab is first saved (0600) to
              \$CCC_STATE_DIR/crontab.bak-jev-review-<mode>-<timestamp>.
  --restore   Remove the retirement marker instead (rollback); needs --apply
              to write.
  --status    Print active / retired line counts and exit 0.

Env overrides: CCC_CRONTAB_CMD, CCC_CLAUDE_DIR, CCC_STATE_DIR,
CCC_JEV_REVIEW_RETIRE_DATE (YYYYMMDD stamp for the marker).
EOF
}

STATUS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) APPLY=0 ;;
    --apply) APPLY=1 ;;
    --restore) MODE=restore ;;
    --status) STATUS=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if ! [[ "$STAMP" =~ ^[0-9]{8}$ ]]; then
  echo "CCC_JEV_REVIEW_RETIRE_DATE must be YYYYMMDD" >&2
  exit 2
fi
if ! command -v "$CRONTAB" >/dev/null 2>&1; then
  echo "crontab command not found: $CRONTAB" >&2
  exit 3
fi

# `crontab -l` exits non-zero when the user has no crontab: treat as empty.
current="$("$CRONTAB" -l 2>/dev/null || true)"

is_active_target() { # <line>
  local line="$1"
  case "$line" in
    *"$NEEDLE"*) ;;
    *) return 1 ;;
  esac
  [[ ! "$line" =~ ^[[:space:]]*# ]]
}
is_retired_target() { # <line>
  [[ "$1" =~ $MARKER_RE ]] && [[ "$1" == *"$NEEDLE"* ]]
}

active=0; retired=0
while IFS= read -r line || [ -n "$line" ]; do
  if is_active_target "$line"; then active=$((active + 1)); fi
  if is_retired_target "$line"; then retired=$((retired + 1)); fi
done <<<"$current"

if [ "$STATUS" = 1 ]; then
  echo "jev-review cron: active=$active retired=$retired"
  exit 0
fi

new=""; changed=0
while IFS= read -r line || [ -n "$line" ]; do
  out="$line"
  if [ "$MODE" = retire ] && is_active_target "$line"; then
    out="#jev-review-retired-${STAMP}# $line"
    changed=$((changed + 1))
    echo "- $line"
    echo "+ $out"
  elif [ "$MODE" = restore ] && is_retired_target "$line"; then
    out="$(printf '%s' "$line" | sed -E "s/$MARKER_RE//")"
    changed=$((changed + 1))
    echo "- $line"
    echo "+ $out"
  fi
  new+="$out"$'\n'
done <<<"$current"

if [ "$changed" = 0 ]; then
  echo "nothing to do (mode=$MODE, active=$active, retired=$retired)"
  exit 0
fi
if [ "$APPLY" = 0 ]; then
  echo "dry-run: $changed line(s) would change (mode=$MODE); re-run with --apply"
  exit 0
fi

umask 077
mkdir -p "$STATE_DIR"
backup="$STATE_DIR/crontab.bak-jev-review-$MODE-$(date +%Y%m%dT%H%M%S)"
if ! printf '%s\n' "$current" >"$backup"; then
  echo "could not write backup: $backup" >&2
  exit 4
fi
chmod 600 "$backup"
if ! printf '%s' "$new" | "$CRONTAB" -; then
  echo "crontab write failed; previous crontab saved at $backup" >&2
  exit 4
fi
echo "applied: $changed line(s) (mode=$MODE); backup $backup"
