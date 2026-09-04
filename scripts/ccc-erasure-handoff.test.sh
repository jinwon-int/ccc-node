#!/usr/bin/env bash
# Tests for ccc-erasure-handoff.py — the #873 step-5 handoff manifest writer.
# Hermetic fixture tree; the writer touches ONLY the two manifest files it
# creates (tree checksum over the fixture inputs must not move). Matrix:
# manifest structure (versioned schema fields), plan digest binding to the
# planner output, owner split (family-wiki disposition proposal vs operator
# decision rows), drain-first outbox depths, --queue marker aggregation,
# body-free output, request validation, and the .md twin.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HANDOFF="$ROOT/scripts/ccc-erasure-handoff.py"
PLANNER="$ROOT/scripts/ccc-erasure-planner.py"
SCHEMA="$ROOT/schemas/erasure-handoff-manifest.v1.schema.json"
INV="$ROOT/schemas/memory-artifact-inventory.v1.json"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  cond: $2"; fi; }

FIX="$TMP/fix"
mkdir -p "$FIX/state" "$FIX/bot/wiki-candidates" "$FIX/home/.nunchi"
export CCC_STATE_DIR="$FIX/state"
export CCC_BOT_DATA_DIR="$FIX/bot"
export T_HOME="$FIX/home/.nunchi"
export HOME="$FIX/home"

python3 -c "import json; json.load(open('$SCHEMA'))" 2>/dev/null \
  && ok "manifest schema file is valid JSON" 'true' \
  || ok "manifest schema file is valid JSON" 'false'

# --- fixture: a real nunchi store (for planner resolution) + queue + outbox --
NUNCHI_DB="$T_HOME/facts.db" NUNCHI_HOME="$T_HOME" CCC_STATE_DIR="$CCC_STATE_DIR" \
  python3 "$ROOT/claude/hooks/nunchi/nunchi.py" init >/dev/null
printf 'candidate\n' > "$CCC_BOT_DATA_DIR/wiki-candidates/c1.json"
QUEUE="$CCC_STATE_DIR/wiki-candidates.md"
cat > "$QUEUE" <<'EOF'
# Wiki Candidates Queue

## [CAND-1] 2026-08-20 — 대기 항목
<!-- nunchi-p3-8 fact#7 h=abc123456789 -->
- status: pending

## [CAND-2] 2026-08-21 — 머지 항목
<!-- nunchi-p3-8 fact#7 h=abc123456789 -->
- status: merged

## [CAND-3] 2026-08-22 — 기각 항목
- status: rejected
EOF

run() { python3 "$HANDOFF" --inventory "$INV" "$@"; }

# --- 1) manifest generation for node-decommission -----------------------------
# Capture the plan digest BEFORE the writer runs: the manifest pair itself
# becomes a retain target of the next plan (classified by design), so the
# equality being tested is "manifest.plan_digest == the plan it was born from".
plan_digest="$(python3 "$PLANNER" --inventory "$INV" node-decommission --json \
  | python3 -c '
import hashlib, json, sys
plan = json.load(sys.stdin)
print(hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=True, indent=1).encode()).hexdigest())')"
out="$(run node-decommission --queue "$QUEUE" --json)"; rc=$?
ok "writer exits 0 and writes the manifest" '[ "$rc" = 0 ] && grep -q "manifest written" <<<"$out"'
MANIFEST="$(grep -oE '/[^ ]+\.json' <<<"$out" | head -1)"
ok "manifest filename follows the versioned pattern" \
  'basename "$MANIFEST" | grep -qE "^erasure-handoff-node-decommission-[0-9TZ]+\.json$"'
ok "md twin written next to the json manifest" '[ -f "${MANIFEST%.json}.md" ]'
ok "manifest carries the versioned schema id" 'grep -qF "\"schema\": \"ccc.erasure-handoff-manifest.v1\"" "$MANIFEST"'
ok "manifest validates against the shipped schema (structure)" \
  "python3 -c \"
import json, sys
m = json.load(open('$MANIFEST'))
s = json.load(open('$SCHEMA'))
missing = [k for k in s['required'] if k not in m]
assert not missing, missing
assert m['schema'] == 'ccc.erasure-handoff-manifest.v1'
assert m['ack']['required'] is True and m['ack']['granted_at'] is None
\"" 

# --- 2) plan digest binding ----------------------------------------------------
ok "manifest records the live plan's canonical digest" \
  "python3 -c \"
import json
m = json.load(open('$MANIFEST'))
assert m['plan_digest'] == '$plan_digest', m['plan_digest']
\""

# --- 3) owner split: wiki disposition vs operator decision ---------------------
ok "wiki-referencing handoff row gets an annotate proposal, decision open" \
  "python3 -c \"
import json
m = json.load(open('$MANIFEST'))
w = [x for x in m['wiki_disposition'] if x['artifact'] == 'distill.wiki_candidates']
assert w and w[0]['proposal'] == 'annotate' and w[0]['decision'] is None, w
\""
ok "operator-owned material lands in operator_decision_required" \
  "python3 -c \"
import json
m = json.load(open('$MANIFEST'))
assert 'upstream.session_transcripts' in [x['artifact'] for x in m['operator_decision_required']]
\""

# --- 4) drain-first outbox depths ----------------------------------------------
printf 'entry\n' > "$CCC_BOT_DATA_DIR/wiki-candidates/c2.json"
out="$(run node-decommission --json)"
MANIFEST2="$(grep -oE '/[^ ]+\.json' <<<"$out" | head -1)"
ok "drain-first counts outbox entries" \
  "python3 -c \"
import json
m = json.load(open('$MANIFEST2'))
d = {x['artifact']: x for x in m['drain_first']}
assert d['distill.wiki_candidates']['pending'] == 2, d['distill.wiki_candidates']
assert d['distill.journal']['pending'] == 0
\""

# --- 5) --queue marker aggregation ---------------------------------------------
ok "queue markers aggregated: statuses + fact ids (deduped)" \
  "python3 -c \"
import json
m = json.load(open('$MANIFEST'))
mk = m['wiki_markers']
assert mk['pending'] == 1 and mk['merged'] == 1 and mk['rejected'] == 1, mk
assert mk['fact_ids'] == [7], mk
\""

# --- 6) body-free output --------------------------------------------------------
ok "manifest never carries fact text (queue content absent)" \
  '! grep -qF "머지 항목" "$MANIFEST" && ! grep -qF "대기 항목" "$MANIFEST"'

# --- 7) read-only: inputs unchanged (only the manifest pair appears) ------------
BEFORE="$(find "$CCC_BOT_DATA_DIR" "$T_HOME" -type f -exec md5sum {} + | sort | md5sum)"
run node-decommission >/dev/null
ok "writer leaves all inputs byte-identical" \
  '[ "$BEFORE" = "$(find "$CCC_BOT_DATA_DIR" "$T_HOME" -type f -exec md5sum {} + | sort | md5sum)" ]'

# --- 8) request validation -------------------------------------------------------
run bogus-request >/dev/null 2>&1; rc=$?
ok "unknown request exits 4 (blocked)" '[ "$rc" = 4 ]'
run audience-erasure >/dev/null 2>&1; rc=$?
ok "audience-erasure without --audience exits 4" '[ "$rc" = 4 ]'
run node-decommission --queue "$FIX/missing-queue.md" >/dev/null 2>&1; rc=$?
ok "unreadable --queue exits 4" '[ "$rc" = 4 ]'

echo "----------------------------------------"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
