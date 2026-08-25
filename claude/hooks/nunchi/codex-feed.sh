#!/usr/bin/env bash
# nunchi codex-feed extractor (#816) — Codex-provider nodes without Claude
# distill. Extracts user/agent messages from NEW codex rollout jsonl files,
# asks codex exec for distill-style facts, and ingests them into the nunchi
# peer_facts DB. Idempotent via seen-file; bounded per run. Runs from cron.
# NOTE: unlike ingest-cron.sh this costs one codex exec call per new file.
# No-op unless nunchi is enabled (state/nunchi.mode=on or CCC_NUNCHI_MODE=on).
set -uo pipefail

STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
MODE="${CCC_NUNCHI_MODE:-$(cat "$STATE/nunchi.mode" 2>/dev/null || echo off)}"
[ "$MODE" = "on" ] || exit 0

HERE="$(cd "$(dirname "$0")" && pwd)"
FM="$HERE/nunchi.py"
NUNCHI_HOME="${NUNCHI_HOME:-$HOME/.nunchi}"
SEEN="$NUNCHI_HOME/codex-seen"
LOCK="$NUNCHI_HOME/.codex-feed.lock"
SESSIONS_DIR="${CODEX_SESSIONS_DIR:-$HOME/.codex/sessions}"
MAX_FILES_PER_RUN="${NUNCHI_FEED_MAX_FILES:-3}"
# Bounded kill: TERM then SIGKILL escalation, stdin detached. An orphaned
# codex exec (Android suspension or an LMK-reaped parent) otherwise sleeps
# forever — fleet incident: 7 orphaned codex.bin processes aged 8/4..8/16
# found on daegyo 2026-08-19.
CODEX_TIMEOUT="${NUNCHI_FEED_CODEX_TIMEOUT_SEC:-300}"
CODEX_KILL_GRACE="${NUNCHI_FEED_CODEX_KILL_GRACE_SEC:-15}"
# Lane tag rides the prompt argv so the stale-lane sweep below can tell this
# lane's codex processes apart from other honcho-shaped codex calls (the
# bridge's honcho extraction uses near-identical prompt text).
LANE_TAG="nunchi-codex-feed-816"
mkdir -p "$NUNCHI_HOME"
touch "$SEEN"

PROMPT_PREFIX='다음은 AI 에이전트 작업 세션의 대화 발췌이다. 다음 세션에서도 알아야 할 사실만 추출해 strict JSON으로 답하라.
형식: {"honcho":[{"kind":"preference|decision|fact|context|correction","text":"<한 문장 한국어 사실>","subject":"user|session|node","because":"<kind=decision이면 결정 이유 한 문장 — 필수, 아니면 생략>"}]}
기준: user=사용자 선호/지시 방식, session=진행 중 작업 맥락/다음 액션, node=이 노드 사실. 잡담/디버깅만 있으면 {"honcho":[]}.
decision: 확정된 결정. 반드시 대화에 실제로 나온 이유를 because에 적어라. 대화에 이유가 없으면 decision으로 출력하지 말고 생략하며, 이유를 추측하거나 지어내지 마라.
JSON 객체 하나만 출력. 설명/마크다운 금지.

대화:
'

(
  flock -n 9 || exit 0
  # Stale-lane sweep: under this lock, any codex exec still carrying the lane
  # tag belongs to an earlier tick (this run has spawned none yet). Kill it so
  # a suspended tick cannot accumulate orphans across cron ticks.
  for pid in $(pgrep -f "codex exec.*${LANE_TAG}" 2>/dev/null || true); do
    [ "$pid" = "$$" ] && continue
    kill -9 "$pid" 2>/dev/null || true
  done
  n=0
  # oldest-first so backfill is chronological
  for f in $(find "$SESSIONS_DIR" -name "*.jsonl" -printf '%T@ %p\n' 2>/dev/null | sort -n | cut -d' ' -f2-); do
    [ "$n" -ge "$MAX_FILES_PER_RUN" ] && break
    grep -qxF "$f" "$SEEN" && continue
    convo=$(python3 - "$f" <<'PYEOF'
import json, sys
out = []
for ln in open(sys.argv[1]):
    try: d = json.loads(ln)
    except: continue
    if d.get("type") != "event_msg": continue
    p = d.get("payload", {})
    t = p.get("type")
    if t == "user_message":
        out.append("USER: " + str(p.get("message", ""))[:800])
    elif t == "agent_message":
        out.append("AGENT: " + str(p.get("message", ""))[:800])
text = "\n".join(out[-60:])          # last 60 messages
print(text[:40000])                   # byte cap
PYEOF
)
    [ ${#convo} -gt 200 ] || { echo "$f" >> "$SEEN"; continue; }
    resp_full=$(timeout -k "$CODEX_KILL_GRACE" "$CODEX_TIMEOUT" codex exec --skip-git-repo-check "${PROMPT_PREFIX}${convo}

[${LANE_TAG}]" </dev/null 2>/dev/null)
    # codex exec layout: session log ... "tokens used\n<count>\n<final message (multi-line)>"
    resp=$(python3 -c '
import sys
text = sys.stdin.read()
idx = text.rfind("tokens used")
if idx == -1:
    print(text.strip().splitlines()[-1] if text.strip() else "")
else:
    tail = text[idx:].splitlines()[2:]  # skip "tokens used" + count line
    print("\n".join(tail).strip())
' <<< "$resp_full")
    # strict JSON sanity → wrap into distill payload shape → ingest
    NUNCHI_PY="$FM" python3 - "$resp" "$f" <<'PYEOF'
import json, sys, os, subprocess
from datetime import datetime, timezone
raw, path = sys.argv[1], sys.argv[2]
try:
    d = json.loads(raw)
    items = d.get("honcho", [])
    assert isinstance(items, list)
except Exception:
    sys.exit(0)  # not strict JSON → mark seen (caller) to avoid burning calls
sid = os.path.basename(path).replace("rollout-", "").replace(".jsonl", "")
payload = {"session_id": f"codex:{sid}",
           "distilled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "decision_reason_contract": "required-v1",
           "honcho": items}
r = subprocess.run(["python3", os.environ["NUNCHI_PY"], "ingest", "-"],
                   input=json.dumps(payload), capture_output=True, text=True)
print(r.stdout.strip() or r.stderr.strip())
PYEOF
    echo "$f" >> "$SEEN"
    n=$((n+1))
  done
  python3 "$FM" snapshot --limit 25 >/dev/null 2>&1 || true
) 9>"$LOCK"
