#!/usr/bin/env bash
# Background refresh of Family Wiki + Honcho memory caches.
# Run detached from the SessionStart hook so startup never blocks on slow LLM calls.
# Single-flight via flock; each source fail-open; caches updated atomically only on success.
set -uo pipefail

[ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ] && exit 0

STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
CACHE="${CCC_MEMORY_CACHE_DIR:-${HOME:-/root}/.claude/hooks/cache}"
HOOKDIR="${CCC_HOOK_DIR:-${HOME:-/root}/.claude/hooks}"
WIKI="${CCC_WIKI_AGENT_BIN:-${HOME:-/root}/.wiki-agent/bin/wiki-agent}"
HONCHO_CFG="${CCC_HONCHO_CFG:-${CCC_HERMES_DIR:-${HOME:-/root}/.hermes}/honcho.json}"
WIKI_TIMEOUT="${CCC_WIKI_TIMEOUT_SEC:-60}"
HONCHO_TIMEOUT="${CCC_HONCHO_TIMEOUT_SEC:-60}"
HONCHO_ENABLED="${CCC_HONCHO_MEMORY_ENABLED:-1}"
WIKI_ENABLED="${CCC_WIKI_MEMORY_ENABLED:-1}"
ISOLATION_PROFILE="${CCC_NODE_ISOLATION_PROFILE:-fleet}"
[ "$ISOLATION_PROFILE" = "external" ] && WIKI_ENABLED=0
PROFILE="${CCC_MEMORY_PROFILE:-honcho}"
INDEX_DB="${CCC_MEMORY_INDEX_DB:-$STATE_DIR/memory-index.sqlite}"
FACTS_FILE="${CCC_MEMORY_FACTS_FILE:-$STATE_DIR/memory-facts.jsonl}"

REFRESH_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || REFRESH_LIB_DIR="$HOOKDIR"
# shellcheck source=claude/hooks/lib/hook-common.sh
. "$REFRESH_LIB_DIR/lib/hook-common.sh" || exit 0
# shellcheck source=claude/hooks/lib/memory-common.sh
. "$REFRESH_LIB_DIR/lib/memory-common.sh" || exit 0
if ! is_disabled "$AUDIENCE_SCOPED"; then
  memory_scope_core_valid \
    && [ "$INDEX_DB" = "$AUDIENCE_ROOT/$MEMORY_SCOPE/state/memory-index.sqlite" ] \
    && [ "$FACTS_FILE" = "$AUDIENCE_ROOT/$MEMORY_SCOPE/state/memory-facts.jsonl" ] \
    && [ "$SHARED_FACTS_FILE" = "$AUDIENCE_ROOT/shared/state/memory-facts.jsonl" ] \
    || exit 0
  WIKI_ENABLED=0
  if ! honcho_scope_valid; then
    HONCHO_ENABLED=0
  fi
fi
umask 077
mkdir -p "$CACHE" "$STATE_DIR"
# find_memory_tool comes from lib/hook-common.sh.
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
now_ms() { python3 -c 'import time; print(int(time.time()*1000))'; }
bytes_for() { [ -f "$1" ] && wc -c < "$1" | tr -d '[:space:]' || printf '0'; }

# Non-blocking single-flight lock: if a refresh is already running, exit.
exec 9>"$CACHE/.refresh.lock"
flock -n 9 || exit 0

query_from_state() {
  if [ -n "${PREFETCH_QUERY:-}" ]; then printf '%s' "$PREFETCH_QUERY"; return 0; fi
  local query_tool node cwd task
  query_tool="$(find_memory_tool ccc-memory-query.sh 2>/dev/null || true)"
  if [ -n "$query_tool" ]; then
    CCC_WORKTREE="${CCC_WORKTREE:-$(cat "$STATE_DIR/cwd.txt" 2>/dev/null || pwd 2>/dev/null || true)}" "$query_tool" --mode remote 2>/dev/null && return 0
  fi
  node="${CCC_NODE:-$(cat "$STATE_DIR/node.txt" 2>/dev/null || hostname -s 2>/dev/null || printf 'ccc-node')}"
  cwd="$(cat "$STATE_DIR/cwd.txt" 2>/dev/null || pwd 2>/dev/null || printf '')"
  task="$(cat "$STATE_DIR/current-task.txt" 2>/dev/null || printf '')"
  printf '%s' "node ${node}; cwd ${cwd}; task ${task}; Seoyoon ops priorities and current node operating memory"
}

