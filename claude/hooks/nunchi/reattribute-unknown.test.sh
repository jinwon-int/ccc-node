#!/usr/bin/env bash
# reattribute-unknown (ccc-node#2759, #2542 제안 3): move legacy observed='unknown'
# facts to the node name. Private temp DB only (NUNCHI_DB/NUNCHI_HOME/CCC_STATE_DIR/HOME);
# LLM-free. The legacy rows are produced the way they were produced on real nodes — a real
# `ingest` whose node identity does not resolve — so the dedup keys are the real ones.
# Covers: refusals (unknown / non-canonical / contradicting identity), dry-run is read-only,
# apply renames open and closed rows and nothing but `observed`, dedup untouched (a re-fed
# payload stays a no-op), FTS follows (recall by node name finds them, recall "unknown" no
# longer does), collision handling (the already-correct row is never touched), G2 keep-both,
# unproven rows and other peers untouched, atomic rollback, idempotence, ledger, --revert.
# No fleet identifier is written here: roster names are read from NODE_ALIASES at run time
# and the hostname is always a patched placeholder.
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
export HOME="$TMP/home"; mkdir -p "$HOME" "$TMP/state" "$TMP/nunchi"
export NUNCHI_HOME="$TMP/nunchi" NUNCHI_DB="$TMP/nunchi/facts.db" NUNCHI_SNAPSHOT="$TMP/nunchi/snapshot.md"
export CCC_STATE_DIR="$TMP/state"
export GOTGAN_EMBED=off

# Every nunchi call goes through this launcher: the hostname fallback of _node_name()
# (#2541) must not depend on the machine running the suite.
cat >"$TMP/run.py" <<'PY'
import os
import runpy
import sys


class U:  # minimal uname_result stand-in
    nodename = "host-unmapped"


os.uname = lambda: U
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
PY
nu() { python3 "$TMP/run.py" "$NP" "$@"; }       # identity unresolved (the legacy state)
named() { CCC_NODE="$NODE" nu "$@"; }             # identity resolved to a roster node
read -r NODE OTHER ALIAS < <(python3 - "$DIR" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import nunchi
slugs = sorted(set(nunchi.NODE_ALIASES.values()))
alias = next(k for k, v in nunchi.NODE_ALIASES.items() if v == slugs[0] and k != v)
print(slugs[0], slugs[1], alias)
PY
)
q() { python3 -c "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);print('\n'.join('|'.join(str(x) for x in r) for r in c.execute(sys.argv[2])))" "$NUNCHI_DB" "$1"; }
dump() { q "SELECT * FROM peer_facts ${1:-} ORDER BY id"; }
# every column except `observed`
rest() { q "SELECT id,observer,kind,fact,evidence,confidence,valid_from,valid_to,supersedes,dedup,created_at,source_rank,review,because,source_refs,mutability FROM peer_facts WHERE id IN ($1) ORDER BY id"; }
fid() { q "SELECT id FROM peer_facts WHERE fact='$1'"; }
obs() { q "SELECT observed FROM peer_facts WHERE id=$1"; }
tables() { q "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'facts_fts%' ORDER BY name"; }
feed() {  # feed <sid> <kind> <subject> <text>   (identity unresolved)
  printf '{"session_id":"%s","distilled_at":"2026-10-01T00:00:00+00:00","honcho":[{"kind":"%s","subject":"%s","text":"%s"}]}' \
    "$1" "$2" "$3" "$4" | nu ingest - >/dev/null
}
dbcopy() {  # consistent copy of a WAL database
  python3 -c "import sqlite3,sys;s=sqlite3.connect(sys.argv[1]);d=sqlite3.connect(sys.argv[2]);s.backup(d);d.close();s.close()" "$1" "$2"
}

ok "roster names resolved without a literal" '[ -n "$NODE" ] && [ -n "$OTHER" ] && [ -n "$ALIAS" ] && [ "$NODE" != "$OTHER" ]'
nu init >/dev/null 2>&1

