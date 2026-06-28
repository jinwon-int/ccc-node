#!/usr/bin/env bash
# ccc-memory-query.sh — build task-aware local/remote memory queries without network.
# Remote mode is redacted more aggressively before use with Wiki/Honcho refresh.
set -uo pipefail

MODE="local"
OUTPUT="text"
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="${2:-local}"; shift 2 ;;
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

read_file_trim() { [ -f "$1" ] && sed -n '1,40p' "$1" 2>/dev/null || true; }
first_line_file() { [ -f "$1" ] && sed -n '1p' "$1" 2>/dev/null || true; }

node_val="${CCC_NODE:-$(first_line_file "$STATE_DIR/node.txt")}"; [ -n "$node_val" ] || node_val="$(hostname -s 2>/dev/null || printf 'ccc-node')"
cwd_val="${CCC_WORKTREE:-$(first_line_file "$STATE_DIR/cwd.txt")}"; [ -n "$cwd_val" ] || cwd_val="$(pwd 2>/dev/null || printf '')"
task_val="${CCC_MEMORY_QUERY:-$(read_file_trim "$STATE_DIR/current-task.txt")}"; [ -n "$task_val" ] || task_val="current task"
prompt_val="${CCC_CURRENT_PROMPT:-$(read_file_trim "$STATE_DIR/current-prompt.txt")}" 
extra_val="${CCC_MEMORY_QUERY_EXTRA:-}"
issue_val="${CCC_TASK_ISSUE_URL:-${GITHUB_ISSUE_URL:-}}"
pr_val="${CCC_TASK_PR_URL:-${GITHUB_PR_URL:-}}"

git_branch_val=""
git_paths_val=""
if [ -n "$cwd_val" ] && [ -d "$cwd_val/.git" ]; then
  git_branch_val="$(git -C "$cwd_val" branch --show-current 2>/dev/null || true)"
  git_paths_val="$(git -C "$cwd_val" status --short --untracked-files=no 2>/dev/null | sed -E 's/^...//' | sed -n '1,20p' | tr '\n' ' ' | cut -c1-400)"
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
