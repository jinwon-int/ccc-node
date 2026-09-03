#!/usr/bin/env bash
# Tests for nunchi/sessionstart.sh injection wiring (#1264 P2-7): the ranked
# assemble path (default) with legacy head -c fallback, the opt-out, and the
# nunchi.mode gate. Hermetic via NUNCHI_DB/NUNCHI_SNAPSHOT/CCC_STATE_DIR.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/../lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

NUNCHI_HOME="$TMP/nunchi"
export NUNCHI_SNAPSHOT="$NUNCHI_HOME/snapshot.md"
export NUNCHI_DB="$NUNCHI_HOME/facts.db"
STATE="$TMP/state"
# CCC_MEMORY_AUDIENCE_SCOPED=0 explicitly: the ambient value on live nodes is
# 1, and sessionstart.sh exits 0 for the global snapshot on scoped nodes (the
# scoped lanes inject their own per-scope snapshot).
export CCC_STATE_DIR="$STATE" CCC_NUNCHI_MODE=on CCC_NODE=nosuk CCC_MEMORY_AUDIENCE_SCOPED=0
mkdir -p "$NUNCHI_HOME" "$STATE"

NP="$HERE/nunchi.py"
python3 "$NP" init >/dev/null   # proper schema incl. facts_fts

# Legacy-defect fixture: the recency-ordered snapshot puts a >3000B filler
# BEFORE the constraint, so legacy `head -c 3000` cuts the constraint off —
# exactly what the ranked assembly must never do.
{
  echo "## nunchi working memory (static legacy view)"
  echo "- (yukson/task-progress) FILLER-HUGE $(printf 'x%.0s' $(seq 1 3200))"
  echo "- [제약/yukson] CONSTRAINT-RULE-9001 은 규칙이다"
} > "$NUNCHI_SNAPSHOT"
python3 - "$NUNCHI_DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("INSERT INTO peer_facts(observer,observed,kind,fact,evidence,valid_from,dedup,created_at,source_rank,review,mutability)"
          " VALUES('family-assistant','yukson','decision','HINTED-DECISION 프록시 대신 직접 연결 채택','d:h1','2026-08-01','d1','2026-08-01T00:00:00+00:00',1,0,'static')")
c.execute("INSERT INTO peer_facts(observer,observed,kind,fact,evidence,valid_from,dedup,created_at,source_rank,review,mutability)"
          " VALUES('family-assistant','yukson','constraint','CONSTRAINT-RULE-9001 은 규칙이다','d:h3','2026-08-03','d3','2026-08-03T00:00:00+00:00',3,0,'static')")
c.commit()
PY

# task-conditioned hint source: stub mirrors ccc-memory-query.sh --mode local
QBINDIR="$TMP/qbin"; mkdir -p "$QBINDIR"
write_exec_stub "$QBINDIR/ccc-memory-query.sh" <<'STUB'
printf 'HINTED-DECISION 직접 연결'
STUB
export CCC_MEMORY_TOOLS_DIR="$QBINDIR"

out="$(bash "$HERE/sessionstart.sh" 2>/dev/null)"; rc=$?
ok "assemble injection exits 0" '[ "$rc" = 0 ]'
ok "hint-matched decision ranks above the filler" 'grep -q "HINTED-DECISION" <<<"$out"'
ok "constraint survives the budget (legacy cut fixed)" 'grep -q "CONSTRAINT-RULE-9001" <<<"$out"'
ok "huge recency filler is budget-skipped" '! grep -q "FILLER-HUGE" <<<"$out"'
ok "live-check legend absent when all included rows are static" '! grep -q "live-check" <<<"$out"'

out="$(CCC_NUNCHI_ASSEMBLE=0 bash "$HERE/sessionstart.sh" 2>/dev/null)"
ok "opt-out restores legacy recency order (filler present)" 'grep -q "FILLER-HUGE" <<<"$out"'
ok "opt-out truncates the constraint (the defect, honestly reproduced)" '! grep -q "CONSTRAINT-RULE-9001" <<<"$out"'

# assembly failure (NUNCHI_DB is a directory → sqlite cannot open) falls back
out="$(NUNCHI_DB="$TMP" bash "$HERE/sessionstart.sh" 2>/dev/null)"; rc=$?
ok "assemble failure falls back to legacy head -c, rc 0" '[ "$rc" = 0 ] && grep -q "FILLER-HUGE" <<<"$out"'

out="$(CCC_NUNCHI_MODE=off bash "$HERE/sessionstart.sh" 2>/dev/null)"
ok "mode=off injects nothing" '[ -z "$out" ]'

out="$(CCC_NUNCHI_ASSEMBLE_BUDGET=200 bash "$HERE/sessionstart.sh" 2>/dev/null)"
ok "tiny budget keeps the constraint and the hint match" 'grep -q "CONSTRAINT-RULE-9001" <<<"$out" && grep -q "HINTED-DECISION" <<<"$out"'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
