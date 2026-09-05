#!/usr/bin/env bash
# Background refresh of the Family Wiki memory cache.
# Run detached from the SessionStart hook so startup never blocks on slow LLM calls.
# Single-flight via flock; each source fail-open; caches updated atomically only on success.
set -uo pipefail

[ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ] && exit 0

STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
CACHE="${CCC_MEMORY_CACHE_DIR:-${HOME:-/root}/.claude/hooks/cache}"
HOOKDIR="${CCC_HOOK_DIR:-${HOME:-/root}/.claude/hooks}"
WIKI="${CCC_WIKI_AGENT_BIN:-${HOME:-/root}/.wiki-agent/bin/wiki-agent}"
WIKI_TIMEOUT="${CCC_WIKI_TIMEOUT_SEC:-60}"
WIKI_ENABLED="${CCC_WIKI_MEMORY_ENABLED:-1}"
WIKI_FORCE_REFRESH="${CCC_WIKI_FORCE_REFRESH:-0}"
ISOLATION_PROFILE="${CCC_NODE_ISOLATION_PROFILE:-fleet}"
[ "$ISOLATION_PROFILE" = "external" ] && WIKI_ENABLED=0
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
  [ "$MEMORY_AUDIENCE" = "private" ] || WIKI_ENABLED=0
fi
umask 077
mkdir -p "$CACHE" "$STATE_DIR"
# find_memory_tool comes from lib/hook-common.sh.
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
# bash 5 EPOCHREALTIME (no fork); the python3 fallback only runs where the
# variable is unset (bash 4) — it used to cost two interpreter starts per
# source refresh (#1484). Locale-safe: the separator may be `.` or `,`.
now_ms() {
  local t
  if [ -n "${EPOCHREALTIME:-}" ]; then
    t="${EPOCHREALTIME//[.,]/}"
    printf '%s\n' "$(( t / 1000 ))"
  else
    python3 -c 'import time; print(int(time.time()*1000))'
  fi
}
bytes_for() { [ -f "$1" ] && wc -c < "$1" | tr -d '[:space:]' || printf '0'; }

# Non-blocking single-flight lock: if a refresh is already running, exit.
# util-linux flock is absent on some nodes (Termux); a bare `flock` there
# exits 127 and this hook never refreshed, pinning the cache stale (#1480).
# Without it an atomic mkdir lock stands in: released on exit, reclaimed when
# a dead holder left it behind (a kernel flock releases on death; a dir does not).
FLOCK_BIN="${CCC_FLOCK_CLI:-$(command -v flock || true)}"
if [ -n "$FLOCK_BIN" ] && [ -f "$FLOCK_BIN" ] && [ -x "$FLOCK_BIN" ]; then
  exec 9>"$CACHE/.refresh.lock"
  "$FLOCK_BIN" -n 9 || exit 0
else
  LOCK_DIR="$CACHE/.refresh.lock.d"
  if [ -d "$LOCK_DIR" ] && [ -n "$(find "$LOCK_DIR" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
  mkdir "$LOCK_DIR" 2>/dev/null || exit 0
  trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT
  printf 'refresh lock: flock unavailable; using mkdir fallback\n'
fi

query_from_state() {
  local include_prompt="${1:-1}"
  if [ -n "${PREFETCH_QUERY:-}" ]; then printf '%s' "$PREFETCH_QUERY"; return 0; fi
  local query_tool node cwd task
  query_tool="$(find_memory_tool ccc-memory-query.sh 2>/dev/null || true)"
  if [ -n "$query_tool" ]; then
    CCC_MEMORY_QUERY_INCLUDE_PROMPT="$include_prompt" \
      CCC_WORKTREE="${CCC_WORKTREE:-$(cat "$STATE_DIR/cwd.txt" 2>/dev/null || pwd 2>/dev/null || true)}" \
      "$query_tool" --mode remote 2>/dev/null && return 0
  fi
  node="${CCC_NODE:-$(cat "$STATE_DIR/node.txt" 2>/dev/null || hostname -s 2>/dev/null || printf 'ccc-node')}"
  cwd="$(cat "$STATE_DIR/cwd.txt" 2>/dev/null || pwd 2>/dev/null || printf '')"
  task="$(cat "$STATE_DIR/current-task.txt" 2>/dev/null || printf '')"
  printf '%s' "node ${node}; cwd ${cwd}; task ${task}; Seoyoon ops priorities and current node operating memory"
}

record_status() { # <name> <status> <duration_ms> <bytes> <error> [query] [config_hash]
  local name="$1" status="$2" duration="$3" bytes="$4" error="$5"
  local query="${6:-}" config_hash="${7:-}" qhash max_age
  qhash="$(printf '%s' "$query" | sha256sum 2>/dev/null | cut -d' ' -f1)"
  case "$name" in
    wiki) max_age="${CCC_WIKI_CACHE_MAX_AGE_SEC:-${CCC_MEMORY_CACHE_TTL_SEC:-21600}}" ;;
    *) max_age="${CCC_LOCAL_MEMORY_CACHE_MAX_AGE_SEC:-${CCC_MEMORY_CACHE_TTL_SEC:-21600}}" ;;
  esac
  jq -n --arg source "$name" --arg status "$status" --arg refreshed_at "$(now_iso)" \
    --arg error "$error" --arg query_hash "$qhash" --argjson duration_ms "${duration:-0}" \
    --arg config_hash "$config_hash" --argjson bytes "${bytes:-0}" \
    --argjson max_age_sec "${max_age:-0}" \
    '({source:$source,status:$status,refreshed_at:$refreshed_at,duration_ms:$duration_ms,bytes:$bytes,error:$error,error_class:(if $error=="" then "" else ($status) end),query_hash:$query_hash,max_age_sec:$max_age_sec,stale:false}
      + (if $config_hash == "" then {} else {config_hash:$config_hash} end))' \
    > "$CACHE/.${name}.status.json.tmp.$$" 2>/dev/null
  # Atomic like meta.json below: wiki_cache_is_fresh and the doctor read this
  # file while a refresh may be rewriting it; a torn/empty read must not happen.
  if ! mv -f "$CACHE/.${name}.status.json.tmp.$$" "$CACHE/.${name}.status.json" 2>/dev/null; then
    rm -f "$CACHE/.${name}.status.json.tmp.$$"
    return 0
  fi
  cp "$CACHE/.${name}.status.json" "$CACHE/${name}.meta.json" 2>/dev/null || true
}

