#!/usr/bin/env bash
# distill/extract.sh
#   1. Reads transcript jsonl path from CLAUDE_DISTILL_TRANSCRIPT.
#   2. Pulls last N user/assistant turns, strips tool_use/tool_result bulk.
#   3. Applies a secret-regex redact pass (FW-03).
#   4. Calls `claude -p` (OAuth, inherits parent auth) with a focused
#      extract prompt; expects strict JSON: {honcho:[...], wiki_candidates:[...], resume:{...}}.
#   5. Emits the JSON to stdout for distill.sh to dispatch.
#
# CLAUDE_DISTILL_INFLIGHT=1 is set by the parent so the child claude session
# does NOT re-run the SessionStart/PreCompact/etc. hooks.
set -uo pipefail

TRANSCRIPT="${CLAUDE_DISTILL_TRANSCRIPT:-}"
SESSION_ID="${CLAUDE_DISTILL_SESSION:-unknown}"
TRIGGER="${CLAUDE_DISTILL_TRIGGER:-manual}"
MAX_TURNS="${CLAUDE_DISTILL_MAX_TURNS:-80}"
MAX_BYTES="${CLAUDE_DISTILL_MAX_BYTES:-60000}"
MODEL="${CLAUDE_DISTILL_MODEL:-haiku}"
TIMEOUT="${CLAUDE_DISTILL_TIMEOUT:-90}"
SOURCE_CWD="${CLAUDE_DISTILL_SOURCE_CWD:-}"
SOURCE_PROJECT="${CLAUDE_DISTILL_SOURCE_PROJECT:-}"

[ -f "$TRANSCRIPT" ] || { echo "no transcript: $TRANSCRIPT" >&2; exit 1; }