record_status() { # <name> <status> <duration_ms> <bytes> <error> [query]
  local name="$1" status="$2" duration="$3" bytes="$4" error="$5" query="${6:-}" qhash max_age
  qhash="$(printf '%s' "$query" | sha256sum 2>/dev/null | cut -d' ' -f1)"
  case "$name" in
    wiki) max_age="${CCC_WIKI_CACHE_MAX_AGE_SEC:-${CCC_MEMORY_CACHE_TTL_SEC:-21600}}" ;;
    honcho) max_age="${CCC_HONCHO_CACHE_MAX_AGE_SEC:-${CCC_MEMORY_CACHE_TTL_SEC:-21600}}" ;;
    *) max_age="${CCC_LOCAL_MEMORY_CACHE_MAX_AGE_SEC:-${CCC_MEMORY_CACHE_TTL_SEC:-21600}}" ;;
  esac
  jq -n --arg source "$name" --arg status "$status" --arg refreshed_at "$(now_iso)" \
    --arg error "$error" --arg query_hash "$qhash" --argjson duration_ms "${duration:-0}" \
    --argjson bytes "${bytes:-0}" --argjson max_age_sec "${max_age:-0}" \
    '{source:$source,status:$status,refreshed_at:$refreshed_at,duration_ms:$duration_ms,bytes:$bytes,error:$error,error_class:(if $error=="" then "" else ($status) end),query_hash:$query_hash,max_age_sec:$max_age_sec,stale:false}' \
    > "$CACHE/.${name}.status.json"
  cp "$CACHE/.${name}.status.json" "$CACHE/${name}.meta.json" 2>/dev/null || true
}

refresh_wiki() {
  local start end duration q tmp status err bytes
  start="$(now_ms)"
  q=""
  tmp="$CACHE/wiki.txt.tmp.$$"
  status="ok"; err=""; bytes=0
  if is_disabled "$WIKI_ENABLED"; then
    status="disabled"; err="Family Wiki read path disabled"
  else
    q="$(query_from_state)"
    if [ ! -x "$WIKI" ]; then
      status="missing"; err="wiki-agent not executable"
    elif ! timeout "$WIKI_TIMEOUT" "$WIKI" --no-notify prefetch "$q" > "$tmp" 2>"$tmp.err"; then
      status="error"; err="$(tr '\n' ' ' < "$tmp.err" | cut -c1-240)"
    elif [ ! -s "$tmp" ]; then
      status="empty"; err="empty wiki prefetch"
    else
      mv "$tmp" "$CACHE/wiki.txt"
    fi
    bytes="$(bytes_for "$CACHE/wiki.txt")"
  fi
  rm -f "$tmp" "$tmp.err"
  end="$(now_ms)"; duration="$((end - start))"
  record_status wiki "$status" "$duration" "$bytes" "$err" "$q"
}

honcho_chat() { # <base> <workspace> <peer> <target> <token> <reasoning> <query> <output> <error>
  local honcho="$1" workspace="$2" peer="$3" target="$4" token="$5"
  local rl="$6" query="$7" output="$8" error="$9" workspace_path peer_path
  local -a auth_args=()
  workspace_path="$(jq -rn --arg value "$workspace" '$value|@uri')"
  peer_path="$(jq -rn --arg value "$peer" '$value|@uri')"
  if [ -n "$token" ]; then
    auth_args=(-H "Authorization: Bearer $token")
  fi
  timeout "$HONCHO_TIMEOUT" curl -sS -X POST \
    "$honcho/v3/workspaces/$workspace_path/peers/$peer_path/chat" \
    -H 'Content-Type: application/json' \
    "${auth_args[@]}" \
    -d "$(jq -n --arg query "$query" --arg target "$target" --arg rl "$rl" \
      '{query:$query,target:$target,reasoning_level:$rl}')" \
    2>"$error" | jq -r '.content // empty' > "$output" 2>>"$error"
}

