#!/usr/bin/env bash
# Compatibility wrapper for resume-only local distill writes.
# Invalid/empty input and transaction failures remain fail-open for hooks.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || exit 0
input="$(cat 2>/dev/null)"
[ -n "$input" ] || exit 0

printf '%s' "$input" | python3 "$HERE/local-memory-commit.py" --mode resume || exit 0