# ---- fixture: legacy rows through the real ingest path ---------------------------------
feed l1 fact node "legacyalpha 디스크 배치는 단일 볼륨이다"
feed l2 task-progress node "legacybeta 색인 이전 작업을 맡았다"
feed l3 fact node "legacygamma 러너 이미지는 구버전이다"
nu supersede "$(fid "legacygamma 러너 이미지는 구버전이다")" "legacygamma 러너 이미지는 신버전이다" >/dev/null
feed l4 fact node "legacydelta 공유 캐시 경로는 하나다"
feed l5 fact node "legacyepsilon 오너가 직접 말한 규칙이다"
python3 - "$NUNCHI_DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("UPDATE peer_facts SET source_rank=3 WHERE fact LIKE 'legacyepsilon%'")
stamp = "2026-10-01T00:00:00+00:00"
# hand-inserted rows: observed='unknown' but no subject=node dedup key -> unproven
c.execute("INSERT INTO peer_facts(observer,observed,kind,fact,evidence,valid_from,dedup,created_at)"
          " VALUES('family-assistant','unknown','fact','handzeta 출처를 알 수 없는 행','manual',?,'hand-1',?)",
          (stamp, stamp))
c.execute("INSERT INTO peer_facts(observer,observed,kind,fact,evidence,valid_from,dedup,created_at)"
          " VALUES('family-assistant','unknown','context','handtheta dedup 없는 행','manual',?,NULL,?)",
          (stamp, stamp))
c.commit()
PY
nu supersede "$(fid "handzeta 출처를 알 수 없는 행")" "handeta 출처 불명 행의 후속" >/dev/null
# already-correct rows under the node name (a distiller that emitted the slug as subject)
feed n1 fact "$NODE" "legacydelta 공유 캐시 경로는 하나다"
feed n2 fact "$NODE" "legacyepsilon 오너가 직접 말한 규칙이다"
feed n3 fact "$NODE" "nodefact 처음부터 올바르게 귀속된 사실"
# other peers
feed u1 preference user "userfact 사용자는 unknown 이라는 낱말을 쓴다"
feed s1 fact session "sessionfact 세션 한정 사실"
printf '{"honcho":[{"kind":"fact","subject":"session","text":"nosidfact 세션 id 없는 페이로드"}]}' | nu ingest - >/dev/null
feed o1 fact "$OTHER" "otherfact 다른 노드에 대한 사실"

A="$(fid "legacyalpha 디스크 배치는 단일 볼륨이다")"; B="$(fid "legacybeta 색인 이전 작업을 맡았다")"
C="$(fid "legacygamma 러너 이미지는 구버전이다")"; D="$(fid "legacygamma 러너 이미지는 신버전이다")"
DUP="$(q "SELECT id FROM peer_facts WHERE fact LIKE 'legacydelta%' AND observed='unknown'")"
SURV="$(q "SELECT id FROM peer_facts WHERE fact LIKE 'legacydelta%' AND observed='$NODE'")"
HI="$(q "SELECT id FROM peer_facts WHERE fact LIKE 'legacyepsilon%' AND observed='unknown'")"
LO="$(q "SELECT id FROM peer_facts WHERE fact LIKE 'legacyepsilon%' AND observed='$NODE'")"
Z="$(fid "handzeta 출처를 알 수 없는 행")"; E="$(fid "handeta 출처 불명 행의 후속")"; T="$(fid "handtheta dedup 없는 행")"
OTHERS="$(q "SELECT GROUP_CONCAT(id) FROM peer_facts WHERE observed<>'unknown'")"
ok "fixture: unresolved identity stores subject=node as observed=unknown (#2541)" \
  '[ "$(q "SELECT COUNT(*), SUM(valid_to IS NULL) FROM peer_facts WHERE observed=\"unknown\"")" = "9|7" ]'
ok "fixture: closed parent + open supersede successor both sit under unknown" \
  '[ "$(q "SELECT observed||\":\"||(valid_to IS NULL) FROM peer_facts WHERE id=$C")" = "unknown:0" ] && [ "$(q "SELECT observed||\":\"||supersedes FROM peer_facts WHERE id=$D")" = "unknown:$C" ]'