refresh_honcho() {
  local start end duration honcho ws peer target token tmp status err query rl
  local private_tmp shared_tmp legacy_tmp
  start="$(now_ms)"
  tmp="$CACHE/honcho.txt.tmp.$$"
  status="ok"; err=""; query=""
  if is_disabled "$HONCHO_ENABLED" || [ "$PROFILE" = "max-perf" ]; then
    status="disabled"; err="Honcho read path disabled"
  elif [ ! -f "$HONCHO_CFG" ]; then
    status="missing"; err="honcho config missing"
  else
    # Config may use the nested `.hosts.hermes.*` schema (aiPeer/peerName/workspace/
    # apiKey) instead of the legacy top-level keys; read top-level first, fall back to
    # the nested block so both layouts work.
    honcho="$(jq -r 'def nz(x): x | select(. != null and . != ""); nz(.baseUrl) // nz(.hosts.hermes.baseUrl) // empty' "$HONCHO_CFG" 2>/dev/null)"
    ws="$(jq -r 'def nz(x): x | select(. != null and . != ""); nz(.workspace) // nz(.hosts.hermes.workspace) // "seoyoon-family"' "$HONCHO_CFG" 2>/dev/null)"
    peer="$(jq -r 'def nz(x): x | select(. != null and . != ""); nz(.peerName) // nz(.hosts.hermes.peerName) // empty' "$HONCHO_CFG" 2>/dev/null)"
    target="$(jq -r 'def nz(x): x | select(. != null and . != ""); nz(.target) // nz(.hosts.hermes.peerName) // "seo-jin-on"' "$HONCHO_CFG" 2>/dev/null)"
    token="$(jq -r 'def nz(x): x | select(. != null and . != ""); nz(.authToken) // nz(.apiKey) // nz(.hosts.hermes.apiKey) // empty' "$HONCHO_CFG" 2>/dev/null)"
    rl="$(jq -r 'def nz(x): x | select(. != null and . != ""); nz(.reasoningLevel) // nz(.hosts.hermes.dialecticReasoningLevel) // "low"' "$HONCHO_CFG" 2>/dev/null)"
    query="For the current ccc-node task, summarize only directly relevant user preferences, operating constraints, and current priorities. Avoid repeating generic facts."
    if [ -z "$honcho" ] || [ -z "$peer" ]; then
      status="missing"; err="honcho baseUrl or peerName missing"
    else
      if ! is_disabled "$AUDIENCE_SCOPED"; then
        private_tmp="$tmp.private"
        shared_tmp="$tmp.shared"
        legacy_tmp="$tmp.legacy"
        if [ "$MEMORY_AUDIENCE" = "shared" ]; then
          if honcho_chat "$honcho" "$ws--ccc-$HONCHO_SHARED_WORKSPACE_SCOPE" \
            "$peer" "$target" "$token" "$rl" "$query" "$shared_tmp" "$tmp.err"; then
            {
              printf '### Honcho shared audience\n'
              cat "$shared_tmp"
            } > "$tmp"
            mv "$tmp" "$CACHE/honcho.txt"
          else
            status="error"; err="$(tr '\n' ' ' < "$tmp.err" | cut -c1-240)"
          fi
        elif honcho_chat "$honcho" "$ws--ccc-$HONCHO_WORKSPACE_SCOPE" \
          "$peer" "$target" "$token" "$rl" "$query" "$private_tmp" "$tmp.err" \
          && honcho_chat "$honcho" "$ws--ccc-$HONCHO_SHARED_WORKSPACE_SCOPE" \
          "$peer" "$target" "$token" "$rl" "$query" "$shared_tmp" "$tmp.err" \
          && honcho_chat "$honcho" "$ws" "$peer" "$target" "$token" "$rl" \
          "$query" "$legacy_tmp" "$tmp.err"; then
          {
            printf '### Honcho private audience\n'
            cat "$private_tmp"
            printf '\n\n### Honcho shared audience\n'
            cat "$shared_tmp"
            printf '\n\n### Honcho private-only legacy\n'
            cat "$legacy_tmp"
          } > "$tmp"
          mv "$tmp" "$CACHE/honcho.txt"
        else
          status="error"; err="$(tr '\n' ' ' < "$tmp.err" | cut -c1-240)"
        fi
      elif ! honcho_chat "$honcho" "$ws" "$peer" "$target" "$token" "$rl" \
        "$query" "$tmp" "$tmp.err"; then
        status="error"; err="$(tr '\n' ' ' < "$tmp.err" | cut -c1-240)"
      elif [ ! -s "$tmp" ]; then
        status="empty"; err="empty Honcho response"
      else
        mv "$tmp" "$CACHE/honcho.txt"
      fi
    fi
  fi
  rm -f "$tmp" "$tmp.err" "$tmp.private" "$tmp.shared" "$tmp.legacy"
  end="$(now_ms)"; duration="$((end - start))"
  record_status honcho "$status" "$duration" "$(bytes_for "$CACHE/honcho.txt")" "$err" "$query"
}

