#!/usr/bin/env bash
# Tests for distill/wiki-queue.sh — hermetic local state only.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WIKI_QUEUE="$HERE/wiki-queue.sh"
SKILL="$HERE/../../skills/distill/SKILL.md"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

export CCC_STATE_DIR="$TMP/state"
export CCC_DISTILL_HOTNESS_THRESHOLD=3
mkdir -p "$CCC_STATE_DIR"

PAYLOAD='{"session_id":"sess-wiki","trigger":"manual","source_cwd":"/root/project-a","source_project":"-root-project-a","wiki_candidates":[{"title":"Decision A","suggested_path":"pages/team/dungae/DECISIONS.md","summary":"Keep the safe path.","evidence_excerpt":"operator said safe"},{"title":"Runbook B","suggested_path":"pages/nodes/dungae/RUNBOOK.md","summary":"Do the thing.","evidence_excerpt":"command output"}],"honcho":[]}'

disabled_state="$TMP/disabled-state"
out="$(printf '%s' "$PAYLOAD" | CCC_STATE_DIR="$disabled_state" CCC_NODE_ISOLATION_PROFILE=external CCC_WIKI_MEMORY_ENABLED=1 bash "$WIKI_QUEUE" 2>&1)"; rc=$?
ok "wiki-disabled queue exits 0 without creating state or queue" '[ "$rc" = 0 ] && grep -q "skipped reason=disabled" <<<"$out" && [ ! -e "$disabled_state/wiki-candidates.md" ] && [ ! -e "$disabled_state/wiki-candidates.seen" ]'

out="$(printf '%s' "$PAYLOAD" | bash "$WIKI_QUEUE" 2>&1)"; rc=$?
ok "first append exits 0" '[ "$rc" = 0 ]'
ok "first append reports two additions" 'grep -q "wiki-queue session=sess-wiki added=2 skipped(dup)=0 total_in=2" <<<"$out"'
ok "queue file has bootstrap header and candidates" 'grep -q "Wiki Candidates Queue" "$CCC_STATE_DIR/wiki-candidates.md" && grep -q "\[CAND-1\].*Decision A" "$CCC_STATE_DIR/wiki-candidates.md" && grep -q "\[CAND-2\].*Runbook B" "$CCC_STATE_DIR/wiki-candidates.md"'
ok "candidate metadata is recorded" 'grep -q "source-session:.*sess-wiki" "$CCC_STATE_DIR/wiki-candidates.md" && grep -q "source-cwd:.*project-a" "$CCC_STATE_DIR/wiki-candidates.md" && grep -q "status: pending" "$CCC_STATE_DIR/wiki-candidates.md"'
ok "seen file stores two canonical four-field rows" '[ "$(wc -l < "$CCC_STATE_DIR/wiki-candidates.seen")" = 2 ] && awk "NF != 4 {bad=1} END{exit bad}" "$CCC_STATE_DIR/wiki-candidates.seen"'

out="$(printf '%s' "$PAYLOAD" | bash "$WIKI_QUEUE" 2>&1)"; rc=$?
ok "duplicate append exits 0" '[ "$rc" = 0 ]'
ok "duplicate append skips both candidates" 'grep -q "added=0 skipped(dup)=2 total_in=2" <<<"$out"'
ok "duplicate append does not add CAND-3 before threshold" '! grep -q "\[CAND-3\]" "$CCC_STATE_DIR/wiki-candidates.md"'
ok "duplicate append increments seen counts" 'awk '\''$4 && $3 != 2 {bad=1} END{exit bad}'\'' "$CCC_STATE_DIR/wiki-candidates.seen"'

out="$(printf '%s' "$PAYLOAD" | bash "$WIKI_QUEUE" 2>&1)"; rc=$?
ok "threshold crossing emits HOT entries" '[ "$rc" = 0 ] && grep -q "added=2 skipped(dup)=0 total_in=2" <<<"$out" && grep -q "\[CAND-3\].*🔥 HOT (seen ×3).*Decision A" "$CCC_STATE_DIR/wiki-candidates.md" && grep -q "\[CAND-4\].*🔥 HOT (seen ×3).*Runbook B" "$CCC_STATE_DIR/wiki-candidates.md"'

