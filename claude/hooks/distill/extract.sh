#!/usr/bin/env bash
# distill/extract.sh
#   1. Reads transcript jsonl path from CLAUDE_DISTILL_TRANSCRIPT.
#   2. Pulls last N user/assistant turns, strips tool_use/tool_result bulk.
#   3. Applies a secret-regex redact pass (FW-03).
#   4. Calls `claude -p` (OAuth, inherits parent auth) with a focused
#      extract prompt; expects strict JSON: {honcho:[...], wiki_candidates:[...]}.
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

[ -f "$TRANSCRIPT" ] || { echo "no transcript: $TRANSCRIPT" >&2; exit 1; }

# ---- step 1+2: extract last N meaningful turns ----------------------------
# Each line is a JSON event; we want user prompts + assistant text messages.
# Tool calls are summarized as "[tool:<name>]" to keep token budget low.
RAW="$(tail -n 400 "$TRANSCRIPT" 2>/dev/null | jq -r --argjson maxt "$MAX_TURNS" '
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
' 2>/dev/null | tail -n "$MAX_TURNS")"

[ -z "$RAW" ] && { echo "empty transcript content" >&2; exit 1; }

# ---- step 3: redact pass (FW-03) ------------------------------------------
# Mirrors patterns from ~/.claude/hooks/redact.sh + ghp/gho/ghs/sk-/AKIA/PEM.
REDACTED="$(printf '%s' "$RAW" | sed -E \
  -e 's/(ghp|gho|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}/[REDACTED:gh-token]/g' \
  -e 's/sk-[A-Za-z0-9_-]{20,}/[REDACTED:api-key]/g' \
  -e 's/AKIA[A-Z0-9]{16}/[REDACTED:aws-key]/g' \
  -e 's/-----BEGIN [A-Z ]*PRIVATE KEY-----/[REDACTED:pem-begin]/g' \
  -e 's/Bearer [A-Za-z0-9._-]{20,}/Bearer [REDACTED]/g' \
)"

# byte cap (head from the end so most-recent context wins)
if [ "${#REDACTED}" -gt "$MAX_BYTES" ]; then
  REDACTED="...[truncated $((${#REDACTED} - MAX_BYTES)) bytes]...
$(printf '%s' "$REDACTED" | tail -c "$MAX_BYTES")"
fi

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
  ]
}

honcho criteria (working/relational memory; volatile OK):
  - new user preference, communication style, in-flight context that next session needs
  - relationship-level observations about the user
  - DO NOT include node operational facts here — those go to wiki_candidates.

wiki_candidates criteria (durable, public-safe wiki page material):
  - design / architecture / policy decisions for this node
  - new runbook step, incident conclusion, service config change rationale
  - MUST NOT include raw secrets — only locations/handling rules (FW-03).
  - if nothing durable came up this session, return [].

Return [] for either array if nothing qualifies. NEVER invent items.
If the transcript is mostly small talk, code debugging, or trivial Q&A, return {"honcho": [], "wiki_candidates": []}.

OUTPUT CONTRACT — READ TWICE:
- Your ENTIRE response MUST be a single JSON object.
- First non-whitespace character MUST be `{`. Last non-whitespace character MUST be `}`.
- NO prose before. NO prose after. NO numbered list. NO bullet analysis. NO "Here is the result:" preamble.
- NO markdown code fences (no triple backticks, no `json` tag). The harness will still strip fences as a safety net, but do not emit them.
- If you have nothing to extract, emit exactly: {"honcho":[],"wiki_candidates":[]}
EOF
)"

# System-prompt-level constraint (belt + suspenders with the user-prompt instruction).
SYSTEM_CONSTRAINT='Output strict JSON only. The entire response is a single JSON object starting with { and ending with }. No prose, no preamble, no analysis, no markdown fences. If nothing qualifies, return {"honcho":[],"wiki_candidates":[]}.'

INPUT="$(printf '%s\n\n--- transcript (session=%s trigger=%s) ---\n%s\n' \
  "$PROMPT" "$SESSION_ID" "$TRIGGER" "$REDACTED")"

# ---- step 5: call `claude -p` (OAuth via parent process) -------------------
# CLAUDE_DISTILL_INFLIGHT=1 is already exported by distill.sh so the child's
# SessionStart/PreCompact/etc. hooks short-circuit.
#
# Two-attempt strategy:
#   attempt 1 — full input + system constraint.
#   attempt 2 (only if attempt 1's response is not parseable JSON) — same input,
#              but with an even more emphatic system prompt prepended. Most
#              Haiku "prose drift" failures recover on a single strict retry.

call_claude() {
  local sys="$1"
  printf '%s' "$INPUT" | timeout "$TIMEOUT" claude -p \
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

RESULT="$(call_claude "$SYSTEM_CONSTRAINT")"
ec=$?

if [ $ec -ne 0 ] || [ -z "$RESULT" ]; then
  echo "claude -p attempt 1 failed (ec=$ec) or empty result" >&2
  exit 1
fi

CLEAN="$(try_parse "$RESULT")"

# Validate JSON. If it fails, do ONE retry with a stricter system prompt.
if ! printf '%s' "$CLEAN" | jq -e '.honcho and .wiki_candidates' >/dev/null 2>&1; then
  echo "attempt 1 produced non-JSON; retrying with stricter system prompt" >&2
  STRICT='CRITICAL OUTPUT CONTRACT. Your entire response MUST be exactly one JSON object and nothing else. The very first character is { and the very last character is }. No prose. No code fences. No "Here is the JSON". If you have nothing to extract, output exactly: {"honcho":[],"wiki_candidates":[]}'
  RESULT2="$(call_claude "$STRICT")"
  ec2=$?
  if [ $ec2 -ne 0 ] || [ -z "$RESULT2" ]; then
    echo "claude -p attempt 2 failed (ec=$ec2) or empty result" >&2
    echo "--- attempt 1 raw (head 1KB) ---" >&2
    printf '%s\n' "$RESULT" | head -c 1024 >&2
    exit 1
  fi
  CLEAN="$(try_parse "$RESULT2")"
  if ! printf '%s' "$CLEAN" | jq -e '.honcho and .wiki_candidates' >/dev/null 2>&1; then
    echo "attempt 2 also produced non-JSON; giving up" >&2
    echo "--- attempt 2 raw (head 1KB) ---" >&2
    printf '%s\n' "$RESULT2" | head -c 1024 >&2
    exit 1
  fi
  echo "recovered on retry" >&2
fi

# Tag with metadata for downstream consumers.
printf '%s' "$CLEAN" | jq -c \
  --arg sid "$SESSION_ID" \
  --arg trg "$TRIGGER" \
  --arg ts  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '. + {session_id:$sid, trigger:$trg, distilled_at:$ts}'
