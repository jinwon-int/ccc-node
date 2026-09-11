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
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-${HOME:-/root}/.claude/skills}"
MAX_TURNS="${CCC_SKILL_REVIEW_MAX_TURNS:-80}"
MAX_BYTES="${CCC_SKILL_REVIEW_MAX_BYTES:-60000}"
MODEL="${CCC_SKILL_REVIEW_MODEL:-haiku}"
TIMEOUT="${CCC_SKILL_REVIEW_TIMEOUT:-180}"

# #1654: provider-neutral drafting brain. When CCC_SKILL_REVIEW_LLM_CMD is set
# (the same variable the promotion autorepair path already reads) it REPLACES
# the claude CLI: shlex-split the command, feed the prompt on stdin, read the
# JSON response on stdout. This lets piri/codex/non-Claude nodes draft without
# any Anthropic tooling. Unset keeps the historical `claude -p` flow unchanged.
LLM_CMD=()
if [ -n "${CCC_SKILL_REVIEW_LLM_CMD:-}" ]; then
  while IFS= read -r _llm_tok; do
    [ -n "$_llm_tok" ] && LLM_CMD+=("$_llm_tok")
  done < <(python3 -c 'import shlex, sys
try:
    tokens = shlex.split(sys.argv[1])
except ValueError:
    sys.exit(3)
for token in tokens:
    print(token)' "$CCC_SKILL_REVIEW_LLM_CMD" 2>/dev/null) || {
    echo "invalid CCC_SKILL_REVIEW_LLM_CMD (shlex parse failed)" >&2
    exit 2
  }
  if [ "${#LLM_CMD[@]}" -eq 0 ]; then
    echo "empty CCC_SKILL_REVIEW_LLM_CMD" >&2
    exit 2
  fi
fi

# zai fallback config (node-local opt-in, added 2026-09-05). Fail-open: without
# a 0600 regular env file the pipeline is identical to the haiku-only flow.
# Set CCC_SKILL_REVIEW_ZAI_ENV=off (or point at another file) to control.
ZAI_ENV_FILE="${CCC_SKILL_REVIEW_ZAI_ENV:-${HOME:-/root}/.claude/state/skill-review.zai.env}"
if [ "${CCC_SKILL_REVIEW_ZAI_ENV:-}" != "off" ] && [ -f "$ZAI_ENV_FILE" ] && [ ! -L "$ZAI_ENV_FILE" ]; then
  zai_perms="$(stat -c '%a' "$ZAI_ENV_FILE" 2>/dev/null || printf '?')"
  if [ "$zai_perms" = "600" ]; then
    # shellcheck disable=SC1090
    . "$ZAI_ENV_FILE"
  else
    echo "zai fallback disabled: $ZAI_ENV_FILE perms $zai_perms (want 600)" >&2
  fi
fi

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
    -e 's/(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}/\1[REDACTED:api-key]/g' \
    -e 's/AKIA[A-Z0-9]{16}/[REDACTED:aws-key]/g' \
    -e 's/-----BEGIN [A-Z ]*PRIVATE KEY-----/[REDACTED:pem-begin]/g' \
    -e 's/Bearer [A-Za-z0-9._-]{20,}/Bearer [REDACTED]/g' \
    -e 's/((password|passwd|secret|token|api[_-]?key|authorization)[=:[:space:]"'"'"']+)[^[:space:]"'"'"'&|;]+/\1[REDACTED]/gI')"
  # Measure BYTES, not characters (same fix as distill/extract.sh): the
  # character comparison let Korean-heavy content grow ~3x past the budget.
  local byte_len
  byte_len="$(printf '%s' "$redacted" | wc -c | tr -d '[:space:]')"
  if [ "$byte_len" -gt "$max_bytes" ]; then
    redacted="...[truncated $((byte_len - max_bytes)) bytes]...
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

