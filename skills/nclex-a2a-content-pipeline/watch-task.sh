#!/usr/bin/env bash
# watch-task.sh — sanctioned A2A task watcher for nclex pipeline lanes
# (jinwon-int/ccc-node #1389).
#
# Why this exists: during nclex PR #459 an ad-hoc polling watcher waited on a
# task that had already finished, because its case statement matched
# `completed|failed|cancelled|review_verdict_failed` — none of which is a real
# broker TaskStatus except `failed`. 38 consecutive polls LOGGED
# `state=succeeded` and still timed out, the result file was never saved, and
# nothing notified the next session. This template makes that class of bug
# structurally impossible:
#
#   1. Terminal vocabulary is fixed to the broker's actual TaskStatus enum —
#      terminal = `succeeded | failed | canceled` (a2a-nexus
#      packages/broker/src/core/broker-status-predicates.ts:
#      isTerminalTaskStatus; non-terminal: blocked|queued|claimed|running).
#   2. The first poll always prints the OBSERVED status so a vocabulary
#      mismatch is visible in the log from minute one.
#   3. Fail-safe on unknown vocabulary: a parsed-but-unrecognized status seen
#      N consecutive times (default 3) ends the watch as `unknown_state` with
#      the evidence saved. Exiting early on a false guess is strictly safer
#      than never recognizing a finished task (#1389 failure mode).
#   4. EVERY exit path writes the --out file with a machine-readable verdict —
#      succeeded / failed / canceled / unknown_state / timeout /
#      response_failure / http_error / not_found / interrupted. If the out
#      file does not exist, the watcher never finished — that is the
#      detection signal for the next session.
#
# Result file (always created on exit):
#   { "watcher_schema": "nclex-a2a-watch-result.v1", "verdict": ...,
#     "task_status": ..., "polls": N, "broker_url": ...,
#     "finished_at_utc": ..., "note": ..., "task": <raw broker record|null> }
#
# Exit codes: 0 succeeded | 1 failed|canceled | 2 unknown_state | 3 timeout
#             4 response_failure | 5 http_error | 6 not_found | 64 usage | 130 interrupted
#
# Secret handling (skill safety rule: edge secrets are never printed, copied
# or moved): the secret is read from --secret-file (first line) or
# $A2A_EDGE_SECRET and passed to curl via `--header @file`, so it never
# appears in argv (`ps`) or in this script's output.
#
# Usage:
#   watch-task.sh --broker URL --task-id ID --out FILE
#                [--interval SEC] [--max-polls N] [--unknown-limit N]
#                [--response-fail-limit N] [--http-timeout SEC]
#                [--requester-id ID] [--requester-role ROLE]
#                [--secret-file FILE] [--quiet]
#
#   watch-task.sh --broker "http://127.0.0.1:18787" \
#     --task-id "nclex-pr459-terminology-...:RNM-...:terminology_bilingual" \
#     --requester-id nosuk --requester-role analyst \
#     --secret-file /etc/default/a2a-hermes-worker:BROKER_EDGE_SECRET \
#     --out /root/nclex-dispatch/watch-pr459-terminology.json
#
# --secret-file takes either a plain file whose FIRST LINE is the secret, or
# "ENV_FILE:VAR_NAME" to extract one variable from an env-style file (the
# FILE:VAR form splits at the LAST colon — a plain path with no env-var
# extraction need not contain a colon, and never did in fleet practice).
set -uo pipefail

show_help() {
  sed -n '2,63p' "$0" | grep -E '^#( |$)' | sed 's/^# \{0,1\}//'
}

usage() {
  show_help >&2
  exit 64
}

broker=""
task_id=""
out=""
interval=30
max_polls=40
unknown_limit=3
response_fail_limit=5
http_timeout=15
requester_id="${A2A_REQUESTER_ID:-}"
requester_role="${A2A_REQUESTER_ROLE:-}"
secret_file=""
quiet=0

while [ $# -gt 0 ]; do
  case "$1" in
    --broker) broker="${2:-}"; shift 2 ;;
    --task-id) task_id="${2:-}"; shift 2 ;;
    --out) out="${2:-}"; shift 2 ;;
    --interval) interval="${2:-}"; shift 2 ;;
    --max-polls) max_polls="${2:-}"; shift 2 ;;
    --unknown-limit) unknown_limit="${2:-}"; shift 2 ;;
    --response-fail-limit) response_fail_limit="${2:-}"; shift 2 ;;
    --http-timeout) http_timeout="${2:-}"; shift 2 ;;
    --requester-id) requester_id="${2:-}"; shift 2 ;;
    --requester-role) requester_role="${2:-}"; shift 2 ;;
    --secret-file) secret_file="${2:-}"; shift 2 ;;
    --quiet) quiet=1; shift ;;
    --help|-h) show_help; exit 0 ;;
    *) printf 'watch-task.sh: unknown argument: %s\n' "$1" >&2; usage ;;
  esac
