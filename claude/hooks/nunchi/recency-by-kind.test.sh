#!/usr/bin/env bash
# Recency re-rank by kind (ccc-node#2885): the recency term of the state-aware re-rank is full
# strength for state-like kinds and a small share for static ones (preference, decision,
# constraint, procedure). NUNCHI_RECENCY_BY_KIND=0 restores the previous ordering exactly.
# LLM-free, deterministic, against a private temp DB with a made-up fixture (a lighthouse crew).
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
export HOME="$TMP/home"; mkdir -p "$HOME/.nunchi"
export NUNCHI_DB="$TMP/facts.db" NUNCHI_HOME="$TMP/home/.nunchi"
export GOTGAN_EMBED=off
unset NUNCHI_RECENCY_BY_KIND NUNCHI_RECENCY_WEIGHT NUNCHI_STATE_WINDOW NUNCHI_QUERY_PREFIX
python3 "$NP" init >/dev/null 2>&1

python3 - "$NUNCHI_DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
rows = []
# (1) an OLD static rule that matches the query exactly, buried under 44 NEWER state rows that
#     each share one word with it — the shape that pushed exact matches out of rank 1.
rows.append(("decision", "lantern-rota rule: the gamma lamp rotation stays weekly", "2026-09-01T00:00:00+00:00", None))
# the same rule shape, closed AFTER the as-of instant used below: open at that instant.
rows.append(("decision", "beacon-rota rule: the gamma lamp rotation stays daily", "2026-09-01T00:00:00+00:00",
             "2026-11-20T00:00:00+00:00"))
for i in range(44):
    word = ("rotation", "gamma")[i % 2]
    rows.append(("task-progress" if i % 3 else "context",
                 f"crew shift {i} logged the {word} check at dock {i}",
                 f"2026-10-{1 + i % 28:02d}T{i % 24:02d}:00:00+00:00", None))
# (2) state supersedes state (#2464): the old state matches the query as well as the new one
#     and is older than 20 one-word distractors; the new state is the newest row. task-progress
#     is open on both sides; the old decision was closed (superseded) when the new one came in.
rows.append(("task-progress", "foghorn repair status waiting", "2026-09-02T00:00:00+00:00", None))
rows.append(("decision", "keeper roster choice Mira", "2026-09-03T00:00:00+00:00", "2026-11-03T00:00:00+00:00"))
for i in range(20):
    rows.append(("context", f"tide table {i} status and roster noted", f"2026-10-{1 + i:02d}T12:00:00+00:00", None))
rows.append(("task-progress", "foghorn repair status fitted", "2026-11-02T00:00:00+00:00", None))
rows.append(("decision", "keeper roster choice Tomas", "2026-11-03T00:00:00+00:00", None))
# (3) one row of every other kind so the byte-identity check below sees them all.
for k in ("preference", "constraint", "procedure", "observation", "correction", "fact", "node", "odd-kind"):
    rows.append((k, f"harbor ledger note for {k} about the gamma rotation", "2026-10-15T00:00:00+00:00", None))
    rows.append((k, f"harbor ledger note for {k} closed about the gamma rotation", "2026-09-15T00:00:00+00:00",
                 "2026-10-20T00:00:00+00:00"))
for i, (kind, fact, ts, closed) in enumerate(rows):
    c.execute("INSERT INTO peer_facts(observer,observed,kind,fact,valid_from,valid_to,dedup,created_at,source_rank,review)"
              " VALUES('family-assistant','lighthouse',?,?,?,?,?,?,1,0)", (kind, fact, ts, closed, f"d{i}", ts))
c.commit()
PY

# Ranked ids (or reprs) of the production path for a query, in a fresh interpreter per env.
probe() {
  python3 - "$NP" "$@" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("nunchi", sys.argv[1]); n = importlib.util.module_from_spec(spec)
spec.loader.exec_module(n)
c = n.db()
q = sys.argv[2]
as_of = sys.argv[5] if len(sys.argv) > 5 and sys.argv[4] == "--as-of" else None
rows = n.search(c, q, target="lighthouse", limit=int(sys.argv[3]), include_history=True, state_aware=True, as_of=as_of)
print("\n".join(r[3] for r in rows))
PY
}
top1() { probe "$1" "${2:-12}" | sed -n 1p; }

export NUNCHI_STATE_WINDOW=0
# 1. the old exact static match leads with the switch on, and is pushed down with it off
on="$(top1 "lantern-rota gamma rotation")"
off="$(NUNCHI_RECENCY_BY_KIND=0 top1 "lantern-rota gamma rotation")"
ok "static exact match is rank 1 with recency by kind" '[[ "$on" == lantern-rota* ]]'
ok "the old ordering (switch off) pushes it out of rank 1" '[[ "$off" != lantern-rota* ]]'
for v in false off no OFF; do
  o="$(NUNCHI_RECENCY_BY_KIND=$v top1 "lantern-rota gamma rotation")"
  ok "NUNCHI_RECENCY_BY_KIND=$v is off" '[ "$o" = "$off" ]'
done
o="$(NUNCHI_RECENCY_BY_KIND=1 top1 "lantern-rota gamma rotation")"
ok "NUNCHI_RECENCY_BY_KIND=1 is on" '[ "$o" = "$on" ]'

