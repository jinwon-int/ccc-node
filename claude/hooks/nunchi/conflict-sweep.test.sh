#!/usr/bin/env bash
# conflict-sweep (ccc-node#2464 제안 2): implicit-conflict pass that may only flag review=1.
# Private temp DB; the judge is ALWAYS a PATH stub (no real provider, no network, no cost).
# #2910 A: a second call names the stale side of each conflict; stale=old flags the OLDER fact (section 8).
# Covers: candidate scope (same peer scope + same kind, excluded kinds, machine rows), dry-run is
# read-only, --apply flags the newer fact and never closes/supersedes, ledger idempotency and
# owner-clear respect, fail-closed on unusable verdicts, review listing, and the judge-batch
# guard that keeps a lexically disjoint sweep flag from being deterministically auto-cleared.
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
NP="$ROOT/claude/hooks/nunchi/nunchi.py"
JB="$ROOT/claude/hooks/nunchi/judge-batch.py"
export HOME="$TMP/home"; mkdir -p "$HOME/.nunchi" "$TMP/bin" "$TMP/state"
export NUNCHI_DB="$TMP/facts.db" NUNCHI_HOME="$TMP/home/.nunchi" CCC_STATE_DIR="$TMP/state"
export GOTGAN_EMBED=off
unset TYPESAFE_API_KEY NUNCHI_SYNTH_ORDER NUNCHI_PIRI_BIN
export TYPESAFE_API_KEY_FILE="$TMP/no-such-key-file"
# No real provider may ever be reached: codex is always shadowed (claude by write_judge), the pi
# harness backend is off, and the no-backend case runs python by absolute path with an empty PATH.
export NUNCHI_PIRI=0
PY3="$(command -v python3)"
STUBPATH="$TMP/bin:/usr/bin:/bin"
cat >"$TMP/bin/codex" <<'SH'
#!/usr/bin/env bash
cat >/dev/null
exit 127
SH
chmod 755 "$TMP/bin/codex"
CALLS="$TMP/calls"
# Judge stub: flags exactly the pairs whose OLDER line mentions Seattle and NEWER mentions Austin
# (the STALE shape: no shared word); counts its invocations.
write_judge() { # write_judge <mode: austin|old|new|unclear|garbage|outofrange|badstale|twice>
  cat >"$TMP/bin/claude" <<SH
#!/usr/bin/env bash
echo call >>"$CALLS"
case "$1" in
  garbage) echo "I think pair one maybe"; exit 0 ;;
  outofrange) echo '{"conflicts": [99]}'; exit 0 ;;
esac
if printf '%s' "\$2" | grep -q "already been judged to conflict"; then
  # #2910 A second call: the stale side of every listed pair
  n="\$(printf '%s\n' "\$2" | grep -c '^[0-9][0-9]*\. OLDER')"
  case "$1" in
    old|new) st="$1" ;;
    badstale) echo '{"stale": [{"pair": 1, "stale": "maybe"}]}'; exit 0 ;;
    twice) echo '{"stale": [{"pair": 1, "stale": "old"}, {"pair": 1, "stale": "old"}]}'; exit 0 ;;
    *) st=unclear ;;
  esac
  printf '{"stale": [%s]}\n' "\$(seq 1 "\$n" | awk -v st="\$st" '{printf "%s{\"pair\": %d, \"stale\": \"%s\"}", sep, \$1, st; sep=", "}')"
  exit 0
fi
nums="\$(printf '%s\n' "\$2" | awk '/^[0-9]+\. OLDER/ {n=\$1+0; s=/Seattle/} /^   NEWER/ && /Austin/ && s {printf "%s%d", sep, n; sep=","}')"
printf '{"conflicts": [%s]}\n' "\$nums"
SH
  chmod 755 "$TMP/bin/claude"
}
python3 "$NP" init >/dev/null 2>&1
NOW="$(python3 -c 'from datetime import datetime,timezone;print(datetime.now(timezone.utc).isoformat(timespec="seconds"))')"
seed() { # seed <observed> <kind> <fact> <valid_from> <created_at> <evidence> -> id
  python3 - "$@" <<'PY'
import os, sqlite3, sys
o, k, f, vf, ca, ev = sys.argv[1:7]
c = sqlite3.connect(os.environ["NUNCHI_DB"])
cur = c.execute("INSERT INTO peer_facts(observer,observed,kind,fact,evidence,valid_from,dedup,created_at,"
                "source_rank,review) VALUES('family-assistant',?,?,?,?,?,?,?,1,0)", (o, k, f, ev, vf, f, ca))
c.commit(); print(cur.lastrowid)
PY
}
q() { python3 -c 'import os,sqlite3,sys;r=sqlite3.connect(os.environ["NUNCHI_DB"]).execute(sys.argv[1]).fetchone();print("" if not r or r[0] is None else r[0])' "$1"; }

