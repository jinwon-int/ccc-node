#!/usr/bin/env bash
# ccc-memory-index compatibility wrapper — implementation lives beside this file.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/ccc_memory_index.py" "$@"