done

[ -n "$broker" ] || { printf 'watch-task.sh: --broker is required\n' >&2; usage; }
[ -n "$task_id" ] || { printf 'watch-task.sh: --task-id is required\n' >&2; usage; }
[ -n "$out" ] || { printf 'watch-task.sh: --out is required\n' >&2; usage; }
for v in "$interval" "$max_polls" "$unknown_limit" "$response_fail_limit" "$http_timeout"; do
  case "$v" in ''|*[!0-9.]*) printf 'watch-task.sh: numeric value expected, got: %s\n' "$v" >&2; usage ;; esac
done

command -v curl >/dev/null 2>&1 || { printf 'watch-task.sh: curl not found\n' >&2; exit 64; }
command -v python3 >/dev/null 2>&1 || { printf 'watch-task.sh: python3 not found\n' >&2; exit 64; }

secret=""
if [ -n "$secret_file" ]; then
  # Support "FILE:ENV_NAME" to pull one variable out of an env-style file
  # (e.g. the broker node's /etc/default/a2a-hermes-worker) without copying
  # the value anywhere.
  case "$secret_file" in
    *:*)
      f="${secret_file%:*}"; key="${secret_file##*:}"
      [ -n "$f" ] && [ -f "$f" ] || { printf 'watch-task.sh: secret file not found: %s\n' "$f" >&2; exit 64; }
      case "$key" in ''|*[!A-Za-z0-9_]*) printf 'watch-task.sh: --secret-file FILE:VAR needs an env-var NAME, got: %s\n' "$key" >&2; exit 64 ;; esac
      secret="$(sed -n "s/^${key}=//p" "$f" | head -n 1 | tr -d '"' | tr -d '\r')"
      ;;
    *)
      [ -f "$secret_file" ] || { printf 'watch-task.sh: secret file not found: %s\n' "$secret_file" >&2; exit 64; }
      secret="$(head -n 1 "$secret_file" | tr -d '\r\n')"
      ;;
  esac
else
  secret="${A2A_EDGE_SECRET:-}"
fi

# Task URL: percent-encode the id (lane ids contain ":" and ","), join to the
# broker base without trailing-slash surprises.
task_url="$(python3 - "$broker" "$task_id" <<'PY'
import sys, urllib.parse
base, task_id = sys.argv[1], sys.argv[2]
print(base.rstrip("/") + "/tasks/" + urllib.parse.quote(task_id, safe=""))
PY
)" || exit 64

TMPDIR_LOCAL="$(mktemp -d "${TMPDIR:-/tmp}/a2a-watch.XXXXXX")" || exit 64
body_file="$TMPDIR_LOCAL/body.json"
hdrs_file="$TMPDIR_LOCAL/headers.txt"
curl_err="$TMPDIR_LOCAL/curl.err"

cleanup() { rm -rf "$TMPDIR_LOCAL" 2>/dev/null || :; }
trap cleanup EXIT

: > "$hdrs_file"
if [ -n "$secret" ]; then
  printf 'x-a2a-edge-secret: %s\n' "$secret" > "$hdrs_file"
  chmod 600 "$hdrs_file"
else
  printf 'watch-task.sh: no edge secret configured (--secret-file / A2A_EDGE_SECRET); sending unauthenticated\n' >&2
fi
[ -n "$requester_id" ] && printf 'x-a2a-requester-id: %s\n' "$requester_id" >> "$hdrs_file"
[ -n "$requester_role" ] && printf 'x-a2a-requester-role: %s\n' "$requester_role" >> "$hdrs_file"

