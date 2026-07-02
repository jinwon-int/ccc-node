#!/usr/bin/env bash
# ccc doctor compatibility wrapper — implementation lives in ccc_doctor.py.
set -euo pipefail
SCRIPT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$SCRIPT_ROOT/scripts/ccc_doctor.py" "$@"
