#!/usr/bin/env bash
# ccc-memory-search compatibility wrapper — implementation lives beside this file.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/ccc_memory_search.py" "$@"
