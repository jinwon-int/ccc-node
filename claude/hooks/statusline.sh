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
j() { printf '%s' "$input" | jq -r "$1" 2>/dev/null; }

MODEL="$(j '.model.display_name // "?"')"
PCT="$(j '.context_window.used_percentage // 0')"; PCT="${PCT%%.*}"; [[ "$PCT" =~ ^[0-9]+$ ]] || PCT=0
COST="$(j '.cost.total_cost_usd // 0')"
OVER="$(j '.exceeds_200k_tokens // false')"
STYLE="$(j '.output_style.name // empty')"
CWD="$(j '.workspace.current_dir // .cwd // empty')"

# Node label: explicit env override -> state file -> short hostname.
NODE="${CCC_NODE:-}"
[ -z "$NODE" ] && [ -r "$HOME/.claude/state/node.txt" ] && NODE="$(head -1 "$HOME/.claude/state/node.txt" 2>/dev/null)"
[ -z "$NODE" ] && NODE="$(hostname -s 2>/dev/null || echo node)"

# Git branch + dirty marker, best-effort, scoped to the session cwd.
BR=""
if [ -n "$CWD" ] && git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  BR="$(git -C "$CWD" branch --show-current 2>/dev/null)"
  [ -n "$(git -C "$CWD" status --porcelain 2>/dev/null)" ] && BR="${BR}*"
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
