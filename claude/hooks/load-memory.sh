#!/usr/bin/env bash
# SessionStart memory bootstrap for a Claude Code node (node-owned memory).
# Serves built-in MEMORY/USER + bounded cached Family Wiki/Honcho/local hot memory instantly,
# then fires a detached background refresh so the next session is fresh.
set -uo pipefail

# Distill subprocess guard: when a distill pipeline spawns `claude -p ...`,
# we don't want the child to re-load memory / refresh caches / fire more
# distillations. See ~/.claude/hooks/distill.sh for the parent setter.
[ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ] && exit 0

EVENT="${1:-SessionStart}"
STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
CACHE="${CCC_MEMORY_CACHE_DIR:-${HOME:-/root}/.claude/hooks/cache}"
HOOKDIR="${CCC_HOOK_DIR:-${HOME:-/root}/.claude/hooks}"
MEMDIR="${CCC_MEMORY_DIR:-${HOME:-/root}/.claude/memories}"
PROFILE="${CCC_MEMORY_PROFILE:-honcho}"
TTL="${CCC_MEMORY_CACHE_TTL_SEC:-21600}"
MAX_TOTAL="${CCC_MEMORY_MAX_BYTES:-12000}"
MAX_MEM="${CCC_BUILTIN_MEMORY_MAX_BYTES:-4000}"
MAX_WIKI="${CCC_WIKI_MAX_BYTES:-5000}"
MAX_HONCHO="${CCC_HONCHO_MAX_BYTES:-4000}"
MAX_LOCAL="${CCC_LOCAL_MEMORY_MAX_BYTES:-3000}"
MAX_RESUME="${CCC_RESUME_MAX_BYTES:-2000}"
HONCHO_ENABLED="${CCC_HONCHO_MEMORY_ENABLED:-1}"
WIKI_ENABLED="${CCC_WIKI_MEMORY_ENABLED:-1}"
ISOLATION_PROFILE="${CCC_NODE_ISOLATION_PROFILE:-fleet}"
[ "$ISOLATION_PROFILE" = "external" ] && WIKI_ENABLED=0
USER_LABEL="${CCC_MEMORY_USER_LABEL:-Seo Jin On}"
# Local hot-memory search is ON by default for every profile now that the
# default retrieval reranks with durability/source/recency boosts; set
# CCC_LOCAL_MEMORY_ENABLED=0/false/off to opt out. hybrid/max-perf always query
# it regardless (that is part of their definition).
LOCAL_ENABLED="${CCC_LOCAL_MEMORY_ENABLED:-}"
QUERY="${CCC_MEMORY_QUERY:-}"

is_disabled() { case "${1:-}" in 0|false|FALSE|off|OFF|no|NO) return 0;; *) return 1;; esac; }

scan_injection_block() { # <label> <text>
  local label="$1" text="$2" scanned
  if [ -x "$HOOKDIR/scan-injection.sh" ] \
    && scanned="$(printf '%s' "$text" | "$HOOKDIR/scan-injection.sh" "$label" 2>/dev/null)"; then
    printf '%s' "$scanned"
  else
    printf '%s' "$text"
  fi
}

limit_bytes() { # <max> <text>
  local max="$1"
  python3 -c 'import sys
limit = int(sys.argv[1])
data = sys.stdin.buffer.read()
if limit > 0 and len(data) > limit:
    # Reserve room for the truncation marker so the total output stays within
    # <limit> bytes. Slicing to <limit> and THEN appending the suffix used to
    # overshoot the declared cap by the suffix length (~38 bytes).
    suffix = "\n… [truncated by CCC memory budget]\n".encode("utf-8")
    keep = max(0, limit - len(suffix))
    text = data[:keep].decode("utf-8", errors="ignore")
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.write(suffix)
else:
    sys.stdout.buffer.write(data)
' "$max"
}

