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
#   CCC_MODEL           model override passed as --model; optional
#   CCC_CLAUDE_BIN      claude binary (default: claude)
#   CCC_HEADLESS_TIMEOUT wall-clock cap in seconds (default: 1500; 0 disables).
#                       Guards against a run that never returns (e.g. an unbounded
#                       polling loop in generated Bash). Exit 124 on timeout.
#   CCC_HEADLESS_FAIL_STDOUT_BYTES  on a failed run, how many bytes of the captured
#                       stdout to echo to stderr for diagnosis (default: 2000; 0 disables).
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
[ -n "${CCC_MODEL:-}" ] && args+=(--model "$CCC_MODEL")

ERRF="$(mktemp "${TMPDIR:-/tmp}"/ccc-headless.XXXXXX.err)"
trap 'rm -f "$ERRF"' EXIT

# Wall-clock guard: a generated unbounded loop (e.g. `until <cond>; do sleep 5; done`)
# otherwise hangs the caller forever. Skipped when coreutils `timeout` is absent.
TMO="${CCC_HEADLESS_TIMEOUT:-1500}"
runner=("$BIN")
case "$TMO" in
  0|'') ;;
  *[!0-9]*) echo "ccc-headless: invalid CCC_HEADLESS_TIMEOUT: $TMO" >&2; exit 2 ;;
  *) command -v timeout >/dev/null 2>&1 && runner=(timeout -k 30 "$TMO" "$BIN") ;;
esac

if [ ! -t 0 ]; then
  RESP="$(cat | "${runner[@]}" "${args[@]}" 2>"$ERRF")"
else
  RESP="$("${runner[@]}" "${args[@]}" 2>"$ERRF")"
fi
rc=$?

# 실패 경로에서 stdout 을 버리면 진단 근거가 통째로 사라진다. `claude
# --output-format json` 은 오류 본문도 stdout 으로 내는데, 그 stdout 은 위에서
# $RESP 에 담겨 있고 stderr 는 비어 있는 경우가 많다. 실제로 agent-cron 의
# prompt 태스크 4건(2026-09-11~18)이 "claude exited 1" 한 줄만 남기고 실패해
# 사후 규명이 불가능했다. 그래서 실패 시 stdout 앞부분을 stderr 로 함께 낸다.
emit_failure_context() {
  cat "$ERRF" >&2
  local cap="${CCC_HEADLESS_FAIL_STDOUT_BYTES:-2000}"
  case "$cap" in
    ''|*[!0-9]*) cap=2000 ;;
  esac
  [ "$cap" -eq 0 ] && return 0
  if [ -n "$RESP" ]; then
    echo "ccc-headless: stdout (first ${cap}B of $(printf '%s' "$RESP" | wc -c)B):" >&2
    printf '%s' "$RESP" | head -c "$cap" >&2
    echo >&2
  else
    echo "ccc-headless: stdout was empty" >&2
  fi
}

if [ "$rc" -eq 124 ]; then
  echo "ccc-headless: $BIN exceeded CCC_HEADLESS_TIMEOUT=${TMO}s and was killed" >&2
  emit_failure_context
  exit 124
fi

if [ "$rc" -ne 0 ]; then
  echo "ccc-headless: $BIN exited $rc" >&2
  emit_failure_context
  exit "$rc"
fi

SID="$(printf '%s' "$RESP" | jq -r '.session_id // empty' 2>/dev/null)"
COST="$(printf '%s' "$RESP" | jq -r '(.total_cost_usd // .cost.total_cost_usd) // empty' 2>/dev/null)"
echo "ccc-headless: session=${SID:-?} cost=\$${COST:-?} tools=[$ALLOWED]" >&2

# Emit the text result if present, else the raw payload (so callers always get something).
printf '%s' "$RESP" | jq -e '.result' >/dev/null 2>&1 \
  && printf '%s\n' "$(printf '%s' "$RESP" | jq -r '.result')" \
  || printf '%s\n' "$RESP"