ok "fixture: other peers are user / session:<id> / session:unknown / another node" \
  '[ "$(q "SELECT GROUP_CONCAT(observed) FROM (SELECT DISTINCT observed FROM peer_facts WHERE fact LIKE \"userfact%\" OR fact LIKE \"sessionfact%\" OR fact LIKE \"nosidfact%\" OR fact LIKE \"otherfact%\" ORDER BY id)")" = "seo-jin-on,session:s1,session:unknown,$OTHER" ]'
ok "fixture: a missing session id never yields the bare 'unknown' peer" \
  '[ "$(q "SELECT observed FROM peer_facts WHERE fact LIKE \"nosidfact%\"")" = "session:unknown" ]'
before="$(dump)"; tables_before="$(tables)"
out="$(nu metrics)"
ok "metrics: observed_unknown_open counts the open legacy rows" 'grep -qx "observed_unknown_open=7" <<<"$out"'

# ---- refusals: nothing changes, exit non-zero ------------------------------------------
refuse() {  # refuse <label> <pattern> <env-prefix...> -- <args...>
  local label="$1" pat="$2"; shift 2
  local envs=()
  while [ "$1" != "--" ]; do envs+=("$1"); shift; done
  shift
  out="$(env "${envs[@]}" python3 "$TMP/run.py" "$NP" reattribute-unknown "$@" 2>&1)"; rc=$?
  ok "refuse: $label (exit non-zero, says why)" '[ "$rc" != 0 ] && grep -q "$pat" <<<"$out" && grep -q "nothing changed" <<<"$out"'
  ok "refuse: $label (DB untouched, no ledger)" '[ "$(dump)" = "$before" ] && [ "$(tables)" = "$tables_before" ]'
}
refuse "identity resolves to unknown (dry-run)" "resolves to 'unknown'" A=1 --
refuse "identity resolves to unknown (--apply)" "resolves to 'unknown'" A=1 -- --apply
refuse "non-canonical CCC_NODE" "not a canonical fleet node" CCC_NODE=host-unmapped -- --apply
refuse "--node unknown" "not a canonical fleet node" A=1 -- --apply --node unknown
refuse "--node off the roster" "not a canonical fleet node" A=1 -- --apply --node host-unmapped
refuse "--node without a value" "takes a name" A=1 -- --apply --node
refuse "--node followed by a flag" "need --sample >= 0 and --node <name>" A=1 -- --node --apply
refuse "--node contradicting the resolved identity" "contradicts this node's resolved identity" CCC_NODE="$OTHER" -- --apply --node "$NODE"
refuse "--sample not an integer" "is an integer" CCC_NODE="$NODE" -- --apply --sample many
refuse "--revert with --node" "takes no --node" A=1 -- --revert --apply --node "$NODE"
mkdir -p "$TMP/state-named"; printf '%s\n' "$NODE" > "$TMP/state-named/node.txt"
out="$(CCC_STATE_DIR="$TMP/state-named" nu reattribute-unknown 2>&1)"; rc=$?
ok "identity from state/node.txt is accepted (the post-#2542 fleet state)" '[ "$rc" = 0 ] && grep -q "(dry-run): node=$NODE " <<<"$out"'

# ---- dry-run: full report, nothing written ---------------------------------------------
out="$(named reattribute-unknown 2>&1)"; rc=$?
ok "dry-run: counts (total/open/closed, rename, collisions, unproven)" \
  '[ "$rc" = 0 ] && grep -qF "reattribute-unknown (dry-run): node=$NODE unknown=9 (open=7 closed=2) rename=5 (open=4 closed=1) collide_close=1 collide_keep_both=1 unproven_skipped=3" <<<"$out"'
ok "dry-run: by-kind breakdown for open and closed rows" \
  'grep -qx "  open by kind: context=1 fact=5 task-progress=1" <<<"$out" && grep -qx "  closed by kind: fact=2" <<<"$out"'
ok "dry-run: proof breakdown (how each row was shown to be a subject=node fact)" \
  'grep -qx "  by proof: ingest-node=5 supersede-chain=1 unproven=3" <<<"$out"'