PROVIDER="${CCC_SKILL_PROVIDER:-claude}"
PROMPT="$(cat <<EOF
You are the Hermes-style skill self-improvement reviewer for a coding agent node.
You will receive a redacted session transcript and a list of existing skills.
Return STRICT JSON only.

Goal: propose reusable skills worth staging for human approval.

Schema:
{
  "skill_candidates": [
    {
      "name": "lowercase-kebab-name",
      "category": "$PROVIDER",
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
- Frame commands as generic agent-CLI / ccc-node procedures. Use exact commands only if the transcript clearly showed them; otherwise describe the safe decision rule instead of inventing flags.
- Do NOT hard-code a runtime coupling in the skill body: never write 'claude -p', 'codex exec', '~/.claude/', '~/.codex/', 'CLAUDE_*', or 'CODEX_*'. Refer to 'this node's agent CLI' or a neutral tool name so the draft installs on any provider.

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
  # CLAUDE_SKILL_REVIEW_BG belongs only to the detached runner. Passing it to
  # the provider child makes that child's SessionEnd hook start another runner.
  #
  # This autonomous pass only produces JSON text; every filesystem mutation is
  # performed later by the fixed, machine-gated installer. Keep the model
  # runtime tool-free even when the parent environment or user settings are
  # permissive. `--allowedTools` is not a restriction (it only pre-approves),
  # so hide every built-in tool, deny inherited MCP tools/config, and make any
  # unexpected permission request fail instead of prompting.
  printf '%s' "$input" | env -u CLAUDE_SKILL_REVIEW_BG -u CCC_ALLOWED_TOOLS \
    timeout "$TIMEOUT" claude -p \
    --tools "" \
    --disallowedTools "mcp__*" \
    --strict-mcp-config \
    --permission-mode dontAsk \
    --model "$MODEL" \
    --no-session-persistence \
    --output-format text \
    --append-system-prompt "$sys" \
    2>/dev/null
}

# #1654: neutral provider path — stdin prompt, stdout JSON. Same recursion/
# tool hygiene as call_claude: the child only produces text.
call_llm_cmd() {
  local input="$1"
  printf '%s' "$input" | env -u CLAUDE_SKILL_REVIEW_BG -u CCC_ALLOWED_TOOLS \
    timeout "$TIMEOUT" "${LLM_CMD[@]}" \
    2>/dev/null
}

zai_fallback() {
  # Last-resort provider (node-local opt-in): zai GLM over the
  # Anthropic-compatible endpoint via direct curl. Independent of the claude
  # CLI on purpose — a broken/quota-dead CLI must not take this path down too.
  # The bearer token travels via curl config-on-stdin, never argv/process list.
  local input="$1" payload body http curl_rc jq_rc
  if [ -z "${CCC_ZAI_API_KEY:-}" ] || [ -z "${CCC_ZAI_BASE_URL:-}" ]; then
    echo "zai fallback unavailable: env file not loaded" >&2
    return 99
  fi
  payload="$(jq -cn \
    --arg model "${CCC_ZAI_MODEL:-glm-5.3-flash}" \
    --arg system "$STRICT" \
    --arg input "$input" \
    '{model:$model, max_tokens:4096, thinking:{type:"disabled"}, system:$system,
      messages:[{role:"user", content:$input}]}')" || return 1
  body="$(mktemp "${TMPDIR:-/tmp}/zai-extract.XXXXXX")" || return 1
  http="$(curl -sS --max-time "$TIMEOUT" -o "$body" -w '%{http_code}' \
    -H 'content-type: application/json' \
    --config - "${CCC_ZAI_BASE_URL%/}/v1/messages" \
    -d "$payload" <<CFG 2>/dev/null
header = "Authorization: Bearer ${CCC_ZAI_API_KEY}"
header = "anthropic-version: 2023-06-01"
CFG
)"
  curl_rc=$?
  case "${http:-}" in 2*) : ;; *) curl_rc=1 ;; esac
  if [ "$curl_rc" -ne 0 ]; then
    echo "zai fallback failed http=${http:-none}" >&2
    rm -f "$body"
    return 1
  fi
  jq -r '(.content // []) | map(select(.type == "text") | .text) | join("")' "$body" 2>/dev/null
  jq_rc=$?
  rm -f "$body"
  return "$jq_rc"
}

