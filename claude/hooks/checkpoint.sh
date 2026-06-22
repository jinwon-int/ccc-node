#!/usr/bin/env bash
# Working-state checkpoint across compaction boundaries.
#   PreCompact  : snapshot working-state.md so nothing is lost when context is compacted.
#   PostCompact : re-inject working-state.md into context so the next turn knows what it was doing.
# The agent is expected to keep /root/.claude/state/working-state.md current during long/multi-session tasks.
set -uo pipefail

# Distill subprocess guard (see ~/.claude/hooks/distill.sh).
[ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ] && exit 0

EVENT="${1:-PreCompact}"
# State dir is overridable for testing / non-root installs (#82).
STATE_DIR="${CCC_STATE_DIR:-/root/.claude/state}"
STATE_FILE="$STATE_DIR/working-state.md"
CKPT_DIR="$STATE_DIR/checkpoints"
LOG="$STATE_DIR/checkpoint.log"
ts="$(date +%Y%m%d_%H%M%S)"
mkdir -p "$CKPT_DIR"

if [ "$EVENT" = "PreCompact" ]; then
  if [ -s "$STATE_FILE" ]; then
    cp "$STATE_FILE" "$CKPT_DIR/working-state-$ts.md"
    echo "[$ts] PreCompact: snapshot -> checkpoints/working-state-$ts.md" >> "$LOG"
    msg="working-state.md checkpoint saved: checkpoints/working-state-$ts.md"
  else
    echo "[$ts] PreCompact: no working-state.md to snapshot" >> "$LOG"
    msg="working-state.md empty; snapshot skipped (keep it updated for long tasks)."
  fi
  # retain the 30 most recent checkpoints
  ls -1t "$CKPT_DIR"/working-state-*.md 2>/dev/null | tail -n +31 | xargs -r rm -f
  jq -n --arg m "$msg" '{systemMessage:$m, suppressOutput:true}'
  exit 0
fi

# PostCompact (or anything else): re-inject the working state.
state="$(cat "$STATE_FILE" 2>/dev/null)"
latest="$(ls -1t "$CKPT_DIR"/working-state-*.md 2>/dev/null | head -1)"
bytes="$(printf '%s' "$state" | wc -c | tr -d ' ')"
echo "[$ts] PostCompact: re-injected working-state (${bytes} bytes)" >> "$LOG"

ctx="# Working-state checkpoint (auto-injected: PostCompact)

This is the pre-compaction task context. Continue from here. (Durable facts: prefer Wiki/memory.)

## working-state.md
${state:-(working-state.md empty — if a task is in progress, keep $STATE_DIR/working-state.md updated as objective / progress / next step)}

Latest checkpoint: ${latest:-(none)}"

jq -n --arg ctx "$ctx" '{hookSpecificOutput:{hookEventName:"PostCompact",additionalContext:$ctx}}'