# finish <verdict> <exit_code> <note> — writes the out file atomically, prints
# the machine-readable result line, exits. Never skip the out file: its
# absence is the "watcher died without finishing" signal.
finish() {
  verdict="$1"; code="$2"; note="$3"
  out_tmp="$out.tmp.$$"
  if python3 - "$out_tmp" "$verdict" "$polls" "$task_url" "$last_status" "$note" "$body_file" <<'PY'
import datetime, json, os, sys
out_tmp, verdict, polls, url, status, note = sys.argv[1:7]
body_file = sys.argv[7]
task = None
if body_file and os.path.exists(body_file) and os.path.getsize(body_file) > 0:
    try:
        with open(body_file, "r", encoding="utf-8") as fh:
            task = json.load(fh)
    except Exception:
        task = None
doc = {
    "watcher_schema": "nclex-a2a-watch-result.v1",
    "verdict": verdict,
    "task_status": status or None,
    "polls": int(polls),
    "broker_url": url,
    "finished_at_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "note": note,
    "task": task,
}
with open(out_tmp, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PY
  then
    mv "$out_tmp" "$out" 2>/dev/null || { printf 'watch-task.sh: cannot write %s\n' "$out" >&2; rm -f "$out_tmp"; }
  else
    printf 'watch-task.sh: result serialization failed; raw body kept at %s\n' "$body_file" >&2
  fi
  printf 'WATCHER_RESULT=%s polls=%s status=%s out=%s\n' "$verdict" "$polls" "${last_status:-<none>}" "$out"
  exit "$code"
}

interrupted=0
on_signal() { interrupted=1; }
trap on_signal INT TERM HUP

polls=0
last_status=""
unknown_streak=0
response_fail_streak=0

while [ "$polls" -lt "$max_polls" ]; do
  if [ "$interrupted" -eq 1 ]; then
    finish interrupted 130 "watcher received SIGINT/TERM/HUP before a terminal status"
  fi

  polls=$((polls + 1))

  code="$(curl -sS --max-time "$http_timeout" --header @"$hdrs_file" \
               -o "$body_file" -w '%{http_code}' "$task_url" 2>"$curl_err")"
  rc=$?

  if [ "$rc" -ne 0 ]; then
    response_fail_streak=$((response_fail_streak + 1))
    err_line="$(head -c 200 "$curl_err" 2>/dev/null | head -n 1)"
    [ "$quiet" -eq 0 ] && printf 'poll=%s status=<curl rc=%s> %s\n' "$polls" "$rc" "$err_line"
    if [ "$response_fail_streak" -ge "$response_fail_limit" ]; then
      finish response_failure 4 "curl failed ${response_fail_streak}x consecutively (last rc=${rc}: ${err_line})"
    fi
  elif [ "$code" = 404 ]; then
    finish not_found 6 "task not found at broker — check --broker/--task-id against the manifest"
  elif [ "$code" -ge 400 ] 2>/dev/null && [ "$code" -lt 500 ] 2>/dev/null; then
    finish http_error 5 "broker rejected the poll (HTTP ${code}) — auth/probe misconfiguration, retrying will not heal it"
  elif [ "$code" -ge 500 ] 2>/dev/null; then
    response_fail_streak=$((response_fail_streak + 1))
    [ "$quiet" -eq 0 ] && printf 'poll=%s status=<HTTP %s>\n' "$polls" "$code"
    if [ "$response_fail_streak" -ge "$response_fail_limit" ]; then
      finish response_failure 4 "broker 5xx ${response_fail_streak}x consecutively"
    fi
  else
    status="$(python3 -c 'import json,sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        print(json.load(fh).get("status") or "")
except Exception:
    print("")' "$body_file" 2>/dev/null)"
    if [ -z "$status" ]; then
      # Parsed HTTP 2xx but no usable status — treat like unknown vocabulary.
      status="<unparseable>"
    fi
    last_status="$status"

    if [ "$polls" -eq 1 ] || [ "$quiet" -eq 0 ]; then
      printf 'poll=%s status=%s\n' "$polls" "$status"
    fi

    case "$status" in
      succeeded)
        finish succeeded 0 "task succeeded (broker TaskStatus vocabulary)"
        ;;
      failed|canceled)
        finish "$status" 1 "task ended with terminal status: ${status} (error/result in .task)"
        ;;
      blocked|queued|claimed|running)
        unknown_streak=0
        response_fail_streak=0
        ;;
      *)
        unknown_streak=$((unknown_streak + 1))
        response_fail_streak=0
        if [ "$unknown_streak" -ge "$unknown_limit" ]; then
          finish unknown_state 2 "status '${status}' is outside the broker TaskStatus vocabulary (blocked|queued|claimed|running|succeeded|failed|canceled) on ${unknown_streak} consecutive polls — see #1389"
        fi
        ;;
    esac
  fi

  # Interruptible sleep: plain `sleep N` would delay trap delivery until the
  # child exits; background+wait lets the signal land immediately.
  sleep "$interval" &
  wait $! || true
done

finish timeout 3 "no terminal status after ${max_polls} polls (${interval}s interval) — task may still be running; re-check GET ${task_url}"
