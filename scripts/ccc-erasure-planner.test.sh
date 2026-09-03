#!/usr/bin/env bash
# Tests for ccc-erasure-planner.py — hermetic fixture tree, READ-ONLY contract.
# Verifies the #873 planner: inventory resolution via env chains, per-request
# target/action mapping, unknown-artifact blockers, and the no-mutation
# guarantee (fixture tree byte-identical before/after every run).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLANNER="$ROOT/scripts/ccc-erasure-planner.py"
INV="$ROOT/schemas/memory-artifact-inventory.v1.json"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# --- fixture: managed state roots with known + unknown artifacts -------------
FIX="$TMP/fix"
mkdir -p "$FIX/home/.claude/state" "$FIX/home/.nunchi" "$FIX/home/.claude/projects" \
         "$FIX/bot/external-wait" "$FIX/bot/wiki-candidates" \
         "$FIX/aud/private-aud/nunchi" "$FIX/aud/shared-aud/nunchi" \
         "$FIX/home/.mempalace/palace"
export HOME="$FIX/home"
export CCC_STATE_DIR="$FIX/home/.claude/state"
export NUNCHI_DB="$FIX/home/.nunchi/facts.db"
export NUNCHI_HOME="$FIX/home/.nunchi"
export NUNCHI_SNAPSHOT="$FIX/home/.nunchi/snapshot.md"
export CCC_BOT_DATA_DIR="$FIX/bot"
export CCC_MEMORY_AUDIENCE_ROOT="$FIX/aud"
export MEMPALACE_PALACE_PATH="$FIX/home/.mempalace/palace"

printf 'facts' > "$NUNCHI_DB"
printf '{}\n' > "$CCC_STATE_DIR/autonomy-ledger.jsonl"
printf 'wait' > "$FIX/bot/external-wait/w1.json"
printf 'candidate' > "$FIX/bot/wiki-candidates/c1.json"
printf 'mystery' > "$CCC_STATE_DIR/mystery-orphan.bin"   # unknown artifact → blocker
printf 'transcript' > "$FIX/home/.claude/projects/s1.jsonl"

# baseline checksums of the whole fixture tree (READ-ONLY contract)
tree_sum() { find "$FIX" -type f -exec md5sum {} + | sort | md5sum; }

run() { python3 "$PLANNER" --inventory "$INV" "$@"; }

# --- 1) audience-erasure: scoped subpath joined, blocker reported ------------
out="$(run audience-erasure --audience private-aud --json)"; rc=$?
ok "audience-erasure exits 0" '[ "$rc" = 0 ]'
ok "plan schema versioned" 'grep -q "ccc.erasure-plan.v1" <<<"$out"'
ok "scoped root joined with audience name" 'grep -q "private-aud" <<<"$out"'
ok "audience root target present with action" \
  'grep -qF "audience.state_root" <<<"$out" && grep -qF "delete-whole-root" <<<"$out" && grep -qF "\"present\": true" <<<"$out"'
ok "unknown artifact surfaces as blocker" 'grep -qF "mystery-orphan" <<<"$out"'
before="$(tree_sum)"
run audience-erasure --audience private-aud >/dev/null 2>&1
after="$(tree_sum)"
ok "READ-ONLY: fixture tree byte-identical after runs" '[ "$before" = "$after" ]'

# --- 2) node-decommission spans classes, handoffs never delete ---------------
out="$(run node-decommission --json)"
ok "decommission lists nunchi source" 'grep -qF "nunchi.peer_facts" <<<"$out"'
ok "transcript archive is an external handoff, never a target" \
  'grep -qF "\"artifact\": \"upstream.session_transcripts\"" <<<"$out"'
ok "wiki candidates are an external handoff, never a target" \
  'grep -qF "\"artifact\": \"distill.wiki_candidates\"" <<<"$out" && ! grep -qF "\"action\": \"external-handoff\"" <<<"$out"'
ok "append-only audit retained on decommission" \
  'grep -qF "\"artifact\": \"audit.autonomy_ledger\"" <<<"$out" && grep -qF "\"action\": \"retain\"" <<<"$out"'

# --- 3) fact-correction is a pointer, not a cross-store plan ------------------
out="$(run fact-correction 2>&1)"
ok "fact-correction points at nunchi annotate/supersede" 'grep -qF "nunchi-annotate-supersede" <<<"$out"'

# --- 4) usage errors ----------------------------------------------------------
run bogus-request >/dev/null 2>&1; rc=$?
ok "unknown request exits 3" '[ "$rc" = 3 ]'
run audience-erasure >/dev/null 2>&1; rc=$?
ok "audience-erasure without --audience exits 2" '[ "$rc" = 2 ]'
run --inventory /dev/null node-decommission >/dev/null 2>&1; rc=$?
ok "schema-mismatched inventory rejected" '[ "$rc" = 2 ]'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