build_input() {
  printf '%s\n\n--- existing skills ---\n%s\n\n--- transcript metadata ---\nsession=%s trigger=%s source_cwd=%s source_project=%s\n\n--- redacted transcript ---\n%s\n' \
    "$PROMPT" "$EXISTING" "$SESSION_ID" "$TRIGGER" "$SOURCE_CWD" "$SOURCE_PROJECT" "$REDACTED"
}

try_parse() { printf '%s' "$1" | sed -E '/^[[:space:]]*```/d'; }

valid_candidates() { printf '%s' "$1" | jq -e '.skill_candidates and (.skill_candidates | type == "array")' >/dev/null 2>&1; }

emit() {
  printf '%s' "$1" | jq -c \
    --arg sid "$SESSION_ID" \
    --arg trg "$TRIGGER" \
    --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg source_cwd "$SOURCE_CWD" \
    --arg source_project "$SOURCE_PROJECT" \
    '. + {session_id:$sid, trigger:$trg, reviewed_at:$ts, source_cwd:$source_cwd, source_project:$source_project}'
}

INPUT="$(build_input)"
CLEAN=""

if [ "${#LLM_CMD[@]}" -gt 0 ]; then
  # Provider-neutral path (#1654): the configured LLM command replaces the
  # claude CLI entirely. One normal attempt, one retry with the strict reminder
  # appended, then the shared zai last-resort below.
  RESULT="$(call_llm_cmd "$INPUT")"
  ec=$?
  if [ "$ec" -ne 0 ] || [ -z "$RESULT" ]; then
    echo "LLM cmd attempt failed (ec=$ec) or empty; retrying with strict reminder" >&2
  else
    CLEAN="$(try_parse "$RESULT")"
  fi
  if ! valid_candidates "$CLEAN"; then
    strict_input="$(printf '%s\n%s' "$INPUT" "$STRICT")"
    RESULT2="$(call_llm_cmd "$strict_input")"
    ec2=$?
    if [ "$ec2" -ne 0 ] || [ -z "$RESULT2" ]; then
      echo "strict retry failed (ec=$ec2) or empty" >&2
      CLEAN=""
    else
      CLEAN="$(try_parse "$RESULT2")"
    fi
  fi
else
  RESULT="$(call_claude "$SYSTEM_CONSTRAINT" "$INPUT")"
  ec=$?
  if [ "$ec" -ne 0 ] || [ -z "$RESULT" ]; then
    echo "claude -p attempt failed (ec=$ec) or empty; skipping strict retry" >&2
  else
    CLEAN="$(try_parse "$RESULT")"
    if ! valid_candidates "$CLEAN"; then
      RESULT2="$(call_claude "$STRICT" "$INPUT")"
      ec2=$?
      if [ "$ec2" -ne 0 ] || [ -z "$RESULT2" ]; then
        echo "strict retry failed (ec=$ec2) or empty" >&2
        CLEAN=""
      else
        CLEAN="$(try_parse "$RESULT2")"
      fi
    fi
  fi
fi

if ! valid_candidates "$CLEAN"; then
  echo "haiku path produced no valid JSON; trying zai fallback" >&2
  ZAI_OUT="$(zai_fallback "$INPUT")"
  zrc=$?
  if [ "$zrc" -eq 0 ] && [ -n "$ZAI_OUT" ]; then
    CLEAN="$(try_parse "$ZAI_OUT")"
  fi
fi

if ! valid_candidates "$CLEAN"; then
  echo "all extract attempts failed" >&2
  exit 1
fi
emit "$CLEAN"
