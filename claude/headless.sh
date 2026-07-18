#!/usr/bin/env bash
# ccc-headless — non-interactive Claude Code runner for cron / A2A / CI.
# Wraps `claude -p` with structured JSON output, a SAFE-by-default tool baseline, and
# session/cost logging. Prints the model's text result to stdout; logs session_id + cost
# to stderr. Exit code mirrors the `claude` exit code.
#
# Non-interactive mode loads the same hooks/settings (audit/redact/notify) as an
# interactive session. Do NOT add --bare for runs that must keep that
# observability: --bare skips hook/plugin/settings auto-discovery entirely.
#
# Usage:
#   ccc-headless.sh "find and summarize TODOs"
#   echo "$DIFF" | ccc-headless.sh "review this diff for bugs"
#   CCC_ALLOWED_TOOLS="Read,Grep,Glob,Bash" ccc-headless.sh "run the tests"
#   CCC_PERMISSION_MODE=dontAsk ccc-headless.sh "locked-down CI run"   # deny anything not allow-listed
#
# Env:
#   CCC_ALLOWED_TOOLS   comma list for --allowedTools (default: Read,Grep,Glob — read-only)
#   CCC_PERMISSION_MODE permission mode baseline (e.g. dontAsk, acceptEdits); optional
#   CCC_CLAUDE_BIN      claude binary (default: claude)
set -uo pipefail

PROMPT="${1:-}"
if [ -z "$PROMPT" ]; then
  echo "usage: ccc-headless.sh <prompt>   (optional data on stdin)" >&2
  exit 2
fi

BIN="${CCC_CLAUDE_BIN:-claude}"
ALLOWED="${CCC_ALLOWED_TOOLS:-Read,Grep,Glob}"
command -v "$BIN" >/dev/null 2>&1 || { echo "ccc-headless: '$BIN' not found in PATH" >&2; exit 127; }

args=(-p "$PROMPT" --output-format json --allowedTools "$ALLOWED")
[ -n "${CCC_PERMISSION_MODE:-}" ] && args+=(--permission-mode "$CCC_PERMISSION_MODE")

ERRF="$(mktemp "${TMPDIR:-/tmp}"/ccc-headless.XXXXXX.err)"
trap 'rm -f "$ERRF"' EXIT

if [ ! -t 0 ]; then
  RESP="$(cat | "$BIN" "${args[@]}" 2>"$ERRF")"
else
  RESP="$("$BIN" "${args[@]}" 2>"$ERRF")"
fi
rc=$?

if [ "$rc" -ne 0 ]; then
  echo "ccc-headless: $BIN exited $rc" >&2
  cat "$ERRF" >&2
  exit "$rc"
fi

SID="$(printf '%s' "$RESP" | jq -r '.session_id // empty' 2>/dev/null)"
COST="$(printf '%s' "$RESP" | jq -r '(.total_cost_usd // .cost.total_cost_usd) // empty' 2>/dev/null)"
echo "ccc-headless: session=${SID:-?} cost=\$${COST:-?} tools=[$ALLOWED]" >&2

# Emit the text result if present, else the raw payload (so callers always get something).
printf '%s' "$RESP" | jq -e '.result' >/dev/null 2>&1 \
  && printf '%s\n' "$(printf '%s' "$RESP" | jq -r '.result')" \
  || printf '%s\n' "$RESP"