refresh_wiki & wiki_pid=$!
refresh_honcho & honcho_pid=$!
wait "$wiki_pid" || true
wait "$honcho_pid" || true

# Consolidate near-duplicate distilled facts BEFORE indexing, so superseded
# copies drop out of this same refresh. Best-effort; never blocks startup.
consolidate_status="skipped"; consolidate_error=""
consolidate_script="$(find_memory_tool ccc-memory-consolidate.sh 2>/dev/null || true)"
if [ -n "$consolidate_script" ]; then
  if out="$(timeout 30 "$consolidate_script" 2>&1)"; then
    consolidate_status="ok"
  else
    consolidate_status="error"; consolidate_error="$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-240)"
  fi
fi
record_status fact_consolidate "$consolidate_status" 0 0 "$consolidate_error"

# Update local hot-memory index opportunistically. It is best-effort and never blocks hook startup.
index_status="skipped"; index_error=""
index_script="$(find_memory_tool ccc-memory-index.sh 2>/dev/null || true)"
if [ -n "$index_script" ]; then
  if out="$(timeout 30 "$index_script" update 2>&1)"; then
    index_status="ok"
  else
    index_status="error"; index_error="$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-240)"
  fi
fi
record_status local_index "$index_status" 0 0 "$index_error"

# A private DM can recall shared facts too. Keep the shared index warm without
# ever importing private paths into it; every path override stays inside the
# public audience root and both remote memory sources are disabled.
if ! is_disabled "$AUDIENCE_SCOPED" \
  && [ "$MEMORY_AUDIENCE" = "private" ] \
  && [ -n "$index_script" ] \
  && [ -n "$SHARED_STATE_DIR" ] \
  && [ "$SHARED_STATE_DIR" != "$STATE_DIR" ]; then
  mkdir -p "$SHARED_STATE_DIR" "$SHARED_CACHE_DIR" "$SHARED_MEMDIR" 2>/dev/null || true
  CCC_STATE_DIR="$SHARED_STATE_DIR" \
  CCC_MEMORY_INDEX_DB="$SHARED_STATE_DIR/memory-index.sqlite" \
  CCC_MEMORY_CACHE_DIR="$SHARED_CACHE_DIR" \
  CCC_MEMORY_DIR="$SHARED_MEMDIR" \
  CCC_MEMORY_FACTS_FILE="${SHARED_FACTS_FILE:-$SHARED_STATE_DIR/memory-facts.jsonl}" \
  CCC_WIKI_MEMORY_ENABLED=0 \
  CCC_HONCHO_MEMORY_ENABLED=0 \
    timeout 30 "$index_script" update >/dev/null 2>&1 || true
fi

# Merge per-source statuses into one meta document.
jq -s '{generated_at:(now|todate), sources: map({(.source): del(.source)}) | add}' \
  "$CACHE/.wiki.status.json" "$CACHE/.honcho.status.json" \
  "$CACHE/.fact_consolidate.status.json" "$CACHE/.local_index.status.json" \
  > "$CACHE/meta.json.tmp" 2>/dev/null && mv "$CACHE/meta.json.tmp" "$CACHE/meta.json"

now_iso > "$CACHE/.last-refresh"
