#!/usr/bin/env bash
# ccc-node statusline — one-line at-a-glance: node · model · git · context · cost · A2A.
# Claude Code passes session JSON on stdin (see /en/statusline). Prints one line to stdout;
# whatever is printed becomes the status bar. Must be fast and never block.
#
# Wired via the node-local settings.json `statusLine` field (the main status line is not
# applied from a plugin's settings.json — only `agent`/`subagentStatusLine` are). Install
# path: ~/.claude/hooks/statusline.sh.
set -uo pipefail

input="$(cat)"

# Default values for all fields (used if jq fails or input is empty).
MODEL="?"; PCT=0; COST=0; OVER=false; STYLE=""; CWD=""

# Single jq call to extract all fields at once (was 6 separate calls).
# If jq fails (empty input, malformed JSON), defaults above are used.
eval "$(jq -r '
  @sh "MODEL=\(.model.display_name // "?")",
  @sh "PCT=\(.context_window.used_percentage // 0)",
  @sh "COST=\(.cost.total_cost_usd // 0)",
  @sh "OVER=\(.exceeds_200k_tokens // false)",
  @sh "STYLE=\(.output_style.name // "")",
  @sh "CWD=\(.workspace.current_dir // .cwd // "")"
' <<<"$input" 2>/dev/null)" || true

# Sanitize PCT to integer
PCT="${PCT%%.*}"
[[ "$PCT" =~ ^[0-9]+$ ]] || PCT=0

# Best-effort, allowlisted usage snapshot for the local Telegram /usage command.
# The collector emits nothing and status-line rendering must never depend on it.
USAGE_COLLECTOR="${CCC_STATUSLINE_USAGE_COLLECTOR:-$HOME/.claude/hooks/statusline-usage.py}"
if [ -x "$USAGE_COLLECTOR" ]; then
  printf '%s' "$input" | "$USAGE_COLLECTOR" >/dev/null 2>&1 || true
fi

# Node label: explicit env override -> state file -> short hostname.
NODE="${CCC_NODE:-}"
[ -z "$NODE" ] && [ -r "$HOME/.claude/state/node.txt" ] && NODE="$(head -1 "$HOME/.claude/state/node.txt" 2>/dev/null)"
[ -z "$NODE" ] && NODE="$(hostname -s 2>/dev/null || echo node)"

# Git branch + dirty marker with TTL cache (5s). Best-effort, scoped to session cwd.
BR=""
if [ -n "$CWD" ]; then
  # Use md5 of CWD as cache key (safe, deterministic, short).
  CWD_KEY="$(printf '%s' "$CWD" | md5sum | cut -d' ' -f1)"
  CACHE_DIR="$HOME/.claude/cache/git-status"
  CACHE_FILE="$CACHE_DIR/$CWD_KEY.json"
  CACHE_TTL=5

  # Read from cache if fresh (exists and <TTL seconds old).
  NOW="$(date +%s)"
  if [ -r "$CACHE_FILE" ]; then
    CACHED_TS="$(jq -r '.timestamp // 0' "$CACHE_FILE" 2>/dev/null || echo 0)"
    if [ "$((NOW - CACHED_TS))" -lt "$CACHE_TTL" ]; then
      BR="$(jq -r '.branch // empty' "$CACHE_FILE" 2>/dev/null)"
      [ "$(jq -r '.dirty // false' "$CACHE_FILE" 2>/dev/null)" = "true" ] && BR="${BR}*"
    fi
  fi

  # Cache miss or stale: recompute and store.
  if [ -z "$BR" ] && git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    CURRENT_BRANCH="$(git -C "$CWD" branch --show-current 2>/dev/null)"
    DIRTY_MARKER=""
    [ -n "$(git -C "$CWD" status --porcelain 2>/dev/null)" ] && DIRTY_MARKER="*"
    BR="${CURRENT_BRANCH}${DIRTY_MARKER}"

    # Write cache (ignore failures — statusline must never block).
    mkdir -p "$CACHE_DIR" 2>/dev/null
    jq -n \
      --arg branch "$CURRENT_BRANCH" \
      --argjson dirty "${DIRTY_MARKER:+true}" \
      --argjson ts "$NOW" \
      '{branch: $branch, dirty: $dirty, timestamp: $ts}' >"$CACHE_FILE" 2>/dev/null || true
  fi
fi

# A2A marker: current claimed task id, if the claim flow recorded one (graceful when absent).
A2A=""
[ -r "$HOME/.claude/state/a2a-current" ] && A2A="$(head -1 "$HOME/.claude/state/a2a-current" 2>/dev/null)"

c() { printf '\033[%sm' "$1"; }
RST="$(c 0)"
if   [ "$PCT" -ge 80 ]; then CC="$(c '1;31')"
elif [ "$PCT" -ge 50 ]; then CC="$(c '33')"
else CC="$(c '32')"; fi

OUT="$(c '1;36')${NODE}${RST} $(c '35')${MODEL}${RST}"
[ -n "$BR" ] && OUT="${OUT} $(c '90')⎇ ${BR}${RST}"
OUT="${OUT} ${CC}${PCT}% ctx${RST}"
[ "$OVER" = "true" ] && OUT="${OUT} $(c '1;31')⚠200k${RST}"
COSTR="$(printf '%.2f' "$COST" 2>/dev/null || echo 0)"
OUT="${OUT} $(c '90')\$${COSTR}${RST}"
[ -n "$A2A" ] && OUT="${OUT} $(c '1;33')A2A:${A2A}${RST}"
[ -n "$STYLE" ] && [ "$STYLE" != "null" ] && OUT="${OUT} $(c '90')[${STYLE}]${RST}"

printf '%s\n' "$OUT"