# Cross-source injection dedup. The local hot-memory search re-surfaces hits from
# MEMORY.md/USER.md (source=memory) and the wiki/honcho caches (source=cache) that
# are ALSO injected verbatim as their own blocks above — double-spending the
# bounded injection budget. Drop such a hit only when its snippet is already fully
# present in the injected text, so anything truncated away from the canonical
# block is still kept (lossless). Structured (distilled-fact) and distill-state
# hits have no other injection path and are always kept.
# Set CCC_MEMORY_INJECT_DEDUP=0/false/off to disable.
dedup_local_hot() { # <injected-text> <search-json>
  if is_disabled "${CCC_MEMORY_INJECT_DEDUP:-1}"; then printf '%s' "$2"; return 0; fi
  # JSON is passed via env, not stdin: the heredoc below occupies stdin.
  INJECTED="$1" SEARCH_JSON="$2" python3 - 2>/dev/null <<'PY' || printf '%s' "$2"
import json, os, re, sys
raw = os.environ.get("SEARCH_JSON", "")
try:
    doc = json.loads(raw)
except Exception:
    sys.stdout.write(raw); sys.exit(0)
results = doc.get("results") if isinstance(doc, dict) else None
if not isinstance(results, list) or not results:
    sys.stdout.write(raw); sys.exit(0)

def norm(t):
    return " ".join(re.findall(r"[0-9a-z가-힣]+", (t or "").lower()))

injected = norm(os.environ.get("INJECTED", ""))
kept, dropped = [], 0
for r in results:
    if str(r.get("source") or "") not in ("memory", "cache"):
        kept.append(r); continue
    snip = str(r.get("snippet") or r.get("content") or r.get("text") or "")
    snip = snip.replace("[", " ").replace("]", " ")
    frags = [f for f in (norm(p) for p in re.split(r"\s*(?:…|\.\.\.)\s*", snip)) if len(f) >= 12]
    if injected and frags and all(f in injected for f in frags):
        dropped += 1; continue
    kept.append(r)
doc["results"] = kept
if dropped:
    doc["injectionDedup"] = {"dropped": dropped, "kept": len(kept)}
sys.stdout.write(json.dumps(doc, ensure_ascii=False))
PY
}

# Fail closed immediately when Wiki memory is disabled, even before the next
# background index update removes a stale wiki.txt row from SQLite.
filter_disabled_wiki_hits() { # <search-json>
  if ! is_disabled "$WIKI_ENABLED"; then printf '%s' "$1"; return 0; fi
  SEARCH_JSON="$1" python3 - 2>/dev/null <<'PY' || printf '%s' '{"results":[]}'
import json, os, pathlib, sys
raw = os.environ.get("SEARCH_JSON", "")
try:
    doc = json.loads(raw)
except Exception:
    sys.stdout.write('{"results":[]}'); raise SystemExit(0)
results = doc.get("results") if isinstance(doc, dict) else None
if not isinstance(results, list):
    sys.stdout.write('{"results":[]}'); raise SystemExit(0)
def visible(row):
    if not isinstance(row, dict):
        return False
    p = pathlib.PurePath(str(row.get("path") or ""))
    source = str(row.get("source") or "").lower()
    if p.name in {"wiki.txt", "wiki-candidates.md"}:
        return False
    if source == "distill-local":
        return True
    return not (p.name == "distill-last.json" or "distill-history" in p.parts or source.startswith("distill"))
doc["results"] = [row for row in results if visible(row)]
sys.stdout.write(json.dumps(doc, ensure_ascii=False))
PY
}

