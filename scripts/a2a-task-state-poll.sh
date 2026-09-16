#!/usr/bin/env bash
# a2a-task-state-poll.sh — read-only A2A broker task-state poller whose
# terminal evidence comes from the broker record itself, never from repo
# queries (jinwon-int/ccc-node #1760).
#
# Why this exists: the danso dispatch waves used an ad-hoc poller whose
# terminal branch guessed PR evidence with
#   gh pr list --search "in:title doctor OR B3"
# When task danso-c1-doctor-20260915-r2 finished, PR #115 genuinely existed,
# but the title search matched nothing and the shared task-state.log recorded
# a bare `prs=` — an evidence log that read like "no PR" to every later
# session. Repo-side text search is a heuristic about how GitHub tokenizes a
# title; the broker result already carries the authoritative pointers
# (`result.output.prUrl`, or `result.output.github.prUrl`). This poller reads
# those fields directly and, when they are absent, logs an explicit
# `pr=none-in-broker-result` marker instead of a silent empty value.
#
# Design contracts:
#   1. Terminal vocabulary is the broker's actual TaskStatus enum —
#      terminal = `succeeded | failed | canceled`; non-terminal =
#      blocked|queued|claimed|running (a2a-nexus
#      packages/broker/src/core/broker-status-predicates.ts, same contract as
#      the sanctioned watcher skills/nclex-a2a-content-pipeline/watch-task.sh
#      from #1389).
#   2. A failed or canceled task may still carry PR evidence ("a failed task
#      with a PR remains failed" — the finalizer reviews that artifact
#      independently), so the pr= line is emitted for every terminal state.
#   3. The poller is one-shot per invocation (a transient timer or cron
#      re-invokes it); it performs no internal retry loop and no repo access
#      of any kind — no `gh`, no network beyond the broker GET.
#   4. The log is append-only. Existing lines are never rewritten; new lines
#      carry fresh timestamps. Evidence corrections belong in new lines.
#   5. Secret-safe: the edge secret is read via --secret-file (FILE or
#      FILE:VAR_NAME) and passed to curl through a 0600 header file, so it
#      never appears in argv or in output. See
#      skills/nclex-a2a-content-pipeline/watch-task.sh (#1389) for the same
#      rule.
#
# Usage (watch mode — one broker GET per task):
#   a2a-task-state-poll.sh --broker URL --secret-file FILE[:VAR] \
#       --task TASK_ID --log LOG_PATH [--task T2 --log L2 ...]
#
#   Every invocation appends to each LOG_PATH exactly one state line, plus a
#   pr= line when the observed status is terminal:
#     2026-09-16T09:00:00+0900 state=succeeded
#     2026-09-16T09:00:00+0900 pr=https://github.com/o/r/pull/115
#   Non-terminal statuses get no pr= line. Fetch, HTTP, and parse failures
#   are logged as state=fetch-failed / state=http-<code> / state=unparseable
#   and never fabricate a terminal or PR value.
#
# Usage (offline analysis — classify a saved broker task body):
#   a2a-task-state-poll.sh --from-file BODY_JSON
#     Prints `status=<s>` and `pr_url=<u|none-in-broker-result>` — the same
#     extraction the watch mode uses, runnable against a saved GET body for
#     post-mortems and tests.
#
# Exit codes (watch mode): 0 all observed tasks logged · 64 usage ·
#   1 no task could be polled (every pair failed) — the log lines still
#   record what happened, so a later invocation is always safe.
set -uo pipefail
umask 077

show_help() {
  sed -n '2,58p' "$0" | grep -E '^#( |$)' | sed 's/^# \{0,1\}//'
}

usage() {
  show_help >&2
  exit 64
}

broker=""
secret_file=""
tasks=()   # parallel arrays: task ids and log paths
logs=()
from_file=""
http_timeout=15

while [ $# -gt 0 ]; do
  case "$1" in
    --broker) broker="${2:-}"; shift 2 ;;
    --secret-file) secret_file="${2:-}"; shift 2 ;;
    --task) tasks+=("${2:-}"); shift 2 ;;
    --log) logs+=("${2:-}"); shift 2 ;;
    --from-file) from_file="${2:-}"; shift 2 ;;
    --http-timeout) http_timeout="${2:-}"; shift 2 ;;
    --help|-h) show_help; exit 0 ;;
    *) printf 'a2a-task-state-poll.sh: unknown argument: %s\n' "$1" >&2; usage ;;
  esac
done

# Extract status and prUrl from a broker task body (file). Reused by the
# offline mode so the watch mode and post-mortems cannot drift apart.
# Prints:
#   status=<status or unparseable>
#   pr_url=<url or none-in-broker-result>
classify_body() {
  python3 - "$1" <<'PY'
import json, sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        body = json.load(fh)
except Exception:
    print("status=unparseable")
    print("pr_url=none-in-broker-result")
    raise SystemExit(0)

if not isinstance(body, dict):
    print("status=unparseable")
    print("pr_url=none-in-broker-result")
    raise SystemExit(0)

status = body.get("status") or body.get("state") or ""

pr_url = None
result = body.get("result")
if isinstance(result, dict):
    output = result.get("output")
    if isinstance(output, dict):
        candidate = output.get("prUrl")
        if isinstance(candidate, str) and candidate:
            pr_url = candidate
        else:
            github = output.get("github")
            if isinstance(github, dict):
                candidate = github.get("prUrl")
                if isinstance(candidate, str) and candidate:
                    pr_url = candidate

print("status=%s" % (status if isinstance(status, str) and status else "unparseable"))
print("pr_url=%s" % (pr_url if pr_url else "none-in-broker-result"))
PY
}