OLD_TS="2026-09-01T00:00:00+00:00"
old="$(seed seo-jin-on context "I have been based in Seattle for the last few years." "$OLD_TS" "$OLD_TS" distill:s1)"
desk="$(seed seo-jin-on context "Has a standing desk at home." "2026-09-02T00:00:00+00:00" "2026-09-02T00:00:00+00:00" distill:s1)"
oldpref="$(seed seo-jin-on preference "Prefers dark roast coffee." "$OLD_TS" "$OLD_TS" distill:s1)"
new="$(seed seo-jin-on context "Austin address updated, utilities being set up." "$NOW" "$NOW" distill:s2)"
cons="$(seed seo-jin-on constraint "Austin rule: never move the server." "$NOW" "$NOW" distill:s2)"
auto="$(seed seo-jin-on context "Austin office lease signed (machine feed)." "$NOW" "$NOW" auto-distill:distill:s3)"
other="$(seed peer-b context "Austin mirror configured." "$NOW" "$NOW" distill:s4)"

# 1. candidates only — no judge call, no mutation
: >"$CALLS"; write_judge austin
out="$(PATH="$STUBPATH" python3 "$NP" conflict-sweep --judge none 2>&1)"
ok "same-peer same-kind older fact is a candidate" 'grep -q "^#$new ⟂ #$old \[candidate\]" <<<"$out"'
ok "different kind is never paired" '! grep -q "⟂ #$oldpref" <<<"$out"'
ok "constraint newer side excluded" '! grep -q "^#$cons " <<<"$out"'
ok "auto-distill machine row never a newer side" '! grep -q "^#$auto " <<<"$out"'
ok "another peer never pairs with the user peer" '! grep -q "^#$other ⟂ #$old" <<<"$out"'
ok "--judge none spends no model call" '[ ! -s "$CALLS" ]'
ok "--judge none --apply is refused" '! PATH="$STUBPATH" python3 "$NP" conflict-sweep --judge none --apply >/dev/null 2>&1'

# 2. dry-run judge: verdict shown, DB untouched
out="$(PATH="$STUBPATH" python3 "$NP" conflict-sweep 2>&1)"
ok "dry-run reports the conflict with its stale side" 'grep -q "^#$new ⟂ #$old \[conflict stale=unclear\]" <<<"$out"'
ok "dry-run summary names the mode" 'grep -q "conflict-sweep (dry-run) judge=llm: .*conflicts=1 stale_old=0 flagged=0" <<<"$out"'
ok "dry-run sets no flag" '[ "$(q "SELECT review FROM peer_facts WHERE id=$new")" = 0 ]'
ok "dry-run writes no ledger" '[ "$(q "SELECT COUNT(*) FROM conflict_sweep")" = 0 ]'

# 3. apply: newer flagged, older untouched, ledger complete
out="$(PATH="$STUBPATH" python3 "$NP" conflict-sweep --apply 2>&1)"
ok "apply flags the newer fact" '[ "$(q "SELECT review FROM peer_facts WHERE id=$new")" = 1 ]'
ok "apply never closes the older fact" '[ -z "$(q "SELECT valid_to FROM peer_facts WHERE id=$old")" ]'
ok "apply never supersedes" '[ "$(q "SELECT COUNT(*) FROM peer_facts WHERE supersedes IS NOT NULL OR valid_to IS NOT NULL")" = 0 ]'
ok "older fact keeps review=0" '[ "$(q "SELECT review FROM peer_facts WHERE id=$old")" = 0 ]'
ok "ledger records the conflict pair" '[ "$(q "SELECT verdict FROM conflict_sweep WHERE new_id=$new AND old_id=$old")" = conflict ]'
ok "ledger records judged non-conflicts too" '[ "$(q "SELECT verdict FROM conflict_sweep WHERE new_id=$new AND old_id=$desk")" = none ]'
out="$(python3 "$NP" review 2>&1)"
ok "review queue names the sweep partner" 'grep -q "^#$new .*\[conflict-sweep ⟂ #$old\]" <<<"$out"'

