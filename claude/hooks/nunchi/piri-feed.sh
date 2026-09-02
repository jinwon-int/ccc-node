#!/usr/bin/env bash
# nunchi piri-feed extractor (#816) — Piri-provider nodes.
#
# This supplementary nunchi lane extracts user/agent messages from NEW Piri
# session JSONL files, asks the configured Piri CLI for peer facts in one
# non-interactive print-mode run, and ingests them into the nunchi DB. The main
# ccc distill journal independently owns local/Honcho/Wiki write-back.
# Idempotent via a seen-file; bounded per run. Runs from cron.
# NOTE: unlike ingest-cron.sh this costs one Piri run per new file.
# No-op unless nunchi is enabled (state/nunchi.mode=on or CCC_NUNCHI_MODE=on).
set -uo pipefail
umask 077

STATE="${CCC_STATE_DIR:-$HOME/.claude/state}"
MODE="${CCC_NUNCHI_MODE:-$(cat "$STATE/nunchi.mode" 2>/dev/null || echo off)}"
[ "$MODE" = on ] || exit 0

HERE="$(cd "$(dirname "$0")" && pwd)"
FM="$HERE/nunchi.py"

# In audience-scoped mode the cron entry is a body-free dispatcher. It visits
# only canonical direct children of the configured opaque audience root and
# reinvokes this script with a scope-local transcript, DB, snapshot, seen file
# and extractor session directory. The legacy/global path below is unchanged
# when the flag is absent.
if [ "${CCC_NUNCHI_AUDIENCE_SCOPED:-0}" = 1 ] \
    && [ "${CCC_NUNCHI_SCOPED_CHILD:-0}" != 1 ]; then
  audience_root="${CCC_NUNCHI_AUDIENCE_ROOT:-}"
  max_scopes="${CCC_NUNCHI_MAX_SCOPES_PER_RUN:-64}"
  case "$max_scopes" in
    ''|*[!0-9]*) max_scopes=64 ;;
    *) [ "$max_scopes" -ge 1 ] && [ "$max_scopes" -le 64 ] || max_scopes=64 ;;
  esac
  rc=0
  while IFS= read -r scope_root; do
    [ -d "$scope_root/piri/sessions" ] || continue
    scope="${scope_root##*/}"
    kind=private
    [ "$scope" = shared ] && kind=shared
    CCC_NUNCHI_SCOPED_CHILD=1 \
      CCC_NUNCHI_AUDIENCE_SCOPE="$scope" \
      CCC_NUNCHI_AUDIENCE_KIND="$kind" \
      PIRI_CODING_AGENT_SESSION_DIR="$scope_root/piri/sessions" \
      PIR_SESSIONS_DIR="$scope_root/piri/sessions" \
      NUNCHI_HOME="$scope_root/nunchi" \
      NUNCHI_DB="$scope_root/nunchi/facts.db" \
      NUNCHI_SNAPSHOT="$scope_root/nunchi/snapshot.md" \
      bash "$0" || rc=1
  done < <(python3 - "$audience_root" "$max_scopes" <<'PY'
import os
from pathlib import Path
import re
import stat
import sys

root = Path(sys.argv[1])
limit = int(sys.argv[2])
try:
    meta = root.lstat()
except OSError:
    raise SystemExit(0)
if not (
    root.is_absolute()
    and stat.S_ISDIR(meta.st_mode)
    and meta.st_uid == os.geteuid()
    and not stat.S_IMODE(meta.st_mode) & 0o077
):
    raise SystemExit(0)
count = 0
for child in sorted(root.iterdir(), key=lambda item: item.name):
    if count >= limit:
        break
    if child.name != "shared" and not re.fullmatch(r"private-[0-9a-f]{32}", child.name):
        continue
    try:
        item = child.lstat()
    except OSError:
        continue
    if not (
        stat.S_ISDIR(item.st_mode)
        and item.st_uid == os.geteuid()
        and not stat.S_IMODE(item.st_mode) & 0o077
    ):
        continue
    print(child)
    count += 1
PY
  )
  exit "$rc"
fi

NUNCHI_HOME="${NUNCHI_HOME:-$HOME/.nunchi}"
SEEN="$NUNCHI_HOME/piri-seen"
LOCK="$NUNCHI_HOME/.piri-feed.lock"
# PIRI_CODING_AGENT_SESSION_DIR is already the direct session directory. The
# global default is <agent-dir>/sessions and may contain cwd subdirectories.
if [ -z "${PIR_SESSIONS_DIR:-}" ]; then
  if [ -n "${PIRI_CODING_AGENT_SESSION_DIR:-}" ]; then
    PIR_SESSIONS_DIR="$PIRI_CODING_AGENT_SESSION_DIR"
  else
    PIR_SESSIONS_DIR="${PIRI_CODING_AGENT_DIR:-$HOME/.piri/agent}/sessions"
  fi
