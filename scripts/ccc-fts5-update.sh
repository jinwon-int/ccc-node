#!/usr/bin/env bash
# ccc-fts5-update.sh — rebuild the local FTS5 memory index.
# Respects CCC_STATE_DIR / CCC_MEMORY_DIR / CCC_MEMORY_CACHE_DIR.
# Default is no-op (CCC_MEMORY_PROFILE=honcho); only indexes when
# CCC_MEMORY_PROFILE=hybrid or CCC_MEMORY_PROFILE=max-perf.
# Run as: ccc-fts5-update.sh
set -euo pipefail

PROFILE="${CCC_MEMORY_PROFILE:-honcho}"

case "$PROFILE" in
  hybrid|max-perf) ;;
  *)
    echo '{"status":"skipped","reason":"CCC_MEMORY_PROFILE not hybrid/max-perf","profile":"'"$PROFILE"'"}'
    exit 0
    ;;
esac

HERE="$(cd "$(dirname "$0")" && pwd)"
python3 "$HERE/ccc-fts5-index.py" update
