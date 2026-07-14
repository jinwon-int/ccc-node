#!/usr/bin/env bash
# distill/queue-drain.sh — SessionStart drain worker for honcho-queue.jsonl.
#
# Reads up to MAX_BATCH lines from the queue (failed Honcho pushes), retries
# each, drops on success, increments attempt counter on failure (in-band
# `_attempts` metadata), and after MAX_ATTEMPTS moves to `.dead` for manual
# review.
#
# Fail-open everywhere — never blocks SessionStart.
# Single-flight via flock so concurrent SessionStarts don't race.
# Run with `CLAUDE_DISTILL_INFLIGHT=1` from the SessionStart hook to bypass
# load-memory/load-tools/checkpoint/etc. and avoid recursion.
set -uo pipefail

# Distill subprocess guard (defensive — SessionStart should already have it set).
[ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ] || export CLAUDE_DISTILL_INFLIGHT=1

STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
LOG="$STATE_DIR/distill.log"
QUEUE="$STATE_DIR/honcho-queue.jsonl"
WORK="$STATE_DIR/.honcho-queue.inflight"
DEAD="$STATE_DIR/honcho-queue.jsonl.dead"
CFG="${CCC_HONCHO_CFG:-${HOME:-/root}/.hermes/honcho.json}"
QUEUE_LOCK="$STATE_DIR/.honcho-queue.lock"
DRAIN_LOCK="$STATE_DIR/.honcho-queue-drain.lock"

MAX_BATCH="${CCC_DISTILL_DRAIN_BATCH:-20}"
MAX_ATTEMPTS="${CCC_DISTILL_DRAIN_MAX_ATTEMPTS:-3}"

mkdir -p "$STATE_DIR" 2>/dev/null
log() { printf '%s [drain] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG" 2>/dev/null; }

# ---- single-flight + early exits ------------------------------------------
# Keep drain single-flight separate from the short producer/queue mutation
# lock. The drain lock may span network I/O; the queue lock never does.
exec 8>"$DRAIN_LOCK"
flock -n 8 || { log "skip reason=lock-held"; exit 0; }

[ -s "$WORK" ] || [ -s "$QUEUE" ] || exit 0
[ -f "$CFG" ] || { log "skip reason=no-honcho-cfg"; exit 0; }

# Off-switch respected (no point draining if user disabled distill entirely).
if [ -f "$STATE_DIR/distill.disabled" ]; then
  log "skip reason=distill-disabled"
  exit 0
fi

# ---- Honcho config --------------------------------------------------------
BASE="$(jq -r '.baseUrl // empty' "$CFG" 2>/dev/null)"
WS="$(jq -r '.workspace // "seoyoon-family"' "$CFG" 2>/dev/null)"
AI_PEER="$(jq -r '(.hosts|objects|.hermes.aiPeer) // .aiPeer // "family-assistant"' "$CFG" 2>/dev/null)"  # objects: tolerate legacy hosts:[] array
TOKEN="$(jq -r '.authToken // .apiKey // empty' "$CFG" 2>/dev/null)"
case "$BASE" in "<"*">") BASE="" ;; esac  # unfilled seed placeholder => unconfigured
[ -n "$BASE" ] || { log "skip reason=no-baseUrl"; exit 0; }

AUTH=()
[ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")

# Origin node label for replay traceability. AI_PEER is the target Honcho peer;
# NODE is the source node that produced/replayed the distill facts.
NODE="${CCC_NODE:-}"
[ -z "$NODE" ] && [ -r "$STATE_DIR/node.txt" ] && NODE="$(head -1 "$STATE_DIR/node.txt" 2>/dev/null)"
[ -z "$NODE" ] && NODE="$(hostname -s 2>/dev/null || echo ccc-node)"

# Quick /health probe — if Honcho is down, leave the queue intact and try
# next SessionStart. No point burning retry attempts against a known-down host.
HEALTH_HTTP="$(curl -sS -m 3 -o /dev/null -w '%{http_code}' "$BASE/health" 2>/dev/null)"
case "$HEALTH_HTTP" in
  200|204) ;;
  *) log "skip reason=honcho-health http=$HEALTH_HTTP"; exit 0 ;;
esac

# ---- durable queue handoff ------------------------------------------------
# A leftover worklist means a prior drain crashed after the atomic handoff.
# Reprocess it at-least-once. Otherwise, move the active queue to the durable
# private worklist while holding the same short lock used by producers.
if [ ! -s "$WORK" ]; then
  rm -f "$WORK" 2>/dev/null || true
  (
    flock 9 || exit 1
    [ -s "$QUEUE" ] || exit 2
    mv "$QUEUE" "$WORK" || exit 1
    : > "$QUEUE" || exit 1
  ) 9>"$QUEUE_LOCK"
  handoff_rc=$?
  case "$handoff_rc" in
    0) ;;
    2) exit 0 ;;
    *) log "skip reason=queue-handoff-failed"; exit 0 ;;
  esac
