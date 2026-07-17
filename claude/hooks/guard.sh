#!/usr/bin/env bash
# PreToolUse guard — thin shim delegating to guard.py (issue #452).
#
# The enforcement logic lives in guard.py (real shell tokenization via shlex).
# This shim preserves the historical contract and install surface: the hook still
# invokes `bash .../guard.sh`, stdin carries the PreToolUse payload, exit 2 denies.
#
# Fail-OPEN only when the interpreter/implementation is unavailable — the same
# "availability > enforcement" posture the bash guard used for a missing jq, so a
# node without python3 is not bricked. guard.py itself fails CLOSED on internal
# errors and on every matched gate.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || HERE="${HOME:-/root}/.claude/hooks"
PY="$HERE/guard.py"

if command -v python3 >/dev/null 2>&1 && [ -f "$PY" ]; then
  exec python3 "$PY"
fi

echo "ccc-node guard: python3 or guard.py unavailable — guard NOT enforced (fail-open)." >&2
exit 0
