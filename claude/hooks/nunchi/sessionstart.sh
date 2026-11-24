#!/usr/bin/env bash
# nunchi SessionStart injection (#816).
# Prints the latest peer_facts snapshot so it lands in session context.
# Fail-open: never blocks session start; bounded in bytes by the assemble
# budgets and in characters by CCC_NUNCHI_INJECT_MAX_CHARS. Typically ~1s on
# a phone node (assemble + task query + injection scan, #2191; hook timeout 5s).
# No-op unless nunchi is enabled (state/nunchi.mode=on or CCC_NUNCHI_MODE=on).
#
# Snapshot regeneration is asynchronous (background) when stale (#893):
# - Stale snapshots (>15min) are injected immediately, then refreshed in background.
# - This prevents Python/repository latency from blocking SessionStart.
set -uo pipefail

STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
MODE="${CCC_NUNCHI_MODE:-$(cat "$STATE/nunchi.mode" 2>/dev/null || echo off)}"
[ "$MODE" = "on" ] || exit 0

# Node-global nunchi has no scope-local provenance yet. Match the managed Codex
# loader's fail-closed boundary and disable it for every audience-scoped runtime.
case "${CCC_MEMORY_AUDIENCE_SCOPED:-0}" in
  1|true|TRUE|on|ON|yes|YES) exit 0 ;;
esac

HERE="$(cd "$(dirname "$0")" && pwd)"
HOOKDIR="$(cd "$HERE/.." && pwd)"
# shellcheck source=claude/hooks/lib/hook-common.sh
. "$HOOKDIR/lib/hook-common.sh" 2>/dev/null || true
NUNCHI_HOME="${NUNCHI_HOME:-$HOME/.nunchi}"
SNAP="${NUNCHI_SNAPSHOT:-$NUNCHI_HOME/snapshot.md}"
[ -f "$SNAP" ] || exit 0

# P2-7 (#1264): ranked, budget-bounded assembly replaces the blind `head -c`
# truncation when enabled (default on; CCC_NUNCHI_ASSEMBLE=0 opts out). The
# task hint joins the same task-conditioned prefetch path load-memory.sh uses
# (ccc-memory-query.sh --mode local, then current-task.txt); without a hint
# the assembly degrades to constraints-first + recency, which is still
# strictly better than recency-only truncation where newer filler could cut
# off a constraint. ANY failure inside the assembly falls back to the exact
# legacy `head -c 3000`, so the worst case stays today's behavior.
inject_legacy() { head -c 3000 "$SNAP" 2>/dev/null || true; }

# #2191: the injected block now carries machine-extracted auto-distill rows
# (#2186), so it goes through the same runtime scanner load-memory.sh uses
# (credential redaction + prompt-injection neutralization). Run it through
# bash — exec'ing its `#!/usr/bin/env` shebang dies with 126 on Termux
# (#1157). Fail-open like load-memory.sh, but say so on stderr (#1160).
scan_block() { # <text> [max-bytes] [max-chars]
  local text="$1" cap="${2:-}" chars="${3:-}" scanned
  if [ ! -f "$HOOKDIR/scan-injection.sh" ]; then
    printf '%s\n' "$text"
    return
  fi
  if scanned="$(printf '%s' "$text" | bash "$HOOKDIR/scan-injection.sh" nunchi "$cap" "$chars" 2>/dev/null)"; then
    printf '%s\n' "$scanned"
  else
    printf 'nunchi sessionstart: scan-injection failed; injecting UNSCANNED block\n' >&2
    printf '%s\n' "$text"
  fi
}

# ccc-memory-query.sh always prints a labelled line ("task: current task;
# node: <host>; cwd: <path>; git_branch: ...") — fine for Wiki prefetch, but
# as an FTS hint the labels, the node name and the "current task" placeholder
# match generic facts and reorder the few fact slots (#2193 review). Keep only
# the real task text and the git branch / changed paths.
hint_terms() { # <query-line>
  local raw="$1" out="" field key val IFS=';'
  for field in $raw; do
    field="${field# }"; key="${field%%:*}"; val="${field#*: }"
    [ "$key" = "$field" ] && continue
    case "$key" in
      task) [ "$val" = "current task" ] || out="$out $val" ;;
      git_branch|git_changed_paths) out="$out $val" ;;
    esac
  done
  printf '%s' "${out# }"
}

# #2884: the byte budgets bound the block (constraints 12,000 + facts 3,000),
# but Claude Code caps a hook's output at 10,000 CHARACTERS and replaces a
# longer one with a file path and a ~2,000-character preview. 15,000 bytes of
# mostly-Hangul facts is about 9,000-10,900 characters, so on some nodes every
# session received the preview instead of the block. `assemble` now sizes the
# block to this ceiling (UTF-16 units, the cap's own unit; 500 of headroom),
# and the scanner re-applies it because a redaction marker can be longer than
# what it replaces. 0 turns the ceiling off. The legacy `head -c 3000` path is
# at most 3,000 characters and needs nothing.
max_chars="${CCC_NUNCHI_INJECT_MAX_CHARS:-9500}"
case "$max_chars" in ''|*[!0-9]*) max_chars=9500 ;; esac
# Plain decimal from here on: arithmetic would read a leading zero as octal,
# and more digits than any real ceiling means "no ceiling".
if [ "${#max_chars}" -gt 9 ]; then max_chars=0; else max_chars="$((10#$max_chars))"; fi
scan_chars=""
# One character of the ceiling is the newline scan_block prints after the block.
[ "$max_chars" -gt 0 ] && scan_chars="$((max_chars - 1))"

cap=""
if [ "${CCC_NUNCHI_ASSEMBLE:-1}" = "0" ] || [ "${CCC_NUNCHI_ASSEMBLE:-1}" = "false" ]; then
  block="$(inject_legacy)"
  cap=3000
else
  # #2191: was `"$d" --mode local` — it executed the DIRECTORY, and looked in
  # a scripts/ dir the deployed tree does not have, so the task hint was
  # always empty. Resolve the tool like load-memory.sh and run it via bash.
  hint=""
  if command -v find_memory_tool >/dev/null 2>&1 \
      && query_tool="$(find_memory_tool ccc-memory-query.sh 2>/dev/null)"; then
    hint="$(hint_terms "$(bash "$query_tool" --mode local 2>/dev/null || true)")"
  fi
  [ -n "$hint" ] || hint="$(cat "${STATE}/current-task.txt" 2>/dev/null || true)"
  if ! block="$(python3 "$HERE/nunchi.py" assemble \
       --budget "${CCC_NUNCHI_ASSEMBLE_BUDGET:-3000}" --max-chars "$max_chars" \
       --hint "$hint" 2>/dev/null)"; then
    block="$(inject_legacy)"
    cap=3000
  fi
fi
[ -n "$block" ] && scan_block "$block" "$cap" "$scan_chars"

# Regenerate asynchronously if stale (>15min); cron refreshes every 10min normally.
# The background refresh updates the snapshot for the next session; this session
# already received the (possibly stale) current snapshot above.
if [ -n "$(find "$SNAP" -mmin +15 2>/dev/null)" ]; then
  (
    # Detached background process with nohup to survive shell exit.
    # Redirect all output to /dev/null to avoid interfering with SessionStart.
    nohup python3 "$HERE/nunchi.py" snapshot --limit 25 >/dev/null 2>&1 &
    disown $! 2>/dev/null || true
  ) 2>/dev/null || true
fi
