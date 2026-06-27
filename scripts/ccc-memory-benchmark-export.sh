#!/usr/bin/env bash
# ccc-memory-benchmark-export.sh — disabled-by-default local fixture export helper.
# It exports synthetic ccc-node memory scenario fixtures for benchmark/adaptor experiments.
# It never reads real user memory unless --from-state is explicitly supplied.
set -uo pipefail
MODE="synthetic"
STATE_DIR=""
OUTPUT="jsonl"
while [ $# -gt 0 ]; do
  case "$1" in
    --synthetic) MODE="synthetic"; shift ;;
    --from-state) MODE="from-state"; STATE_DIR="${2:-}"; shift 2 ;;
    --json) OUTPUT="json"; shift ;;
    --help|-h)
      echo "usage: $0 [--synthetic] [--from-state <state-dir>] [--json]"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
python3 - "$MODE" "$STATE_DIR" "$OUTPUT" <<'PY'
import json, sys
from pathlib import Path
mode, state_dir, output = sys.argv[1:]
rows=[]
if mode == "synthetic":
    rows = [
        {"id":"accurate-retrieval", "competency":"accurate_retrieval", "memory":["Seo Jin On prefers Korean practical evidence-based reports."], "query":"Korean practical evidence reports", "expected":"Seo Jin On prefers Korean practical evidence-based reports."},
        {"id":"temporal-current", "competency":"temporal_current", "memory":["Historical editor was Vim.", "Current editor is Helix."], "query":"current editor", "expected":"Current editor is Helix."},
        {"id":"selective-forgetting", "competency":"selective_forgetting", "memory":["Durable policy: SessionStart no-network fail-open.", "Volatile task progress: PR pending."], "query":"durable startup policy", "expected":"Durable policy: SessionStart no-network fail-open."},
        {"id":"benchmark-adapter", "competency":"benchmark_export", "memory":["Benchmark adapter exports synthetic fixtures by default."], "query":"benchmark adapter default export", "expected":"Benchmark adapter exports synthetic fixtures by default."},
    ]
elif mode == "from-state":
    if not state_dir:
        raise SystemExit("--from-state requires a state dir")
    base=Path(state_dir)
    facts=base/"memory-facts.jsonl"
    if not facts.exists():
        raise SystemExit(f"memory facts file not found: {facts}")
    for i,line in enumerate(facts.read_text(encoding='utf-8', errors='replace').splitlines(), start=1):
        if not line.strip():
            continue
        obj=json.loads(line)
        rows.append({"id":obj.get("id") or f"fact-{i}", "competency":"operator_supplied", "memory":[obj.get("text","")], "query":"", "expected":obj.get("text","")})
else:
    raise SystemExit("invalid mode")
if output == "json":
    print(json.dumps({"ok":True,"mode":mode,"real_memory_read":mode=="from-state","items":rows}, ensure_ascii=False, indent=2))
else:
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
PY
