#!/usr/bin/env bash
# Tests for distill/local-facts.sh — hermetic local state only.
# Verifies the distill→local-index learning loop: a fact distilled this session
# is appended to memory-facts.jsonl in the schema the SQLite index reads, with
# append-time dedup, bounded growth, fail-open, and an off-switch. The final
# block proves the end-to-end recall: index rebuild + search surfaces the fact.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
LOCAL_FACTS="$HERE/local-facts.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

export CCC_STATE_DIR="$TMP/state"
mkdir -p "$CCC_STATE_DIR"
FACTS="$CCC_STATE_DIR/memory-facts.jsonl"

PAYLOAD='{"session_id":"sess-lf","trigger":"sessionend","distilled_at":"2026-06-28T00:00:00Z","honcho":[
  {"kind":"preference","text":"Operator prefers Helix as the current editor","subject":"operator"},
  {"kind":"decision","text":"Honcho auth is enforced via OAuth subprocess","subject":"honcho"}
],"wiki_candidates":[]}'

# ---- first append ----------------------------------------------------------
out="$(printf '%s' "$PAYLOAD" | bash "$LOCAL_FACTS" 2>&1)"; rc=$?
ok "first append exits 0" '[ "$rc" = 0 ]'
ok "first append reports two facts" 'grep -q "appended 2 fact" <<<"$out"'
ok "facts file has two lines" '[ "$(wc -l < "$FACTS")" = 2 ]'
ok "facts file is chmod 600" '[ "$(stat -c %a "$FACTS")" = 600 ]'
ok "each line is valid json with index schema" 'jq -e "select(.id and .kind and .text and .review == \"auto-local\" and .privacy == \"private\" and .source.type == \"distill\")" "$FACTS" >/dev/null'
ok "id is distill-prefixed" 'jq -re ".id" "$FACTS" | grep -q "^distill-"'
ok "tags carry distilled + trigger" 'jq -e "select((.tags | index(\"distilled\")) and (.tags | index(\"sessionend\")))" "$FACTS" >/dev/null'
ok "durability is omitted (index derives it)" '[ "$(jq -r "has(\"durability\")" "$FACTS" | sort -u)" = "false" ]'

# ---- dedup -----------------------------------------------------------------
out="$(printf '%s' "$PAYLOAD" | bash "$LOCAL_FACTS" 2>&1)"; rc=$?
ok "duplicate append exits 0" '[ "$rc" = 0 ]'
ok "duplicate append adds nothing" '[ "$(wc -l < "$FACTS")" = 2 ]'

# A new fact mixed with seen ones: only the new one is appended.
MIXED='{"session_id":"sess-lf2","trigger":"manual","honcho":[
  {"kind":"preference","text":"Operator prefers Helix as the current editor","subject":"operator"},
  {"kind":"fact","text":"Default memory profile is honcho","subject":"ccc"}
]}'
out="$(printf '%s' "$MIXED" | bash "$LOCAL_FACTS" 2>&1)"; rc=$?
ok "mixed append adds only the new fact" '[ "$rc" = 0 ] && grep -q "appended 1 fact" <<<"$out" && [ "$(wc -l < "$FACTS")" = 3 ]'

# ---- bounded growth --------------------------------------------------------
boundstate="$TMP/bound"; mkdir -p "$boundstate"
boundfacts="$boundstate/memory-facts.jsonl"
BIG='{"session_id":"sess-big","trigger":"manual","honcho":[
  {"kind":"fact","text":"alpha one"},{"kind":"fact","text":"beta two"},
  {"kind":"fact","text":"gamma three"},{"kind":"fact","text":"delta four"}
]}'
out="$(CCC_STATE_DIR="$boundstate" CCC_MEMORY_FACTS_FILE="$boundfacts" CCC_LOCAL_FACTS_MAX=2 \
  bash "$LOCAL_FACTS" <<<"$BIG" 2>&1)"; rc=$?
ok "bounded growth caps file at max, keeping most recent" '[ "$rc" = 0 ] && [ "$(wc -l < "$boundfacts")" = 2 ] && grep -q "delta four" "$boundfacts" && ! grep -q "alpha one" "$boundfacts"'

# ---- fail-open -------------------------------------------------------------
out="$(printf '' | bash "$LOCAL_FACTS" 2>&1)"; rc=$?
ok "empty input exits 0" '[ "$rc" = 0 ]'
out="$(printf 'not json at all {{{' | bash "$LOCAL_FACTS" 2>&1)"; rc=$?
ok "garbage input exits 0 with no crash" '[ "$rc" = 0 ]'
NOHONCHO='{"session_id":"x","trigger":"manual","honcho":[]}'
before="$(wc -l < "$FACTS")"
out="$(printf '%s' "$NOHONCHO" | bash "$LOCAL_FACTS" 2>&1)"; rc=$?
ok "empty honcho list writes nothing" '[ "$rc" = 0 ] && [ "$(wc -l < "$FACTS")" = "$before" ]'

