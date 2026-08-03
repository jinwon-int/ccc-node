#!/usr/bin/env bash
# nunchi weekly bench runner (#824 Phase 1 prep for Phase 2).
# Runs the fixed Q-set through `nunchi.py dialectic`, records per-query
# latency + answer to ~/.nunchi/bench-YYYYMMDD.md for the fleet parity gate
# (Phase 2 exit: two weeks of zero "Honcho-only answers" + zero hallucination).
# Honcho-side answers are collected separately (read-only chat) and compared
# by the reviewing agent; this script never calls Honcho.
# No-op unless nunchi is enabled. Costs one Haiku call per query.
set -uo pipefail

STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
MODE="${CCC_NUNCHI_MODE:-$(cat "$STATE/nunchi.mode" 2>/dev/null || echo off)}"
[ "$MODE" = "on" ] || exit 0

HERE="$(cd "$(dirname "$0")" && pwd)"
QSET="${NUNCHI_BENCH_QSET:-$HERE/bench-qset.tsv}"
NUNCHI_HOME="${NUNCHI_HOME:-$HOME/.nunchi}"
TARGET="${NUNCHI_BENCH_TARGET:-seo-jin-on}"
OUT="$NUNCHI_HOME/bench-$(date +%Y%m%d).md"
[ -f "$QSET" ] || { echo "qset missing: $QSET" >&2; exit 2; }

mkdir -p "$NUNCHI_HOME"
{
  echo "# nunchi bench $(date -Is) node=${CCC_NODE:-$(hostname -s)}"
  echo
} >> "$OUT"

# #890 P5 — body-free DB health counters land in the same weekly file so the
# reviewing agent sees retrieval quality AND write-gate hygiene side by side.
{
  echo "## metrics ($(date -Is))"
  python3 "$HERE/nunchi.py" metrics </dev/null 2>&1 | sed 's/^/- /'
  echo
} >> "$OUT"

tail -n +2 "$QSET" | while IFS=$'\t' read -r qid category query expect; do
  [ -n "$qid" ] || continue
  start="$(date +%s)"
  # The loop itself reads the Q-set from stdin. Provider CLIs launched by
  # nunchi may probe or drain inherited stdin even when the prompt is passed
  # as argv, which used to consume q2..q5 after the first query. Bench inputs
  # are fully specified by argv, so isolate every child from the Q-set pipe.
  ans="$(python3 "$HERE/nunchi.py" dialectic "$query" --target "$TARGET" </dev/null 2>&1)"
  rc=$?
  dur=$(( $(date +%s) - start ))
  {
    echo "## $qid ($category) — ${dur}s rc=$rc"
    echo "- Q: $query"
    echo "- expect: $expect"
    printf '%s\n\n' "$ans" | sed 's/^/  > /'
  } >> "$OUT"
done
echo "bench written: $OUT"
