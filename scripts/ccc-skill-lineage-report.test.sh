#!/usr/bin/env bash
# Hermetic tests for the read-only skill intake lineage report (#2641). Every
# case builds its own ledger in a temp dir; the live ~/.claude is never read.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$ROOT/scripts/ccc_skill_lineage_report_test.py"