if [ -n "$from_file" ]; then
  [ -f "$from_file" ] || { printf 'a2a-task-state-poll.sh: body file not found: %s\n' "$from_file" >&2; usage; }
  classify_body "$from_file"
  exit 0
fi

[ -n "$broker" ] || { printf 'a2a-task-state-poll.sh: --broker is required\n' >&2; usage; }
[ "${#tasks[@]}" -gt 0 ] || { printf 'a2a-task-state-poll.sh: at least one --task/--log pair is required\n' >&2; usage; }
[ "${#tasks[@]}" -eq "${#logs[@]}" ] || { printf 'a2a-task-state-poll.sh: --task and --log counts differ\n' >&2; usage; }
case "$http_timeout" in ''|*[!0-9.]*) printf 'a2a-task-state-poll.sh: numeric --http-timeout expected\n' >&2; usage ;; esac
for i in "${!tasks[@]}"; do
  if [ -z "${tasks[$i]}" ] || [ -z "${logs[$i]}" ]; then
    printf 'a2a-task-state-poll.sh: empty --task/--log value\n' >&2
    usage
  fi
done

command -v curl >/dev/null 2>&1 || { printf 'a2a-task-state-poll.sh: curl not found\n' >&2; exit 64; }
command -v python3 >/dev/null 2>&1 || { printf 'a2a-task-state-poll.sh: python3 not found\n' >&2; exit 64; }

# Secret → 0600 header file; the value never appears in argv or output.
hdrs_file="$(mktemp "${TMPDIR:-/tmp}/a2a-poll-hdrs.XXXXXX")"
cleanup() { rm -f "$hdrs_file" 2>/dev/null || :; }
trap cleanup EXIT
: > "$hdrs_file"
if [ -n "$secret_file" ]; then
  case "$secret_file" in
    *:*)
      f="${secret_file%:*}"; key="${secret_file##*:}"
      if [ -z "$f" ] || [ ! -f "$f" ]; then
        printf 'a2a-task-state-poll.sh: secret file not found: %s\n' "$f" >&2
        exit 64
      fi
      case "$key" in ''|*[!A-Za-z0-9_]*) printf 'a2a-task-state-poll.sh: --secret-file FILE:VAR needs an env-var NAME, got: %s\n' "$key" >&2; exit 64 ;; esac
      secret="$(sed -n "s/^${key}=//p" "$f" | head -n 1 | tr -d '"' | tr -d '\r')"
      ;;
    *)
      [ -f "$secret_file" ] || { printf 'a2a-task-state-poll.sh: secret file not found: %s\n' "$secret_file" >&2; exit 64; }
      secret="$(head -n 1 "$secret_file" | tr -d '\r\n')"
      ;;
  esac
  [ -n "$secret" ] && printf 'x-a2a-edge-secret: %s\n' "$secret" > "$hdrs_file"
fi
chmod 600 "$hdrs_file" 2>/dev/null || :

poll_count=0
poll_ok_count=0

# Append one evidence line and force owner-only mode on the log. Append-only
# means existing lines are never rewritten; tightening the mode is not a
# content change. A log inherited from a wider-umask writer (the #1760 CI
# failure) must not stay group/world-readable just because this process did
# not create it.
log_line() { # <log_path> <line>
  printf '%s\n' "$2" >> "$1"
  chmod 600 "$1" 2>/dev/null || :
}

for i in "${!tasks[@]}"; do
  task_id="${tasks[$i]}"
  log_path="${logs[$i]}"
  poll_count=$((poll_count + 1))
  ts="$(date '+%Y-%m-%dT%H:%M:%S%z')"

  # Lane ids may contain ":" or ","; percent-encode everything reserved.
  task_url="$(python3 - "$broker" "$task_id" <<'PY'
import sys, urllib.parse
base, task_id = sys.argv[1], sys.argv[2]
print(base.rstrip("/") + "/tasks/" + urllib.parse.quote(task_id, safe=""))
PY
)" || { log_line "$log_path" "$ts state=fetch-failed"; continue; }

  body_file="$(mktemp "${TMPDIR:-/tmp}/a2a-poll-body.XXXXXX")"
  code="$(curl -sS --max-time "$http_timeout" --header @"$hdrs_file" \
               -o "$body_file" -w '%{http_code}' "$task_url" 2>/dev/null)"
  curl_rc=$?
  if [ "$curl_rc" -ne 0 ] || [ -z "$code" ]; then
    log_line "$log_path" "$ts state=fetch-failed"
    rm -f "$body_file"
    continue
  fi
  case "$code" in
    2*)
      classified="$(classify_body "$body_file")"
      status="$(printf '%s\n' "$classified" | sed -n 's/^status=//p')"
      pr_url="$(printf '%s\n' "$classified" | sed -n 's/^pr_url=//p')"
      rm -f "$body_file"
      log_line "$log_path" "$ts state=${status:-unparseable}"
      case "$status" in
        succeeded|failed|canceled)
          log_line "$log_path" "$ts pr=${pr_url:-none-in-broker-result}"
          ;;
      esac
      poll_ok_count=$((poll_ok_count + 1))
      ;;
    *)
      # 4xx/5xx or an empty code: record the observable fact, never a guess.
      log_line "$log_path" "$ts state=http-${code:-none}"
      rm -f "$body_file"
      ;;
  esac
done

[ "$poll_ok_count" -gt 0 ] || exit 1
exit 0
