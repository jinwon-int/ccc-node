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
