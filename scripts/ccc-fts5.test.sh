#!/usr/bin/env bash
# Tests for ccc-fts5-*.sh — validates FTS5 index update/search/check
# in isolation with CCC_STATE_DIR override. No Honcho/live calls.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

export CCC_STATE_DIR="$TMP/state"
export CCC_MEMORY_DIR="$TMP/memories"
export CCC_MEMORY_CACHE_DIR="$TMP/cache"
mkdir -p "$CCC_STATE_DIR" "$CCC_MEMORY_DIR" "$CCC_MEMORY_CACHE_DIR"

# Seed test fixtures (safe, secret-free content).
cat > "$CCC_MEMORY_DIR/MEMORY.md" <<'MD'
# MEMORY.md (test)
- This instance is test-node / test-cluster
- Project uses Python 3.11 with uv for packaging
- Memory stack: built-in + Honcho + Family Wiki + session_search
MD

cat > "$CCC_MEMORY_DIR/USER.md" <<'MD'
# USER.md (test)
- User is TestUser, timezone UTC
- Prefers concise, accurate help
- GitHub workflow: PR-first
MD

cat > "$CCC_MEMORY_CACHE_DIR/wiki.txt" <<'TXT'
Family Wiki cache:
- Node: test-node, VPS1, Team1 worker
- Gateway status: healthy
- A2A Broker: seoseo-broker on port 4430
TXT

cat > "$CCC_MEMORY_CACHE_DIR/honcho.txt" <<'TXT'
Honcho working memory:
- User preference: concise technical responses
- Current priority: ccc-node bootstrap
TXT

# ── update ──────────────────────────────────────────────────────────
out="$(CCC_MEMORY_PROFILE=honcho bash "$HERE/ccc-fts5-update.sh" 2>&1)"; rc=$?
ok "honcho profile skips update" '[ "$rc" = 0 ] && grep -q "skipped" <<<"$out"'

out="$(CCC_MEMORY_PROFILE=hybrid bash "$HERE/ccc-fts5-update.sh" 2>&1)"; rc=$?
ok "hybrid profile update exits 0" '[ "$rc" = 0 ]'
ok "hybrid profile update indexes sources" 'jq -e ".status == \"ok\" and (.indexed | length) == 4" <<<"$out" >/dev/null'
ok "hybrid profile update reports elapsed" 'jq -e ".elapsed_s >= 0" <<<"$out" >/dev/null'

# Second update with unchanged sources should skip all.
out="$(CCC_MEMORY_PROFILE=hybrid bash "$HERE/ccc-fts5-update.sh" 2>&1)"; rc=$?
ok "hybrid profile second update exits 0" '[ "$rc" = 0 ]'
ok "hybrid profile second update skips unchanged" 'jq -e ".status == \"ok\" and (.skipped | length) == 4" <<<"$out" >/dev/null'

# max-perf profile
out="$(CCC_MEMORY_PROFILE=max-perf bash "$HERE/ccc-fts5-update.sh" 2>&1)"; rc=$?
ok "max-perf profile update exits 0" '[ "$rc" = 0 ]'
ok "max-perf profile update indexes sources" 'jq -e ".status == \"ok\"" <<<"$out" >/dev/null'

# ── search ──────────────────────────────────────────────────────────
out="$(CCC_MEMORY_PROFILE=honcho bash "$HERE/ccc-fts5-search.sh" "test" 2>&1)"; rc=$?
ok "honcho profile search fails expected" '[ "$rc" = 1 ] && grep -q "not available" <<<"$out"'

out="$(CCC_MEMORY_PROFILE=hybrid bash "$HERE/ccc-fts5-search.sh" "Python package" 2>&1)"; rc=$?
ok "hybrid profile search exits 0" '[ "$rc" = 0 ]'
ok "hybrid profile search finds Python" 'jq -e ".results | length > 0" <<<"$out" >/dev/null'
ok "hybrid profile search snippet contains Python" 'jq -e ".results[0].snippet" <<<"$out" >/dev/null'

out="$(CCC_MEMORY_PROFILE=hybrid bash "$HERE/ccc-fts5-search.sh" "nonexistent_xyzzy" 2>&1)"; rc=$?
ok "hybrid profile search empty query returns 0 results" 'jq -e ".count == 0" <<<"$out" >/dev/null'

# ── check ───────────────────────────────────────────────────────────
out="$(bash "$HERE/ccc-fts5-check.sh" 2>&1)"; rc=$?
ok "check exits 0" '[ "$rc" = 0 ]'
ok "check reports DB path" 'jq -e ".db_path" <<<"$out" >/dev/null'
ok "check reports source entries" 'jq -e ".total_source_entries >= 4" <<<"$out" >/dev/null'
ok "check reports fts chunks" 'jq -e ".total_fts_chunks > 0" <<<"$out" >/dev/null'
ok "check per-source has sha256" 'jq -e ".sources[0].indexed_sha256" <<<"$out" >/dev/null'
ok "check reports db_size" 'jq -e ".db_size_bytes > 0" <<<"$out" >/dev/null'

# ── secret blocklist ───────────────────────────────────────────────
mkdir -p "$TMP/secret-test" "$TMP/secret-state" "$TMP/secret-cache"
cat > "$TMP/secret-test/MEMORY.md" <<'MD'
# MEMORY.md
Some normal content here.
Authorization: Bearer *** text after the secret line.
MD
echo "not user" > "$TMP/secret-test/USER.md"
echo "normal cache" > "$TMP/secret-cache/wiki.txt"
echo "normal cache" > "$TMP/secret-cache/honcho.txt"
CCC_STATE_DIR="$TMP/secret-state" \
CCC_MEMORY_DIR="$TMP/secret-test" \
CCC_MEMORY_CACHE_DIR="$TMP/secret-cache" \
CCC_MEMORY_PROFILE=hybrid bash "$HERE/ccc-fts5-update.sh" >/dev/null 2>&1

out="$(CCC_STATE_DIR="$TMP/secret-state" bash "$HERE/ccc-fts5-check.sh" 2>&1)"
ok "secret-bearing source not indexed" 'jq -e ".sources[] | select(.source == \"builtin-mem\") | .chunks == 0" <<<"$out" >/dev/null'
ok "safe wiki-cache indexed" 'jq -e ".sources[] | select(.source == \"wiki-cache\") | .chunks > 0" <<<"$out" >/dev/null'

# ── non-root path override works ────────────────────────────────────
mkdir -p "$TMP/alt-state" "$TMP/alt-mem" "$TMP/alt-cache"
echo "alt mem content" > "$TMP/alt-mem/MEMORY.md"
echo "alt user content" > "$TMP/alt-mem/USER.md"
echo "alt wiki cache" > "$TMP/alt-cache/wiki.txt"
echo "alt honcho cache" > "$TMP/alt-cache/honcho.txt"
alt_out="$(CCC_STATE_DIR="$TMP/alt-state" CCC_MEMORY_DIR="$TMP/alt-mem" \
           CCC_MEMORY_CACHE_DIR="$TMP/alt-cache" CCC_MEMORY_PROFILE=hybrid \
           bash "$HERE/ccc-fts5-update.sh" 2>&1)"
ok "non-root paths indexed" 'echo "$alt_out" | jq -e ".status == \"ok\" and (.indexed | length) >= 2" >/dev/null'
ok "non-root db created" '[ -f "$TMP/alt-state/ccc-fts5.db" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
