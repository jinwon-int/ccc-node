#!/usr/bin/env bash
# ccc-memory-query.sh — build task-aware local/remote memory queries without network.
# Remote mode is redacted more aggressively before use with Wiki/Honcho refresh.
set -uo pipefail

MODE="local"
OUTPUT="text"
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) [ "$#" -ge 2 ] || { echo "--mode requires a value" >&2; exit 2; }; MODE="${2:-local}"; shift 2 ;;
    --local) MODE="local"; shift ;;
    --remote) MODE="remote"; shift ;;
    --json) OUTPUT="json"; shift ;;
    --help|-h)
      echo "usage: $0 [--mode local|remote] [--json]"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
case "$MODE" in local|remote) ;; *) echo "invalid mode: $MODE" >&2; exit 2 ;; esac

STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
MAX_BYTES="${CCC_MEMORY_QUERY_MAX_BYTES:-}"
if [ -z "$MAX_BYTES" ]; then
  if [ "$MODE" = "remote" ]; then MAX_BYTES="${CCC_MEMORY_REMOTE_QUERY_MAX_BYTES:-900}"; else MAX_BYTES="${CCC_MEMORY_LOCAL_QUERY_MAX_BYTES:-1400}"; fi
fi

# Bash builtins instead of one sed fork per file (#1484): this runs on the
# SessionStart critical path (load-memory.sh) and again from refresh-memory.sh.
read_file_trim() { # first 40 lines (was: sed -n '1,40p')
  local -a lines=()
  [ -f "$1" ] || return 0
  mapfile -t -n 40 lines 2>/dev/null < "$1" || return 0
  [ "${#lines[@]}" -gt 0 ] && printf '%s\n' "${lines[@]}"
  return 0
}
first_line_file() { # first line (was: sed -n '1p')
  local line=""
  [ -f "$1" ] || return 0
  IFS= read -r line 2>/dev/null < "$1" || [ -n "$line" ] || return 0
  printf '%s\n' "$line"
}

node_val="${CCC_NODE:-$(first_line_file "$STATE_DIR/node.txt")}"; [ -n "$node_val" ] || node_val="$(hostname -s 2>/dev/null || printf 'ccc-node')"
cwd_val="${CCC_WORKTREE:-$(first_line_file "$STATE_DIR/cwd.txt")}"; [ -n "$cwd_val" ] || cwd_val="$(pwd 2>/dev/null || printf '')"
task_val="${CCC_MEMORY_QUERY:-$(read_file_trim "$STATE_DIR/current-task.txt")}"; [ -n "$task_val" ] || task_val="current task"
prompt_val="${CCC_CURRENT_PROMPT:-$(read_file_trim "$STATE_DIR/current-prompt.txt")}" 
case "${CCC_MEMORY_QUERY_INCLUDE_PROMPT:-1}" in
  0|false|FALSE|off|OFF|no|NO) prompt_val="" ;;
esac
extra_val="${CCC_MEMORY_QUERY_EXTRA:-}"
issue_val="${CCC_TASK_ISSUE_URL:-${GITHUB_ISSUE_URL:-}}"
pr_val="${CCC_TASK_PR_URL:-${GITHUB_PR_URL:-}}"