ok "dry-run: sample lines carry id, kind, state, created date and proof" \
  'grep -qE "^  rename #$A \[fact\] open created=[0-9]{4}-[0-9]{2}-[0-9]{2} proof=ingest-node$" <<<"$out" && grep -qE "^  rename #$C \[fact\] closed created=.* proof=ingest-node$" <<<"$out" && grep -qE "^  rename #$D \[fact\] open created=.* proof=supersede-chain$" <<<"$out"'
ok "dry-run: collision and unproven rows are listed with what happens to them" \
  'grep -qE "^  collide #$DUP .* duplicate-of=#$SURV$" <<<"$out" && grep -qE "^  collide #$HI .* duplicate-of=#$LO$" <<<"$out" && [ "$(grep -c "^  unproven #" <<<"$out")" = 3 ]'
ok "dry-run: sample is body-free by default" '! grep -qE "legacy(alpha|beta|gamma|delta|epsilon)|hand(zeta|eta|theta)" <<<"$out"'
ok "dry-run: says nothing changed and how to apply" 'grep -q "^dry-run — nothing changed. Back up the DB, then: nunchi.py reattribute-unknown --apply$" <<<"$out"'
out="$(named reattribute-unknown --show-text --sample 2 2>&1)"
ok "dry-run: --show-text adds truncated text, --sample caps each list" \
  'grep -q "rename #$A .*: legacyalpha 디스크" <<<"$out" && [ "$(grep -c "^  rename #" <<<"$out")" = 2 ] && [ "$(grep -c "^  unproven #" <<<"$out")" = 2 ]'
out="$(named reattribute-unknown --sample 0 2>&1)"
ok "dry-run: --sample 0 prints counts only" '[ "$(grep -cE "^  (rename|collide|unproven) #" <<<"$out")" = 0 ] && grep -q "unknown=9" <<<"$out"'
out="$(named reattribute-unknown --json 2>&1)"
ok "dry-run: --json is machine-readable and body-free" \
  '[ "$(python3 -c "import json,sys;r=json.load(sys.stdin);print(r[\"mode\"],r[\"node_source\"],r[\"unknown_total\"],r[\"rename\"],r[\"collide_close\"],r[\"collide_keep_both\"],r[\"unproven_skipped\"],r[\"by_proof\"][\"supersede-chain\"],r[\"by_kind\"][\"open\"][\"fact\"],any(\"text\" in s for s in r[\"rename_sample\"]))" <<<"$out")" = "dry-run resolved 9 5 1 1 3 1 5 False" ]'
out="$(nu reattribute-unknown --node "$ALIAS" --json 2>&1)"; rc=$?
ok "--node override: an alias resolves to the canonical slug when the identity is unknown" \
  '[ "$rc" = 0 ] && [ "$(python3 -c "import json,sys;r=json.load(sys.stdin);print(r[\"node\"],r[\"node_source\"],r[\"rename\"])" <<<"$out")" = "$NODE --node 5" ]'
ok "dry-run (all forms) wrote nothing: rows, tables, ledger" '[ "$(dump)" = "$before" ] && [ "$(tables)" = "$tables_before" ]'
out="$(nu recall "$NODE" 2>&1)"
ok "before apply: recall by node name does not find the legacy rows" '! grep -q "legacyalpha" <<<"$out" && grep -q "nodefact" <<<"$out"'
out="$(nu recall unknown 2>&1)"
ok "before apply: the legacy rows answer to 'unknown'" 'grep -q "legacyalpha" <<<"$out" && grep -q "legacybeta" <<<"$out"'

# ---- --node override applies on a node whose identity does not resolve (DB copy) --------
dbcopy "$NUNCHI_DB" "$TMP/override.db"
out="$(NUNCHI_DB="$TMP/override.db" nu reattribute-unknown --apply --node "$NODE" 2>&1)"; rc=$?
ok "--node override: apply moves the rows" \
  '[ "$rc" = 0 ] && grep -q "applied: renamed 5, closed 1 as duplicates; observed=unknown now 2 open / 3 total" <<<"$out" && [ "$(NUNCHI_DB="$TMP/override.db" q "SELECT observed FROM peer_facts WHERE id=$A")" = "$NODE" ]'

