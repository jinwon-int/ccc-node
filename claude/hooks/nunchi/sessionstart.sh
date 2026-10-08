#!/usr/bin/env bash
# nunchi SessionStart injection (#816).
# Prints the latest peer_facts snapshot so it lands in session context.
# Fail-open, fast (<1s): never blocks session start; byte-capped at 3000.
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
scan_block() { # <text>
  local text="$1" scanned
  if [ ! -f "$HOOKDIR/scan-injection.sh" ]; then
    printf '%s\n' "$text"
    return
  fi
  if scanned="$(printf '%s' "$text" | bash "$HOOKDIR/scan-injection.sh" nunchi 2>/dev/null)"; then
    printf '%s\n' "$scanned"
  else
    printf 'nunchi sessionstart: scan-injection failed; injecting UNSCANNED block\n' >&2
    printf '%s\n' "$text"
  fi
}

if [ "${CCC_NUNCHI_ASSEMBLE:-1}" = "0" ] || [ "${CCC_NUNCHI_ASSEMBLE:-1}" = "false" ]; then
  block="$(inject_legacy)"
else
  # #2191: was `"$d" --mode local` — it executed the DIRECTORY, and looked in
  # a scripts/ dir the deployed tree does not have, so the task hint was
  # always empty. Resolve the tool like load-memory.sh and run it via bash.
  hint=""
  if command -v find_memory_tool >/dev/null 2>&1 \
      && query_tool="$(find_memory_tool ccc-memory-query.sh 2>/dev/null)"; then
    hint="$(bash "$query_tool" --mode local 2>/dev/null || true)"
  fi
  [ -n "$hint" ] || hint="$(cat "${STATE}/current-task.txt" 2>/dev/null || true)"
  if ! block="$(python3 "$HERE/nunchi.py" assemble \
       --budget "${CCC_NUNCHI_ASSEMBLE_BUDGET:-3000}" --hint "$hint" 2>/dev/null)"; then
    block="$(inject_legacy)"
  fi
fi
[ -n "$block" ] && scan_block "$block"

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