# 4. idempotent: nothing left to ask; an owner clear is never undone
: >"$CALLS"
out="$(PATH="$STUBPATH" python3 "$NP" conflict-sweep --apply 2>&1)"
ok "re-run has no pairs left" 'grep -q "pairs=0 " <<<"$out"'
ok "re-run spends no model call" '[ ! -s "$CALLS" ]'
python3 "$NP" review "$new" --clear >/dev/null
PATH="$STUBPATH" python3 "$NP" conflict-sweep --apply >/dev/null 2>&1
ok "owner clear is respected on the next sweep" '[ "$(q "SELECT review FROM peer_facts WHERE id=$new")" = 0 ]'

# 5. fail-closed: unusable verdicts leave neither flags nor ledger rows, exit 1
newer2="$(seed seo-jin-on context "Austin school enrollment done for the kids." "$NOW" "$NOW" distill:s5)"
before="$(q "SELECT COUNT(*) FROM conflict_sweep")"
write_judge garbage
PATH="$STUBPATH" python3 "$NP" conflict-sweep --apply >/dev/null 2>&1; rc=$?
ok "unparseable verdict exits 1" '[ "$rc" = 1 ]'
ok "unparseable verdict flags nothing" '[ "$(q "SELECT review FROM peer_facts WHERE id=$newer2")" = 0 ]'
ok "unparseable verdict writes no ledger" '[ "$(q "SELECT COUNT(*) FROM conflict_sweep")" = "$before" ]'
write_judge outofrange
PATH="$STUBPATH" python3 "$NP" conflict-sweep --apply >/dev/null 2>&1; rc=$?
ok "out-of-range pair number is fail-closed" '[ "$rc" = 1 ] && [ "$(q "SELECT COUNT(*) FROM conflict_sweep")" = "$before" ]'
PATH="$TMP/nobin" "$PY3" "$NP" conflict-sweep --apply >/dev/null 2>&1; rc=$?
ok "no backend at all is fail-closed" '[ "$rc" = 1 ] && [ "$(q "SELECT COUNT(*) FROM conflict_sweep")" = "$before" ]'
write_judge austin
PATH="$STUBPATH" python3 "$NP" conflict-sweep --apply >/dev/null 2>&1
ok "the retried pair is judged once the judge works" '[ "$(q "SELECT review FROM peer_facts WHERE id=$newer2")" = 1 ]'

# 6. window / caps / validation
out="$(PATH="$STUBPATH" python3 "$NP" conflict-sweep --judge none --since-days 0 2>&1)"; rc=$?
ok "--since-days must be positive" '[ "$rc" != 0 ]'
newer3="$(seed seo-jin-on context "Austin gym membership started." "$NOW" "$NOW" distill:s6)"
out="$(PATH="$STUBPATH" python3 "$NP" conflict-sweep --judge none --max-pairs 1 2>&1)"
ok "--max-pairs caps the pair list" 'grep -q "pairs=1 " <<<"$out"'
out="$(PATH="$STUBPATH" python3 "$NP" conflict-sweep --judge none --target peer-b --json 2>&1)"
ok "--target scopes the newer side and --json is valid" \
  'python3 -c "import json,sys;d=json.loads(sys.argv[1]);assert all(i[\"observed\"]==\"peer-b\" for i in d[\"items\"])" "$out"'

# 7. judge-batch: a sweep flag (lexically disjoint) must not be deterministically cleared
OLDC="2026-08-19T00:00:00+00:00"
rm -f "$NUNCHI_DB"; python3 "$NP" init >/dev/null 2>&1
a="$(seed peer-c context "Runs the nightly backup from the Seoul rack." "$OLDC" "$OLDC" distill:j1)"
b="$(seed peer-c context "Moved every job to the Frankfurt host last week." "$OLDC" "$OLDC" distill:j2)"
lone="$(seed peer-c context "Lonely flagged note with no sibling." "$OLDC" "$OLDC" distill:j3)"
python3 - "$b" "$a" "$lone" <<'PY'
import os, sqlite3, sys
b, a, lone = map(int, sys.argv[1:4])
c = sqlite3.connect(os.environ["NUNCHI_DB"])
c.execute("UPDATE peer_facts SET review=1 WHERE id IN (?,?)", (b, lone))
c.execute("INSERT INTO conflict_sweep(new_id, old_id, verdict, method, swept_at)"
          " VALUES(?,?,'conflict','llm','2026-08-19T00:00:00+00:00')", (b, a))
