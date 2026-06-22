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

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
