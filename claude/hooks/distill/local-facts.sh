#!/usr/bin/env bash
# Compatibility wrapper for facts-only local distill writes.
# The Python committer owns rendering, the two-target lock, the rollback
# pre-image, and the body-free ledger.  Fail-open remains the hook contract.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || exit 0
STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
[ -f "$STATE_DIR/distill.disabled" ] && { echo "local-facts skipped: disabled"; exit 0; }

input="$(cat 2>/dev/null)"
[ -n "$input" ] || { echo "local-facts: no input"; exit 0; }

printf '%s' "$input" | python3 "$HERE/local-memory-commit.py" --mode facts || exit 0
