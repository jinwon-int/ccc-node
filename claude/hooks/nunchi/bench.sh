#!/usr/bin/env bash
# nunchi weekly bench runner (#824 Phase 1 prep for Phase 2).
# Runs the fixed Q-set through `nunchi.py dialectic`, records per-query
# latency + answer to ~/.nunchi/bench-YYYYMMDD.md for the fleet parity gate
# (Phase 2 exit: two weeks of zero "Honcho-only answers" + zero hallucination).
# Honcho-side answers are collected separately (read-only chat) and compared
# by the reviewing agent; this script never calls Honcho.
# No-op unless nunchi is enabled. Costs one Haiku call per query.
#
# #1078 — a provider CLI that is logged out prints its notice to stdout and
# exits 0, so rc alone cannot tell "the backend answered" from "the backend
# never ran". Such a row also contains no "기록 없음", which made contaminated
# nodes score as the BEST performers on the Phase 2 parity gate. Every row now
# carries an explicit status= field and the run ends with a validity summary,
# so the reviewing agent sees sample contamination before reading any answer.
# Rows are marked, never dropped: the answer text stays on disk so a human can
# overrule a false positive.
set -uo pipefail

STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
MODE="${CCC_NUNCHI_MODE:-$(cat "$STATE/nunchi.mode" 2>/dev/null || echo off)}"
[ "$MODE" = "on" ] || exit 0

# In audience-scoped mode the cron entry is a body-free dispatcher. It visits
# only canonical direct children of the configured opaque audience root and
# reinvokes this script against that scope's own DB and snapshot, writing a
# scope-local bench file. The scope enumerator below is kept byte-identical to
# the copies in piri-feed.sh and mempalace-refresh.sh; bench.test.sh asserts
# the three stay in sync.
#
# Without this the run always scored $HOME/.nunchi. On a scoped node ingest
# writes only to <audience-root>/<scope>/nunchi/facts.db, so the unscoped DB
# stops receiving facts the moment scoping is enabled and the Phase 2 parity
# gate (#827) measured a frozen store instead of live memory — observed on
# soonwook (bench read 52 facts @2026-07-30 while ingest held 58 @2026-08-19),
# jingun, bangtong and dungae.
if [ "${CCC_NUNCHI_AUDIENCE_SCOPED:-0}" = 1 ] \
    && [ "${CCC_NUNCHI_SCOPED_CHILD:-0}" != 1 ]; then
  audience_root="${CCC_NUNCHI_AUDIENCE_ROOT:-}"
  max_scopes="${CCC_NUNCHI_MAX_SCOPES_PER_RUN:-64}"
  case "$max_scopes" in
    ''|*[!0-9]*) max_scopes=64 ;;
    *) [ "$max_scopes" -ge 1 ] && [ "$max_scopes" -le 64 ] || max_scopes=64 ;;
  esac
  rc=0
  while IFS= read -r scope_root; do
    # Bench a scope only once it has a fact store. A scope that has never
    # ingested would otherwise emit a full sheet of "기록 없음" and be counted
    # as retrieval failure against the gate rather than as absent sample.
    [ -f "$scope_root/nunchi/facts.db" ] || continue
    scope="${scope_root##*/}"
    kind=private
    [ "$scope" = shared ] && kind=shared
    CCC_NUNCHI_SCOPED_CHILD=1 \
      CCC_NUNCHI_AUDIENCE_SCOPE="$scope" \
      CCC_NUNCHI_AUDIENCE_KIND="$kind" \
      NUNCHI_HOME="$scope_root/nunchi" \
      NUNCHI_DB="$scope_root/nunchi/facts.db" \
      NUNCHI_SNAPSHOT="$scope_root/nunchi/snapshot.md" \
      bash "$0" || rc=1
  done < <(python3 - "$audience_root" "$max_scopes" <<'PY'
import os
from pathlib import Path
import re
import stat
import sys

root = Path(sys.argv[1])
limit = int(sys.argv[2])
try:
    meta = root.lstat()
except OSError:
    raise SystemExit(0)
if not (
    root.is_absolute()
    and stat.S_ISDIR(meta.st_mode)
    and meta.st_uid == os.geteuid()
    and not stat.S_IMODE(meta.st_mode) & 0o077
):
    raise SystemExit(0)
count = 0
for child in sorted(root.iterdir(), key=lambda item: item.name):
    if count >= limit:
        break
    if child.name != "shared" and not re.fullmatch(r"private-[0-9a-f]{32}", child.name):
        continue
    try:
        item = child.lstat()
    except OSError:
        continue
    if not (
        stat.S_ISDIR(item.st_mode)
        and item.st_uid == os.geteuid()
        and not stat.S_IMODE(item.st_mode) & 0o077
    ):
        continue
    print(child)
    count += 1
PY
  )
  exit "$rc"
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
QSET="${NUNCHI_BENCH_QSET:-$HERE/bench-qset.tsv}"
NUNCHI_HOME="${NUNCHI_HOME:-$HOME/.nunchi}"
TARGET="${NUNCHI_BENCH_TARGET:-seo-jin-on}"
OUT="$NUNCHI_HOME/bench-$(date +%Y%m%d).md"
# Provider notices that mean "no answer was produced". Extended-regex,
# case-insensitive, overridable per node for providers not covered here.
INVALID_RE="${NUNCHI_BENCH_INVALID_RE:-Not logged in|Please run /login|Invalid API key|authentication_error|insufficient_quota|rate_limit_exceeded}"
# Ways an answer says "the evidence does not contain this". The dialectic prompt
# asks for the literal "기록 없음", but backends paraphrase it, and a literal
# match scored those paraphrases as successes. Kept byte-identical to
# nunchi.py's NO_RECORD_RE; nunchi.test.sh asserts the two stay in sync.
NO_RECORD_RE="${NUNCHI_BENCH_NO_RECORD_RE:-기록 없음|기록이 없|근거가 없|근거 기록이 없|찾을 수 없|확인할 수 없|정보가 없}"
# #827 — TM-2029 assigns durable cross-node knowledge to the Family Wiki, so the
# parity gate must ask "nunchi + Wiki", not "nunchi alone". A per-node store can
# never answer a cross-node question, which made the old gate unpassable by
# design rather than by defect. Only consulted when nunchi found nothing.
WIKI_CLI="${NUNCHI_BENCH_WIKI_CLI:-$(command -v wiki-agent 2>/dev/null || echo "$HOME/.wiki-agent/bin/wiki-agent")}"
WIKI_TIMEOUT="${NUNCHI_BENCH_WIKI_TIMEOUT_SEC:-40}"
[ -f "$QSET" ] || { echo "qset missing: $QSET" >&2; exit 2; }

