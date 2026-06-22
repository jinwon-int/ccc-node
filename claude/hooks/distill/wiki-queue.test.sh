#!/usr/bin/env bash
# Tests for distill/wiki-queue.sh — hermetic local state only.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WIKI_QUEUE="$HERE/wiki-queue.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

export CCC_STATE_DIR="$TMP/state"
mkdir -p "$CCC_STATE_DIR"

PAYLOAD='{"session_id":"sess-wiki","trigger":"manual","wiki_candidates":[{"title":"Decision A","suggested_path":"pages/team/dungae/DECISIONS.md","summary":"Keep the safe path.","evidence_excerpt":"operator said safe"},{"title":"Runbook B","suggested_path":"pages/nodes/dungae/RUNBOOK.md","summary":"Do the thing.","evidence_excerpt":"command output"}],"honcho":[]}'

out="$(printf '%s' "$PAYLOAD" | bash "$WIKI_QUEUE" 2>&1)"; rc=$?
ok "first append exits 0" '[ "$rc" = 0 ]'
ok "first append reports two additions" 'grep -q "wiki-queue session=sess-wiki added=2 skipped(dup)=0 total_in=2" <<<"$out"'
ok "queue file has bootstrap header and candidates" 'grep -q "Wiki Candidates Queue" "$CCC_STATE_DIR/wiki-candidates.md" && grep -q "\[CAND-1\].*Decision A" "$CCC_STATE_DIR/wiki-candidates.md" && grep -q "\[CAND-2\].*Runbook B" "$CCC_STATE_DIR/wiki-candidates.md"'
ok "candidate metadata is recorded" 'grep -q "source-session:.*sess-wiki" "$CCC_STATE_DIR/wiki-candidates.md" && grep -q "status: pending" "$CCC_STATE_DIR/wiki-candidates.md"'
ok "seen file stores two hashes" '[ "$(wc -l < "$CCC_STATE_DIR/wiki-candidates.seen")" = 2 ]'

out="$(printf '%s' "$PAYLOAD" | bash "$WIKI_QUEUE" 2>&1)"; rc=$?
ok "duplicate append exits 0" '[ "$rc" = 0 ]'
ok "duplicate append skips both candidates" 'grep -q "added=0 skipped(dup)=2 total_in=2" <<<"$out"'
ok "duplicate append does not add CAND-3" '! grep -q "\[CAND-3\]" "$CCC_STATE_DIR/wiki-candidates.md"'

EMPTY='{"session_id":"sess-empty","trigger":"manual","wiki_candidates":[],"honcho":[]}'
before="$(find "$CCC_STATE_DIR" -type f -printf '%P %s\n' | sort)"
out="$(printf '%s' "$EMPTY" | bash "$WIKI_QUEUE" 2>&1)"; rc=$?
after="$(find "$CCC_STATE_DIR" -type f -printf '%P %s\n' | sort)"
ok "empty candidates exits 0" '[ "$rc" = 0 ] && grep -q "no wiki candidates" <<<"$out"'
ok "empty candidates performs no writes" '[ "$before" = "$after" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