# ---- off-switch ------------------------------------------------------------
offstate="$TMP/off"; mkdir -p "$offstate"
touch "$offstate/distill.disabled"
out="$(CCC_STATE_DIR="$offstate" bash "$LOCAL_FACTS" <<<"$PAYLOAD" 2>&1)"; rc=$?
ok "off-switch skips and writes no facts file" '[ "$rc" = 0 ] && grep -q "disabled" <<<"$out" && [ ! -f "$offstate/memory-facts.jsonl" ]'

# ---- audience privacy labels ----------------------------------------------
audroot="$TMP/audiences"; private_scope="private-11111111111111111111111111111111"
sharedstate="$audroot/shared/state"; privatestate="$audroot/$private_scope/state"
mkdir -p "$sharedstate" "$privatestate"
CCC_STATE_DIR="$sharedstate" CCC_MEMORY_AUDIENCE_SCOPED=1 CCC_MEMORY_AUDIENCE=shared \
  CCC_MEMORY_AUDIENCE_ROOT="$audroot" CCC_MEMORY_SCOPE=shared \
  CCC_MEMORY_FACTS_FILE="$sharedstate/memory-facts.jsonl" \
  bash "$LOCAL_FACTS" <<<"$PAYLOAD" >/dev/null 2>&1
CCC_STATE_DIR="$privatestate" CCC_MEMORY_AUDIENCE_SCOPED=1 CCC_MEMORY_AUDIENCE=private \
  CCC_MEMORY_AUDIENCE_ROOT="$audroot" CCC_MEMORY_SCOPE="$private_scope" \
  CCC_MEMORY_FACTS_FILE="$privatestate/memory-facts.jsonl" \
  bash "$LOCAL_FACTS" <<<"$PAYLOAD" >/dev/null 2>&1
ok "shared audience facts are explicitly public" 'jq -e "select(.privacy == \"shared\" and .audience == \"shared\")" "$sharedstate/memory-facts.jsonl" >/dev/null'
ok "private audience facts stay private" 'jq -e "select(.privacy == \"private\" and .audience == \"private\")" "$privatestate/memory-facts.jsonl" >/dev/null'
badstate="$TMP/bad-audience"; mkdir -p "$badstate"
out="$(CCC_STATE_DIR="$badstate" CCC_MEMORY_AUDIENCE_SCOPED=1 CCC_MEMORY_AUDIENCE=unexpected CCC_MEMORY_AUDIENCE_ROOT="$audroot" CCC_MEMORY_SCOPE=shared bash "$LOCAL_FACTS" <<<"$PAYLOAD" 2>&1)"; rc=$?
ok "invalid scoped audience fails closed without writing" '[ "$rc" = 0 ] && grep -q "invalid audience" <<<"$out" && [ ! -e "$badstate/memory-facts.jsonl" ]'

# ---- end-to-end recall via index + search ----------------------------------
# A distilled fact must be locally recallable next session through the hot index.
if python3 -c "import sqlite3" 2>/dev/null; then
  e2e="$TMP/e2e"; mkdir -p "$e2e/memories" "$e2e/cache"
  e2efacts="$e2e/memory-facts.jsonl"
  E2E_PAYLOAD='{"session_id":"sess-e2e","trigger":"sessionend","honcho":[
    {"kind":"preference","text":"Operator switched the current editor to Helix from Neovim","subject":"operator"}
  ]}'
  CCC_STATE_DIR="$e2e" CCC_MEMORY_FACTS_FILE="$e2efacts" bash "$LOCAL_FACTS" <<<"$E2E_PAYLOAD" >/dev/null 2>&1
  CCC_STATE_DIR="$e2e" CCC_MEMORY_CACHE_DIR="$e2e/cache" CCC_MEMORY_DIR="$e2e/memories" \
    CCC_MEMORY_FACTS_FILE="$e2efacts" bash "$ROOT/scripts/ccc-memory-index.sh" rebuild >/dev/null 2>&1
  out="$(CCC_STATE_DIR="$e2e" CCC_MEMORY_INDEX_DB="$e2e/memory-index.sqlite" \
    bash "$ROOT/scripts/ccc-memory-search.sh" "current editor Helix" 2>&1)"; rc=$?
  ok "index+search recalls the distilled fact" '[ "$rc" = 0 ] && grep -qi "Helix" <<<"$out"'
else
  echo "skip: end-to-end recall (python sqlite3 absent)"
fi

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
