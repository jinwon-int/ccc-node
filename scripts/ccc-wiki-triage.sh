#!/usr/bin/env bash
# ccc-wiki-triage.sh — read-only/local triage for distill-generated Wiki candidates.
# Never writes to Family Wiki; local decision marks are stored under CCC_STATE_DIR only.
set -uo pipefail

STATE_DIR="${CCC_STATE_DIR:-/root/.claude/state}"
CANDIDATES="${CCC_WIKI_CANDIDATES_FILE:-$STATE_DIR/wiki-candidates.md}"
DECISIONS="${CCC_WIKI_TRIAGE_DECISIONS:-$STATE_DIR/wiki-candidate-decisions.json}"
CMD="${1:-list}"
ID="${2:-}"

usage() {
  cat >&2 <<'EOF'
usage: ccc-wiki-triage.sh list|show <candidate-id>|mark-approved <candidate-id>|mark-rejected <candidate-id>|mark-held <candidate-id>

Local-only helper. It does not call wiki-agent and does not write Family Wiki.
EOF
}

case "$CMD" in list|show|mark-approved|mark-rejected|mark-held) ;; --help|-h) usage; exit 0 ;; *) usage; exit 2 ;; esac
if [ "$CMD" != "list" ] && [ -z "$ID" ]; then usage; exit 2; fi
mkdir -p "$STATE_DIR"

python3 - "$CMD" "$ID" "$CANDIDATES" "$DECISIONS" <<'PY'
import json, re, sys, time
from pathlib import Path
cmd, cand_id, candidates_path, decisions_path = sys.argv[1:]
candidates_file = Path(candidates_path)
decisions_file = Path(decisions_path)
text = candidates_file.read_text(encoding="utf-8", errors="replace") if candidates_file.exists() else ""
SECRET_LINE = re.compile(r"(?i)(token|secret|password|api[_-]?key|authorization|private[_-]?key|cookie|session)\s*[:=]|bearer\s+[A-Za-z0-9._-]+")

def clean(body: str) -> str:
    lines=[]
    for line in body.splitlines():
        if SECRET_LINE.search(line):
            lines.append("[REDACTED_SENSITIVE_LINE]")
        else:
            lines.append(line)
    return "\n".join(lines).strip()

def parse(md: str):
    rows=[]
    matches=list(re.finditer(r"^##\s+([^\n]+)\s*$", md, re.M))
    for idx,m in enumerate(matches):
        cid=m.group(1).strip().split()[0]
        end=matches[idx+1].start() if idx+1 < len(matches) else len(md)
        body=clean(md[m.end():end])
        rows.append({"id":cid,"title":m.group(1).strip(),"body":body,"bytes":len(body.encode())})
    if not rows and md.strip():
        rows.append({"id":"CAND-001","title":"CAND-001","body":clean(md),"bytes":len(md.encode())})
    return rows

def load_decisions():
    if not decisions_file.exists(): return {}
    try: return json.loads(decisions_file.read_text())
    except Exception: return {}

def save_decisions(d):
    decisions_file.write_text(json.dumps(d, ensure_ascii=False, indent=2)+"\n")
    try: decisions_file.chmod(0o600)
    except OSError: pass

rows=parse(text)
decisions=load_decisions()
for r in rows:
    r["decision"] = decisions.get(r["id"], {}).get("decision", "unreviewed")
    r["redaction_applied"] = "[REDACTED_SENSITIVE_LINE]" in r["body"]
if cmd == "list":
    print(json.dumps({"ok":True,"file":str(candidates_file),"count":len(rows),"candidates":[{k:v for k,v in r.items() if k != "body"} for r in rows]}, ensure_ascii=False, indent=2))
elif cmd == "show":
    row=next((r for r in rows if r["id"] == cand_id), None)
    if not row:
        print(json.dumps({"ok":False,"error":"candidate not found","id":cand_id}, ensure_ascii=False)); sys.exit(1)
    print(json.dumps({"ok":True,"candidate":row,"wiki_write_performed":False,"next_step":"review then use wiki-agent write-path/pr manually if approved"}, ensure_ascii=False, indent=2))
else:
    decision={"mark-approved":"approved","mark-rejected":"rejected","mark-held":"held"}[cmd]
    if not any(r["id"] == cand_id for r in rows):
        print(json.dumps({"ok":False,"error":"candidate not found","id":cand_id}, ensure_ascii=False)); sys.exit(1)
    decisions[cand_id]={"decision":decision,"updated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    save_decisions(decisions)
    print(json.dumps({"ok":True,"id":cand_id,"decision":decision,"decisions_file":str(decisions_file),"wiki_write_performed":False}, ensure_ascii=False, indent=2))
PY