# Render the (deduped) local hot-memory search JSON as compact, readable lines
# for injection. The raw search JSON carries full filesystem paths, a per-result
# score and an 8-field `signals` object that are debug-only noise to the model
# and waste the bounded injection budget — the agent only needs the snippet and
# which source it came from. The search tool and ccc-memory-explain still emit
# full JSON for diagnostics; this only changes what gets injected.
# Set CCC_MEMORY_INJECT_RENDER=0/false/off to inject the raw JSON instead.
render_local_hot() { # <search-json>
  if is_disabled "${CCC_MEMORY_INJECT_RENDER:-1}"; then printf '%s' "$1"; return 0; fi
  SEARCH_JSON="$1" python3 - 2>/dev/null <<'PY' || printf '%s' "$1"
import json, os, re, sys
raw = os.environ.get("SEARCH_JSON", "")
try:
    doc = json.loads(raw)
except Exception:
    sys.stdout.write(raw); sys.exit(0)
results = doc.get("results") if isinstance(doc, dict) else None
if not isinstance(results, list):
    sys.stdout.write(raw); sys.exit(0)
LABEL = {"memory": "memory", "cache": "cache", "structured": "fact",
         "state": "distill", "distill-history": "distill", "distill-local": "distill"}
lines = []
for r in results:
    if not isinstance(r, dict):
        continue
    snip = str(r.get("snippet") or r.get("content") or r.get("text") or "")
    snip = re.sub(r"\s+", " ", snip.replace("[", "").replace("]", "")).strip()
    # FTS snippets bracket matches and wrap gaps in "…"; drop the leading/trailing
    # ellipsis so the rendered line reads cleanly (internal gaps are kept).
    snip = re.sub(r"^\s*(?:…|\.\.\.)\s*|\s*(?:…|\.\.\.)\s*$", "", snip)
    if not snip:
        continue
    lines.append(f"- ({LABEL.get(str(r.get('source') or ''), 'memory')}) {snip}")
sys.stdout.write("\n".join(lines))
PY
}

find_memory_tool() { # <tool-name>
  local name="$1" d
  for d in "${CCC_MEMORY_TOOLS_DIR:-}" "$HOOKDIR" "$HOOKDIR/../../scripts"; do
    [ -n "$d" ] || continue
    if [ -x "$d/$name" ]; then printf '%s\n' "$d/$name"; return 0; fi
  done
  return 1
}

build_memory_query() {
  if [ -n "${QUERY:-}" ]; then printf '%s' "$QUERY"; return 0; fi
  local query_tool
  query_tool="$(find_memory_tool ccc-memory-query.sh 2>/dev/null || true)"
  if [ -n "$query_tool" ]; then
    CCC_WORKTREE="${CCC_WORKTREE:-$(pwd 2>/dev/null || true)}" "$query_tool" --mode local 2>/dev/null && return 0
  fi
  cat "$STATE_DIR/current-task.txt" 2>/dev/null || printf 'current task'
}
QUERY="$(build_memory_query)"

age_seconds() { # <file>
  local f="$1" now ts
  [ -f "$f" ] || { printf '%s' '-1'; return; }
  now="$(date -u +%s)"
  ts="$(date -u -r "$f" +%s 2>/dev/null || printf '0')"
  [ "$ts" = "0" ] && printf '%s' '-1' || printf '%s' "$((now - ts))"
}

stale_note() { # <label> <file>
  local label="$1" file="$2" age
  age="$(age_seconds "$file")"
  if [ "$age" -lt 0 ]; then
    printf '%s cache missing' "$label"
  elif [ "$age" -gt "$TTL" ]; then
    printf '%s cache stale (%ss old)' "$label" "$age"
  else
    printf '%s cache fresh (%ss old)' "$label" "$age"
  fi
}

# Built-in node memory lives under ~/.claude/memories; legacy Hermes memory is fallback only.
mem="$(cat "$MEMDIR/MEMORY.md" "$MEMDIR/USER.md" 2>/dev/null)"
[ -z "$mem" ] && mem="$(cat ${HOME:-/root}/.hermes/memories/MEMORY.md ${HOME:-/root}/.hermes/memories/USER.md 2>/dev/null)"
wiki=""
if ! is_disabled "$WIKI_ENABLED"; then
  wiki="$(cat "$CACHE/wiki.txt" 2>/dev/null)"
fi
honcho=""
if ! is_disabled "$HONCHO_ENABLED" && [ "$PROFILE" != "max-perf" ]; then
  honcho="$(cat "$CACHE/honcho.txt" 2>/dev/null)"
fi
resume="$(cat "${CCC_RESUME_FILE:-$STATE_DIR/resume.md}" 2>/dev/null)"

