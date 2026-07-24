#!/usr/bin/env bash
# Stop hook — "evidence before declaring" gate (harness roadmap #13, Tier 1.5 / item #8).
#
# Opt-in via CCC_EVIDENCE_GATE=1. If THIS session changed files (Write/Edit/
# MultiEdit/NotebookEdit) but the audit log shows no verification activity
# (tests / dry-run / --check / diff or status review / CI checks), block the
# Stop ONCE and ask for evidence. This is a reminder gate, not hard enforcement:
#
#   - Off by default (fail-open: exit 0 unless CCC_EVIDENCE_GATE=1).
#   - Loop-safe: passes immediately when stop_hook_active is set, so it can
#     block at most once per stop sequence.
#   - Scoped: only considers audit entries for the current session_id.
#   - Conservative: a broad evidence pattern (incl. `git diff`/`git status`)
#     keeps false positives low — the goal is to nudge, not to wall off.
#
# Block is signalled the Claude Code way: print {"decision":"block","reason":..}
# to stdout and exit 0.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
# shellcheck source=claude/hooks/lib/lifecycle-common.sh
[ -r "$HERE/lib/lifecycle-common.sh" ] && . "$HERE/lib/lifecycle-common.sh"

# Distill subprocess guard (see ~/.claude/hooks/distill.sh).
[ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ] && exit 0

[ "${CCC_EVIDENCE_GATE:-0}" = "1" ] || exit 0

input="$(cat 2>/dev/null)"
[ -n "$input" ] || exit 0

active="$(printf '%s' "$input" | jq -r '.stop_hook_active // false' 2>/dev/null)"
[ "$active" = "true" ] && exit 0   # already continued by this gate -> don't loop

sid="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)"
[ -n "$sid" ] || exit 0            # cannot scope to a session -> don't gate
session_ref=""
if command -v ccc_lifecycle_ref >/dev/null 2>&1; then
  session_ref="$(ccc_lifecycle_ref "$sid" 2>/dev/null || true)"
fi

state_dir="$(ccc_lifecycle_state_dir 2>/dev/null || true)"
LOG="${CCC_AUDIT_LOG:-${state_dir:+$state_dir/audit.jsonl}}"
[ -n "$LOG" ] || exit 0
[ -f "$LOG" ] || exit 0

# Audit entries for this session only. Raw session_id matching keeps existing
# pre-upgrade records readable; new records use only the opaque session_ref.
sess="$(jq -c --arg s "$sid" --arg r "$session_ref" \
  'select((.session_ref == $r and $r != "") or .session_id == $s)' "$LOG" 2>/dev/null)"
[ -n "$sess" ] || exit 0

# Did this session change files? If not, there is nothing to verify.
changed="$(printf '%s\n' "$sess" \
  | jq -r 'select(.file_change == true or .tool=="Write" or .tool=="Edit" or .tool=="MultiEdit" or .tool=="NotebookEdit") | .tool' \
  2>/dev/null | head -1)"
[ -n "$changed" ] || exit 0

# Any verification evidence among this session's Bash commands?
ev_re='pytest|unittest|(npm|pnpm|yarn)( run)? test|\btest\b|validate|verify|--dry-run|--check|shellcheck|\bbats\b|gh pr (checks|view)|git diff|git status'
evidence="$(printf '%s\n' "$sess" \
  | jq -r 'select(.verification == true) | "verified"' 2>/dev/null | head -1)"
if [ -z "$evidence" ]; then
  # Compatibility with pre-upgrade records that still carried raw commands.
  evidence="$(printf '%s\n' "$sess" \
    | jq -r 'select(.tool=="Bash") | .command // empty' 2>/dev/null \
    | grep -iE "$ev_re" | head -1)"
fi
[ -n "$evidence" ] && exit 0

reason="이 세션에서 파일을 변경했지만 audit 로그에 검증 흔적(테스트/실행/--dry-run/diff·status 검토/CI)이 없습니다. 변경이 동작하는지 확인하는 증거를 남긴 뒤 종료하세요. 검증이 불필요하면 그대로 다시 종료하면 통과합니다."
jq -nc --arg r "$reason" '{decision:"block", reason:$r}' 2>/dev/null
exit 0