git_branch_val=""
git_paths_val=""
if [ -n "$cwd_val" ] && [ -d "$cwd_val/.git" ]; then
  # Git state with the same 5s TSV cache statusline.sh keeps (#1484): the key
  # is the cwd with non-alphanumerics folded to `_` (last 200 chars), the TTL
  # is EPOCHSECONDS-based. Two files under the shared cache dir:
  #   <key>.tsv        statusline's  ts<TAB>branch<TAB>dirty(0|1)
  #   <key>.query.tsv  this script's ts<TAB>branch<TAB>changed-paths
  # A fresh statusline row with dirty=0 already proves there are no changed
  # tracked paths, so it answers this script's question without a git fork; a
  # dirty=1 row does not (it may be untracked files only), so that case falls
  # through to this script's own row, then to git. Only the query row is
  # written here: the porcelain output this script needs (--untracked-files=no)
  # cannot produce statusline's dirty flag, so its row is never guessed at.
  git_cache_dir="${CCC_GIT_STATUS_CACHE_DIR:-${HOME:-/root}/.claude/cache/git-status}"
  git_cache_key="${cwd_val//[!a-zA-Z0-9]/_}"
  [ "${#git_cache_key}" -gt 200 ] && git_cache_key="${git_cache_key:${#git_cache_key}-200}"
  git_cache_ttl="${CCC_GIT_STATUS_CACHE_TTL:-5}"
  case "$git_cache_ttl" in ''|*[!0-9]*) git_cache_ttl=5 ;; esac
  git_now="${EPOCHSECONDS:-$(date +%s)}"
  git_hit=0
  git_cache_fresh() { # <ts>
    case "${1:-}" in ''|*[!0-9]*) return 1 ;; esac
    [ "$((git_now - $1))" -lt "$git_cache_ttl" ]
  }
  if [ -r "$git_cache_dir/$git_cache_key.query.tsv" ]; then
    c_ts=""; c_br=""; c_paths=""
    IFS=$'\t' read -r c_ts c_br c_paths 2>/dev/null < "$git_cache_dir/$git_cache_key.query.tsv" || true
    if git_cache_fresh "$c_ts"; then
      git_branch_val="$c_br"; git_paths_val="$c_paths"; git_hit=1
    fi
  fi
  if [ "$git_hit" = 0 ] && [ -r "$git_cache_dir/$git_cache_key.tsv" ]; then
    c_ts=""; c_br=""; c_dirty=""
    IFS=$'\t' read -r c_ts c_br c_dirty 2>/dev/null < "$git_cache_dir/$git_cache_key.tsv" || true
    if git_cache_fresh "$c_ts" && [ "$c_dirty" = "0" ]; then
      git_branch_val="$c_br"; git_paths_val=""; git_hit=1
    fi
  fi
  if [ "$git_hit" = 0 ]; then
    git_branch_val="$(git -C "$cwd_val" branch --show-current 2>/dev/null || true)"
    # Was: status --short | sed 's/^...//' | sed -n '1,20p' | tr '\n' ' ' | cut -c1-400
    # — one builtin pass now (first 20 rows, strip the XY+space prefix, join
    # with spaces, cap at 400 bytes like GNU cut -c).
    git_status_lines=()
    mapfile -t -n 20 git_status_lines < <(git -C "$cwd_val" status --short --untracked-files=no 2>/dev/null)
    git_paths_val=""
    for git_status_line in ${git_status_lines[@]+"${git_status_lines[@]}"}; do
      git_paths_val+="${git_status_line:3} "
    done
    if [ -n "$git_paths_val" ]; then
      git_paths_val="$(LC_ALL=C; printf '%s' "${git_paths_val:0:400}")"
    fi
    # Best-effort, atomic (tmp + mv): the query must never fail on cache I/O.
    if mkdir -p "$git_cache_dir" 2>/dev/null; then
      git_cache_tmp="$git_cache_dir/$git_cache_key.query.tsv.$$"
      if printf '%s\t%s\t%s\n' "$git_now" "$git_branch_val" "$git_paths_val" > "$git_cache_tmp" 2>/dev/null; then
        mv -f "$git_cache_tmp" "$git_cache_dir/$git_cache_key.query.tsv" 2>/dev/null || rm -f "$git_cache_tmp" 2>/dev/null
      else
        rm -f "$git_cache_tmp" 2>/dev/null
      fi
    fi
  fi
fi

export CCC_QUERY_NODE="$node_val" CCC_QUERY_CWD="$cwd_val" CCC_QUERY_TASK="$task_val" \
  CCC_QUERY_PROMPT="$prompt_val" CCC_QUERY_EXTRA="$extra_val" CCC_QUERY_ISSUE="$issue_val" \
  CCC_QUERY_PR="$pr_val" CCC_QUERY_GIT_BRANCH="$git_branch_val" CCC_QUERY_GIT_PATHS="$git_paths_val"

python3 - "$MODE" "$MAX_BYTES" "$OUTPUT" <<'PY'
import json, os, re, sys
mode, max_bytes, output = sys.argv[1], int(sys.argv[2]), sys.argv[3]
fields = {
    "node": os.environ.get("CCC_QUERY_NODE", ""),
    "cwd": os.environ.get("CCC_QUERY_CWD", ""),
    "task": os.environ.get("CCC_QUERY_TASK", ""),
    "prompt": os.environ.get("CCC_QUERY_PROMPT", ""),
    "issue": os.environ.get("CCC_QUERY_ISSUE", ""),
    "pr": os.environ.get("CCC_QUERY_PR", ""),
    "git_branch": os.environ.get("CCC_QUERY_GIT_BRANCH", ""),
    "git_changed_paths": os.environ.get("CCC_QUERY_GIT_PATHS", ""),
    "extra": os.environ.get("CCC_QUERY_EXTRA", ""),
}
SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+|bearer\s+)[^\s,'\"`]+"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|access[_-]?key|private[_-]?key|cookie|session|signature)\b\s*[:=]\s*[^\s,'\"`]+"),
    re.compile(r"(?i)([?&](?:access_token|token|api_key|apikey|key|secret|password|sig|signature)=)[^&\s]+"),
    re.compile(r"gho_[A-Za-z0-9_]+"),
]
def redact(text: str) -> str:
    for pat in SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", text)
    lines = []
    for line in text.splitlines():
        upper = line.upper()
        if mode == "remote" and any(k in upper for k in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PRIVATE_KEY", "COOKIE", "AUTHORIZATION")):
            lines.append("[REDACTED_SENSITIVE_LINE]")
        else:
            lines.append(line)
    return " ".join(" ".join(lines).split())
parts = []
for label in ("task", "prompt", "node", "cwd", "issue", "pr", "git_branch", "git_changed_paths", "extra"):
    val = redact(fields[label])
    if not val:
        continue
    if label == "cwd" and mode == "remote":
        val = val.split("/")[-1] or val
    parts.append(f"{label}: {val}")
query = "; ".join(parts) or "current task"
raw = query.encode("utf-8")
truncated = False
if max_bytes > 0 and len(raw) > max_bytes:
    query = raw[:max_bytes].decode("utf-8", errors="ignore") + " … [query truncated]"
    truncated = True
if output == "json":
    print(json.dumps({"mode": mode, "query": query, "bytes": len(query.encode()), "truncated": truncated}, ensure_ascii=False))
else:
    print(query)
PY
