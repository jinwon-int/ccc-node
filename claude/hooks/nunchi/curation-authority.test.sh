#!/usr/bin/env bash
# Curation authority tiers (ccc-node#2464 제안 4, docs/nunchi-curation-authority.md).
# Static guards over the nunchi sources plus the `review` listing contract — LLM-free,
# against a private temp DB. A pass that needs more authority changes the doc and this
# suite in the same PR.
# shellcheck disable=SC2034 # assertion variables are consumed through ok/eval
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  cond: $2"; fi; }
DIR="$ROOT/claude/hooks/nunchi"
NP="$DIR/nunchi.py"
JB="$DIR/judge-batch.py"
DOC="$ROOT/docs/nunchi-curation-authority.md"

# ---- rule 1: no path deletes a fact row -------------------------------------------------
srcs=()
for f in "$DIR"/*.py; do srcs+=("$f"); done
del_hits="$(grep -niE 'DELETE[[:space:]]+FROM[[:space:]]+peer_facts' "${srcs[@]}" || true)"
ok "no nunchi source deletes from peer_facts" '[ -z "$del_hits" ]'
[ -n "$del_hits" ] && echo "$del_hits"

# ---- rule 2: judge-batch (unattended cron) writes review=0 and nothing else -------------
jb_writes="$(grep -nE 'UPDATE[[:space:]]+peer_facts|INSERT[[:space:]]+(OR[[:space:]]+[A-Z]+[[:space:]]+)?INTO[[:space:]]+peer_facts' "$JB" || true)"
jb_other="$(grep -vE 'UPDATE peer_facts SET review=0 WHERE id=\?' <<<"$jb_writes" | grep . || true)"
ok "judge-batch has at least one peer_facts write (guard is live)" '[ -n "$jb_writes" ]'
ok "judge-batch writes peer_facts only as review=0" '[ -z "$jb_other" ]'
[ -n "$jb_other" ] && echo "$jb_other"

# ---- rule 3: conflict-sweep flags, never closes -----------------------------------------
sweep_src="$(python3 - "$NP" <<'PY'
import ast, sys
src = open(sys.argv[1], encoding="utf-8").read()
for node in ast.parse(src).body:
    if isinstance(node, ast.FunctionDef) and node.name == "conflict_sweep":
        print(ast.get_source_segment(src, node))
PY
)"
ok "conflict_sweep function found" '[ -n "$sweep_src" ]'
ok "conflict_sweep never writes valid_to" '! grep -qE "SET[^\"]*valid_to[[:space:]]*=" <<<"$sweep_src"'
ok "conflict_sweep sets review=1" 'grep -q "SET review=1" <<<"$sweep_src"'

# ---- rule 3b: g5-legacy-close (#2221) sets because/kind only, never closes or flags ----
g5_src="$(python3 - "$NP" <<'PY'
import ast, sys
src = open(sys.argv[1], encoding="utf-8").read()
for node in ast.parse(src).body:
    if isinstance(node, ast.FunctionDef) and node.name == "g5_legacy_close":
        print(ast.get_source_segment(src, node))
PY
)"
ok "g5_legacy_close function found" '[ -n "$g5_src" ]'
ok "g5_legacy_close never writes valid_to" '! grep -qE "SET[^\"]*valid_to[[:space:]]*=" <<<"$g5_src"'
ok "g5_legacy_close never writes the review flag" '! grep -qE "SET[^\"]*review[[:space:]]*=" <<<"$g5_src"'
ok "g5_legacy_close peer_facts writes are because/kind only" \
  '[ -z "$(grep -oE "UPDATE peer_facts SET [a-z_=?, ]+" <<<"$g5_src" | grep -vE "^UPDATE peer_facts SET (because=\?|kind=\?, mutability=\?)$")" ]'

# ---- rule 3c: task-progress TTL (#2759) is off by default and never ages out an owner
# statement, a review-flagged fact, or a fact the owner reopened --------------------------
ttl_src="$(python3 - "$NP" <<'PY'
import ast, sys
src = open(sys.argv[1], encoding="utf-8").read()
for node in ast.parse(src).body:
    if isinstance(node, ast.FunctionDef) and node.name == "_close_expired_task_progress":
        print(ast.get_source_segment(src, node))
PY
)"
ok "_close_expired_task_progress function found" '[ -n "$ttl_src" ]'
ok "task-progress TTL default is off (\"0\")" \
  'grep -qF "_TASK_PROGRESS_TTL_DAYS = _float_env(\"NUNCHI_TASK_PROGRESS_TTL_DAYS\", \"0\")" "$NP"'
ok "task-progress TTL selects task-progress only and excludes rank 3, review and already-swept rows" \
  'grep -qF "kind='"'"'task-progress'"'"' AND valid_to IS NULL" <<<"$ttl_src" && grep -qF "COALESCE(source_rank, 1) < 3" <<<"$ttl_src" && grep -qF "COALESCE(review, 0) = 0" <<<"$ttl_src" && grep -qF "id NOT IN (SELECT fact_id FROM ttl_close)" <<<"$ttl_src"'
ok "task-progress TTL writes peer_facts as valid_to only" \
  '[ "$(grep -oE "UPDATE peer_facts SET [a-z_=?, ]+" <<<"$ttl_src")" = "UPDATE peer_facts SET valid_to=? " ]'
ok "doc names the task-progress TTL knob and its default" \
  'grep -qF "NUNCHI_TASK_PROGRESS_TTL_DAYS" "$DOC" && grep -qF "off by default" "$DOC"'

# ---- rule 3d: reattribute-unknown (#2759) moves provable subject=node rows from the legacy
# 'unknown' peer to the node name; `observed` is the only thing it rewrites about a fact ---
reattr_src="$(python3 - "$NP" <<'PY'
import ast, sys
src = open(sys.argv[1], encoding="utf-8").read()
for node in ast.parse(src).body:
    if isinstance(node, ast.FunctionDef) and (
            node.name.startswith("_reattr_") or node.name == "reattribute_unknown"):
        print(ast.get_source_segment(src, node))
PY
)"
reattr_writes="$(grep -oE "UPDATE peer_facts SET [a-z_]+" <<<"$reattr_src" | sort -u)"
ok "reattribute_unknown functions found" \
  'grep -q "^def reattribute_unknown" <<<"$reattr_src" && grep -q "^def _reattr_apply" <<<"$reattr_src"'
ok "reattribute-unknown: every peer_facts write starts from the observed column" \
  '[ "$reattr_writes" = "UPDATE peer_facts SET observed" ]'
ok "reattribute-unknown never writes fact text, kind, rank, review, dedup or timestamps" \
  '! grep -qE "(^|[ ,\"])(fact|kind|mutability|source_rank|review|dedup|created_at|valid_from|because|source_refs)[[:space:]]*=[[:space:]]*(\?|NULL|CASE)" <<<"$reattr_src"'
ok "reattribute-unknown: every write is guarded on the row's current peer" \
  '[ "$(grep -c "UPDATE peer_facts SET" <<<"$reattr_src")" = "$(grep -cE "WHERE id=\? AND observed=\?" <<<"$reattr_src")" ]'
ok "reattribute-unknown selects the legacy peer only and proves each row" \
  'grep -qF "FROM peer_facts WHERE observed=? ORDER BY id" <<<"$reattr_src" && grep -qF "_reattr_proof(observer, fact, dedup, parent, proven)" <<<"$reattr_src" && grep -qF "_UNKNOWN_PEER = \"unknown\"" "$NP"'
ok "reattribute-unknown resolves a canonical target before the DB is opened" \
  'grep -qF "target, source = _reattr_target(node)  # refuse before the DB is opened" <<<"$reattr_src"'

# ---- the boundary is stated where the owner reads it ------------------------------------
ok "usage text carries the curation authority block" 'grep -q "^Curation authority (#2464 제안 4" "$NP"'
ok "usage lists review and retag" 'grep -qE "^  nunchi.py review \[" "$NP" && grep -qE "^  nunchi.py retag " "$NP"'
ok "authority doc exists" '[ -f "$DOC" ]'
for cmd in review-stale constraints-triage conflict-sweep g5-legacy-close reattribute-unknown supersede merge annotate retag "review <id> --clear"; do
  ok "doc names owner command: $cmd" 'grep -qF -- "$cmd" "$DOC"'
done
ok "docs index links the authority page" 'grep -qF "(nunchi-curation-authority.md)" "$ROOT/docs/README.md"'

# ---- review listing contract ------------------------------------------------------------
export HOME="$TMP/home"; mkdir -p "$HOME/.nunchi"
export NUNCHI_DB="$TMP/facts.db"
export GOTGAN_EMBED=off
python3 "$NP" init >/dev/null 2>&1
out="$(python3 "$NP" review 2>&1)"; rc=$?
ok "empty queue: review exits 0" '[ "$rc" = 0 ]'
ok "empty queue: last line states the authority boundary" '[ "$(tail -1 <<<"$out")" = "$(python3 -c "import importlib.util as u,sys;s=u.spec_from_file_location(\"n\",sys.argv[1]);m=u.module_from_spec(s);s.loader.exec_module(m);print(m.REVIEW_AUTHORITY_LINE)" "$NP")" ]'
ok "authority line names the doc and the no-delete rule" 'grep -q "nothing deletes a fact (docs/nunchi-curation-authority.md)" <<<"$out"'
python3 - "$NUNCHI_DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("INSERT INTO peer_facts(observer,observed,kind,fact,evidence,valid_from,created_at,review)"
          " VALUES('t','user','fact','queued fact for authority test','', '2026-10-20T00:00:00+00:00',"
          " '2026-10-20T00:00:00+00:00',1)")
c.commit()
PY
out="$(python3 "$NP" review 2>&1)"
ok "queued fact is listed" 'grep -q "queued fact for authority test" <<<"$out"'
ok "pending summary keeps its shape, one line above the authority line" '[ "$(tail -2 <<<"$out" | head -1 | cut -d" " -f1-2)" = "1 pending" ] && tail -1 <<<"$out" | grep -q "^authority: "'
fid="$(python3 -c "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute('SELECT id FROM peer_facts WHERE review=1').fetchone()[0])" "$NUNCHI_DB")"
clr="$(python3 "$NP" review "$fid" --clear 2>&1)"
ok "per-fact clear output is unchanged (no authority line)" '[ "$clr" = "#$fid review flag cleared" ]'
rows="$(python3 -c "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute('SELECT COUNT(*) FROM peer_facts').fetchone()[0])" "$NUNCHI_DB")"
ok "clearing keeps the row (flag only)" '[ "$rows" = 1 ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