# ---- a failure mid-apply rolls the whole run back ----------------------------------------
dbcopy "$NUNCHI_DB" "$TMP/atomic.db"
python3 -c "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);c.execute('CREATE VIEW reattribute_unknown AS SELECT 1 AS fact_id');c.commit()" "$TMP/atomic.db"
atomic_before="$(NUNCHI_DB="$TMP/atomic.db" dump)"
out="$(NUNCHI_DB="$TMP/atomic.db" named reattribute-unknown --apply 2>&1)"; rc=$?
ok "atomic: a mid-run SQLite error exits non-zero and leaves every row as it was" \
  '[ "$rc" != 0 ] && [ "$(NUNCHI_DB="$TMP/atomic.db" dump)" = "$atomic_before" ]'
out="$(NUNCHI_DB="$TMP/atomic.db" nu recall unknown 2>&1)"
ok "atomic: the FTS index is rolled back with the rows" 'grep -q "legacyalpha" <<<"$out"'

# ---- apply ---------------------------------------------------------------------------------
rest_before="$(rest "$A,$B,$C,$D,$HI")"
dup_before="$(q "SELECT observer,kind,fact,confidence,valid_from,supersedes,dedup,created_at,source_rank,review,because,source_refs,mutability,evidence FROM peer_facts WHERE id=$DUP")"
correct_before="$(dump "WHERE id IN ($OTHERS)")"
unproven_before="$(dump "WHERE id IN ($Z,$E,$T)")"
rows_before="$(q "SELECT COUNT(*) FROM peer_facts")"
out="$(named reattribute-unknown --apply 2>&1)"; rc=$?
ok "apply: reports what it did and what is left" \
  '[ "$rc" = 0 ] && grep -qF "reattribute-unknown (apply): node=$NODE unknown=9" <<<"$out" && grep -q "^applied: renamed 5, closed 1 as duplicates; observed=unknown now 2 open / 3 total (ledger: reattribute_unknown; undo: nunchi.py reattribute-unknown --revert --apply)$" <<<"$out"'
ok "apply: open legacy rows now sit under the node name" \
  '[ "$(obs "$A")" = "$NODE" ] && [ "$(obs "$B")" = "$NODE" ] && [ "$(obs "$D")" = "$NODE" ]'
ok "apply: the closed legacy row is renamed too and stays closed" \
  '[ "$(q "SELECT observed||\":\"||(valid_to IS NOT NULL) FROM peer_facts WHERE id=$C")" = "$NODE:1" ]'
ok "apply: nothing but observed changed on renamed rows (text, kind, rank, review, timestamps, dedup, evidence)" \
  '[ "$(rest "$A,$B,$C,$D,$HI")" = "$rest_before" ]'
ok "apply: dedup stays the subject=node ingest key (it never contained the observed peer)" \
  '[ "$(q "SELECT dedup FROM peer_facts WHERE id=$A")" = "$(python3 -c "import hashlib;print(hashlib.sha1(\"family-assistant|node|legacyalpha 디스크 배치는 단일 볼륨이다\".encode()).hexdigest())")" ]'
out="$(printf '{"session_id":"l1","distilled_at":"2026-10-01T00:00:00+00:00","honcho":[{"kind":"fact","subject":"node","text":"legacyalpha 디스크 배치는 단일 볼륨이다"}]}' | named ingest - 2>&1)"
ok "apply: re-feeding the original payload under the resolved identity is still a no-op" \
  'grep -q "ingested 0/1" <<<"$out" && [ "$(q "SELECT COUNT(*) FROM peer_facts")" = "$rows_before" ]'
ok "collision: the legacy duplicate is renamed and closed with a merged-away lineage token" \
  '[ "$(q "SELECT observed||\"|\"||(valid_to IS NOT NULL)||\"|\"||evidence FROM peer_facts WHERE id=$DUP")" = "$NODE|1|distill:l4 merged-away:#$SURV" ]'
ok "collision: nothing else changed on the closed duplicate" \
  '[ "$(q "SELECT observer,kind,fact,confidence,valid_from,supersedes,dedup,created_at,source_rank,review,because,source_refs,mutability,REPLACE(evidence,\" merged-away:#$SURV\",\"\") FROM peer_facts WHERE id=$DUP")" = "$dup_before" ]'