c.commit()
PY
cat >"$TMP/bin/claude" <<'SH'
#!/usr/bin/env bash
cat >/dev/null
exit 1
SH
cat >"$TMP/bin/codex" <<'SH'
#!/usr/bin/env bash
cat >/dev/null
exit 127
SH
chmod 755 "$TMP/bin/claude" "$TMP/bin/codex"
PATH="$TMP/bin:$PATH" NUNCHI_JUDGE_APPLY=1 python3 "$JB" >/dev/null 2>&1
ok "judge-batch keeps the sweep flag (partner is its live sibling)" '[ "$(q "SELECT review FROM peer_facts WHERE id=$b")" = 1 ]'
ok "judge-batch still clears a lonely flag deterministically" '[ "$(q "SELECT review FROM peer_facts WHERE id=$lone")" = 0 ]'
python3 - "$a" <<'PY'
import os, sqlite3, sys
c = sqlite3.connect(os.environ["NUNCHI_DB"])
c.execute("UPDATE peer_facts SET valid_to='2026-08-20T00:00:00+00:00' WHERE id=?", (int(sys.argv[1]),))
c.commit()
PY
PATH="$TMP/bin:$PATH" NUNCHI_JUDGE_APPLY=1 python3 "$JB" >/dev/null 2>&1
ok "once the older partner is closed the sweep flag clears like any lonely flag" '[ "$(q "SELECT review FROM peer_facts WHERE id=$b")" = 0 ]'

# 8. #2910 A — the judge names the stale side; stale=old flags the OLDER fact
rm -f "$NUNCHI_DB"; python3 "$NP" init >/dev/null 2>&1
s_old="$(seed seo-jin-on context "I have been based in Seattle for the last few years." "$OLD_TS" "$OLD_TS" distill:k1)"
s_new="$(seed seo-jin-on context "Austin address updated, utilities being set up." "$NOW" "$NOW" distill:k2)"
write_judge old
out="$(PATH="$STUBPATH" python3 "$NP" conflict-sweep 2>&1)"
ok "dry-run shows the stale side" 'grep -q "^#$s_new ⟂ #$s_old \[conflict stale=old\]" <<<"$out"'
ok "dry-run counts stale_old" 'grep -q "conflicts=1 stale_old=1 flagged=0" <<<"$out"'
: >"$CALLS"
out="$(PATH="$STUBPATH" python3 "$NP" conflict-sweep --apply --json 2>&1)"
ok "a batch with a conflict costs exactly one more call (direction)" '[ "$(wc -l <"$CALLS")" = 2 ]'
ok "--json carries the stale side" \
  'python3 -c "import json,sys;d=json.loads(sys.argv[1]);assert [i[\"stale\"] for i in d[\"items\"] if i[\"verdict\"]==\"conflict\"]==[\"old\"] and d[\"stale_old\"]==1" "$out"'
ok "stale=old flags the older fact" '[ "$(q "SELECT review FROM peer_facts WHERE id=$s_old")" = 1 ]'
ok "stale=old leaves the newer fact unflagged" '[ "$(q "SELECT review FROM peer_facts WHERE id=$s_new")" = 0 ]'
ok "stale=old still closes nothing" '[ "$(q "SELECT COUNT(*) FROM peer_facts WHERE valid_to IS NOT NULL")" = 0 ]'
ok "ledger records the stale side" '[ "$(q "SELECT stale FROM conflict_sweep WHERE new_id=$s_new AND old_id=$s_old")" = old ]'
out="$(python3 "$NP" review 2>&1)"
ok "review names the replacing fact and the fold command" \
  'grep -q "^#$s_old .*\[conflict-sweep stale → #$s_new: nunchi.py merge $s_old --into $s_new\]" <<<"$out"'
ok "review does not list the unflagged newer fact" '! grep -q "^#$s_new " <<<"$out"'
PATH="$TMP/bin:$PATH" NUNCHI_JUDGE_APPLY=1 python3 "$JB" >/dev/null 2>&1
ok "judge-batch keeps an older-side flag while the newer fact is open" '[ "$(q "SELECT review FROM peer_facts WHERE id=$s_old")" = 1 ]'
REPORT="$CCC_STATE_DIR/nunchi-review-report.md"
ok "judge report proposes folding old into new" \
  'grep -q "^## conflict-sweep stale-old" "$REPORT" && grep -qx "nunchi.py merge $s_old --into $s_new  # conflict-sweep stale=old" "$REPORT"'
