#!/usr/bin/env bash
# ccc-fts5-search.sh — full-text search the local FTS5 memory index.
# Respects CCC_STATE_DIR.
# Default is hard-fail (CCC_MEMORY_PROFILE=honcho has no local index).
# Usage: ccc-fts5-search.sh "<query>" [-n <limit>]
set -euo pipefail

PROFILE="${CCC_MEMORY_PROFILE:-honcho}"

case "$PROFILE" in
  hybrid|max-perf) ;;
  *)
    echo '{"status":"error","message":"CCC_MEMORY_PROFILE='"'$PROFILE'"' - local FTS5 index not available (needs hybrid or max-perf)"}'
    exit 1
    ;;
esac

HERE="$(cd "$(dirname "$0")" && pwd)"
python3 "$HERE/ccc-fts5-index.py" search "$@"
