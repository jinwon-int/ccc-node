#!/usr/bin/env bash
# gate-sim.sh — answer "would auto mode have installed this?" without installing
# anything (#355 follow-up).
#
# In approve mode the machine gates never run, so a pending draft carrying no
# autosave-block.json has NOT passed anything — it has simply never been
# checked. That makes the question "are the gates good enough to trust with
# auto mode?" unanswerable from the queue alone.
#
# This script answers it: it replays the same gate functions autoinstall.sh
# uses, over drafts that already have a human verdict, and prints gate verdict
# beside human verdict. It is strictly read-only — it installs nothing, writes
# nothing under the skills root, and never changes the autosave mode.
#
# Usage:
#   gate-sim.sh                 # every draft in the pending queue, incl. archived
#   gate-sim.sh --json          # machine-readable
#   gate-sim.sh <path>...       # specific SKILL.md files or draft dirs
#
# Exit status is 0 whenever the run completed; a gate BLOCK is a finding to
# report, not a failure of this tool.
set -uo pipefail
export LC_ALL=C

HERE="$(cd "$(dirname "$0")" && pwd)"
AUTO="$HERE/autoinstall.sh"
CLAUDE_DIR="${CCC_CLAUDE_DIR:-${HOME:-/root}/.claude}"
STATE_DIR="${CCC_SKILL_REVIEW_STATE_DIR:-$CLAUDE_DIR/state}"
PENDING_DIR="${CCC_SKILL_REVIEW_PENDING_DIR:-$STATE_DIR/pending-skills}"

[ -r "$AUTO" ] || { echo "gate-sim: cannot read $AUTO" >&2; exit 2; }

# Source autoinstall.sh purely to reuse its gate functions. The verb is pinned
# to `status` on purpose: autoinstall.sh dispatches on "${1:-run}", so sourcing
# it without an explicit verb would hand it THIS script's arguments. `status`
# is read-only and its output is discarded.
# shellcheck source=claude/hooks/skill-review/autoinstall.sh
. "$AUTO" status >/dev/null 2>&1 || true

for fn in gate_lint gate_secrets gate_node_specific; do
  declare -f "$fn" >/dev/null 2>&1 || {
    echo "gate-sim: $fn unavailable after sourcing $AUTO" >&2; exit 2; }
done

# gate_dedup is deliberately NOT replayed: it compares a draft against the
# currently installed skills, so any draft that was already installed would
# trivially collide with itself and report a meaningless BLOCK.
GATES=(gate_lint gate_secrets gate_node_specific)
declare -f gate_codex_compat >/dev/null 2>&1 && GATES+=(gate_codex_compat)
declare -f gate_unverified_claims >/dev/null 2>&1 && GATES+=(gate_unverified_claims)

json=0
args=()
for a in "$@"; do
  case "$a" in
    --json) json=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) args+=("$a") ;;
  esac
done

# Human verdict is read from the archive suffix, not meta.json: batch reviews
# have historically moved a draft directory without rewriting its status field,
# so meta.json can still say "pending" long after a human rejected it.
human_verdict() {
  case "$1" in
    *.installed-*) printf 'installed' ;;
    *.approved-*)  printf 'approved' ;;
    *.rejected-*)  printf 'rejected' ;;
    *)             printf 'pending' ;;
  esac
}

simulate() { # <skill.md> -> "PASS" | "BLOCK:<gate>=<reason>[;...]"
  local f="$1" out rc verdict="" g
  for g in "${GATES[@]}"; do
    out="$("$g" "$f" 2>&1)"; rc=$?
    [ "$rc" -eq 0 ] && continue
    [ -n "$out" ] || out="rc=$rc"
    verdict="${verdict:+$verdict;}${g#gate_}=${out//$'\n'/ }"
  done
  [ -n "$verdict" ] && printf 'BLOCK:%s' "$verdict" || printf 'PASS'
}

targets=()
if [ "${#args[@]}" -gt 0 ]; then
  targets=("${args[@]}")
else
  [ -d "$PENDING_DIR" ] || { echo "gate-sim: no pending dir at $PENDING_DIR" >&2; exit 2; }
  while IFS= read -r d; do targets+=("$d"); done < <(
    find "$PENDING_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort)
fi

rows=()
for t in "${targets[@]}"; do
  if [ -d "$t" ]; then
    f="$t/SKILL.md"; dir="$t"
  else
    f="$t"; dir="$(dirname "$t")"
  fi
  [ -f "$f" ] || continue
  name="$(basename "$dir")"
  [ -f "$dir/meta.json" ] && command -v jq >/dev/null 2>&1 \
    && name="$(jq -r '.name // empty' "$dir/meta.json" 2>/dev/null)" || true
  [ -n "$name" ] || name="$(basename "$dir")"
  blocked_then="-"
  [ -f "$dir/autosave-block.json" ] && command -v jq >/dev/null 2>&1 \
    && blocked_then="$(jq -r '.reason // "-"' "$dir/autosave-block.json" 2>/dev/null)"
  rows+=("$name|$(human_verdict "$(basename "$dir")")|$blocked_then|$(simulate "$f")")
done

if [ "$json" = 1 ] && command -v jq >/dev/null 2>&1; then
  printf '%s\n' "${rows[@]}" | jq -R -s 'split("\n") | map(select(length>0)) | map(split("|"))
    | map({name:.[0], human:.[1], blocked_when_staged:.[2],
           gate:(if (.[3]|startswith("BLOCK")) then "BLOCK" else "PASS" end),
           detail:.[3]})'
  exit 0
fi

printf '%-46s %-10s %-22s %s\n' "DRAFT" "HUMAN" "BLOCKED-WHEN-STAGED" "GATE (replayed now)"
printf '%-46s %-10s %-22s %s\n' "$(printf '%.0s-' {1..46})" "----------" \
  "$(printf '%.0s-' {1..22})" "-------------------"
for r in "${rows[@]}"; do
  IFS='|' read -r n h b g <<<"$r"
  printf '%-46s %-10s %-22s %s\n' "${n:0:46}" "$h" "${b:0:22}" "$g"
done
printf '\n%d draft(s) replayed. Nothing was installed, moved, or modified.\n' "${#rows[@]}"