# 2. newer state still beats stale state of the same kind (#2464), with the switch on and off
for sw in 1 0; do
  o="$(NUNCHI_RECENCY_BY_KIND=$sw probe "foghorn repair status" 2 | sed -n 1p)"
  ok "task-progress: newer state first (NUNCHI_RECENCY_BY_KIND=$sw)" '[[ "$o" == *fitted* ]]'
  o="$(NUNCHI_RECENCY_BY_KIND=$sw probe "keeper roster choice" 2 | sed -n 1p)"
  ok "decision: newer state first (NUNCHI_RECENCY_BY_KIND=$sw)" '[[ "$o" == *Tomas* ]]'
done
# the old decision row in the pool is closed: had it kept the static share it would win on bm25
o="$(NUNCHI_RECENCY_BY_KIND=1 NUNCHI_RECENCY_WEIGHT=0 probe "keeper roster choice" 2 | sed -n 1p)"
ok "bm25 alone ranks the stale decision first (the test has teeth)" '[[ "$o" == *Mira* ]]'
# with --as-of, a static row closed after that instant was open then and keeps the small share
o="$(probe "beacon-rota gamma rotation" 12 --as-of 2026-11-01T00:00:00+00:00 | sed -n 1p)"
ok "as-of: a rule closed later counts as open at that instant" '[[ "$o" == beacon-rota* ]]'
o="$(probe "beacon-rota gamma rotation" 12 | sed -n 1p)"
ok "without as-of the same closed rule gets the full recency weight" '[[ "$o" != beacon-rota* ]]'

# 3. byte-identical to the previous code with the switch off: the whole search() path for many
#    queries, limits, weights and windows, against the pre-#2885 _state_rerank kept verbatim here.
out="$(python3 - "$NP" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("nunchi", sys.argv[1]); n = importlib.util.module_from_spec(spec)
spec.loader.exec_module(n)

def old_rerank(c, rows, query, where, args, limit, as_of=None):  # pre-#2885 body, vectors absent
    if not rows:
        return rows
    weight = n._env_float("NUNCHI_RECENCY_WEIGHT", 0.25, 0.0, 10.0)
    by_recency = sorted(rows, key=lambda r: (r[4] or "", r[0]), reverse=True)
    rec_pos = {r[0]: i for i, r in enumerate(by_recency)}
    score = {r[0]: i + weight * rec_pos[r[0]] for i, r in enumerate(rows)}
    return sorted(rows, key=lambda r: score[r[0]])[:limit]

c = n.db()
new_rerank = n._state_rerank
queries = ["lantern-rota gamma rotation", "gamma", "rotation check dock", "foghorn repair status",
           "keeper roster choice", "harbor ledger note", "crew shift 7", "nothing matches this"]
same = differs = cases = 0
for weight in ("0", "0.1", "0.25", "1", "5"):
    for window in ("0", "5"):
        os.environ["NUNCHI_RECENCY_WEIGHT"] = weight; os.environ["NUNCHI_STATE_WINDOW"] = window
        for q in queries:
            for limit in (1, 3, 12, 30):
                for target, as_of in ((None, None), ("lighthouse", None), ("lighthouse", "2026-10-10T00:00:00+00:00")):
                    os.environ["NUNCHI_RECENCY_BY_KIND"] = "0"
                    n._state_rerank = new_rerank
                    got = repr(n.search(c, q, target, limit, include_history=True, state_aware=True, as_of=as_of))
                    n._state_rerank = old_rerank
                    want = repr(n.search(c, q, target, limit, include_history=True, state_aware=True, as_of=as_of))
                    os.environ["NUNCHI_RECENCY_BY_KIND"] = "1"
                    n._state_rerank = new_rerank
                    on = repr(n.search(c, q, target, limit, include_history=True, state_aware=True, as_of=as_of))
                    cases += 1
                    same += got == want
                    differs += on != want
print(cases, same, differs)
PY
)"
read -r cases same differs <<<"$out"
ok "switch off is byte-identical to the old re-rank on every case" '[ "$cases" -gt 0 ] && [ "$same" = "$cases" ]'
ok "the comparison is not vacuous (switch on changes some orderings)" '[ "$differs" -gt 0 ]'

# 4. the static kinds are exactly the static mutability class
out="$(python3 - "$NP" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("nunchi", sys.argv[1]); n = importlib.util.module_from_spec(spec)
spec.loader.exec_module(n)
print(" ".join(sorted(k for k in n._KINDS + n._INTERNAL_KINDS + n._LEGACY_KINDS if n._mutability(k) == "static")))
print(0 < n._RECENCY_STATIC_SHARE < 1)
PY
)"
ok "static kinds are preference decision constraint procedure" '[ "$(sed -n 1p <<<"$out")" = "constraint decision preference procedure" ]'
ok "the static share is between 0 and 1" '[ "$(sed -n 2p <<<"$out")" = True ]'

# 5. searching writes nothing
before="$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$NUNCHI_DB")"
probe "lantern-rota gamma rotation" 12 >/dev/null
after="$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$NUNCHI_DB")"
ok "recall does not modify the store" '[ "$before" = "$after" ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