# Limit the canonical blocks first (static caps) so we can measure their slack
# before sizing the local hot block.
mem="$(scan_injection_block built-in-memory "$mem" | limit_bytes "$MAX_MEM")"
resume="$(scan_injection_block resume-pointer "$resume" | limit_bytes "$MAX_RESUME")"
if ! is_disabled "$WIKI_ENABLED"; then
  wiki="$(scan_injection_block family-wiki-cache "$wiki" | limit_bytes "$MAX_WIKI")"
fi
honcho="$(scan_injection_block honcho-cache "$honcho" | limit_bytes "$MAX_HONCHO")"

# Relevance-aware budget. The per-block caps sum to more than CCC_MEMORY_MAX_BYTES,
# so today the tail (Honcho) is simply truncated and any budget a small/empty block
# leaves unused (no wiki/honcho cache, or max-perf which drops Honcho) is wasted —
# while the local hot block is also under-filled because the search returns only
# CCC_MEMORY_SEARCH_LIMIT (5) results regardless. Reclaim that slack for the local
# hot block — the task-conditioned, most query-relevant source — by growing BOTH
# its byte budget AND how many results we fetch to fill it. Purely additive: never
# below MAX_LOCAL / the default limit (worst case == today); the final MAX_TOTAL
# cap still bounds the whole injection. Disable with CCC_MEMORY_DYNAMIC_BUDGET=0.
alloc_local="$MAX_LOCAL"
search_limit="${CCC_MEMORY_SEARCH_LIMIT:-}"
if ! is_disabled "${CCC_MEMORY_DYNAMIC_BUDGET:-1}"; then
  msize="$(printf '%s' "$mem" | wc -c)"
  wsize="$(printf '%s' "$wiki" | wc -c)"
  hsize="$(printf '%s' "$honcho" | wc -c)"
  rsize="$(printf '%s' "$resume" | wc -c)"
  # alloc = byte budget for local (>= MAX_LOCAL, reclaiming slack up to the total
  # minus a ~1000B scaffold reserve); dyn_limit = results to fetch to fill it
  # (~180B/result, clamped to [5,25]). The final limit_bytes is the hard bound.
  budget_out="$(python3 -c 'import sys
total, reserve, maxlocal, bpr, base, maxlim, m, r, w, h = (int(x) for x in sys.argv[1:])
alloc = max(maxlocal, total - reserve - m - r - w - h)
print(alloc, max(base, min(maxlim, alloc // bpr)))' \
    "$MAX_TOTAL" 1000 "$MAX_LOCAL" 180 5 25 "$msize" "$rsize" "$wsize" "$hsize" 2>/dev/null || true)"
  alloc_candidate="${budget_out%% *}"
  limit_candidate="${budget_out##* }"
  case "$alloc_candidate" in ''|*[!0-9]*) ;; *) alloc_local="$alloc_candidate" ;; esac
  if [ -z "$search_limit" ]; then
    case "$limit_candidate" in ''|*[!0-9]*) ;; *) search_limit="$limit_candidate" ;; esac
  fi
fi

local_hot=""
if [ "$PROFILE" = "hybrid" ] || [ "$PROFILE" = "max-perf" ] || ! is_disabled "$LOCAL_ENABLED"; then
  search_tool="$(find_memory_tool ccc-memory-search.sh 2>/dev/null || true)"
  if [ -n "$search_tool" ]; then
    # No line-cap here: dedup/render parse the whole JSON (a partial cut would
    # break json.loads and fall back to raw). Result count is bounded by
    # search_limit and the byte budget is enforced by limit_bytes below.
    # CCC_MEMORY_RECORD_USAGE=1: this is the real injection retrieval, so let the
    # search record retrieval-frequency feedback (diagnostics stay read-only).
    local_hot="$({ CCC_MEMORY_RECORD_USAGE=1 CCC_MEMORY_SEARCH_LIMIT="$search_limit" "$search_tool" "$QUERY" 2>/dev/null || true; })"
  fi
fi

local_hot="$(filter_disabled_wiki_hits "$local_hot")"

