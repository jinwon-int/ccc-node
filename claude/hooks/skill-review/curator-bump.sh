#!/usr/bin/env bash
# PostToolUse(Skill) → curator telemetry bump (#752).
# Contract: ALWAYS exit 0. Telemetry must never block/delay a foreground
# skill invocation — any failure is swallowed after a best-effort bump.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
CURATOR="$HERE/curator.py"

payload="$(cat 2>/dev/null)" || exit 0
[ -n "$payload" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
[ -f "$CURATOR" ] || exit 0

name="$(printf '%s' "$payload" | jq -r '.tool_input.skill // empty' 2>/dev/null)" || exit 0
[ -n "$name" ] || exit 0

python3 "$CURATOR" bump --event use --name "$name" >/dev/null 2>&1 || true
exit 0
