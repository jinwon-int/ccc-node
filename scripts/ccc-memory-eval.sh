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

if [ "${1:-}" = "--scenario" ] || [ "${CCC_MEMORY_EVAL_MODE:-}" = "scenario" ]; then
  KEEP_TMP="${CCC_MEMORY_EVAL_KEEP_TMP:-0}"
  if [ -n "${CCC_STATE_DIR:-}" ]; then
    mkdir -p "$CCC_STATE_DIR"
    STATE_DIR="$(mktemp -d "$CCC_STATE_DIR/ccc-memory-scenario.XXXXXX")"
  else
    STATE_DIR="$(mktemp -d)"
  fi
  cleanup_scenario() { [ "$KEEP_TMP" = "1" ] || rm -rf "$STATE_DIR"; }
  trap cleanup_scenario EXIT
  CACHE="$STATE_DIR/cache"
  MEMORY_DIR="$STATE_DIR/memories"
  FACTS="$STATE_DIR/memory-facts.jsonl"
  mkdir -p "$CACHE" "$MEMORY_DIR" "$STATE_DIR"
  printf 'ccc-node SessionStart must stay no-network and fail-open. Durable operating policy uses PR-first evidence and human-gated Wiki triage. historical editor Vim was used before the current editor changed.\n' > "$MEMORY_DIR/MEMORY.md"
  printf 'Seo Jin On prefers Korean practical evidence-based reports with risks and next steps.\n' > "$MEMORY_DIR/USER.md"
  printf 'Family Wiki cache: ccc-node memory roadmap includes scenario eval and explainable retrieval.\n' > "$CACHE/wiki.txt"
  printf 'Honcho cache: current task concerns local-first memory hardening and benchmark adapter experiments.\n' > "$CACHE/honcho.txt"
  cat > "$FACTS" <<'JSONL'
{"id":"new-editor","kind":"preference","text":"Current editor preference for ccc-node memory fixtures is Helix.","entities":["ccc-node","Helix"],"tags":["temporal","preference"],"observed_at":"2026-06-27T00:00:00Z","valid_from":"2026-06-27T00:00:00Z","valid_until":null,"confidence":0.95,"durability":"durable","privacy":"private","review":"auto-local","source":{"type":"scenario-fixture","path":"memory-facts.jsonl"}}
{"id":"old-editor","kind":"preference","text":"Historical editor preference for ccc-node memory fixtures was Vim before Helix.","entities":["ccc-node","Vim"],"tags":["historical","preference"],"observed_at":"2026-01-01T00:00:00Z","valid_from":"2026-01-01T00:00:00Z","valid_until":"2026-06-27T00:00:00Z","confidence":0.8,"durability":"durable","privacy":"private","review":"auto-local","source":{"type":"scenario-fixture","path":"memory-facts.jsonl"}}
{"id":"volatile-pr","kind":"task-progress","text":"Volatile task progress says a no-network startup PR draft is pending and should not outrank durable policy.","entities":["ccc-node"],"tags":["volatile"],"observed_at":"2026-06-27T00:01:00Z","confidence":0.6,"durability":"volatile","privacy":"private","review":"auto-local","source":{"type":"scenario-fixture","path":"memory-facts.jsonl"}}
{"id":"benchmark-adapter","kind":"procedure","text":"Benchmark adapter experiments must be disabled by default and export only synthetic scenario fixtures unless an operator explicitly points at real memory.","entities":["benchmark adapter","ccc-node"],"tags":["benchmark","safety"],"observed_at":"2026-06-27T00:02:00Z","confidence":0.9,"durability":"durable","privacy":"private","review":"auto-local","source":{"type":"scenario-fixture","path":"memory-facts.jsonl"}}
JSONL
  printf 'scenario-node\n' > "$STATE_DIR/node.txt"
  CCC_STATE_DIR="$STATE_DIR" CCC_MEMORY_CACHE_DIR="$CACHE" CCC_MEMORY_DIR="$MEMORY_DIR" CCC_MEMORY_FACTS_FILE="$FACTS" "$INDEX_TOOL" rebuild >/dev/null
  python3 - "$SEARCH_TOOL" "$STATE_DIR" <<'PY'
import json, os, subprocess, sys, time
search_tool, state_dir = sys.argv[1], sys.argv[2]
cases = [
  {"id":"accurate-retrieval", "query":"Korean practical evidence reports", "expected":["USER.md"], "competency":"accurate_retrieval"},
  {"id":"incremental-structured", "query":"benchmark adapter experiments synthetic scenario fixtures", "expected":["benchmark-adapter"], "competency":"test_time_learning"},
  {"id":"temporal-current", "query":"current editor preference Helix", "expected":["new-editor"], "competency":"temporal_current"},
  {"id":"temporal-history", "query":"historical editor Vim before Helix", "expected":["old-editor"], "competency":"temporal_history"},
  {"id":"volatile-demotion", "query":"durable operating policy no-network startup", "expected":["MEMORY.md"], "not_top":["volatile-pr"], "competency":"selective_forgetting"},
]
per=[]; latencies=[]
for c in cases:
    start=time.time()
    env={"CCC_STATE_DIR":state_dir,"CCC_MEMORY_INDEX_DB":f"{state_dir}/memory-index.sqlite","CCC_MEMORY_RETRIEVAL":"hybrid-local","CCC_MEMORY_SEARCH_LIMIT":"5"}
    cp=subprocess.run([search_tool, c["query"]], env=env, text=True, capture_output=True, timeout=10)
    latencies.append(int((time.time()-start)*1000))
    results=json.loads(cp.stdout).get("results", []) if cp.returncode == 0 else []
    paths=[r.get("path","") for r in results]
    ranks=[]
    for exp in c["expected"]:
        rank=next((i+1 for i,p in enumerate(paths) if exp in p), None)
        ranks.append(rank)
    forbidden_top=False
    if c.get("not_top") and paths:
        forbidden_top=any(x in paths[0] for x in c["not_top"])
    per.append({**c,"paths":paths,"ranks":ranks,"forbidden_top":forbidden_top})
ks=[1,3,5]
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
metrics["temporal_current_accuracy"]=1.0 if next(r for r in per if r["id"]=="temporal-current")["ranks"][0] == 1 else 0.0
metrics["conflict_resolution_accuracy"]=(metrics["temporal_current_accuracy"] + (1.0 if next(r for r in per if r["id"]=="temporal-history")["ranks"][0] == 1 else 0.0))/2
metrics["volatile_exclusion_accuracy"]=1.0 if not next(r for r in per if r["id"]=="volatile-demotion")["forbidden_top"] else 0.0
latencies_sorted=sorted(latencies)
def pct(values,q):
    if not values: return 0
    return values[min(len(values)-1, max(0, int(round((len(values)-1)*q))))]
metrics["latency_p50_ms"]=pct(latencies_sorted,0.50)
metrics["latency_p95_ms"]=pct(latencies_sorted,0.95)
ok=metrics["recall_at_5"] >= 0.8 and metrics["precision_at_1"] >= 0.6 and metrics["temporal_current_accuracy"] == 1.0 and metrics["volatile_exclusion_accuracy"] == 1.0
print(json.dumps({"ok":ok,"mode":"scenario","cases":per,"metrics":metrics,"state_dir":state_dir}, ensure_ascii=False, indent=2))
PY
  exit $?
fi

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