fi

# ---- drain loop -----------------------------------------------------------
TMP="$(mktemp "$STATE_DIR/.honcho-queue.XXXXXX.tmp")"
DRAINED=0; FAILED=0; DROPPED=0; PROCESSED=0

# Process up to MAX_BATCH lines; keep the rest in $TMP.
while IFS= read -r line; do
  [ -z "$line" ] && continue
  if [ "$PROCESSED" -ge "$MAX_BATCH" ]; then
    # Beyond batch — keep verbatim for next run.
    printf '%s\n' "$line" >> "$TMP"
    continue
  fi
  PROCESSED=$((PROCESSED + 1))

  # Parse + read attempts counter (default 0).
  attempts="$(printf '%s' "$line" | jq -r '._attempts // 0' 2>/dev/null)"
  [ -z "$attempts" ] && attempts=0

  # If already at the attempt cap, mark dead and skip.
  if [ "$attempts" -ge "$MAX_ATTEMPTS" ]; then
    printf '%s\n' "$line" >> "$DEAD"
    DROPPED=$((DROPPED + 1))
    continue
  fi

  # Extract pieces (mirrors honcho-push.sh).
  sid="$(printf '%s' "$line" | jq -r '.session_id // "unknown"')"
  trg="$(printf '%s' "$line" | jq -r '.trigger // "manual"')"
  ts="$(printf '%s' "$line" | jq -r '.distilled_at // empty')"
  facts="$(printf '%s' "$line" | jq -c '.honcho // []')"
  [ "$(printf '%s' "$facts" | jq 'length')" = "0" ] && {
    # Empty facts — nothing to re-push; drop quietly.
    DROPPED=$((DROPPED + 1))
    continue
  }

  content="$(printf '%s' "$facts" | jq -r --arg s "$sid" --arg t "$trg" '
    "[distill trigger=\($t) session=\($s) (replayed)]\n" +
    (map("- (\(.kind // "fact")) \(.text // "")") | join("\n"))
  ' 2>/dev/null)"
  [ "${#content}" -gt 24000 ] && content="${content:0:24000}...[truncated]"

  # Ensure session (idempotent on Honcho side).
  curl -sS -m 8 -o /dev/null \
    -X POST "$BASE/v3/workspaces/$WS/sessions" \
    -H "Content-Type: application/json" \
    "${AUTH[@]}" \
    --data "$(jq -nc --arg id "$sid" --arg node "$NODE" \
      '{id:$id, metadata:{source:"claude-code-distill", node:$node, replay:true}}')" \
    >/dev/null 2>&1 || true

  # POST the message.
  http="$(jq -nc \
    --arg peer "$AI_PEER" --arg content "$content" --arg node "$NODE" \
    --argjson facts "$facts" \
    --arg sid "$sid" --arg trg "$trg" --arg ts "$ts" \
    '{messages:[{peer_id:$peer, content:$content,
                 metadata:{source:"claude-code-distill",
                           node:$node,
                           claude_session:$sid,
                           trigger:$trg,
                           distilled_at:$ts,
                           replay:true,
                           facts:$facts}}]}' \
    | curl -sS -m 10 -o /dev/null -w '%{http_code}' \
      -X POST "$BASE/v3/workspaces/$WS/sessions/$sid/messages" \
      -H "Content-Type: application/json" \
      "${AUTH[@]}" \
      --data-binary @-)"

  case "$http" in
    200|201|202)
      DRAINED=$((DRAINED + 1))
      # success — line is consumed, do NOT requeue
      ;;
    *)
      FAILED=$((FAILED + 1))
      # Increment attempts and keep for next round.
      printf '%s' "$line" | jq -c --argjson n "$((attempts + 1))" '. + {_attempts:$n}' >> "$TMP" 2>/dev/null
      ;;
  esac
done < "$WORK"

# Merge survivors ahead of any payloads appended to the new active queue while
# this worklist was being processed. Remove WORK only after the active queue is
# durable; a crash before then intentionally replays WORK at-least-once.
(
  flock 9 || exit 1
  if [ -s "$TMP" ]; then
    [ ! -s "$QUEUE" ] || cat "$QUEUE" >> "$TMP" || exit 1
    mv "$TMP" "$QUEUE" || exit 1
  else
    rm -f "$TMP" 2>/dev/null || true
  fi
  rm -f "$WORK" || exit 1
) 9>"$QUEUE_LOCK"
finalize_rc=$?
if [ "$finalize_rc" -ne 0 ]; then
  log "skip reason=queue-finalize-failed worklist=$WORK"
  exit 0
fi

log "drained ok=$DRAINED failed=$FAILED dropped=$DROPPED processed=$PROCESSED"
exit 0
