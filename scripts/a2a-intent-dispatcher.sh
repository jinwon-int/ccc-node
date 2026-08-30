#!/usr/bin/env bash
# a2a-intent-dispatcher.sh — intent-routing wrapper for the a2a-broker-worker
# external handler contract (WORKER_HANDLER_COMMAND).
#
# Canonical fleet source (this file); deploy via
# scripts/install-a2a-review-handler.sh. Node-local copies forked and drifted
# historically — gongyung ran the generic mjs dispatcher against review intents
# and every review died as an unverifiable generic ack (2026-08-30).
#
# Reads the full task JSON from stdin exactly once, routes by task.intent:
#   skills-intake-review -> $INTAKE_REVIEW_HANDLER
#   anything else        -> $DEFAULT_TASK_HANDLER (word-split, exec'd)
#
# Env (worker child inherits the worker env file, so node config lives there):
#   INTAKE_REVIEW_HANDLER  review handler path
#                          (default: this script's dir / skills-intake-review-handler.sh)
#   DEFAULT_TASK_HANDLER   default dispatcher command line
#                          (default: node <this dir>/a2a-task-handler.mjs)
#
# Exit code and stdout/stderr semantics are the handler contract's: result JSON
# on stdout, exit 0 = terminal result, nonzero = retryable failure.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INTAKE_REVIEW_HANDLER="${INTAKE_REVIEW_HANDLER:-$SCRIPT_DIR/skills-intake-review-handler.sh}"
DEFAULT_TASK_HANDLER="${DEFAULT_TASK_HANDLER:-node $SCRIPT_DIR/a2a-task-handler.mjs}"

log() { echo "a2a-intent-dispatcher: $*" >&2; }

command -v jq >/dev/null 2>&1 || { echo "a2a-intent-dispatcher: jq required" >&2; exit 1; }

tmp="$(mktemp)" || { echo "a2a-intent-dispatcher: mktemp failed" >&2; exit 1; }
trap 'rm -f "$tmp"' EXIT
cat > "$tmp" 2>/dev/null || { echo "a2a-intent-dispatcher: stdin read failed" >&2; exit 1; }
[ -s "$tmp" ] || { echo "a2a-intent-dispatcher: empty task" >&2; exit 1; }

intent="$(jq -r '.intent // empty' "$tmp" 2>/dev/null)"
log "routing intent=${intent:-<none>}"

case "$intent" in
  skills-intake-review|skills_intake_review)
    [ -x "$INTAKE_REVIEW_HANDLER" ] || { echo "a2a-intent-dispatcher: review handler not executable: $INTAKE_REVIEW_HANDLER" >&2; exit 1; }
    exec bash "$INTAKE_REVIEW_HANDLER" < "$tmp"
    ;;
  *)
    # Intentional word split of an operator-owned command line (shellcheck-disable=SC2086)
    exec $DEFAULT_TASK_HANDLER < "$tmp"
    ;;
esac
