#!/usr/bin/env bash
# distill/honcho-push.sh
# Reads distilled JSON on stdin ({honcho:[...], wiki_candidates:[...], session_id, trigger, distilled_at}).
# Pushes honcho items as a single message into the Honcho session.
#
# Endpoint: POST {baseUrl}/v3/workspaces/{ws}/sessions/{sid}/messages
#   body: {messages: [{peer_id, content, metadata}]}
# Session is ensure-created first (POST .../sessions) — 409 on conflict is fine.
#
# Fail-open: on any error, append the payload to ~/.claude/state/honcho-queue.jsonl
# for next-SessionStart retry (not implemented yet — placeholder for future).
set -uo pipefail

CFG=/root/.hermes/honcho.json
QUEUE=/root/.claude/state/honcho-queue.jsonl
mkdir -p "$(dirname "$QUEUE")" 2>/dev/null

[ -f "$CFG" ] || { echo "no honcho.json"; exit 0; }

BASE="$(jq -r '.baseUrl // empty' "$CFG" 2>/dev/null)"
WS="$(jq -r '.workspace // "seoyoon-family"' "$CFG" 2>/dev/null)"
AI_PEER="$(jq -r '.hosts.hermes.aiPeer // .aiPeer // "dungae"' "$CFG" 2>/dev/null)"
TOKEN="$(jq -r '.authToken // .apiKey // empty' "$CFG" 2>/dev/null)"

[ -n "$BASE" ] || { echo "no baseUrl"; exit 0; }

PAYLOAD="$(cat 2>/dev/null)"
[ -n "$PAYLOAD" ] || exit 0

# Extract pieces.
SID="$(printf '%s' "$PAYLOAD" | jq -r '.session_id // "unknown"')"
TRG="$(printf '%s' "$PAYLOAD" | jq -r '.trigger // "manual"')"
TS="$(printf '%s' "$PAYLOAD" | jq -r '.distilled_at // empty')"
HONCHO_FACTS="$(printf '%s' "$PAYLOAD" | jq -c '.honcho // []')"
N="$(printf '%s' "$HONCHO_FACTS" | jq 'length')"

if [ "$N" = "0" ]; then
  echo "no honcho facts to push (session=$SID)"
  exit 0
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
  --data "$(jq -nc --arg id "$SID" --arg ai "$AI_PEER" \
    '{id:$id, metadata:{source:"claude-code-distill", node:"dungae"}}')" \
  2>&1 || true

# --- Step B: POST the distilled message.
RESP="$(jq -nc \
  --arg peer "$AI_PEER" \
  --arg content "$CONTENT" \
  --argjson facts "$HONCHO_FACTS" \
  --arg sid "$SID" --arg trg "$TRG" --arg ts "$TS" \
  '{messages:[{peer_id:$peer, content:$content,
               metadata:{source:"claude-code-distill",
                         node:"dungae",
                         claude_session:$sid,
                         trigger:$trg,
                         distilled_at:$ts,
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
