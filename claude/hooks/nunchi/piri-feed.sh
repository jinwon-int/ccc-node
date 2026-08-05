#!/usr/bin/env bash
# nunchi piri-feed extractor (#816) — Piri-provider nodes.
#
# Piri sessions are not read by the Claude/Codex distill journal (the Piri RPC
# runtime exposes no distill/write-back extractor), so — like codex-feed.sh on a
# Codex node — this lane extracts user/agent messages from NEW Piri session
# jsonl files, asks the configured Piri CLI for distill-style facts in one
# non-interactive print-mode run, and ingests them into the nunchi peer_facts
# DB. Idempotent via a seen-file; bounded per run. Runs from cron.
# NOTE: unlike ingest-cron.sh this costs one Piri run per new file.
# No-op unless nunchi is enabled (state/nunchi.mode=on or CCC_NUNCHI_MODE=on).
set -uo pipefail

STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
MODE="${CCC_NUNCHI_MODE:-$(cat "$STATE/nunchi.mode" 2>/dev/null || echo off)}"
[ "$MODE" = on ] || exit 0

HERE="$(cd "$(dirname "$0")" && pwd)"
FM="$HERE/nunchi.py"
NUNCHI_HOME="${NUNCHI_HOME:-$HOME/.nunchi}"
SEEN="$NUNCHI_HOME/piri-seen"
LOCK="$NUNCHI_HOME/.piri-feed.lock"
# Piri stores sessions under <state-dir>/sessions/<cwd-slug>/<id>.jsonl.
PIR_SESSIONS_DIR="${PIR_SESSIONS_DIR:-${PIRI_CODING_AGENT_SESSION_DIR:-$HOME/.piri/agent}/sessions}"
# The Piri CLI the bridge runs (ccc-node PiriRuntime). Falls back to `piri` on PATH.
PIR_CLI="${CCC_PIRI_CLI_PATH:-piri}"
# Isolate extractor runs so their own sessions never land under PIR_SESSIONS_DIR
# (which would make every cron tick re-extract the previous extractor's session).
EXTRACTOR_SESSION_DIR="$NUNCHI_HOME/.piri-feed-extractor-sessions"
MAX_FILES_PER_RUN="${NUNCHI_FEED_MAX_FILES:-3}"
mkdir -p "$NUNCHI_HOME" "$EXTRACTOR_SESSION_DIR"
touch "$SEEN"

PROMPT_PREFIX='다음은 AI 에이전트 작업 세션의 대화 발췌이다. 다음 세션에서도 알아야 할 사실만 추출해 strict JSON으로 답하라.
형식: {"honcho":[{"kind":"preference|fact|context|correction","text":"<한 문장 한국어 사실>","subject":"user|session|node"}]}
기준: user=사용자 선호/지시 방식, session=진행 중 작업 맥락/다음 액션, node=이 노드 사실. 잡담/디버깅만 있으면 {"honcho":[]}.
JSON 객체 하나만 출력. 설명/마크다운 금지.

대화:
'

(
  flock -n 9 || exit 0
  # Piri absent → nothing to extract this tick; stay quiet and idempotent.
  command -v "$PIR_CLI" >/dev/null 2>&1 || {
    [ -x "$PIR_CLI" ] || exit 0
  }
  n=0
  # oldest-first so backfill is chronological; recurse across cwd-slug subdirs.
  for f in $(find "$PIR_SESSIONS_DIR" -name "*.jsonl" -printf '%T@ %p\n' 2>/dev/null | sort -n | cut -d' ' -f2-); do
    [ "$n" -ge "$MAX_FILES_PER_RUN" ] && break
    grep -qxF "$f" "$SEEN" && continue
    convo=$(python3 - "$f" <<'PYEOF'
import json, sys
out = []
for ln in open(sys.argv[1]):
    try:
        d = json.loads(ln)
    except Exception:
        continue
    if d.get("type") != "message":
        continue
    m = d.get("message", {})
    role = m.get("role")
    content = m.get("content")
    # Piri content is either a string or a list of {type: text|...} blocks.
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        text = ""
    if role == "user":
        out.append("USER: " + text[:800])
    elif role == "assistant":
        out.append("AGENT: " + text[:800])
    # toolResult and other roles carry no user/agent voice — skip.
text = "\n".join(out[-60:])          # last 60 messages
print(text[:40000])                   # byte cap
PYEOF
)
    [ ${#convo} -gt 200 ] || { echo "$f" >> "$SEEN"; continue; }
    # One non-interactive Piri print-mode run. PIRI_CODING_AGENT_SESSION_DIR
    # keeps the extractor's own session out of the scanned tree.
    resp=$(timeout 300 env PIRI_CODING_AGENT_SESSION_DIR="$EXTRACTOR_SESSION_DIR" \
      "$PIR_CLI" --mode text --print "${PROMPT_PREFIX}${convo}" 2>/dev/null || true)
    [ -n "$resp" ] || { echo "$f" >> "$SEEN"; continue; }
    # The model may wrap JSON in prose; tolerate a leading fence/trailing text.
    NUNCHI_PY="$FM" python3 - "$resp" "$f" <<'PYEOF'
import json, sys, os, re, subprocess
from datetime import datetime, timezone
raw, path = sys.argv[1], sys.argv[2]
# Extract the last {...} JSON object in the response (print mode may add a
# trailing blank line or model commentary around the object).
m = re.search(r"\{.*\}", raw, re.S)
if not m:
    sys.exit(0)
try:
    d = json.loads(m.group(0))
    items = d.get("honcho", [])
    assert isinstance(items, list)
except Exception:
    sys.exit(0)  # not strict JSON → mark seen (caller) to avoid burning calls
sid = os.path.basename(path)
# Piri session files are <timestamp>_<uuid>.jsonl; keep the uuid as the id.
sid = re.sub(r".*_", "", sid.replace(".jsonl", ""))
payload = {"session_id": f"piri:{sid}",
           "distilled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
