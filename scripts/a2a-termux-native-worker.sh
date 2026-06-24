#!/usr/bin/env bash
# Validate or launch the Termux-native A2A worker harness.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$ROOT/scripts/a2a_termux_native_worker.py" "$@"
