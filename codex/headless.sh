#!/usr/bin/env bash
# ccc-codex-headless — ephemeral, non-interactive Codex runner for agent-cron.
#
# The default is deliberately read-only. Operators must explicitly set
# CCC_CODEX_SANDBOX=workspace-write or danger-full-access for broader access.
set -uo pipefail

PROMPT="${1:-}"
if [ -z "$PROMPT" ]; then
  echo "usage: ccc-codex-headless <prompt>" >&2
  exit 2
fi

BIN="${CCC_CODEX_BIN:-codex}"
SANDBOX="${CCC_CODEX_SANDBOX:-read-only}"
MODEL="${CCC_CODEX_MODEL:-${CCC_MODEL:-}}"
REASONING="${CCC_CODEX_REASONING_EFFORT:-}"
PROFILE="${CCC_CODEX_PROFILE:-}"
WORKDIR="${CCC_CODEX_WORKDIR:-$PWD}"

case "$SANDBOX" in
  read-only|workspace-write|danger-full-access) ;;
  *) echo "ccc-codex-headless: invalid CCC_CODEX_SANDBOX: $SANDBOX" >&2; exit 2 ;;
esac
case "$REASONING" in
  ''|none|minimal|low|medium|high|xhigh) ;;
  *) echo "ccc-codex-headless: invalid CCC_CODEX_REASONING_EFFORT: $REASONING" >&2; exit 2 ;;
esac

command -v "$BIN" >/dev/null 2>&1 || {
  echo "ccc-codex-headless: '$BIN' not found in PATH" >&2
  exit 127
}
[ -d "$WORKDIR" ] || {
  echo "ccc-codex-headless: workdir does not exist: $WORKDIR" >&2
  exit 2
}

OUT="$(mktemp "${TMPDIR:-/tmp}/ccc-codex-headless.XXXXXX.out")"
EVENTS="$(mktemp "${TMPDIR:-/tmp}/ccc-codex-headless.XXXXXX.jsonl")"
ERR="$(mktemp "${TMPDIR:-/tmp}/ccc-codex-headless.XXXXXX.err")"
trap 'rm -f "$OUT" "$EVENTS" "$ERR"' EXIT

args=(
  exec
  --ephemeral
  --json
  --color never
  --sandbox "$SANDBOX"
  -c 'approval_policy="never"'
  -C "$WORKDIR"
  --output-last-message "$OUT"
)
[ -n "$PROFILE" ] && args+=(-p "$PROFILE")
[ -n "$MODEL" ] && args+=(-m "$MODEL")
[ -n "$REASONING" ] && args+=(-c "model_reasoning_effort=\"$REASONING\"")

"$BIN" "${args[@]}" "$PROMPT" >"$EVENTS" 2>"$ERR"
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "ccc-codex-headless: codex exited $rc" >&2
  cat "$ERR" >&2
  exit "$rc"
fi

if [ -s "$OUT" ]; then
  cat "$OUT"
  exit 0
fi

# Older Codex builds may not populate --output-last-message. Keep a bounded,
# structured fallback instead of returning the whole JSONL event stream.
if command -v jq >/dev/null 2>&1; then
  jq -r -s '
    [ .[]
      | select(.type == "item.completed")
      | select(.item.type == "agent_message")
      | .item.text
    ] | last // empty
  ' "$EVENTS"
else
  echo "ccc-codex-headless: no final message and jq is unavailable" >&2
  exit 1
fi