mkdir -p "$NUNCHI_HOME"
{
  # scope= is body-free: it names the opaque audience partition, never its
  # contents. Without it the per-scope sheets are indistinguishable and the
  # fleet roll-up cannot tell one node's two scopes from two nodes.
  scope_label=""
  [ -n "${CCC_NUNCHI_AUDIENCE_SCOPE:-}" ] \
    && scope_label=" scope=${CCC_NUNCHI_AUDIENCE_SCOPE} kind=${CCC_NUNCHI_AUDIENCE_KIND:-private}"
  echo "# nunchi bench $(date -Is) node=${CCC_NODE:-$(hostname -s)}${scope_label}"
  echo
} >> "$OUT"

# #890 P5 — body-free DB health counters land in the same weekly file so the
# reviewing agent sees retrieval quality AND write-gate hygiene side by side.
{
  echo "## metrics ($(date -Is))"
  python3 "$HERE/nunchi.py" metrics </dev/null 2>&1 | sed 's/^/- /'
  echo
} >> "$OUT"

# Per-run status ledger. $OUT is append-mode and may already hold an earlier
# run from the same day, so the summary must count THIS run only.
LEDGER="$(mktemp "${TMPDIR:-/tmp}/nunchi-bench-XXXXXX")" || exit 2
trap 'rm -f "$LEDGER"' EXIT

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
  # rc is reported unchanged — it is a true fact about the child. status is a
  # separate judgement about whether the row is usable as a parity sample.
  status=OK
  reason=
  if [ "$rc" != 0 ]; then
    status=INVALID; reason="exit-$rc"
  elif [ -z "${ans//[[:space:]]/}" ]; then
    status=INVALID; reason=empty
  elif printf '%s' "$ans" | grep -qiE "$INVALID_RE"; then
    status=INVALID; reason=provider-failure
  fi
  # Which layer answered. Only meaningful for a usable sample: an INVALID row
  # says nothing about retrieval, so it is never attributed to a layer.
  source=none
  wiki_ans=
  if [ "$status" = OK ]; then
    if printf '%s' "$ans" | grep -qE "$NO_RECORD_RE"; then
      # nunchi found nothing — consult the layer TM-2029 made responsible for
      # durable cross-node knowledge before counting this against the gate.
      if [ -n "$WIKI_CLI" ] && [ -x "$WIKI_CLI" ]; then
        wiki_ctx="$(timeout "$WIKI_TIMEOUT" "$WIKI_CLI" find "$query" </dev/null 2>/dev/null | head -c 4000)"
        if [ -n "${wiki_ctx//[[:space:]]/}" ]; then
          wiki_ans="$(printf '%s' "$wiki_ctx" \
            | timeout "$WIKI_TIMEOUT" python3 "$HERE/nunchi.py" synthesize "$query" 2>&1)"
          if [ -n "${wiki_ans//[[:space:]]/}" ] \
             && ! printf '%s' "$wiki_ans" | grep -qE "$NO_RECORD_RE" \
             && ! printf '%s' "$wiki_ans" | grep -qiE "$INVALID_RE"; then
            source=wiki
          fi
        fi
      fi
    else
      source=nunchi
    fi
  fi
  printf '%s\t%s\t%s\n' "$qid" "$status" "$source" >> "$LEDGER"
  {
    if [ -n "$reason" ]; then
      echo "## $qid ($category) — ${dur}s rc=$rc status=$status reason=$reason source=$source"
    else
      echo "## $qid ($category) — ${dur}s rc=$rc status=$status source=$source"
    fi
    echo "- Q: $query"
    echo "- expect: $expect"
    printf '%s\n\n' "$ans" | sed 's/^/  > /'
    if [ "$source" = wiki ]; then
      echo "- Wiki 계층 답변:"
      printf '%s\n\n' "$wiki_ans" | sed 's/^/  w> /'
    fi
  } >> "$OUT"