wiki_cache_is_fresh() { # <query_hash>
  local query_hash="$1"
  local status_file="$CACHE/.wiki.status.json"
  local max_age="${CCC_WIKI_CACHE_MAX_AGE_SEC:-${CCC_MEMORY_CACHE_TTL_SEC:-21600}}"
  local has_cache=0
  is_disabled "$WIKI_FORCE_REFRESH" || return 1
  case "$max_age" in ''|*[!0-9]*) return 1 ;; esac
  [ "$max_age" -gt 0 ] || return 1
  [ -f "$status_file" ] || return 1
  [ -s "$CACHE/wiki.txt" ] && has_cache=1
  jq -e --arg query_hash "$query_hash" --arg has_cache "$has_cache" \
    --argjson now "$(date -u +%s)" --argjson max_age "$max_age" '
      # ok|empty both mean the wiki agent WAS reached and answered (#781): the
      # declared TTL was recorded but never enforced, so every SessionStart
      # paid a full prefetch to rebuild a cache it considered fresh for 6h.
      # ok additionally requires the cache file to still exist so an
      # externally cleared cache repopulates immediately.
      (((.status == "ok") and ($has_cache == "1")) or (.status == "empty"))
      and (.query_hash == $query_hash)
      and ((try (.refreshed_at | fromdateiso8601) catch 0) as $refreshed
        | $refreshed > 0
        and $now >= $refreshed
        and (($now - $refreshed) < $max_age))
    ' "$status_file" >/dev/null 2>&1
}

refresh_wiki() {
  local start end duration q q_stable query_hash tmp status err bytes
  start="$(now_ms)"
  q=""; q_stable=""
  tmp="$CACHE/wiki.txt.tmp.$$"
  status="ok"; err=""; bytes=0
  if is_disabled "$WIKI_ENABLED"; then
    status="disabled"; err="Family Wiki read path disabled"
  else
    # Freshness key excludes the per-session prompt (the #781 honcho decision):
    # wiki.txt is consumed as a shared cache across sessions, so a changing
    # prompt must not defeat the TTL and re-pay a prefetch every SessionStart.
    q_stable="$(query_from_state 0)"
    query_hash="$(printf '%s' "$q_stable" | sha256sum 2>/dev/null | cut -d' ' -f1)"
    if wiki_cache_is_fresh "$query_hash"; then
      printf 'wiki refresh skipped reason=fresh max_age_sec=%s\n' \
        "${CCC_WIKI_CACHE_MAX_AGE_SEC:-${CCC_MEMORY_CACHE_TTL_SEC:-21600}}" >&2
      return 0
    fi
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
  # The stored query_hash is the freshness key, so it records the stable query
  # (prompt excluded) that wiki_cache_is_fresh compares against.
  record_status wiki "$status" "$duration" "$bytes" "$err" "$q_stable"
}

refresh_wiki & wiki_pid=$!
wait "$wiki_pid" || true

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
    timeout 30 "$index_script" update >/dev/null 2>&1 || true
fi

# Merge per-source statuses into one meta document.
jq -s '{generated_at:(now|todate), sources: map({(.source): del(.source)}) | add}' \
  "$CACHE/.wiki.status.json" \
  "$CACHE/.fact_consolidate.status.json" "$CACHE/.local_index.status.json" \
  > "$CACHE/meta.json.tmp" 2>/dev/null && mv "$CACHE/meta.json.tmp" "$CACHE/meta.json"

now_iso > "$CACHE/.last-refresh"
