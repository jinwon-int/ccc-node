#!/usr/bin/env bash
# nunchi codex-feed extractor (#816) — Codex-provider nodes without Claude
# distill. Extracts user/agent messages from new or growing rollout jsonl files,
# asks codex exec for distill-style facts, and ingests them into the nunchi
# peer_facts DB. Idempotent via fingerprint receipts; bounded per run. Runs from cron.
# NOTE: unlike ingest-cron.sh this costs one codex exec call per new file.
# No-op unless nunchi is enabled (state/nunchi.mode=on or CCC_NUNCHI_MODE=on).
set -uo pipefail
umask 077
# Reconsider receipts written by the event-only reader.
export NUNCHI_FEED_READER_VERSION=2

STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
MODE="${CCC_NUNCHI_MODE:-$(cat "$STATE/nunchi.mode" 2>/dev/null || echo off)}"
[ "$MODE" = "on" ] || exit 0

HERE="$(cd "$(dirname "$0")" && pwd)"
FM="$HERE/nunchi.py"
RECEIPTS="$HERE/feed-receipt.py"
# shellcheck source=claude/hooks/nunchi/feed-common.sh
. "$HERE/feed-common.sh" 2>/dev/null || {
  echo "${0##*/}: feed-common.sh missing beside this feed — the harness is only partially deployed; re-run setup.sh. Refusing to run rather than tick without ingesting (#1698)." >&2
  exit 2
}
NUNCHI_HOME="${NUNCHI_HOME:-$HOME/.nunchi}"
SEEN="$NUNCHI_HOME/codex-seen"
RECEIPT_FILE="$NUNCHI_HOME/codex-receipts.jsonl"
LOCK="$NUNCHI_HOME/.codex-feed.lock"
SESSIONS_DIR="${CODEX_SESSIONS_DIR:-$HOME/.codex/sessions}"
MAX_FILES_PER_RUN="${NUNCHI_FEED_MAX_FILES:-3}"
case "$MAX_FILES_PER_RUN" in
  ''|*[!0-9]*) MAX_FILES_PER_RUN=3 ;;
  *) [ "$MAX_FILES_PER_RUN" -ge 1 ] && [ "$MAX_FILES_PER_RUN" -le 20 ] || MAX_FILES_PER_RUN=3 ;;
esac
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
# Hold an owner-private no-follow lock for the complete read/modify/write run.
# --locked is only the internal child; cron invokes this script without args.
if [ "${1:-}" != --locked ]; then
  exec python3 "$RECEIPTS" run-locked "$LOCK" bash "$0" --locked
fi

PROMPT_PREFIX='다음은 AI 에이전트 작업 세션의 대화 발췌이다. 다음 세션에서도 알아야 할 사실만 추출해 strict JSON으로 답하라.
형식: {"honcho":[{"kind":"preference|decision|observation|context|constraint|task-progress|procedure|fact|correction","text":"<한 문장 한국어 사실>","subject":"user|session|node","because":"<kind=decision이면 결정 이유 한 문장 — 필수, 아니면 생략>"}]}
기준: user=사용자 선호/지시 방식, session=진행 중 작업 맥락/다음 액션, node=이 노드 사실. 잡담/디버깅만 있으면 {"honcho":[]}.
kind 정의 — 가장 좁게 맞는 하나만 고르고 애매한 것을 decision으로 흘리지 마라:
  preference=일하는 방식에 대한 사용자의 지속적 선호.
  decision=실제로 내려져 확정된 선택. because 필수.
  task-progress=진행/완료 보고. 결정이 아니다.
  correction=이전 진술이 틀렸음을 바로잡는 정정.
  procedure=반복 사용 가능한 여러 단계의 절차/방법.
  fact=지금 관찰된 사실 진술.
  observation=사용자/세션의 행동에 대한 관계적 관찰.
  context=다음 세션이 이어받아야 할 진행 중 배경.
  constraint=사용자가 말한 상시 금지/필수 규칙.
decision: 확정된 결정. 반드시 대화에 실제로 나온 이유를 because에 적어라. 대화에 이유가 없으면 decision으로 출력하지 말고 생략하며, 이유를 추측하거나 지어내지 마라.
JSON 객체 하나만 출력. 설명/마크다운 금지.

대화:
'