fi
# The Piri CLI the bridge runs (ccc-node PiriRuntime). Falls back to `piri` on PATH.
PIR_CLI="${CCC_PIRI_CLI_PATH:-piri}"
# Isolate extractor runs so their own sessions never land under PIR_SESSIONS_DIR
# (which would make every cron tick re-extract the previous extractor's session).
EXTRACTOR_SESSION_DIR="$NUNCHI_HOME/.piri-feed-extractor-sessions"
MAX_FILES_PER_RUN="${NUNCHI_FEED_MAX_FILES:-3}"
case "$MAX_FILES_PER_RUN" in
  ''|*[!0-9]*) MAX_FILES_PER_RUN=3 ;;
  *) [ "$MAX_FILES_PER_RUN" -ge 1 ] && [ "$MAX_FILES_PER_RUN" -le 20 ] \
    || MAX_FILES_PER_RUN=3 ;;
esac
mkdir -p "$NUNCHI_HOME" "$EXTRACTOR_SESSION_DIR"
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
  # Piri absent → nothing to extract this tick; exit 0 (idempotent) but say
  # so — a CLI that is never on PATH must not fail silent for weeks. The
  # filesystem fallback requires a regular file: -x alone matches searchable
  # directories (a checkout root ships ./piri), which used to pass the guard.
  command -v "$PIR_CLI" >/dev/null 2>&1 || {
    { [ -f "$PIR_CLI" ] && [ -x "$PIR_CLI" ]; } || {
      echo "piri-feed: Piri CLI not runnable (CCC_PIRI_CLI_PATH unset and piri not on PATH) — extraction skipped" >&2
      exit 0
    }
  }
  n=0
  sources=$(find "$PIR_SESSIONS_DIR" -type f -name "*.jsonl" 2>/dev/null | wc -l | tr -d " ")
  visited=0
  # oldest-first so backfill is chronological; NUL delimiters preserve safe
  # configured paths containing whitespace. Symlinks are excluded here and
  # independently rejected by the bounded reader below.
  while IFS= read -r -d '' record; do
    f="${record#* }"
    grep -qxF "$f" "$SEEN" && continue
    [ "$visited" -ge "$MAX_FILES_PER_RUN" ] && break
    visited=$((visited+1))
    convo=$(python3 - "$f" "$PIR_SESSIONS_DIR" <<'PYEOF'
import json, os, stat, sys
path = os.path.abspath(sys.argv[1])
root = os.path.abspath(sys.argv[2])
if os.path.commonpath((path, root)) != root:
    raise SystemExit(0)
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
try:
    fd = os.open(path, flags)
except OSError:
    raise SystemExit(0)
meta = os.fstat(fd)
if not (stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1
        and meta.st_uid == os.geteuid()
        and not stat.S_IMODE(meta.st_mode) & 0o077
        and meta.st_size <= 16 * 1024 * 1024):
    os.close(fd)
    raise SystemExit(0)
out = []
with os.fdopen(fd, encoding="utf-8", errors="replace") as handle:
  for ln in handle:
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
           "provider": "piri",
           "memory_audience": os.environ.get("CCC_NUNCHI_AUDIENCE_KIND"),
           "memory_scope": os.environ.get("CCC_NUNCHI_AUDIENCE_SCOPE"),
           "distilled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "decision_reason_contract": "required-v1",
           "honcho": items}
r = subprocess.run(["python3", os.environ["NUNCHI_PY"], "ingest", "-"],
                   input=json.dumps(payload), capture_output=True, text=True)
print(r.stdout.strip() or r.stderr.strip())
PYEOF
    echo "$f" >> "$SEEN"
    n=$((n+1))
  done < <(find "$PIR_SESSIONS_DIR" -type f -name "*.jsonl" \
    -printf '%T@ %p\0' 2>/dev/null | sort -z -n)
  python3 "$FM" snapshot --limit 25 >/dev/null 2>&1 || true
  # Liveness tick shared with ingest-cron.sh (schema ccc.nunchi.ingest.v1):
  # ccc-doctor judges the ingest lane by this file's age, so a lane that runs
  # but never writes it looks stale forever once a node switches provider
  # (2026-09-02: five nodes flagged ingest-tick-stale after moving to the
  # piri/codex feeds — the claude-era file just aged out). sources = session
  # files considered this run, ingested = sessions processed.
  _status="${CCC_NUNCHI_INGEST_STATUS:-$NUNCHI_HOME/ingest.status.json}"
  _tmp="$_status.$$"
  if printf '{"schema":"ccc.nunchi.ingest.v1","finished_at":%d,"sources":%d,"ingested":%d,"retired":0,"deferred":0,"feed":"%s"}\n' \
      "$(date -u +%s)" "${sources:-0}" "${n:-0}" "piri" > "$_tmp" 2>/dev/null; then
    mv -f "$_tmp" "$_status" 2>/dev/null || rm -f "$_tmp"
  else
    rm -f "$_tmp" 2>/dev/null
  fi
) 9>"$LOCK"