# ---- step 1+2+3: build the redacted, byte-capped transcript window --------
# Wrapped in a function so the timeout-retry path can rebuild with smaller
# (turns, bytes) on ec=124 (#72).
build_redacted() {
  local max_turns="$1" max_bytes="$2"
  local raw redacted
  raw="$(tail -n 400 "$TRANSCRIPT" 2>/dev/null | jq -r '
    select(.type == "user" or .type == "assistant")
    | . as $e
    | (.message.content // .content // "") as $c
    | if ($c | type) == "string" then
        "[\($e.type)] \($c)"
      elif ($c | type) == "array" then
        "[\($e.type)] " + (
          $c | map(
            if .type == "text" then .text
            elif .type == "tool_use" then "[tool:\(.name // "?")]"
            elif .type == "tool_result" then "[tool_result:\(.tool_use_id // "?" | .[0:8])]"
            else "[\(.type // "?")]"
            end
          ) | join("\n")
        )
      else "" end
  ' 2>/dev/null | tail -n "$max_turns")"
  [ -z "$raw" ] && return 1

  # FW-03 redact pass — mirrors ~/.claude/hooks/redact.sh + ghp/gho/ghs/sk-/AKIA/PEM.
  redacted="$(printf '%s' "$raw" | sed -E \
    -e 's/(ghp|gho|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}/[REDACTED:gh-token]/g' \
    -e 's/sk-[A-Za-z0-9_-]{20,}/[REDACTED:api-key]/g' \
    -e 's/AKIA[A-Z0-9]{16}/[REDACTED:aws-key]/g' \
    -e 's/-----BEGIN [A-Z ]*PRIVATE KEY-----/[REDACTED:pem-begin]/g' \
    -e 's/Bearer [A-Za-z0-9._-]{20,}/Bearer [REDACTED]/g' \
  )"

  # byte cap — keep the tail so most-recent context wins
  if [ "${#redacted}" -gt "$max_bytes" ]; then
    redacted="...[truncated $((${#redacted} - max_bytes)) bytes]...
$(printf '%s' "$redacted" | tail -c "$max_bytes")"
  fi

  printf '%s' "$redacted"
}

REDACTED="$(build_redacted "$MAX_TURNS" "$MAX_BYTES")"
[ -z "$REDACTED" ] && { echo "empty transcript content" >&2; exit 1; }

# ---- step 4: build extract prompt -----------------------------------------
PROMPT="$(cat <<'EOF'
You are a memory-distillation pass for a Claude Code node.
You will receive a redacted slice of a session transcript between USER (Seo Jin On / 서진원) and ASSISTANT (dungae, a Hermes Team2 worker).

Extract two kinds of items and return STRICT JSON only.

Schema:
{
  "honcho": [
    {
      "kind": "preference" | "decision" | "observation" | "context",
      "text": "<one-sentence Korean fact about the user, relationship, or in-flight work>",
      "subject": "user" | "session" | "node"
    }
  ],
  "wiki_candidates": [
    {
      "title": "<short Korean title>",
      "suggested_path": "<e.g. pages/team/dungae/DECISIONS.md or pages/nodes/dungae/RUNBOOK.md or pages/log.md>",
      "summary": "<2-4 sentence Korean summary of the durable operational fact / decision / runbook step>",
      "evidence_excerpt": "<<= 200 chars verbatim Korean quote from the transcript>"
    }
  ],
  "resume": {
    "last_activity": "<what the user/assistant was doing at the end of the session, Korean, <= 160 chars>",
    "pending_action": "<the next concrete action, or empty string>",
    "awaiting_user": false,
    "open_question": "<unanswered user-facing question / approval request, or empty string>",
    "next_step": "<one safest next step, or empty string>",
    "evidence": ["<PR/issue/commit/run id if present>"]
  }
}

honcho criteria (working/relational memory; volatile OK):
  - new user preference, communication style, in-flight context that next session needs
  - relationship-level observations about the user
  - DO NOT include node operational facts here — those go to wiki_candidates.

wiki_candidates criteria (durable, public-safe wiki page material):
  - design / architecture / policy decisions for this node
  - new runbook step, incident conclusion, service config change rationale
  - MUST NOT include raw secrets — only locations/handling rules (FW-03).
  - Extract an item ONLY if ALL three hold (GitHub issue #298):
      1. reusable — a future session would act differently because of it
      2. new — not already recorded in the wiki or an earlier session's extract
      3. settled — a confirmed decision/fact, not work still in progress
  - NEVER extract (exclusion list):
      - progress snapshots of ongoing or completed work ("~진행 중", "~완료함")
      - watch/observe items ("~를 관찰해야 함", pending verification)
      - an issue/topic already extracted in a previous session (same #NNN issue)
      - implementation details that are not design decisions
  - At most 3 wiki_candidates per session — keep only the most durable ones.
  - if nothing durable came up this session, return [].

Return [] for either array if nothing qualifies. NEVER invent items.
Always include a `resume` object. Use empty strings/false/[] when there is no meaningful in-flight handoff.
For `resume`, summarize only the last actionable thread: pending approval, unfinished work, open question, and PR/issue/run evidence. Do not include raw secrets or long transcript text.
If the transcript is mostly small talk, code debugging, or trivial Q&A, return {"honcho": [], "wiki_candidates": [], "resume": {"last_activity":"","pending_action":"","awaiting_user":false,"open_question":"","next_step":"","evidence":[]}}.

OUTPUT CONTRACT — READ TWICE:
- Your ENTIRE response MUST be a single JSON object.
- First non-whitespace character MUST be `{`. Last non-whitespace character MUST be `}`.
- NO prose before. NO prose after. NO numbered list. NO bullet analysis. NO "Here is the result:" preamble.
- NO markdown code fences (no triple backticks, no `json` tag). The harness will still strip fences as a safety net, but do not emit them.
- If you have nothing to extract, emit exactly: {"honcho":[],"wiki_candidates":[],"resume":{"last_activity":"","pending_action":"","awaiting_user":false,"open_question":"","next_step":"","evidence":[]}}
EOF
)"

# System-prompt-level constraint (belt + suspenders with the user-prompt instruction).
SYSTEM_CONSTRAINT='Output strict JSON only. The entire response is a single JSON object starting with { and ending with }. Include honcho, wiki_candidates, and resume keys. No prose, no preamble, no analysis, no markdown fences. If nothing qualifies, return {"honcho":[],"wiki_candidates":[],"resume":{"last_activity":"","pending_action":"","awaiting_user":false,"open_question":"","next_step":"","evidence":[]}}.'

# Even more emphatic prompt used on retries (timeout or JSON-drift).
STRICT='CRITICAL OUTPUT CONTRACT. Your entire response MUST be exactly one JSON object and nothing else. The very first character is { and the very last character is }. Include honcho, wiki_candidates, and resume keys. No prose. No code fences. No "Here is the JSON". If you have nothing to extract, output exactly: {"honcho":[],"wiki_candidates":[],"resume":{"last_activity":"","pending_action":"","awaiting_user":false,"open_question":"","next_step":"","evidence":[]}}'

# ---- step 5: call `claude -p` (OAuth via parent process) -------------------
# CLAUDE_DISTILL_INFLIGHT=1 is already exported by distill.sh so the child's
# SessionStart/PreCompact/etc. hooks short-circuit.
#
# Three-attempt strategy:
#   attempt 1 — full input + system constraint.
#   attempt 2a (only on ec=124 / timeout, #72) — rebuild input with halved
#               turns/bytes, retry with STRICT prompt.
#   attempt 2b (only on JSON-drift, #70) — same input, STRICT prompt.

call_claude() {
  local sys="$1" input="$2"
  printf '%s' "$input" | timeout "$TIMEOUT" claude -p \
    --model "$MODEL" \
    --no-session-persistence \
    --output-format text \
    --append-system-prompt "$sys" \
    2>/dev/null
}

try_parse() {
  # Strip possible markdown fences (```json … ```). The rest should be valid JSON.
  printf '%s' "$1" | sed -E '/^[[:space:]]*```/d'
}

build_input() {
  # $1 = REDACTED slice (already byte-capped)
  printf '%s\n\n--- transcript (session=%s trigger=%s) ---\n%s\n' \
    "$PROMPT" "$SESSION_ID" "$TRIGGER" "$1"
}

INPUT="$(build_input "$REDACTED")"
RESULT="$(call_claude "$SYSTEM_CONSTRAINT" "$INPUT")"
ec=$?

# Timeout (#72): halve the window and retry once with STRICT.
if [ $ec -eq 124 ]; then
  half_turns=$(( MAX_TURNS / 2 ))
  half_bytes=$(( MAX_BYTES / 2 ))
  echo "attempt 1 timed out (ec=124); retrying with max_turns=$half_turns max_bytes=$half_bytes + STRICT" >&2
  REDACTED2="$(build_redacted "$half_turns" "$half_bytes")"
  if [ -n "$REDACTED2" ]; then
    INPUT="$(build_input "$REDACTED2")"
    RESULT="$(call_claude "$STRICT" "$INPUT")"
    ec=$?
  fi
  if [ $ec -ne 0 ] || [ -z "$RESULT" ]; then
    echo "claude -p timeout-retry also failed (ec=$ec) or empty" >&2
    exit 1
  fi
  echo "recovered on timeout retry" >&2
elif [ $ec -ne 0 ] || [ -z "$RESULT" ]; then
  echo "claude -p attempt 1 failed (ec=$ec) or empty result" >&2
  exit 1
fi

CLEAN="$(try_parse "$RESULT")"

# JSON-drift retry (#70): same input window, STRICT prompt.
if ! printf '%s' "$CLEAN" | jq -e '.honcho and .wiki_candidates and (.resume | type == "object")' >/dev/null 2>&1; then
  echo "attempt produced non-JSON; retrying with STRICT system prompt" >&2
  RESULT2="$(call_claude "$STRICT" "$INPUT")"
  ec2=$?
  if [ $ec2 -ne 0 ] || [ -z "$RESULT2" ]; then
    echo "JSON-drift retry failed (ec=$ec2) or empty" >&2
    echo "--- previous attempt raw (head 1KB) ---" >&2
    printf '%s\n' "$RESULT" | head -c 1024 >&2
    exit 1
  fi
  CLEAN="$(try_parse "$RESULT2")"
  if ! printf '%s' "$CLEAN" | jq -e '.honcho and .wiki_candidates and (.resume | type == "object")' >/dev/null 2>&1; then
    echo "JSON-drift retry also produced non-JSON; giving up" >&2
    echo "--- retry raw (head 1KB) ---" >&2
    printf '%s\n' "$RESULT2" | head -c 1024 >&2
    exit 1
  fi
  echo "recovered on JSON-drift retry" >&2
fi

# Tag with metadata for downstream consumers.
printf '%s' "$CLEAN" | jq -c \
  --arg sid "$SESSION_ID" \
  --arg trg "$TRIGGER" \
  --arg ts  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg source_cwd "$SOURCE_CWD" \
  --arg source_project "$SOURCE_PROJECT" \
  '. + {session_id:$sid, trigger:$trg, distilled_at:$ts, source_cwd:$source_cwd, source_project:$source_project}'
