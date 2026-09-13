#!/usr/bin/env bash
# /distill status — read-only last-run + queue summary. Packaged from the
# former inline block in skills/distill/SKILL.md so the parsing is
# deterministic and regression-tested (tests/test_distill_skill_parsing.py)
# instead of drifting inside the skill text.
#
# Read-only: every input below is opened for reading only; distill state is
# never written, renamed, or created.
STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
QUEUE="$STATE/wiki-candidates.md"
SEEN="$STATE/wiki-candidates.seen"
jq -r '"trigger=\(.trigger) session=\(.session_id) at=\(.distilled_at) honcho=\(.honcho|length) wiki=\(.wiki_candidates|length)"' \
  "$STATE/distill-last.json" 2>/dev/null
tail -5 "$STATE/distill.log" 2>/dev/null
CUTOFF="$(date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf '0000-00-00T00:00:00Z')"
awk -v cutoff="$CUTOFF" '
  function reset() { pending=0; distilled=""; hot=0; stale_marker=0 }
  function flush() {
    if (!seen) return
    total++
    if (pending) pending_n++
    if (hot) hot_n++
    if (pending && (stale_marker || (distilled != "" && distilled < cutoff))) stale_n++
  }
  BEGIN { reset() }
  /^## \[CAND-[0-9]+\]/ { flush(); seen=1; reset(); if ($0 ~ /🔥 HOT/) hot=1; if ($0 ~ /\(stale: pending review\)/) stale_marker=1; next }
  /^- status: pending/ { pending=1; next }
  /^- distilled-at: / { distilled=$3; next }
  END { flush(); printf "wiki-candidates total=%d pending=%d stale=%d hot=%d\n", total+0, pending_n+0, stale_n+0, hot_n+0 }
' "$QUEUE" 2>/dev/null
awk -v th="${CCC_DISTILL_HOTNESS_THRESHOLD:-3}" 'NF >= 4 && $3 >= th {hot++} END { printf "seen-hot=%d threshold=%s\n", hot+0, th }' "$SEEN" 2>/dev/null