(
  # Stale-lane sweep: under this lock, any codex exec still carrying the lane
  # tag belongs to an earlier tick (this run has spawned none yet). Kill it so
  # a suspended tick cannot accumulate orphans across cron ticks.
  for pid in $(pgrep -f "codex exec.*${LANE_TAG}" 2>/dev/null || true); do
    [ "$pid" = "$$" ] && continue
    kill -9 "$pid" 2>/dev/null || true
  done
  n=0 failed=0 visited=0
  sources=$(find "$SESSIONS_DIR" -name "*.jsonl" 2>/dev/null | wc -l | tr -d " ")
  # oldest-first so backfill is chronological
  while IFS= read -r -d '' record; do
    f="${record#* }"
    token=$(python3 "$RECEIPTS" due "$RECEIPT_FILE" "$f" "$SEEN")
    due_rc=$?
    [ "$due_rc" = 3 ] && continue
    [ "$due_rc" = 0 ] || { failed=$((failed+1)); continue; }
    [ "$visited" -ge "$MAX_FILES_PER_RUN" ] && break
    visited=$((visited+1))
    convo=$(python3 "$HERE/session-tail.py" codex "$f" "$SESSIONS_DIR")
    read_rc=$?
    if [ "$read_rc" != 0 ]; then
      python3 "$RECEIPTS" failed "$RECEIPT_FILE" "$f" "$token" >/dev/null
      failed=$((failed+1)); continue
    fi
    [ ${#convo} -gt 200 ] || { python3 "$RECEIPTS" stored "$RECEIPT_FILE" "$f" "$token" >/dev/null; continue; }
    resp_full=$(timeout -k "$CODEX_KILL_GRACE" "$CODEX_TIMEOUT" codex exec --ephemeral --skip-git-repo-check "${PROMPT_PREFIX}${convo}

[${LANE_TAG}]" </dev/null 2>/dev/null)
    extract_rc=$?
    resp="$resp_full"
    if [ "$extract_rc" != 0 ]; then
      python3 "$RECEIPTS" failed "$RECEIPT_FILE" "$f" "$token" >/dev/null
      failed=$((failed+1)); continue
    fi
    # strict JSON sanity → wrap into distill payload shape → ingest
    NUNCHI_PY="$FM" python3 - "$resp" "$f" <<'PYEOF'
import json, sys, os, subprocess, re
from datetime import datetime, timezone
raw, path = sys.argv[1], sys.argv[2]
try:
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    d = json.loads(raw)
    items = d["honcho"]
    assert isinstance(items, list) and all(isinstance(x, dict) and isinstance(x.get("text"), str) and x["text"].strip() for x in items)
except Exception:
    sys.exit(2)  # failed extraction remains retryable
sid = os.path.basename(path).replace("rollout-", "").replace(".jsonl", "")
payload = {"session_id": f"codex:{sid}",
           "distilled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "decision_reason_contract": "required-v1",
           "honcho": items}
r = subprocess.run(["python3", os.environ["NUNCHI_PY"], "ingest", "-"],
                   input=json.dumps(payload), capture_output=True, text=True)
print("nunchi-feed: ingest " + ("ok" if r.returncode == 0 else "failed"))
sys.exit(r.returncode)
PYEOF
    ingest_rc=$?
    if [ "$ingest_rc" = 0 ]; then
      if python3 "$RECEIPTS" stored "$RECEIPT_FILE" "$f" "$token" >/dev/null; then
        n=$((n+1))
      else
        failed=$((failed+1))
      fi
    else
      python3 "$RECEIPTS" failed "$RECEIPT_FILE" "$f" "$token" >/dev/null
      failed=$((failed+1))
    fi
  done < <(find "$SESSIONS_DIR" -type f -name "*.jsonl" -printf '%T@ %p\0' 2>/dev/null | sort -z -n)
  python3 "$FM" snapshot --limit 25 >/dev/null 2>&1 || true
  # Liveness tick, written by the shared feed-common.sh helper (#1698):
  # ccc-doctor judges the ingest lane by this file's age, so a lane that runs
  # but never writes it looks stale forever once a node switches provider
  # (2026-09-02: five nodes flagged ingest-tick-stale after moving to the
  # piri/codex feeds — the claude-era file just aged out). sources = session
  # files considered this run, ingested = sessions processed.
  _status="${CCC_NUNCHI_INGEST_STATUS:-$NUNCHI_HOME/ingest.status.json}"
  nunchi_write_status "$_status" codex "${sources:-0}" "${n:-0}" 0 "${failed:-0}"
)
