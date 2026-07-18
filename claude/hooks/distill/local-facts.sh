#!/usr/bin/env bash
# distill/local-facts.sh
# Reads distilled JSON on stdin ({honcho:[{kind,text,subject}], session_id,
# trigger, distilled_at}) and appends each honcho fact to the local
# memory-facts.jsonl in the schema the SQLite index already reads
# (structured_fact_docs). This closes the test-time-learning loop: a fact the
# agent learns this session becomes locally recallable next session via the hot
# index — no network, independent of Honcho (which the max-perf profile drops).
#
# Local-only, append-with-dedup, bounded growth, fail-open. The index re-redacts
# and re-dedupes on read, so this is best-effort; it never blocks distill.
set -uo pipefail

STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
FACTS_FILE="${CCC_MEMORY_FACTS_FILE:-$STATE_DIR/memory-facts.jsonl}"
MAX_FACTS="${CCC_LOCAL_FACTS_MAX:-1000}"
mkdir -p "$STATE_DIR" 2>/dev/null || true

# Off-switch shared with the rest of the distill pipeline.
[ -f "$STATE_DIR/distill.disabled" ] && { echo "local-facts skipped: disabled"; exit 0; }

input="$(cat 2>/dev/null)"
[ -n "$input" ] || { echo "local-facts: no input"; exit 0; }

AUDIENCE_SCOPED="${CCC_MEMORY_AUDIENCE_SCOPED:-0}"
MEMORY_AUDIENCE="${CCC_MEMORY_AUDIENCE:-legacy}"
case "$AUDIENCE_SCOPED:$MEMORY_AUDIENCE" in
  1:private|1:shared|true:private|true:shared|TRUE:private|TRUE:shared|on:private|on:shared|ON:private|ON:shared|yes:private|yes:shared|YES:private|YES:shared)
    audience_root="${CCC_MEMORY_AUDIENCE_ROOT:-}"
    memory_scope="${CCC_MEMORY_SCOPE:-}"
    valid=0
    case "$MEMORY_AUDIENCE:$memory_scope" in
      shared:shared) valid=1 ;;
      private:private-*)
        suffix="${memory_scope#private-}"
        if [ "${#suffix}" = 32 ]; then
          case "$suffix" in *[!0-9a-f]*) ;; *) valid=1 ;; esac
        fi
        ;;
    esac
    [ "$valid" = 1 ] \
      && [ -n "$audience_root" ] \
      && [ "$STATE_DIR" = "$audience_root/$memory_scope/state" ] \
      && [ "$FACTS_FILE" = "$audience_root/$memory_scope/state/memory-facts.jsonl" ] \
      || { echo "local-facts skipped: invalid audience paths"; exit 0; }
    ;;
  0:*|false:*|FALSE:*|off:*|OFF:*|no:*|NO:*) ;;
  *) echo "local-facts skipped: invalid audience metadata"; exit 0 ;;
esac

DISTILL_JSON="$input" python3 - "$FACTS_FILE" "$MAX_FACTS" "$AUDIENCE_SCOPED" "$MEMORY_AUDIENCE" <<'PY' || exit 0
import hashlib, json, os, re, sys, tempfile
from datetime import datetime, timezone

facts_file, max_facts = sys.argv[1], int(sys.argv[2] or 1000)
audience_scoped = sys.argv[3].lower() not in {"0", "false", "off", "no"}
memory_audience = sys.argv[4]
raw = os.environ.get("DISTILL_JSON", "")
try:
    doc = json.loads(raw)
except Exception:
    sys.exit(0)
items = doc.get("honcho") if isinstance(doc, dict) else None
if not isinstance(items, list) or not items:
    sys.exit(0)

session = str(doc.get("session_id") or "")
trigger = str(doc.get("trigger") or "")
distilled_at = str(doc.get("distilled_at") or "")
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
observed_at = distilled_at or now

def norm(t):
    return " ".join(re.findall(r"[0-9a-z가-힣]+", (t or "").lower()))

# Existing normalized texts, for append-time dedup (the index dedupes again).
existing_lines = []
seen = set()
if os.path.isfile(facts_file):
    with open(facts_file, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            existing_lines.append(line)
            try:
                seen.add(norm(json.loads(line).get("text") or ""))
            except Exception:
                pass

added = []
for it in items:
    if not isinstance(it, dict):
        continue
    text = str(it.get("text") or "").strip()
    if not text:
        continue
    n = norm(text)
    if not n or n in seen:
        continue
    seen.add(n)
    kind = str(it.get("kind") or "observation")
    subject = str(it.get("subject") or "").strip()
    fid = "distill-" + hashlib.sha256((n + session).encode("utf-8")).hexdigest()[:12]
    fact = {
        "id": fid,
        "kind": kind,
        "text": text,
        "review": "auto-local",
        "privacy": "shared" if audience_scoped and memory_audience == "shared" else "private",
        "confidence": 0.7,
        "observed_at": observed_at,
        "entities": [subject] if subject else [],
        "tags": ["distilled"] + ([trigger] if trigger else []),
        "source": {"type": "distill", "session": session, "trigger": trigger},
    }
    if audience_scoped:
        fact["audience"] = memory_audience
    added.append(json.dumps(fact, ensure_ascii=False))

if not added:
    sys.exit(0)

lines = existing_lines + added
if len(lines) > max_facts:
    lines = lines[-max_facts:]  # bound growth: keep the most recent

d = os.path.dirname(facts_file) or "."
os.makedirs(d, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=d, prefix=".memory-facts.", suffix=".tmp")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, facts_file)
except Exception:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    sys.exit(0)
print(f"local-facts: appended {len(added)} fact(s) to {facts_file}")
PY
