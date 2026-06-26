#!/usr/bin/env bash
# skill-review/extract.sh
# Reads CLAUDE_SKILL_REVIEW_TRANSCRIPT and asks a small Claude model to propose
# reusable SKILL.md drafts. Strict JSON out; no filesystem writes here.
set -uo pipefail

TRANSCRIPT="${CLAUDE_SKILL_REVIEW_TRANSCRIPT:-}"
SESSION_ID="${CLAUDE_SKILL_REVIEW_SESSION:-unknown}"
TRIGGER="${CLAUDE_SKILL_REVIEW_TRIGGER:-manual}"
SOURCE_CWD="${CLAUDE_SKILL_REVIEW_SOURCE_CWD:-}"
SOURCE_PROJECT="${CLAUDE_SKILL_REVIEW_SOURCE_PROJECT:-}"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-/root/.claude/skills}"
MAX_TURNS="${CCC_SKILL_REVIEW_MAX_TURNS:-80}"
MAX_BYTES="${CCC_SKILL_REVIEW_MAX_BYTES:-60000}"
MODEL="${CCC_SKILL_REVIEW_MODEL:-haiku}"
TIMEOUT="${CCC_SKILL_REVIEW_TIMEOUT:-180}"

[ -f "$TRANSCRIPT" ] || { echo "no transcript: $TRANSCRIPT" >&2; exit 1; }

build_redacted() {
  local max_turns="$1" max_bytes="$2"
  local raw redacted
  raw="$(tail -n 500 "$TRANSCRIPT" 2>/dev/null | jq -r '
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
  redacted="$(printf '%s' "$raw" | sed -E \
    -e 's/(ghp|gho|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}/[REDACTED:gh-token]/g' \
    -e 's/sk-[A-Za-z0-9_-]{20,}/[REDACTED:api-key]/g' \
    -e 's/AKIA[A-Z0-9]{16}/[REDACTED:aws-key]/g' \
    -e 's/-----BEGIN [A-Z ]*PRIVATE KEY-----/[REDACTED:pem-begin]/g' \
    -e 's/Bearer [A-Za-z0-9._-]{20,}/Bearer [REDACTED]/g' \
    -e 's/((password|passwd|secret|token|api[_-]?key|authorization)[=:[:space:]"'"'"']+)[^[:space:]"'"'"'&|;]+/\1[REDACTED]/gI')"
  if [ "${#redacted}" -gt "$max_bytes" ]; then
    redacted="...[truncated $((${#redacted} - max_bytes)) bytes]...
$(printf '%s' "$redacted" | tail -c "$max_bytes")"
  fi
  printf '%s' "$redacted"
}

existing_skills() {
  if [ ! -d "$SKILLS_DIR" ]; then
    printf '(none)'
    return 0
  fi
  find "$SKILLS_DIR" -maxdepth 2 -name SKILL.md 2>/dev/null | sort | while IFS= read -r f; do
    name="$(awk 'NR>1 && /^---/{exit} /^name:/ {sub(/^name:[[:space:]]*/,""); print; exit}' "$f" 2>/dev/null)"
    desc="$(awk 'NR>1 && /^---/{exit} /^description:/ {sub(/^description:[[:space:]]*/,""); print; exit}' "$f" 2>/dev/null)"
    [ -n "$name" ] && printf -- '- %s — %s\n' "$name" "$desc"
  done
}

REDACTED="$(build_redacted "$MAX_TURNS" "$MAX_BYTES")"
[ -z "$REDACTED" ] && { echo "empty transcript content" >&2; exit 1; }
EXISTING="$(existing_skills | head -80)"

PROMPT="$(cat <<'EOF'
You are the Hermes-style skill self-improvement reviewer for a Claude Code node.
You will receive a redacted session transcript and a list of existing skills.
Return STRICT JSON only.

Goal: propose reusable Claude Code skills worth staging for human approval.

Schema:
{
  "skill_candidates": [
    {
      "name": "lowercase-kebab-name",
      "category": "claude",
      "summary": "one sentence explaining what this captures",
      "reason": "why the session shows a reusable procedure",
      "evidence_excerpt": "<=200 chars from transcript, no secrets",
      "skill_md": "complete SKILL.md content with YAML frontmatter"
    }
  ]
}

Criteria:
- Propose at most 2 candidates.
- Return [] if no non-trivial reusable multi-step workflow, correction, debugging path, or operator preference emerged.
- Do NOT duplicate an existing skill; patching existing skills is out of scope for this hook, so return [] if an existing skill already covers it.
- Do NOT capture one-off task narratives, PR numbers, transient errors, mutable live node facts, raw secrets, endpoints, tokens, private message text, or credentials.
- Keep proposed skills node-agnostic and public-safe. Mention credential locations/handling rules only, never values.
- A valid SKILL.md starts with YAML frontmatter containing name and description. Description must be concise and routing-friendly.
- The body should include: When to Use, Procedure, Safety, Verification.
- Frame commands as Claude Code / ccc-node procedures. Use exact commands only if the transcript clearly showed them; otherwise describe the safe decision rule instead of inventing flags.

OUTPUT CONTRACT:
- Your entire response is a single JSON object.
- First non-whitespace char is { and last is }.
- No markdown fences. No prose. No analysis.
- If nothing qualifies, output exactly: {"skill_candidates":[]}.
EOF
)"
SYSTEM_CONSTRAINT='Output strict JSON only: one object with key skill_candidates. No prose, no markdown fences.'
STRICT='CRITICAL: Output exactly one JSON object and nothing else. If no candidates, output {"skill_candidates":[]}.'

call_claude() {
  local sys="$1" input="$2"
  printf '%s' "$input" | timeout "$TIMEOUT" claude -p \
    --model "$MODEL" \
    --no-session-persistence \
    --output-format text \
    --append-system-prompt "$sys" \
    2>/dev/null
}

build_input() {
  printf '%s\n\n--- existing skills ---\n%s\n\n--- transcript metadata ---\nsession=%s trigger=%s source_cwd=%s source_project=%s\n\n--- redacted transcript ---\n%s\n' \
    "$PROMPT" "$EXISTING" "$SESSION_ID" "$TRIGGER" "$SOURCE_CWD" "$SOURCE_PROJECT" "$REDACTED"
}

try_parse() { printf '%s' "$1" | sed -E '/^[[:space:]]*```/d'; }

INPUT="$(build_input)"
RESULT="$(call_claude "$SYSTEM_CONSTRAINT" "$INPUT")"
ec=$?
if [ $ec -ne 0 ] || [ -z "$RESULT" ]; then
  echo "claude -p attempt failed (ec=$ec) or empty" >&2
  exit 1
fi
CLEAN="$(try_parse "$RESULT")"
if ! printf '%s' "$CLEAN" | jq -e '.skill_candidates and (.skill_candidates | type == "array")' >/dev/null 2>&1; then
  RESULT2="$(call_claude "$STRICT" "$INPUT")"
  ec2=$?
  if [ $ec2 -ne 0 ] || [ -z "$RESULT2" ]; then
    echo "strict retry failed (ec=$ec2) or empty" >&2
    exit 1
  fi
  CLEAN="$(try_parse "$RESULT2")"
fi

printf '%s' "$CLEAN" | jq -c \
  --arg sid "$SESSION_ID" \
  --arg trg "$TRIGGER" \
  --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg source_cwd "$SOURCE_CWD" \
  --arg source_project "$SOURCE_PROJECT" \
  '. + {session_id:$sid, trigger:$trg, reviewed_at:$ts, source_cwd:$source_cwd, source_project:$source_project}'