out="$(printf '%s' "$PAYLOAD" | bash "$WIKI_QUEUE" 2>&1)"; rc=$?
ok "post-HOT duplicates skip again" '[ "$rc" = 0 ] && grep -q "added=0 skipped(dup)=2 total_in=2" <<<"$out" && ! grep -q "\[CAND-5\]" "$CCC_STATE_DIR/wiki-candidates.md"'

LEGACY_TITLE="Legacy Single"
LEGACY_HASH="$(printf '%s' "$LEGACY_TITLE" | tr '[:upper:]' '[:lower:]' | tr -s ' ' | sha256sum | cut -c1-12)"
echo "$LEGACY_HASH" >> "$CCC_STATE_DIR/wiki-candidates.seen"
LEGACY_PAYLOAD='{"session_id":"sess-legacy","trigger":"manual","wiki_candidates":[{"title":"Legacy Single","suggested_path":"pages/log.md","summary":"Legacy row test.","evidence_excerpt":"legacy"}],"honcho":[]}'
out="$(printf '%s' "$LEGACY_PAYLOAD" | bash "$WIKI_QUEUE" 2>&1)"; rc=$?
ok "single-hash legacy seen line is normalized and deduped" '[ "$rc" = 0 ] && grep -q "added=0 skipped(dup)=1 total_in=1" <<<"$out" && awk -v h="$LEGACY_HASH" '\''$4 == h && NF == 4 {found=1} END{exit !found}'\'' "$CCC_STATE_DIR/wiki-candidates.seen"'

EMPTY='{"session_id":"sess-empty","trigger":"manual","wiki_candidates":[],"honcho":[]}'
before="$(find "$CCC_STATE_DIR" -type f -printf '%P %s\n' | sort)"
out="$(printf '%s' "$EMPTY" | bash "$WIKI_QUEUE" 2>&1)"; rc=$?
after="$(find "$CCC_STATE_DIR" -type f -printf '%P %s\n' | sort)"
ok "empty candidates exits 0" '[ "$rc" = 0 ] && grep -q "no wiki candidates" <<<"$out"'
ok "empty candidates performs no writes when no stale entry exists" '[ "$before" = "$after" ]'

cat >> "$CCC_STATE_DIR/wiki-candidates.md" <<'MD'

## [CAND-99] 2020-01-01 — Old Pending
- suggested-path: `pages/log.md`
- proposed-id: TM-?? (assign at PR time)
- source-session: `old` (trigger=manual)
- distilled-at: 2020-01-01T00:00:00Z
- status: pending
- summary: Old pending item.

## [CAND-100] 2020-01-01 — Old Merged
- suggested-path: `pages/log.md`
- proposed-id: TM-?? (assign at PR time)
- source-session: `old` (trigger=manual)
- distilled-at: 2020-01-01T00:00:00Z
- status: merged
- summary: Old merged item.
MD
out="$(printf '%s' "$EMPTY" | bash "$WIKI_QUEUE" 2>&1)"; rc=$?
out2="$(printf '%s' "$EMPTY" | bash "$WIKI_QUEUE" 2>&1)"; rc2=$?
ok "stale pending entry is marked once" '[ "$rc" = 0 ] && grep -q "\[CAND-99\].*(stale: pending review)" "$CCC_STATE_DIR/wiki-candidates.md" && [ "$(grep -c "stale: pending review" "$CCC_STATE_DIR/wiki-candidates.md")" = 1 ] && [ "$rc2" = 0 ]'
ok "merged entries are not marked stale" '! grep -q "\[CAND-100\].*(stale: pending review)" "$CCC_STATE_DIR/wiki-candidates.md"'
ok "distill status documents pending stale hot counts" 'grep -q "pending=.*stale=.*hot=" "$SKILL" || grep -q "pending/stale/hot" "$SKILL"'

# ---- Issue #133: title normalization collapses cosmetic variants -----------
# Reset state so the dedup tests don't fight prior fixtures.
rm -rf "$CCC_STATE_DIR"
mkdir -p "$CCC_STATE_DIR"

