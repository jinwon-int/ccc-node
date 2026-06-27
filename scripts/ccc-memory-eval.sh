#!/usr/bin/env bash
# ccc-memory-eval.sh — local, no-network smoke/eval harness for memory changes.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
find_tool() { # <tool-name> <repo-relative-fallback>
  local name="$1" fallback="$2" d
  for d in "${CCC_MEMORY_TOOLS_DIR:-}" "$SCRIPT_DIR" "$ROOT/scripts"; do
    [ -n "$d" ] || continue
    if [ -x "$d/$name" ]; then printf '%s\n' "$d/$name"; return 0; fi
  done
  if [ -x "$ROOT/$fallback" ]; then printf '%s\n' "$ROOT/$fallback"; return 0; fi
  return 1
}
INDEX_TOOL="$(find_tool ccc-memory-index.sh scripts/ccc-memory-index.sh)"
SEARCH_TOOL="$(find_tool ccc-memory-search.sh scripts/ccc-memory-search.sh)"
LOAD_HOOK="$(find_tool load-memory.sh claude/hooks/load-memory.sh)"
QUERY="${1:-user preferences}"

if [ "${1:-}" = "--golden" ] || [ "${CCC_MEMORY_EVAL_MODE:-}" = "golden" ]; then
  KEEP_TMP="${CCC_MEMORY_EVAL_KEEP_TMP:-0}"
  if [ -n "${CCC_STATE_DIR:-}" ]; then
    mkdir -p "$CCC_STATE_DIR"
    STATE_DIR="$(mktemp -d "$CCC_STATE_DIR/ccc-memory-golden.XXXXXX")"
  else
    STATE_DIR="$(mktemp -d)"
  fi
  cleanup_golden() { [ "$KEEP_TMP" = "1" ] || rm -rf "$STATE_DIR"; }
  trap cleanup_golden EXIT
  CACHE="$STATE_DIR/cache"
  MEMORY_DIR="$STATE_DIR/memories"
  mkdir -p "$CACHE" "$MEMORY_DIR" "$STATE_DIR"
  printf 'Seoyoon A2A uses Seoseo broker for Team1 and Gwakga broker for Team2. Durable work is PR-first and broker-backed.
ccc-node SessionStart must stay no-network and fail-open.
' > "$MEMORY_DIR/MEMORY.md"
  printf 'Seo Jin On prefers Korean practical evidence-based reports with facts, risks, and next steps.
' > "$MEMORY_DIR/USER.md"
  printf 'Family Wiki candidate: ccc-node memory cache TTL, stale warning, and human-gated Wiki candidate triage.
' > "$CACHE/wiki.txt"
  printf 'Honcho summary: retain Honcho as relational memory; use conservative cadence and task supplement.
' > "$CACHE/honcho.txt"
  printf 'golden-node
' > "$STATE_DIR/node.txt"
  CCC_STATE_DIR="$STATE_DIR" CCC_MEMORY_CACHE_DIR="$CACHE" CCC_MEMORY_DIR="$MEMORY_DIR" "$INDEX_TOOL" rebuild >/dev/null
  python3 - "$SEARCH_TOOL" "$STATE_DIR" <<'PY'
import json, subprocess, sys, time
search_tool, state_dir = sys.argv[1], sys.argv[2]
cases = [
  {"id":"a2a-brokers", "query":"Team1 Seoseo broker Team2 Gwakga", "expected":["MEMORY.md"]},
  {"id":"startup-boundary", "query":"SessionStart no-network fail-open", "expected":["MEMORY.md"]},
  {"id":"korean-report", "query":"Korean practical evidence reports", "expected":["USER.md"]},
  {"id":"cache-stale", "query":"memory cache TTL stale warning", "expected":["wiki.txt"]},
  {"id":"honcho-relational", "query":"Honcho relational task supplement", "expected":["honcho.txt"]},
]
ks=[1,3,5]
start=time.time()
per=[]
latencies=[]
for c in cases:
    one_start=time.time()
    cp=subprocess.run([search_tool, c["query"]], env={"CCC_STATE_DIR":state_dir,"CCC_MEMORY_INDEX_DB":f"{state_dir}/memory-index.sqlite"}, text=True, capture_output=True, timeout=10)
    latencies.append(int((time.time()-one_start)*1000))
    results=json.loads(cp.stdout).get("results", []) if cp.returncode == 0 else []
    paths=[r.get("path","") for r in results]
    ranks=[]
    for exp in c["expected"]:
        rank=next((i+1 for i,p in enumerate(paths) if exp in p), None)
        ranks.append(rank)
    per.append({"id":c["id"],"query":c["query"],"expected":c["expected"],"paths":paths,"ranks":ranks})
metrics={}
for k in ks:
    precisions=[]; recalls=[]
    for row in per:
        top=row["paths"][:k]
        hits=sum(1 for exp in row["expected"] if any(exp in p for p in top))
        precisions.append(hits / max(1, min(k, len(top) or k)))
        recalls.append(hits / len(row["expected"]))
    metrics[f"precision_at_{k}"]=sum(precisions)/len(precisions)
    metrics[f"recall_at_{k}"]=sum(recalls)/len(recalls)
rr=[]
for row in per:
    best=min([r for r in row["ranks"] if r], default=None)
    rr.append(0 if best is None else 1/best)
metrics["mrr"]=sum(rr)/len(rr)
latencies_sorted=sorted(latencies)
def pct(values, q):
    if not values: return 0
    idx=min(len(values)-1, max(0, int(round((len(values)-1)*q))))
    return values[idx]
