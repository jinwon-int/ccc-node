#!/usr/bin/env bash
# converge-distill-peer.sh
# ---------------------------------------------------------------------------
# Fleet convergence helper: make this node's distill author peer resolve to the
# single shared, harness-neutral peer id `family-assistant` (ccc-node PR #327).
#
# Background: distill messages were historically scattered across several author
# peers (hermes / dungae / soonwook / nosuk) because nodes carried an explicit
# `aiPeer` override in ~/.hermes/honcho.json, or fell through to an old hardcoded
# fallback. The distill hooks now default to `family-assistant`, so a node with
# NO explicit override converges automatically after `setup.sh`. This script
# fixes the remaining case: an explicit `aiPeer` (or `.hosts.hermes.aiPeer`) that
# is not yet `family-assistant`.
#
# Idempotent. Read-only by default (--check). Never prints secrets.
#
# Usage:
#   scripts/converge-distill-peer.sh            # --check (read-only report)
#   scripts/converge-distill-peer.sh --apply    # rewrite aiPeer -> family-assistant (backs up first)
#   scripts/converge-distill-peer.sh --verify    # live: push a labelled canary + confirm author peer
#
# Exit codes: 0 ok/converged/no-op, 1 usage/error, 2 apply needed (in --check).
# ---------------------------------------------------------------------------
set -uo pipefail

TARGET_PEER="family-assistant"
CFG="${CCC_HONCHO_CFG:-${HOME:-/root}/.hermes/honcho.json}"
MODE="check"
case "${1:-}" in
  ""|--check) MODE="check" ;;
  --apply)    MODE="apply" ;;
  --verify)   MODE="verify" ;;
  -h|--help)  sed -n '2,26p' "$0"; exit 0 ;;
  *) echo "unknown arg: $1 (use --check|--apply|--verify)"; exit 1 ;;
esac

command -v jq >/dev/null 2>&1 || { echo "FATAL: jq not found"; exit 1; }

if [ ! -f "$CFG" ]; then
  echo "node=$(hostname -s) cfg=$CFG status=absent — distill push is off on this node; nothing to converge."
  exit 0
fi
if ! jq -e . "$CFG" >/dev/null 2>&1; then
  echo "FATAL: $CFG is not valid JSON"; exit 1
fi

# Resolve current author peer the SAME way the distill hooks do:
#   .hosts.hermes.aiPeer  (nested, higher precedence)  //  .aiPeer  (flat)
NESTED="$(jq -r '(.hosts|objects|.hermes.aiPeer) // empty' "$CFG" 2>/dev/null)"  # objects: tolerate legacy hosts:[] array
FLAT="$(jq -r '(.aiPeer) // empty' "$CFG" 2>/dev/null)"
if [ -n "$NESTED" ]; then CUR="$NESTED"; FIELD=".hosts.hermes.aiPeer";
elif [ -n "$FLAT" ]; then CUR="$FLAT"; FIELD=".aiPeer";
else CUR=""; FIELD="(none — uses hook fallback)"; fi

NODE="$(hostname -s 2>/dev/null || echo node)"
echo "node=$NODE cfg=$CFG current_aiPeer='${CUR:-<unset>}' resolved_field=$FIELD target='$TARGET_PEER'"

# Already converged? (explicit target, or unset -> relies on the new fallback)
if [ "$CUR" = "$TARGET_PEER" ]; then
  echo "status=converged — aiPeer already '$TARGET_PEER'. no-op."; exit 0
fi
if [ -z "$CUR" ]; then
  echo "status=fallback — no explicit aiPeer; distill hook fallback now yields '$TARGET_PEER'. no config edit needed."
  [ "$MODE" = "verify" ] || exit 0
fi

if [ "$MODE" = "check" ]; then
  echo "status=needs-apply — explicit aiPeer '$CUR' != '$TARGET_PEER'. Run with --apply to converge."
  exit 2
fi

# --- apply -----------------------------------------------------------------
if [ "$MODE" = "apply" ] || { [ "$MODE" = "verify" ] && [ -n "$CUR" ] && [ "$CUR" != "$TARGET_PEER" ]; }; then
  TS="$(date +%Y%m%d-%H%M%S 2>/dev/null || echo ts)"
  BAK="${CFG}.bak-${TS}"
  cp -p "$CFG" "$BAK" || { echo "FATAL: backup failed"; exit 1; }
  TMP="$(mktemp "${CFG}.XXXXXX")"
  # Write to whichever field currently holds the explicit override.
  if [ "$FIELD" = ".hosts.hermes.aiPeer" ]; then
    jq --arg p "$TARGET_PEER" '.hosts.hermes.aiPeer=$p' "$CFG" > "$TMP"
  else
    jq --arg p "$TARGET_PEER" '.aiPeer=$p' "$CFG" > "$TMP"
  fi
  if jq -e . "$TMP" >/dev/null 2>&1; then
    mv "$TMP" "$CFG"
    echo "status=applied — $FIELD '$CUR' -> '$TARGET_PEER' (backup: $BAK)"
  else
    rm -f "$TMP"; echo "FATAL: produced invalid JSON, aborted (original intact)"; exit 1
  fi
fi

# --- verify (live canary) ---------------------------------------------------
if [ "$MODE" = "verify" ]; then
  BASE="$(jq -r 'def nz(x): x|select(.!=null and .!=""); nz(.baseUrl)//nz(.hosts|objects|.hermes.baseUrl)//empty' "$CFG")"
  WS="$(jq -r 'def nz(x): x|select(.!=null and .!=""); nz(.workspace)//nz(.hosts|objects|.hermes.workspace)//"seoyoon-family"' "$CFG")"
  TOKEN="$(jq -r 'def nz(x): x|select(.!=null and .!=""); nz(.authToken)//nz(.apiKey)//nz(.hosts|objects|.hermes.apiKey)//empty' "$CFG")"
  [ -n "$BASE" ] || { echo "verify: no baseUrl in cfg; skip live check"; exit 0; }
  SID="converge-verify-${NODE}-$(date +%Y%m%d 2>/dev/null || echo d)"
  AUTH=(); [ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")
  curl -sS -m 8 -o /dev/null "${AUTH[@]}" -H 'Content-Type: application/json' \
    -X POST "$BASE/v3/workspaces/$WS/sessions" \
    --data "$(jq -nc --arg id "$SID" '{id:$id, metadata:{source:"converge-verify"}}')" 2>/dev/null || true
  curl -sS -m 10 -o /dev/null -w "verify-push http=%{http_code}\n" "${AUTH[@]}" -H 'Content-Type: application/json' \
    -X POST "$BASE/v3/workspaces/$WS/sessions/$SID/messages" \
    --data "$(jq -nc --arg p "$TARGET_PEER" --arg n "$NODE" \
      '{messages:[{peer_id:$p, content:"[converge-verify] distill author peer check", metadata:{source:"claude-code-distill", node:$n, canary:true}}]}')"
  echo "verify: posted canary as peer_id='$TARGET_PEER' node='$NODE' session='$SID'."
  echo "verify: confirm in Honcho that this session's message author == '$TARGET_PEER'."
fi
exit 0
