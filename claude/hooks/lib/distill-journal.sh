#!/usr/bin/env bash
# shellcheck disable=SC2034 # CCC_CHECK_* results are consumed by sourcing diagnostics.
# Shared read-only journal selection for the distill/memory diagnostics (#1703).
# Installed with the hook tree; never source the bridge credential .env.
ccc_check_distill_journal() {
  local lib_dir provider repo repo_file
  lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || return 1
  # shellcheck source=claude/hooks/nunchi/feed-common.sh
  . "$lib_dir/../nunchi/feed-common.sh" || return 1
  BOT_DATA_DIR="${BOT_DATA_DIR:-${PROJECT_ROOT:-$PWD}/.telegram_bot}"
  repo="$(cd "$lib_dir/../../.." && pwd)" || return 1
  if [ ! -f "$repo/bridge/start.sh" ]; then
    # setup records the source checkout for installed hooks. Read only its
    # path; no code or environment file is evaluated.
    repo_file="${CCC_CLAUDE_DIR:-${HOME:-/root}/.claude}/self-update.repo"
    repo=""
    if [ -f "$repo_file" ] && [ ! -L "$repo_file" ]; then
      IFS= read -r repo < "$repo_file" || :
    fi
  fi
  provider="$(nunchi_runtime_provider "${repo:+$repo/bridge/.env}")"
  CCC_CHECK_JOURNAL_PROVIDER="$provider"
  CCC_CHECK_JOURNAL_SOURCE=provider_default
  if [ -n "${CCC_DISTILL_JOURNAL_DIR:-}" ]; then
    CCC_CHECK_JOURNAL="$CCC_DISTILL_JOURNAL_DIR"
    CCC_CHECK_JOURNAL_SOURCE=override
  else
    # Match bridge/__main__.py's DistillJournal construction; all other
    # runtime providers share the ordinary journal.
    case "$provider" in
      danso) CCC_CHECK_JOURNAL="$BOT_DATA_DIR/danso-distill-journal" ;;
      *) CCC_CHECK_JOURNAL="$BOT_DATA_DIR/distill-journal" ;;
    esac
  fi
  CCC_CHECK_JOURNAL_STATUS=present
  CCC_CHECK_JOURNAL_REASON=journal_present
  if [ -L "$CCC_CHECK_JOURNAL" ] || { [ -e "$CCC_CHECK_JOURNAL" ] && [ ! -d "$CCC_CHECK_JOURNAL" ]; }; then
    CCC_CHECK_JOURNAL_STATUS=degraded
    CCC_CHECK_JOURNAL_REASON=unsafe_journal_root
  elif [ ! -e "$CCC_CHECK_JOURNAL" ]; then
    CCC_CHECK_JOURNAL_STATUS=missing
    CCC_CHECK_JOURNAL_REASON=journal_missing
  elif [ ! -r "$CCC_CHECK_JOURNAL" ] || [ ! -x "$CCC_CHECK_JOURNAL" ]; then
    CCC_CHECK_JOURNAL_STATUS=degraded
    CCC_CHECK_JOURNAL_REASON=journal_unreadable
  fi
}

# Every channel journal present on this node (#2720), one path per line.
# ccc_check_distill_journal picks ONE journal from BOT_DATA_DIR, which only a
# bridge session inherits. Run from ssh/cron/a fleet sweep it falls back to the
# Telegram data dir, so on a node that retired Telegram a failing hande/Matrix
# journal was never inspected (fleet sweep, 2026-11-09). These are the locations
# ccc_doctor.py and nunchi ingest-cron.sh already scan. An explicit
# CCC_DISTILL_JOURNAL_DIR keeps the old single-journal scope. Call after
# ccc_check_distill_journal (it settles BOT_DATA_DIR). Read-only: nothing is
# created, and a symlinked root is listed so the caller can report it unsafe.
ccc_check_distill_journal_candidates() {
  local home dir name path seen=$'\n'
  if [ -n "${CCC_DISTILL_JOURNAL_DIR:-}" ]; then
    printf '%s\n' "$CCC_DISTILL_JOURNAL_DIR"
    return 0
  fi
  home="${HOME:-/root}"
  for dir in "${BOT_DATA_DIR:-}" "$home/.telegram_bot" "$home/.ccc-matrix" \
      "$home/.ccc-hande" "${PROJECT_ROOT:+$PROJECT_ROOT/.telegram_bot}"; do
    [ -n "$dir" ] || continue
    for name in distill-journal danso-distill-journal; do
      path="${dir%/}/$name"
      case "$seen" in *$'\n'"$path"$'\n'*) continue ;; esac
      seen="$seen$path"$'\n'
      if [ -d "$path" ] || [ -L "$path" ]; then
        printf '%s\n' "$path"
      fi
    done
  done
}

# Ownership of one channel journal (#2720), shared by the memory and distill
# checks. A channel frontend rewrites health.json in its own data dir every
# watchdog tick; a journal whose health.json is missing or older than
# <live_sec> has no live bridge to retry or prune it. An explicit
# CCC_DISTILL_JOURNAL_DIR is the operator's chosen scope: `override`.
# Usage: ccc_check_distill_journal_owner <journal> <now_epoch> <live_sec>
# Prints "<live|orphaned|override> <health_age_seconds|-1>". Read-only.
ccc_check_distill_journal_owner() {
  local health ts age
  if [ -n "${CCC_DISTILL_JOURNAL_DIR:-}" ]; then
    printf 'override -1'
    return 0
  fi
  health="$(dirname "$1")/health.json"
  ts=0
  [ -f "$health" ] && ts="$(date -u -r "$health" +%s 2>/dev/null || printf '0')"
  case "$ts" in ''|*[!0-9]*) ts=0 ;; esac
  if [ "$ts" = 0 ]; then
    printf 'orphaned -1'
    return 0
  fi
  age=$(( $2 - ts ))
  [ "$age" -ge 0 ] || age=0
  if [ "$age" -le "$3" ]; then
    printf 'live %s' "$age"
  else
    printf 'orphaned %s' "$age"
  fi
}