# Cluster A: two #82 variants — bilingual prefix + punctuation differ, dedup must collapse.
# (Two variants stay below the HOT threshold so we can measure pure dedup behavior.)
ISSUE82_PAYLOAD='{"session_id":"sess-i82","trigger":"manual","wiki_candidates":[
  {"title":"#82 distill fleet rollout: per-node smoke 절차","suggested_path":"pages/log.md","summary":"a"},
  {"title":"이슈 #82: distill fleet verification per-node 체크리스트 현황","suggested_path":"pages/log.md","summary":"b"}
],"honcho":[]}'
out="$(printf '%s' "$ISSUE82_PAYLOAD" | bash "$WIKI_QUEUE" 2>&1)"; rc=$?
ok "issue-anchored variants: first wins, rest dedup" '[ "$rc" = 0 ] && grep -q "added=1 skipped(dup)=1 total_in=2" <<<"$out"'
ok "issue-anchored seen-file has exactly one row for cluster" '[ "$(wc -l < "$CCC_STATE_DIR/wiki-candidates.seen")" = 1 ]'

# Cluster A continued: a third sighting (different cosmetic variant) crosses the
# HOT threshold and produces a 🔥 HOT entry — proving the dedup-hit chain works.
ISSUE82_HOT_PAYLOAD='{"session_id":"sess-i82b","trigger":"manual","wiki_candidates":[
  {"title":"Distill Fleet Rollout (#82) 노드별 검증 체크리스트","suggested_path":"pages/log.md","summary":"c"}
],"honcho":[]}'
out="$(printf '%s' "$ISSUE82_HOT_PAYLOAD" | bash "$WIKI_QUEUE" 2>&1)"; rc=$?
ok "issue-anchored third sighting triggers HOT marking" '[ "$rc" = 0 ] && grep -q "added=1 skipped(dup)=0 total_in=1" <<<"$out" && grep -q "🔥 HOT (seen ×3)" "$CCC_STATE_DIR/wiki-candidates.md"'

# Cluster B: multi-issue title (#82/#83/#84) must NOT collapse into #82-only bucket.
MULTI_PAYLOAD='{"session_id":"sess-multi","trigger":"manual","wiki_candidates":[
  {"title":"ccc-node #82/#83/#84: distill fleet 검증 (rollout/outage/PreCompact)","suggested_path":"pages/log.md","summary":"multi"}
],"honcho":[]}'
out="$(printf '%s' "$MULTI_PAYLOAD" | bash "$WIKI_QUEUE" 2>&1)"; rc=$?
ok "multi-issue title is distinct from single-issue bucket" '[ "$rc" = 0 ] && grep -q "added=1 skipped(dup)=0 total_in=1" <<<"$out" && [ "$(wc -l < "$CCC_STATE_DIR/wiki-candidates.seen")" = 2 ]'

# Cluster C: sigilless variants — round-tag and punctuation should still dedup.
# Two variants stay below HOT threshold; (r18) parens-stripped + round-stripped
# must collapse with the bare form.
SIGILLESS_PAYLOAD='{"session_id":"sess-noissue","trigger":"manual","wiki_candidates":[
  {"title":"agent-cron 계층적 슬라이스 구현 전략 (r18)","suggested_path":"pages/log.md","summary":"a"},
  {"title":"agent-cron 계층적 슬라이스 구현 전략","suggested_path":"pages/log.md","summary":"b"}
],"honcho":[]}'
out="$(printf '%s' "$SIGILLESS_PAYLOAD" | bash "$WIKI_QUEUE" 2>&1)"; rc=$?
ok "sigilless variants dedup via round-tag + punctuation strip" '[ "$rc" = 0 ] && grep -q "added=1 skipped(dup)=1 total_in=2" <<<"$out"'

