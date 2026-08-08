#!/usr/bin/env bash
# Working-state checkpoint across compaction boundaries.
#   PreCompact  : snapshot working-state.md so nothing is lost when context is compacted.
#   PostCompact : re-inject working-state.md into context so the next turn knows what it was doing.
# The agent is expected to keep $HOME/.claude/state/working-state.md current during long/multi-session tasks.
set -uo pipefail

# Distill subprocess guard (see ~/.claude/hooks/distill.sh).
[ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ] && exit 0

EVENT="${1:-PreCompact}"
# State dir is overridable for testing / non-root installs (#82).
STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
STATE_FILE="$STATE_DIR/working-state.md"
CKPT_DIR="$STATE_DIR/checkpoints"
LOG="$STATE_DIR/checkpoint.log"
ts="$(date +%Y%m%d_%H%M%S)"
mkdir -p "$CKPT_DIR"

# Portable mtime prune/select helpers (busybox-safe; see #449).
CKPT_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || CKPT_SELF_DIR="${HOME:-/root}/.claude/hooks"
# shellcheck source=claude/hooks/lib/mtime-prune.sh
if [ -r "$CKPT_SELF_DIR/lib/mtime-prune.sh" ]; then
  . "$CKPT_SELF_DIR/lib/mtime-prune.sh"
elif [ -r "${HOME:-/root}/.claude/hooks/lib/mtime-prune.sh" ]; then
  . "${HOME:-/root}/.claude/hooks/lib/mtime-prune.sh"
fi

if [ "$EVENT" = "PreCompact" ]; then
  if [ -s "$STATE_FILE" ]; then
    cp "$STATE_FILE" "$CKPT_DIR/working-state-$ts.md"
    echo "[$ts] PreCompact: snapshot -> checkpoints/working-state-$ts.md" >> "$LOG"
    msg="working-state.md checkpoint saved: checkpoints/working-state-$ts.md"
  else
    echo "[$ts] PreCompact: no working-state.md to snapshot" >> "$LOG"
    msg="working-state.md empty; snapshot skipped (keep it updated for long tasks)."
  fi
  # retain the 30 most recent checkpoints (portable, whitespace-safe)
  prune_keep_newest "$CKPT_DIR" 'working-state-*.md' 30
  jq -n --arg m "$msg" '{systemMessage:$m, suppressOutput:true}'
  exit 0
fi

# PostCompact (or anything else): re-inject the working state.
state="$(cat "$STATE_FILE" 2>/dev/null)"
# #1045: working-state.md is agent-written and re-enters model context here.
# Every other injection route (load-memory.sh) runs its blocks through
# scan-injection.sh; this was the one unscanned gap. Same fail-open contract
# as load-memory.sh's scan_injection_block: fall back to the raw text when the
# scanner is missing or fails — on a degraded node, losing the checkpoint
# would hurt more than one unscanned re-injection. CCC_SCAN_INJECTION_BIN is
# the test seam / operator override.
# An explicit override is honored exactly (no fallback): the operator/test
# named the scanner, and silently substituting another would defeat the seam.
if [ -n "${CCC_SCAN_INJECTION_BIN:-}" ]; then
  scan_bin="$CCC_SCAN_INJECTION_BIN"
else
  scan_bin="$CKPT_SELF_DIR/scan-injection.sh"
  [ -x "$scan_bin" ] || scan_bin="${HOME:-/root}/.claude/hooks/scan-injection.sh"
fi
if [ -x "$scan_bin" ] \
  && scanned="$(printf '%s' "$state" | "$scan_bin" working-state-checkpoint 2>/dev/null)"; then
  state="$scanned"
fi
latest="$(newest_file "$CKPT_DIR" 'working-state-*.md')"
bytes="$(printf '%s' "$state" | wc -c | tr -d ' ')"
echo "[$ts] PostCompact: re-injected working-state (${bytes} bytes)" >> "$LOG"

ctx="# Working-state checkpoint (auto-injected: PostCompact)

This is the pre-compaction task context. Continue from here. (Durable facts: prefer Wiki/memory.)

## working-state.md
${state:-(working-state.md empty — if a task is in progress, keep $STATE_DIR/working-state.md updated as objective / progress / next step)}

Latest checkpoint: ${latest:-(none)}"

jq -n --arg ctx "$ctx" '{hookSpecificOutput:{hookEventName:"PostCompact",additionalContext:$ctx}}'