ok "collision: exactly one open copy of that fact remains under the node name" \
  '[ "$(q "SELECT GROUP_CONCAT(id) FROM peer_facts WHERE observed=\"$NODE\" AND valid_to IS NULL AND fact LIKE \"legacydelta%\"")" = "$SURV" ]'
ok "G2: a rank-3 legacy row is not closed by a rank-1 duplicate — both stay open" \
  '[ "$(q "SELECT GROUP_CONCAT(id||\":\"||source_rank) FROM peer_facts WHERE observed=\"$NODE\" AND valid_to IS NULL AND fact LIKE \"legacyepsilon%\" ORDER BY id")" = "$HI:3,$LO:1" ]'
ok "already-correct rows and other peers (user, session:*, session:unknown, another node) are byte-identical" \
  '[ "$(dump "WHERE id IN ($OTHERS)")" = "$correct_before" ]'
ok "unproven rows (hand-inserted, NULL dedup, supersede of an unproven row) stay under unknown, untouched" \
  '[ "$(dump "WHERE id IN ($Z,$E,$T)")" = "$unproven_before" ] && [ "$(q "SELECT COUNT(*) FROM peer_facts WHERE observed=\"unknown\"")" = 3 ]'
ok "apply never deletes a row" '[ "$(q "SELECT COUNT(*) FROM peer_facts")" = "$rows_before" ]'
out="$(nu recall "$NODE" --limit 20 2>&1)"
ok "FTS: recall by node name now returns the re-attributed facts" \
  'grep -q "legacyalpha" <<<"$out" && grep -q "legacybeta" <<<"$out" && grep -q "legacygamma 러너 이미지는 신버전" <<<"$out"'
out="$(nu recall unknown --limit 20 2>&1)"
ok "FTS: recall 'unknown' no longer returns them, still returns real matches" \
  '! grep -qE "legacy(alpha|beta|gamma|delta|epsilon)" <<<"$out" && grep -q "userfact" <<<"$out" && grep -q "handtheta" <<<"$out"'
out="$(nu recall legacyalpha --target "$NODE" 2>&1)"
ok "recall --target <node> scopes to the re-attributed fact" 'grep -q "legacyalpha" <<<"$out"'
ok "FTS index matches the content table (integrity-check)" \
  'python3 -c "import sqlite3,sys;sqlite3.connect(sys.argv[1]).execute(\"INSERT INTO facts_fts(facts_fts, rank) VALUES(\x27integrity-check\x27, 1)\")" "$NUNCHI_DB"'
ok "ledger: one body-free row per change" \
  '[ "$(q "SELECT GROUP_CONCAT(name) FROM pragma_table_info(\"reattribute_unknown\")")" = "fact_id,action,old_observed,new_observed,proof,survivor_id,applied_at,reverted_at" ] && [ "$(q "SELECT COUNT(*), SUM(action=\"close\"), SUM(old_observed=\"unknown\" AND new_observed=\"$NODE\"), SUM(reverted_at IS NULL) FROM reattribute_unknown")" = "6|1|6|6" ]'
ok "ledger: records proof and the duplicate's survivor" \
  '[ "$(q "SELECT proof||\":\"||COALESCE(survivor_id,\"-\") FROM reattribute_unknown WHERE fact_id IN ($D,$DUP,$HI) ORDER BY fact_id")" = "$(printf "%s\n%s\n%s" "supersede-chain:-" "ingest-node:$SURV" "ingest-node:$LO")" ]'
out="$(nu metrics)"
ok "metrics: observed_unknown_open drops to the unproven remainder" 'grep -qx "observed_unknown_open=2" <<<"$out"'
named snapshot --limit 5 >/dev/null 2>&1; rc=$?
ok "snapshot still renders after the rename" '[ "$rc" = 0 ] && [ -s "$NUNCHI_SNAPSHOT" ]'

