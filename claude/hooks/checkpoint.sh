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

# An audience-scoped session points CCC_STATE_DIR at a per-audience tree that
# starts empty, so the node's pre-scope working-state.md stops being seen: the
# PreCompact snapshot is skipped and PostCompact re-injects an empty block. The
# failure is silent, and an empty block reads as "no task in progress" rather
# than as a missing file (#1155). Before scoping, CCC_STATE_DIR was unset and
# the default WAS the legacy path, so this hook worked by accident.
#
# memory_audience.py states the contract for that pre-scope data: it is private
# legacy input, read in place, never copied into a public store, and only for a
# private audience. load-memory.sh is the one hook that implements it; mirror
# its gate here rather than falling back unconditionally — an ungated fallback
# would re-inject the node's private working memory into a shared audience.
#
# Defaults keep unscoped nodes byte-identical: CCC_MEMORY_AUDIENCE_SCOPED
# defaults to 0 and CCC_MEMORY_AUDIENCE to "legacy", so the branch is dead
# unless a scoped private session set both — and such a node has CCC_STATE_DIR
# unset anyway, making the legacy path the primary one.
ckpt_is_disabled() { case "${1:-}" in 0|false|FALSE|off|OFF|no|NO) return 0;; *) return 1;; esac; }
LEGACY_STATE_DIR="${CCC_MEMORY_LEGACY_STATE_DIR:-${HOME:-/root}/.claude/state}"
if [ ! -s "$STATE_FILE" ] \
  && ! ckpt_is_disabled "${CCC_MEMORY_AUDIENCE_SCOPED:-0}" \
  && [ "${CCC_MEMORY_AUDIENCE:-legacy}" = "private" ] \
  && [ -n "$LEGACY_STATE_DIR" ] \
  && [ "$LEGACY_STATE_DIR" != "$STATE_DIR" ] \
  && [ -s "$LEGACY_STATE_DIR/working-state.md" ]; then
  STATE_FILE="$LEGACY_STATE_DIR/working-state.md"
fi
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
  ckpt_run_scanner() { "$scan_bin" "$1"; }
else
  scan_bin="$CKPT_SELF_DIR/scan-injection.sh"
  [ -x "$scan_bin" ] || scan_bin="${HOME:-/root}/.claude/hooks/scan-injection.sh"
  # Run the resolved scanner through bash instead of exec'ing it. Exec'ing
  # depends on its `#!/usr/bin/env bash` resolving, and Termux has no /usr, so
  # the exec dies with 126, the substitution below fails, and the fail-open
  # branch re-injects the working state UNSCANNED — silently, on every
  # compaction, on that whole platform (#1157). An explicit override keeps
  # exec'ing as named: it may not be a bash script at all, and forcing an
  # interpreter onto it would defeat the seam.
  ckpt_run_scanner() { bash "$scan_bin" "$1"; }
fi
if [ -x "$scan_bin" ] \
  && scanned="$(printf '%s' "$state" | ckpt_run_scanner working-state-checkpoint 2>/dev/null)"; then
  state="$scanned"
fi
# Stale guard: a working-state last written weeks ago re-enters context here
# looking current, and a dead objective (e.g. a task that finished a month
# ago) can steer the post-compaction session backwards. Flag it instead of
# presenting it as fresh. Age comes from the resolved $STATE_FILE mtime;
# CCC_CKPT_STALE_DAYS overrides the 14-day threshold, 0 disables. Unknown age
# (no python3 / helper missing) stays silent — best-effort, same degradation
# contract as mtime-prune.sh.
stale_note=""
stale_log=""
stale_days="${CCC_CKPT_STALE_DAYS:-14}"
case "$stale_days" in ''|*[!0-9]*) stale_days=14 ;; esac
if [ "$stale_days" -gt 0 ] && [ -s "$STATE_FILE" ] \
  && command -v file_age_days >/dev/null 2>&1; then
  age="$(file_age_days "$STATE_FILE")"
  if [ -n "$age" ] && [ "$age" -ge "$stale_days" ]; then
    stale_note="

⚠ STALE: working-state.md was last modified ${age} days ago — it may describe an already-finished task. Verify against live state before acting on it, and clear it to an idle note when its task closes."
    stale_log=" (stale ${age}d)"
  fi
fi
latest="$(newest_file "$CKPT_DIR" 'working-state-*.md')"
bytes="$(printf '%s' "$state" | wc -c | tr -d ' ')"
echo "[$ts] PostCompact: re-injected working-state (${bytes} bytes)${stale_log}" >> "$LOG"

ctx="# Working-state checkpoint (auto-injected: PostCompact)

This is the pre-compaction task context. Continue from here. (Durable facts: prefer Wiki/memory.)${stale_note}

## working-state.md
${state:-(working-state.md empty — if a task is in progress, keep $STATE_DIR/working-state.md updated as objective / progress / next step)}

Latest checkpoint: ${latest:-(none)}"

jq -n --arg ctx "$ctx" '{hookSpecificOutput:{hookEventName:"PostCompact",additionalContext:$ctx}}'