metrics["latency_p50_ms"]=pct(latencies_sorted, 0.50)
metrics["latency_p95_ms"]=pct(latencies_sorted, 0.95)
metrics["latency_ms"]=int((time.time()-start)*1000)
print(json.dumps({"ok": metrics["recall_at_5"] >= 0.8 and metrics["precision_at_1"] >= 0.6, "mode":"golden", "cases":per, "metrics":metrics, "state_dir":state_dir}, ensure_ascii=False, indent=2))
PY
  exit $?
fi

KEEP_TMP="${CCC_MEMORY_EVAL_KEEP_TMP:-0}"

if [ -n "${CCC_STATE_DIR:-}" ]; then
  CALLER_STATE_DIR="$CCC_STATE_DIR"
  mkdir -p "$CALLER_STATE_DIR"
  STATE_DIR="$(mktemp -d "$CALLER_STATE_DIR/ccc-memory-eval.XXXXXX")"
  CREATED_STATE_DIR=1
else
  STATE_DIR="$(mktemp -d)"
  CREATED_STATE_DIR=1
fi
EVAL_USE_EXTERNAL_DIRS="${CCC_MEMORY_EVAL_USE_EXTERNAL_DIRS:-0}"
if [ "$EVAL_USE_EXTERNAL_DIRS" = "1" ]; then
  CACHE="${CCC_MEMORY_CACHE_DIR:-$STATE_DIR/cache}"
  MEMORY_DIR="${CCC_MEMORY_DIR:-$STATE_DIR/memories}"
else
  # Eval is a smoke harness: keep sample files isolated even when the caller's
  # shell exports real CCC_MEMORY_DIR / CCC_MEMORY_CACHE_DIR.
  CACHE="$STATE_DIR/cache"
  MEMORY_DIR="$STATE_DIR/memories"
fi
INDEX_OUT="$STATE_DIR/eval-index.json"
INDEX_ERR="$STATE_DIR/eval-index.err"
SEARCH_ERR="$STATE_DIR/eval-search.err"
LOAD_ERR="$STATE_DIR/eval-load.err"
START_MS="$(python3 -c 'import time; print(int(time.time()*1000))')"

cleanup() {
  if [ "$KEEP_TMP" = "1" ]; then
    return 0
  fi
  if [ "${CREATED_STATE_DIR:-0}" = "1" ] && [ -n "${STATE_DIR:-}" ] && [ -d "$STATE_DIR" ]; then
    rm -rf "$STATE_DIR"
  fi
}
trap cleanup EXIT
mkdir -p "$CACHE" "$MEMORY_DIR" "$STATE_DIR"
printf 'User prefers concise evidence-based reports.\n' > "$MEMORY_DIR/USER.md"
printf 'Live-check mutable node facts.\n' > "$MEMORY_DIR/MEMORY.md"
printf 'Family Wiki candidate: ccc-node memory cache TTL and Honcho hybrid profile.\n' > "$CACHE/wiki.txt"
printf 'Honcho summary: user prefers Korean practical reports and PR-first workflows.\n' > "$CACHE/honcho.txt"
printf 'eval-node\n' > "$STATE_DIR/node.txt"
printf '%s\n' "$QUERY" > "$STATE_DIR/current-task.txt"

CCC_STATE_DIR="$STATE_DIR" CCC_MEMORY_CACHE_DIR="$CACHE" CCC_MEMORY_DIR="$MEMORY_DIR" \
  "$INDEX_TOOL" rebuild >"$INDEX_OUT" 2>"$INDEX_ERR"
index_rc=$?
search_json="$(CCC_STATE_DIR="$STATE_DIR" CCC_MEMORY_INDEX_DB="$STATE_DIR/memory-index.sqlite" "$SEARCH_TOOL" "$QUERY" 2>"$SEARCH_ERR")"
search_rc=$?
load_json="$(CCC_STATE_DIR="$STATE_DIR" CCC_MEMORY_CACHE_DIR="$CACHE" CCC_MEMORY_DIR="$MEMORY_DIR" CCC_HOOK_DIR="$(dirname "$LOAD_HOOK")" CCC_MEMORY_TOOLS_DIR="$(dirname "$INDEX_TOOL")" CCC_MEMORY_PROFILE=hybrid CCC_LOCAL_MEMORY_ENABLED=1 CCC_MEMORY_QUERY="$QUERY" "$LOAD_HOOK" SessionStart 2>"$LOAD_ERR")"
load_rc=$?
END_MS="$(python3 -c 'import time; print(int(time.time()*1000))')"
bytes="$(printf '%s' "$load_json" | wc -c | tr -d '[:space:]')"
hits="$(printf '%s' "$search_json" | jq '.results | length' 2>/dev/null || printf 0)"

jq -n \
  --arg query "$QUERY" \
  --arg state_dir "$STATE_DIR" \
  --argjson index_rc "$index_rc" \
  --argjson search_rc "$search_rc" \
  --argjson load_rc "$load_rc" \
  --argjson latency_ms "$((END_MS - START_MS))" \
  --argjson injected_bytes "$bytes" \
  --argjson search_hits "$hits" \
  '{ok:($index_rc==0 and $search_rc==0 and $load_rc==0 and $search_hits>0), query:$query, state_dir:$state_dir, latency_ms:$latency_ms, injected_bytes:$injected_bytes, search_hits:$search_hits, rc:{index:$index_rc,search:$search_rc,load:$load_rc}}'
