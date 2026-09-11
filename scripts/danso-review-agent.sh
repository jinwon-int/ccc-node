#!/usr/bin/env bash
# danso-review-agent.sh — REVIEW_AGENT_BIN wrapper that lets danso serve the
# a2a intake review/revise stdin contract (#1665).
#
# The handlers execute the reviewer as
#   timeout $SEC $REVIEW_AGENT_BIN $REVIEW_AGENT_ARGS < prompt.txt
# and parse verdict JSON from stdout. danso does not read prompts from stdin —
# stdin is its internal __tool worker channel and the user prompt is a
# positional argument (jinwon-int/danso src/main.rs) — so this wrapper bridges
# the two: it spools stdin to a 0600 file and hands it to danso via
# --system-context-file, the channel the distill backend already uses for
# packet-sized inputs (bridge/memory/danso_backend.py).
#
# Env:
#   DANSO_REVIEW_PROVIDER  danso --provider           (default glm)
#   DANSO_REVIEW_MODEL     danso --model              (default glm-5.3-flash)
#   DANSO_REVIEW_EFFORT    danso --reasoning-effort   (default medium)
#   DANSO_REVIEW_TIMEOUT   seconds budget             (default 480; the
#                          --provider-timeout-seconds cap mirrors the distill
#                          backend's 300s)
#   DANSO_BIN              danso binary               (default danso on PATH)
# Auth material (DANSO_GLM_* / DANSO_OPENAI_* / DANSO_CHATGPT_*) is inherited
# from the worker env untouched.
#
# Argv: the handlers derive review_model from an explicit --model in
# REVIEW_AGENT_ARGS, so `--model X` / `--model=X` here are parsed and mapped to
# --model X for danso. Any other arguments are ignored.
#
# Isolation mirrors the distill backend: an empty temp HOME and workspace, no
# tools, single turn, host sandbox. stdout is danso's stdout verbatim; the exit
# code is danso's exit code (2 = our own input-contract failure).
set -uo pipefail

MAX_PROMPT_BYTES=2097152  # 2 MiB; intake packets are bounded far below this
DANSO_BIN="${DANSO_BIN:-danso}"
PROVIDER="${DANSO_REVIEW_PROVIDER:-glm}"
MODEL="${DANSO_REVIEW_MODEL:-glm-5.3-flash}"
EFFORT="${DANSO_REVIEW_EFFORT:-medium}"
TIMEOUT="${DANSO_REVIEW_TIMEOUT:-480}"
case "$TIMEOUT" in ''|*[!0-9]*) TIMEOUT=480 ;; esac
PROVIDER_TIMEOUT=$(( TIMEOUT < 300 ? TIMEOUT : 300 ))

die() { printf 'danso-review-agent: %s\n' "$1" >&2; exit "${2:-2}"; }

# Parse the handler's REVIEW_AGENT_ARGS passthrough: only --model matters.
prev=""
for arg in "$@"; do
  if [ "$prev" = "--model" ]; then
    MODEL="$arg"
    prev=""
    continue
  fi
  case "$arg" in
    --model=*) MODEL="${arg#--model=}" ;;
    --model) prev="--model" ;;
  esac
done

command -v "$DANSO_BIN" >/dev/null 2>&1 || die "danso binary not executable: $DANSO_BIN"
command -v jq >/dev/null 2>&1 || die "jq required"

tmp="$(mktemp -d)" || die "mktemp failed"
trap 'rm -rf "$tmp"' EXIT
chmod 700 "$tmp"
prompt="$tmp/prompt.txt"
touch "$prompt" && chmod 600 "$prompt" || die "cannot create prompt file"

# Spool stdin with a hard cap so a runaway packet cannot fill the disk.
head -c $((MAX_PROMPT_BYTES + 1)) > "$prompt" || die "stdin read failed"
size="$(wc -c < "$prompt" | tr -d '[:space:]')"
[ "$size" -gt "$MAX_PROMPT_BYTES" ] && die "prompt exceeds ${MAX_PROMPT_BYTES} bytes"

home="$tmp/home"
ws="$tmp/workspace"
mkdir -m 700 "$home" "$ws" || die "cannot create isolation dirs"

# Run danso with danso's exit code passed through and stdout/stderr ours.
# (No exec: the trap must run to remove the temp tree.)
rc=0
env HOME="$home" "$DANSO_BIN" \
  --provider "$PROVIDER" \
  --model "$MODEL" \
  --reasoning-effort "$EFFORT" \
  --no-tools \
  --max-turns 1 \
  --sandbox host \
  --cwd "$ws" \
  --session "$ws/session.jsonl" \
  --system-context-file "$prompt" \
  --timeout-seconds "$TIMEOUT" \
  --provider-timeout-seconds "$PROVIDER_TIMEOUT" \
  -p "The review packet is in your system context. Apply it and emit only the verdict JSON." || rc=$?
exit "$rc"
