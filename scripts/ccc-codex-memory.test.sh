#!/usr/bin/env bash
# Hermetic unit tests for the Codex memory materializer (#419).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$ROOT/scripts/ccc_codex_memory_test.py"