# ---- idempotence ---------------------------------------------------------------------------
after_first="$(dump)"; ledger_first="$(q "SELECT * FROM reattribute_unknown ORDER BY fact_id")"
out="$(named reattribute-unknown --apply 2>&1)"; rc=$?
ok "second apply: 0 changes, rows and ledger identical" \
  '[ "$rc" = 0 ] && grep -q "rename=0 (open=0 closed=0) collide_close=0 collide_keep_both=0 unproven_skipped=3" <<<"$out" && grep -q "^applied: renamed 0, closed 0 as duplicates" <<<"$out" && [ "$(dump)" = "$after_first" ] && [ "$(q "SELECT * FROM reattribute_unknown ORDER BY fact_id")" = "$ledger_first" ]'

# ---- revert --------------------------------------------------------------------------------
out="$(nu reattribute-unknown --revert 2>&1)"; rc=$?
ok "revert dry-run: lists what would be restored and writes nothing" \
  '[ "$rc" = 0 ] && grep -q "(revert-dry-run): ledger_active=6 restore=6 (reopen=1) skipped_changed=0" <<<"$out" && grep -q "^  reopen #$DUP $NODE -> unknown$" <<<"$out" && [ "$(dump)" = "$after_first" ]'
out="$(nu reattribute-unknown --revert --apply 2>&1)"; rc=$?
ok "revert apply: reports the restore" \
  '[ "$rc" = 0 ] && grep -q "^reverted: restored 6 (reopened 1); observed=unknown now 7 open / 9 total$" <<<"$out"'
ok "revert apply: every row is byte-identical to the state before apply" '[ "$(dump)" = "$before" ]'
ok "revert apply: ledger rows are kept and marked reverted" \
  '[ "$(q "SELECT COUNT(*), SUM(reverted_at IS NOT NULL) FROM reattribute_unknown")" = "6|6" ]'
out="$(nu recall "$NODE" --limit 20 2>&1)"
ok "revert apply: FTS follows back (node-name recall no longer finds the legacy rows)" '! grep -q "legacyalpha" <<<"$out" && grep -q "nodefact" <<<"$out"'
out="$(nu recall unknown --limit 20 2>&1)"
ok "revert apply: 'unknown' recall finds them again" 'grep -q "legacyalpha" <<<"$out"'
out="$(nu reattribute-unknown --revert --apply 2>&1)"
ok "second revert: nothing left to restore" 'grep -q "ledger_active=0 restore=0" <<<"$out" && [ "$(dump)" = "$before" ]'
out="$(NUNCHI_DB="$TMP/fresh.db" nu reattribute-unknown --revert --apply 2>&1)"; rc=$?
ok "revert without a ledger is a clean no-op" '[ "$rc" = 0 ] && grep -q "ledger_active=0 restore=0" <<<"$out"'

# ---- re-apply after revert, then a revert that respects later changes -----------------------
out="$(named reattribute-unknown --apply 2>&1)"
ok "re-apply after revert works and reuses the ledger rows" \
  'grep -q "^applied: renamed 5, closed 1 as duplicates" <<<"$out" && [ "$(q "SELECT COUNT(*), SUM(reverted_at IS NULL) FROM reattribute_unknown")" = "6|6" ]'
python3 - "$NUNCHI_DB" "$DUP" "$B" "$OTHER" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
# the owner reopened the closed duplicate, and moved another row to a different peer
c.execute("UPDATE peer_facts SET valid_to=NULL WHERE id=?", (sys.argv[2],))
c.execute("UPDATE peer_facts SET observed=? WHERE id=?", (sys.argv[4], sys.argv[3]))
c.commit()
PY
out="$(nu reattribute-unknown --revert --apply 2>&1)"
ok "revert leaves a row someone re-attributed since alone, and reports it" \
  'grep -q "restore=5 (reopen=0) skipped_changed=1" <<<"$out" && [ "$(obs "$B")" = "$OTHER" ] && [ "$(q "SELECT reverted_at IS NULL FROM reattribute_unknown WHERE fact_id=$B")" = 1 ]'
ok "revert does not rewrite a close the owner already changed (observed restored only)" \
  '[ "$(q "SELECT observed||\"|\"||(valid_to IS NULL)||\"|\"||evidence FROM peer_facts WHERE id=$DUP")" = "unknown|1|distill:l4 merged-away:#$SURV" ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
