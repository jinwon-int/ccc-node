#!/usr/bin/env bash
# ccc-fts5-check.sh — health check / statistics for the local FTS5 index.
# Respects CCC_STATE_DIR.
# Returns ok JSON even for honcho profile (shows index absent).
# Usage: ccc-fts5-check.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
python3 "$HERE/ccc-fts5-index.py" check