done

total="$(wc -l < "$LEDGER" | tr -d ' ')"
invalid="$(grep -c $'\tINVALID\t' "$LEDGER" || true)"
valid=$(( total - invalid ))
by_nunchi="$(grep -c $'\tnunchi$' "$LEDGER" || true)"
by_wiki="$(grep -c $'\twiki$' "$LEDGER" || true)"
unanswered=$(( valid - by_nunchi - by_wiki ))
{
  echo "## bench-summary ($(date -Is))"
  echo "- queries: $total"
  echo "- valid: $valid"
  echo "- invalid: $invalid"
  # #827 / TM-2339 — the Phase 2 gate counts "queries only Honcho answered".
  # TM-2029 gave durable cross-node knowledge to the Wiki, so the honest
  # denominator is "neither nunchi nor Wiki answered", not "nunchi alone".
  echo "- answered by nunchi: $by_nunchi"
  echo "- answered by wiki: $by_wiki"
  echo "- unanswered (gate candidates): $unanswered"
  if [ "$invalid" -gt 0 ]; then
    # The parity gate counts "기록 없음" answers. An INVALID row has none, so
    # leaving it in the sample makes a dead backend look like a perfect score.
    echo "- ⚠️ SAMPLE CONTAMINATED — exclude this node's run from the Phase 2"
    echo "  parity gate (#827) until the backend is healthy. Invalid rows"
    echo "  produce no \"기록 없음\" and would otherwise score as best-in-fleet."
  fi
  echo
} >> "$OUT"

echo "bench written: $OUT (valid=$valid invalid=$invalid)"