# Cluster D: section-prefix variants dedup.
PREFIX_PAYLOAD='{"session_id":"sess-prefix","trigger":"manual","wiki_candidates":[
  {"title":"Decision: Honcho 인증 강제 절차","suggested_path":"pages/log.md","summary":"a"},
  {"title":"결정: Honcho 인증 강제 절차","suggested_path":"pages/log.md","summary":"b"}
],"honcho":[]}'
out="$(printf '%s' "$PREFIX_PAYLOAD" | bash "$WIKI_QUEUE" 2>&1)"; rc=$?
ok "section-prefix variants (Decision/결정) dedup" '[ "$rc" = 0 ] && grep -q "added=1 skipped(dup)=1 total_in=2" <<<"$out"'

# Cluster E: distinct topics must NOT collapse.
DISTINCT_PAYLOAD='{"session_id":"sess-distinct","trigger":"manual","wiki_candidates":[
  {"title":"xurl media upload requires oauth1","suggested_path":"pages/log.md","summary":"a"},
  {"title":"Streamlit scroll UX patterns","suggested_path":"pages/log.md","summary":"b"}
],"honcho":[]}'
out="$(printf '%s' "$DISTINCT_PAYLOAD" | bash "$WIKI_QUEUE" 2>&1)"; rc=$?
ok "distinct topics are not collapsed" '[ "$rc" = 0 ] && grep -q "added=2 skipped(dup)=0 total_in=2" <<<"$out"'

# ---- Issue #298: per-session cap, dedup TTL env, --compact backlog cleanup --
rm -rf "$CCC_STATE_DIR"
mkdir -p "$CCC_STATE_DIR"

# Cap: 5 distinct candidates, default cap 3 → head 3 queued, 2 truncated.
CAP_PAYLOAD='{"session_id":"sess-cap","trigger":"manual","wiki_candidates":[
  {"title":"cap topic alpha","suggested_path":"pages/log.md","summary":"a"},
  {"title":"cap topic bravo","suggested_path":"pages/log.md","summary":"b"},
  {"title":"cap topic charlie","suggested_path":"pages/log.md","summary":"c"},
  {"title":"cap topic delta","suggested_path":"pages/log.md","summary":"d"},
  {"title":"cap topic echo","suggested_path":"pages/log.md","summary":"e"}
],"honcho":[]}'
out="$(printf '%s' "$CAP_PAYLOAD" | bash "$WIKI_QUEUE" 2>&1)"; rc=$?
ok "per-session cap keeps head 3 of 5" '[ "$rc" = 0 ] && grep -q "added=3 skipped(dup)=0 total_in=5 truncated(cap)=2" <<<"$out"'
ok "cap drops the tail candidates" 'grep -q "cap topic charlie" "$CCC_STATE_DIR/wiki-candidates.md" && ! grep -q "cap topic delta" "$CCC_STATE_DIR/wiki-candidates.md"'

out="$(printf '%s' "$CAP_PAYLOAD" | CCC_DISTILL_MAX_WIKI_CANDS=1 bash "$WIKI_QUEUE" 2>&1)"; rc=$?
ok "cap env override to 1" '[ "$rc" = 0 ] && grep -q "total_in=5 truncated(cap)=4" <<<"$out"'

# Dedup TTL env: a row last seen 10 days ago survives with TTL=30 (dedup hit)
# but is expired by the default 7-day TTL (fresh add).
rm -rf "$CCC_STATE_DIR"
mkdir -p "$CCC_STATE_DIR"
TTL_PAYLOAD='{"session_id":"sess-ttl","trigger":"manual","wiki_candidates":[
  {"title":"ttl topic zulu","suggested_path":"pages/log.md","summary":"z"}
],"honcho":[]}'
printf '%s' "$TTL_PAYLOAD" | bash "$WIKI_QUEUE" >/dev/null 2>&1
OLD_EPOCH=$(( $(date -u +%s) - 10*24*3600 ))
awk -v e="$OLD_EPOCH" '{print e, e, $3, $4}' "$CCC_STATE_DIR/wiki-candidates.seen" > "$CCC_STATE_DIR/wiki-candidates.seen.tmp" \
  && mv "$CCC_STATE_DIR/wiki-candidates.seen.tmp" "$CCC_STATE_DIR/wiki-candidates.seen"