# Dedup the local hot block against what we ACTUALLY inject above (post-redaction,
# post-truncation) before rendering it — so it surfaces index-only content
# (distilled facts) instead of echoing the canonical blocks.
local_hot="$(dedup_local_hot "$mem
$wiki
$honcho" "$local_hot")"
# Render the search JSON to compact readable lines, then apply the (possibly
# enlarged) local byte budget.
local_hot="$(render_local_hot "$local_hot")"
local_hot="$(scan_injection_block local-hot-memory "$local_hot" | limit_bytes "$alloc_local")"

node_label="${CCC_NODE:-$(cat "$STATE_DIR/node.txt" 2>/dev/null || hostname -s 2>/dev/null || printf 'ccc-node')}"
stamp="$(cat "$CACHE/.last-refresh" 2>/dev/null)"
wiki_note="Family Wiki disabled"
if ! is_disabled "$WIKI_ENABLED"; then
  wiki_note="$(stale_note 'Family Wiki' "$CACHE/wiki.txt")"
fi
honcho_note="Honcho disabled"
if ! is_disabled "$HONCHO_ENABLED" && [ "$PROFILE" != "max-perf" ]; then
  honcho_note="$(stale_note 'Honcho' "$CACHE/honcho.txt")"
fi

resume_block=""
if [ -n "${resume:-}" ]; then
  resume_block="▶ 직전 세션에서 이어서:
${resume}
"
fi

operational_note="Operational facts are mutable — live-check the node before asserting or changing anything."
wiki_block=""
if ! is_disabled "$WIKI_ENABLED"; then
  operational_note="Operational facts are mutable — live-check the node and verify Wiki source text before asserting or changing anything."
  wiki_block="
## Family Wiki (cache prefetch — candidates; verify with wiki-agent load before operational claims)
${wiki:-(no wiki cache yet — will populate after first background refresh)}
"
fi

ctx="# ${node_label} session memory (auto-injected: $EVENT)

${resume_block}${operational_note}
Memory profile: ${PROFILE}; last refresh: ${stamp:-never}; ${wiki_note}; ${honcho_note}. A background refresh runs each session for the next one.

## Built-in MEMORY + USER
${mem:-(memory files unavailable)}

## Local hot memory (task-conditioned cache search)
${local_hot:-(local hot memory disabled or no hits)}
${wiki_block}
## Honcho working memory — ${USER_LABEL}
${honcho:-(Honcho disabled or no Honcho cache yet)}"

ctx="$(printf '%s' "$ctx" | limit_bytes "$MAX_TOTAL")"

jq -n --arg ctx "$ctx" --arg event "$EVENT" \
  '{hookSpecificOutput:{hookEventName:$event,additionalContext:$ctx}}'

# Fire-and-forget: refresh caches for the NEXT session, fully detached so startup never waits.
# CCC_MEMORY_NO_REFRESH=1 suppresses it — for hermetic tests (the detached refresh
# rebuilds the index / consolidates facts out-of-band, which otherwise mutates
# shared state mid-test) and for any caller that wants a strictly read-only inject.
run_refresh_memory_bg() { bash "$HOOKDIR/refresh-memory.sh"; }
LOAD_MEMORY_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
SPAWN_DETACHED_LIB="${CCC_SPAWN_DETACHED_LIB:-$HOOKDIR/lib/spawn-detached.sh}"
if [ ! -r "$SPAWN_DETACHED_LIB" ] && [ -n "$LOAD_MEMORY_SELF_DIR" ]; then
  SPAWN_DETACHED_LIB="$LOAD_MEMORY_SELF_DIR/lib/spawn-detached.sh"
fi
case "${CCC_MEMORY_NO_REFRESH:-0}" in
  1|true|TRUE|on|ON|yes|YES) : ;;
  *)
    if [ -r "$SPAWN_DETACHED_LIB" ]; then
      # shellcheck source=claude/hooks/lib/spawn-detached.sh
      . "$SPAWN_DETACHED_LIB"
      spawn_detached "$HOOKDIR/refresh-memory.sh" "" run_refresh_memory_bg || true
    fi
    ;;
esac