python3 "$NP" merge "$s_old" --into "$s_new" >/dev/null 2>&1
ok "the proposed merge closes the stale older fact" '[ -n "$(q "SELECT valid_to FROM peer_facts WHERE id=$s_old")" ]'
ok "after the fold the newer fact stays open" '[ -z "$(q "SELECT valid_to FROM peer_facts WHERE id=$s_new")" ]'
PATH="$TMP/bin:$PATH" NUNCHI_JUDGE_APPLY=1 python3 "$JB" >/dev/null 2>&1
ok "a folded pair leaves the report" '! grep -q "^## conflict-sweep stale-old" "$REPORT"'

# 8a. no conflict in the batch -> no direction call
rm -f "$NUNCHI_DB"; python3 "$NP" init >/dev/null 2>&1
seed seo-jin-on context "Has a standing desk at home." "$OLD_TS" "$OLD_TS" distill:k3 >/dev/null
seed seo-jin-on context "Bought a second monitor." "$NOW" "$NOW" distill:k4 >/dev/null
: >"$CALLS"
PATH="$STUBPATH" python3 "$NP" conflict-sweep --apply >/dev/null 2>&1
ok "a batch without conflicts spends only the conflict call" '[ "$(wc -l <"$CALLS")" = 1 ]'

# 8b. stale=new / unclear keep today's placement (the newer fact)
for st in new unclear; do
  rm -f "$NUNCHI_DB"; python3 "$NP" init >/dev/null 2>&1
  s_old="$(seed seo-jin-on context "I have been based in Seattle for the last few years." "$OLD_TS" "$OLD_TS" distill:k1)"
  s_new="$(seed seo-jin-on context "Austin address updated, utilities being set up." "$NOW" "$NOW" distill:k2)"
  write_judge "$st"
  PATH="$STUBPATH" python3 "$NP" conflict-sweep --apply >/dev/null 2>&1
  ok "stale=$st flags the newer fact" '[ "$(q "SELECT review FROM peer_facts WHERE id=$s_new")" = 1 ] && [ "$(q "SELECT review FROM peer_facts WHERE id=$s_old")" = 0 ]'
  ok "stale=$st is recorded" '[ "$(q "SELECT stale FROM conflict_sweep WHERE new_id=$s_new AND old_id=$s_old")" = "$st" ]'
done

# 8c. fail-closed on a bad stale value or a pair answered twice (second call)
for mode in badstale twice; do
  rm -f "$NUNCHI_DB"; python3 "$NP" init >/dev/null 2>&1
  s_old="$(seed seo-jin-on context "I have been based in Seattle for the last few years." "$OLD_TS" "$OLD_TS" distill:k1)"
  s_new="$(seed seo-jin-on context "Austin address updated, utilities being set up." "$NOW" "$NOW" distill:k2)"
  write_judge "$mode"
  PATH="$STUBPATH" python3 "$NP" conflict-sweep --apply >/dev/null 2>&1; rc=$?
  ok "$mode verdict is fail-closed" '[ "$rc" = 1 ] && [ "$(q "SELECT COUNT(*) FROM conflict_sweep")" = 0 ] && [ "$(q "SELECT SUM(review) FROM peer_facts")" = 0 ]'
done

# 8d. a pre-#2910 ledger gains the column in place; its rows keep the newer-side meaning
rm -f "$NUNCHI_DB"
python3 - <<'PY2'
import os, sqlite3
c = sqlite3.connect(os.environ["NUNCHI_DB"])
c.execute("CREATE TABLE conflict_sweep(new_id INTEGER NOT NULL, old_id INTEGER NOT NULL, verdict TEXT NOT NULL,"
          " method TEXT, swept_at TEXT NOT NULL, PRIMARY KEY(new_id, old_id))")
c.execute("INSERT INTO conflict_sweep VALUES(9, 8, 'conflict', 'llm', '2026-10-01T00:00:00+00:00')")
c.commit()
PY2
python3 "$NP" init >/dev/null 2>&1
ok "old ledger gains the stale column" '[ "$(q "SELECT COUNT(*) FROM pragma_table_info(\"conflict_sweep\") WHERE name=\"stale\"")" = 1 ]'
ok "old ledger rows are kept with stale NULL" '[ "$(q "SELECT COUNT(*) FROM conflict_sweep WHERE new_id=9 AND old_id=8 AND stale IS NULL")" = 1 ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