out="$(printf '%s' "$TTL_PAYLOAD" | CCC_DISTILL_SEEN_TTL_DAYS=30 bash "$WIKI_QUEUE" 2>&1)"
ok "TTL=30 keeps 10-day-old row (dedup hit)" 'grep -q "added=0 skipped(dup)=1 total_in=1" <<<"$out"'
awk -v e="$OLD_EPOCH" '{print e, e, 1, $4}' "$CCC_STATE_DIR/wiki-candidates.seen" > "$CCC_STATE_DIR/wiki-candidates.seen.tmp" \
  && mv "$CCC_STATE_DIR/wiki-candidates.seen.tmp" "$CCC_STATE_DIR/wiki-candidates.seen"
out="$(printf '%s' "$TTL_PAYLOAD" | bash "$WIKI_QUEUE" 2>&1)"
ok "default 7-day TTL expires 10-day-old row (fresh add)" 'grep -q "added=1 skipped(dup)=0 total_in=1" <<<"$out"'

# --compact: retroactive backlog dedup. Build a queue with the dungae-style
# pattern: three pending #82 variants (one HOT-displayed), one merged #82, and
# one distinct pending topic.
rm -rf "$CCC_STATE_DIR"
mkdir -p "$CCC_STATE_DIR"
cat > "$CCC_STATE_DIR/wiki-candidates.md" <<'MD'
# Wiki Candidates Queue (auto-generated by distill; review with `/wiki-record`)

## [CAND-1] 2026-06-20 — #82 distill fleet rollout: per-node smoke 절차
- suggested-path: `pages/log.md`
- status: pending
- summary: variant one

## [CAND-2] 2026-06-22 — 이슈 #82: distill fleet verification 체크리스트 현황
- suggested-path: `pages/log.md`
- status: pending
- summary: variant two

## [CAND-3] 2026-06-24 — 🔥 HOT (seen ×3) — Distill Fleet Rollout (#82) 노드별 검증
- suggested-path: `pages/log.md`
- status: pending
- summary: variant three (hot display)

## [CAND-4] 2026-06-25 — #82 rollout retrospective merged already
- suggested-path: `pages/log.md`
- status: merged
- summary: merged entries are never compacted away

## [CAND-5] 2026-06-26 — Streamlit scroll UX patterns
- suggested-path: `pages/log.md`
- status: pending
- summary: distinct topic must survive
MD
out="$(bash "$WIKI_QUEUE" --compact 2>&1)"; rc=$?
ok "compact exits 0 and reports drops" '[ "$rc" = 0 ] && grep -q "kept=3 dropped(dup)=2 buckets=2" <<<"$out"'
ok "compact keeps newest #82 pending variant" 'grep -q "\[CAND-3\]" "$CCC_STATE_DIR/wiki-candidates.md" && ! grep -q "\[CAND-1\]" "$CCC_STATE_DIR/wiki-candidates.md" && ! grep -q "\[CAND-2\]" "$CCC_STATE_DIR/wiki-candidates.md"'
ok "compact keeps merged and distinct entries" 'grep -q "\[CAND-4\]" "$CCC_STATE_DIR/wiki-candidates.md" && grep -q "\[CAND-5\]" "$CCC_STATE_DIR/wiki-candidates.md"'
ok "compact refreshes seen for surviving buckets" '[ "$(wc -l < "$CCC_STATE_DIR/wiki-candidates.seen")" = 2 ]'
out="$(printf '%s' "{\"session_id\":\"sess-after-compact\",\"trigger\":\"manual\",\"wiki_candidates\":[{\"title\":\"#82 yet another variant\",\"suggested_path\":\"pages/log.md\",\"summary\":\"x\"}],\"honcho\":[]}" | bash "$WIKI_QUEUE" 2>&1)"
ok "post-compact re-extraction of same issue is deduped" 'grep -q "added=0 skipped(dup)=1 total_in=1" <<<"$out"'
out="$(bash "$WIKI_QUEUE" --compact 2>&1)"
ok "compact is idempotent" 'grep -q "dropped(dup)=0" <<<"$out"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
