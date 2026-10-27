#!/usr/bin/env bash
# agent-trust-watch — non-blocking agent hijack warnings (#2538 part 2).
#
# Usage (hook): agent-trust-watch.sh SessionStart|PostToolUse  (event JSON on stdin)
#
# SessionStart: warns the model when the session would load an AGENTS.md /
#   CLAUDE.md that the harness did not write — in a shared ancestor root
#   (/, /tmp, /var/tmp, $PREFIX/tmp, home parent; A2A analysis sessions run
#   under /tmp) or in a download directory outside git — and when endpoint
#   hosts / MCP servers changed since the last session.
# PostToolUse(Bash|Write|Edit|MultiEdit): warns when the tool call changed the
#   security-relevant agent config (Claude settings env *BASE_URL*, MCP servers,
#   Codex base URLs / mcp_servers). Fast path: only the mtimes of the four config
#   files are compared in shell; python runs only when one moved.
#
# Contract: never blocks (TM-1306 keeps blocking guards out), always exit 0,
# fail-open on any error. Emits hookSpecificOutput.additionalContext or
# nothing. Stores only a fingerprint (endpoint scheme://host, MCP name ->
# command basename/host) and mtimes under $CCC_STATE_DIR/agent-trust — never
# file contents or secret values. Off switch: CCC_AGENT_TRUST_WATCH=0.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
# shellcheck source=claude/hooks/lib/hook-common.sh
[ -r "$HERE/lib/hook-common.sh" ] && . "$HERE/lib/hook-common.sh"
command -v is_disabled >/dev/null 2>&1 || is_disabled() { case "${1:-}" in 0|false|FALSE|off|OFF|no|NO) return 0;; *) return 1;; esac; }
umask 077

EVENT="${1:-}"
case "$EVENT" in SessionStart|PostToolUse) ;; *) exit 0 ;; esac
is_disabled "${CCC_AGENT_TRUST_WATCH:-1}" && exit 0
LIB="$HERE/lib/agent_trust.py"
[ -r "$LIB" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

input="$(cat 2>/dev/null)"
STATE="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}/agent-trust"
mkdir -p "$STATE" 2>/dev/null || exit 0

# mtime signature of the files that hold endpoint / MCP config.
H="${HOME:-/root}"
mtimes="$(for f in "$H/.claude/settings.json" "$H/.claude/settings.local.json" "$H/.claude.json" "$H/.codex/config.toml"; do
  printf '%s:%s\n' "$f" "$(stat -c %Y "$f" 2>/dev/null || echo -)"; done)"

if [ "$EVENT" = PostToolUse ]; then
  prev="$(cat "$STATE/mtimes" 2>/dev/null)"
  if [ "$prev" = "$mtimes" ]; then exit 0; fi
  printf '%s\n' "$mtimes" > "$STATE/mtimes" 2>/dev/null
  # First observation on this node: record silently (python seeds the fingerprint).
  if [ -z "$prev" ] && [ ! -f "$STATE/fingerprint.json" ]; then
    timeout 10 python3 "$LIB" --hook PostToolUse --state-dir "$STATE" >/dev/null 2>&1
    exit 0
  fi
  timeout 10 python3 "$LIB" --hook PostToolUse --state-dir "$STATE" 2>/dev/null
  exit 0
fi

cwd=""
if command -v jq >/dev/null 2>&1 && [ -n "$input" ]; then
  cwd="$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null)"
fi
[ -n "$cwd" ] || cwd="$PWD"
printf '%s\n' "$mtimes" > "$STATE/mtimes" 2>/dev/null
timeout 10 python3 "$LIB" --hook SessionStart --cwd "$cwd" --state-dir "$STATE" 2>/dev/null
exit 0
