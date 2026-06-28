#!/usr/bin/env bash
# distill/honcho-push.sh
# Reads distilled JSON on stdin ({honcho:[...], wiki_candidates:[...], session_id, trigger, distilled_at}).
# Pushes honcho items as a single message into the Honcho session.
#
# Endpoint: POST {baseUrl}/v3/workspaces/{ws}/sessions/{sid}/messages
#   body: {messages: [{peer_id, content, metadata}]}
# Session is ensure-created first (POST .../sessions) — 409 on conflict is fine.
#
# Fail-open: on any push/reachability error, append the payload to
# ~/.claude/state/honcho-queue.jsonl for next-SessionStart retry.
set -uo pipefail

CFG="${CCC_HONCHO_CFG:-${HOME:-/root}/.hermes/honcho.json}"
STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
QUEUE="$STATE_DIR/honcho-queue.jsonl"
mkdir -p "$STATE_DIR" 2>/dev/null

[ -f "$CFG" ] || { echo "no honcho.json"; exit 0; }

BASE="$(jq -r '.baseUrl // .hosts.hermes.baseUrl // empty' "$CFG" 2>/dev/null)"
WS="$(jq -r '.workspace // .hosts.hermes.workspace // "seoyoon-family"' "$CFG" 2>/dev/null)"
AI_PEER="$(jq -r '.hosts.hermes.aiPeer // .aiPeer // "dungae"' "$CFG" 2>/dev/null)"
TOKEN="$(jq -r '.authToken // .apiKey // .hosts.hermes.apiKey // empty' "$CFG" 2>/dev/null)"

# Origin node label for traceability across fleet rollout. This is separate
# from AI_PEER (the Honcho peer id) and must not be hard-coded to the original
# development node.
NODE="${CCC_NODE:-}"
[ -z "$NODE" ] && [ -r "$STATE_DIR/node.txt" ] && NODE="$(head -1 "$STATE_DIR/node.txt" 2>/dev/null)"
[ -z "$NODE" ] && NODE="$(hostname -s 2>/dev/null || echo ccc-node)"

[ -n "$BASE" ] || { echo "no baseUrl"; exit 0; }

PAYLOAD="$(cat 2>/dev/null)"
[ -n "$PAYLOAD" ] || exit 0

# Extract pieces.
SID="$(printf '%s' "$PAYLOAD" | jq -r '.session_id // "unknown"')"
TRG="$(printf '%s' "$PAYLOAD" | jq -r '.trigger // "manual"')"
TS="$(printf '%s' "$PAYLOAD" | jq -r '.distilled_at // empty')"
SOURCE_CWD="$(printf '%s' "$PAYLOAD" | jq -r '.source_cwd // empty')"
SOURCE_PROJECT="$(printf '%s' "$PAYLOAD" | jq -r '.source_project // empty')"
HONCHO_FACTS="$(printf '%s' "$PAYLOAD" | jq -c '.honcho // []')"
N="$(printf '%s' "$HONCHO_FACTS" | jq 'length')"

if [ "$N" = "0" ]; then
  echo "no honcho facts to push (session=$SID)"
  exit 0
fi

# Pre-flight Honcho liveness before any push. If Honcho is down, persist the
# payload for later SessionStart drain without burning queue-drain retry attempts.
HEALTH_HTTP="$(curl -sS -m "${CCC_HONCHO_HEALTH_TIMEOUT:-3}" -o /dev/null -w "%{http_code}" "$BASE/health" 2>/dev/null || true)"
if ! printf '%s' "$HEALTH_HTTP" | grep -Eq '^(200|204)$'; then
  printf '%s\n' "$PAYLOAD" >> "$QUEUE" 2>/dev/null || true
  echo "honcho /health probe failed http=${HEALTH_HTTP:-000} session=$SID facts=$N; queued for retry"
  exit 1
fi

# Build a single human-readable message body summarizing the facts.
# Honcho's dialectic engine will reason over it on recall.
CONTENT="$(printf '%s' "$HONCHO_FACTS" | jq -r --arg sid "$SID" --arg trg "$TRG" '
  "[distill trigger=\($trg) session=\($sid)]\n" +
  (map("- (\(.kind // "fact")) \(.text // "")") | join("\n"))
' 2>/dev/null)"

# Cap content (Honcho MessageCreate.content maxLength = 25000).
if [ "${#CONTENT}" -gt 24000 ]; then
  CONTENT="${CONTENT:0:24000}...[truncated]"
fi

# Build curl auth args (only if token present).
AUTH=()
[ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")

# --- Step A: ensure session exists (upsert; 409/422 etc treated as OK).
curl -sS -m 8 -o /dev/null -w "ensure-session http=%{http_code}\n" \
  -X POST "$BASE/v3/workspaces/$WS/sessions" \
  -H "Content-Type: application/json" \
  "${AUTH[@]}" \
  --data "$(jq -nc --arg id "$SID" --arg ai "$AI_PEER" --arg node "$NODE" \
    --arg source_cwd "$SOURCE_CWD" --arg source_project "$SOURCE_PROJECT" \
    '{id:$id, metadata:{source:"claude-code-distill", node:$node, source_cwd:$source_cwd, source_project:$source_project}}')" \
  2>&1 || true

# --- Step B: POST the distilled message.
RESP="$(jq -nc \
  --arg peer "$AI_PEER" \
  --arg node "$NODE" \
  --arg content "$CONTENT" \
  --argjson facts "$HONCHO_FACTS" \
  --arg sid "$SID" --arg trg "$TRG" --arg ts "$TS" \
  --arg source_cwd "$SOURCE_CWD" --arg source_project "$SOURCE_PROJECT" \
  '{messages:[{peer_id:$peer, content:$content,
               metadata:{source:"claude-code-distill",
                         node:$node,
                         claude_session:$sid,
                         trigger:$trg,
                         distilled_at:$ts,
                         source_cwd:$source_cwd,
                         source_project:$source_project,
                         facts:$facts}}]}' \
  | curl -sS -m 10 -w "\n__HTTP__%{http_code}__" \
    -X POST "$BASE/v3/workspaces/$WS/sessions/$SID/messages" \
    -H "Content-Type: application/json" \
    "${AUTH[@]}" \
    --data-binary @-)"

HTTP="$(printf '%s' "$RESP" | sed -n 's/.*__HTTP__\([0-9]*\)__.*/\1/p')"
BODY="$(printf '%s' "$RESP" | sed 's/__HTTP__[0-9]*__$//')"

if [ "$HTTP" = "200" ] || [ "$HTTP" = "201" ] || [ "$HTTP" = "202" ]; then
  echo "honcho push ok http=$HTTP session=$SID facts=$N"
else
  echo "honcho push failed http=$HTTP session=$SID facts=$N"
  echo "body=$(printf '%s' "$BODY" | head -c 400)"
  # Queue for retry (best-effort).
  printf '%s\n' "$PAYLOAD" >> "$QUEUE" 2>/dev/null
  exit 1
fi
