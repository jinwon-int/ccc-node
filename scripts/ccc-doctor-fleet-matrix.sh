#!/usr/bin/env bash
# ccc-doctor-fleet-matrix.sh — read-only fleet rollup for ccc-doctor output.
#
# Input is a text evidence file with blocks:
#   ===== <node> =====
#   <ccc-doctor output or JSON>
#
# Output is JSON only. No SSH, no service changes, no provider sends, no secret
# reads. This script only classifies already-collected evidence.
set -euo pipefail

usage() {
  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
  echo "Usage: bash $0 --evidence FILE [--node-list n1,n2] [--json]" >&2
  exit "${1:-0}"
}

EVIDENCE=""
NODE_LIST="dungae,nosuk,soonwook,gongyung,daegyo"
while [ $# -gt 0 ]; do
  case "$1" in
    --evidence|--status) EVIDENCE="${2:-}"; shift 2 ;;
    --node-list) NODE_LIST="${2:-}"; shift 2 ;;
    --json) shift ;;
    -h|--help) usage 0 ;;
    *) printf 'unknown arg: %s\n' "$1" >&2; usage 2 ;;
  esac
done

[ -n "$EVIDENCE" ] || { echo "--evidence is required" >&2; exit 2; }
[ -f "$EVIDENCE" ] || { echo "evidence not found: $EVIDENCE" >&2; exit 2; }

python3 - "$EVIDENCE" "$NODE_LIST" <<'PY'
import json, re, sys
from pathlib import Path
path = Path(sys.argv[1])
known = [x.strip() for x in sys.argv[2].split(',') if x.strip()]
text = path.read_text(encoding='utf-8', errors='replace')
blocks = {}
current = None
buf = []
for line in text.splitlines():
    m = re.match(r'^=====\s+([^=\s]+)\s+=====$', line.strip())
    if m:
        if current is not None:
            blocks[current] = '\n'.join(buf).strip()
        current = m.group(1)
        buf = []
    elif current is not None:
        buf.append(line)
if current is not None:
    blocks[current] = '\n'.join(buf).strip()

def classify(body):
    low = body.lower()
    if not body:
        return ('수동필요', 'missing_evidence')
    if any(s in low for s in ['permission denied', 'connection refused', 'timed out', 'no route to host', 'ssh:']):
        return ('수동필요', 'node_unreachable_or_probe_failed')
    try:
        obj = json.loads(body)
        serial = json.dumps(obj, ensure_ascii=False).lower()
        if obj.get('ok') is False or '위험' in serial or 'danger' in serial:
            return ('위험', 'doctor_reported_failure')
        if '수동필요' in serial or 'manual' in serial:
            return ('수동필요', 'manual_action_required')
        if '교정가능' in serial or 'fixable' in serial:
            return ('교정가능', 'fixable_drift')
        if '경고' in serial or 'warning' in serial:
            return ('경고', 'warnings_present')
        return ('정상', 'doctor_ok_json')
    except Exception:
        pass
    if re.search(r'\bFAIL=([1-9][0-9]*)\b', body):
        return ('위험', 'test_failures_present')
    if re.search(r'\bPASS=\d+\s+FAIL=0\b', body) or 'doctor ok' in low or '정상' in body:
        return ('정상', 'doctor_ok_text')
    if 'warning' in low or '경고' in body:
        return ('경고', 'warnings_present')
    return ('수동필요', 'unclassified_output')

nodes = []
seen = set()
for name in known + [n for n in blocks if n not in known]:
    if name in seen:
        continue
    seen.add(name)
    body = blocks.get(name, '')
    status, reason = classify(body)
    nodes.append({
        'node': name,
        'status': status,
        'reason': reason,
        'evidencePresent': bool(body),
        'lineCount': len(body.splitlines()) if body else 0,
    })
summary = {k: sum(1 for n in nodes if n['status'] == k) for k in ['정상','경고','교정가능','수동필요','위험']}
print(json.dumps({
    'kind': 'ccc-doctor-fleet-matrix',
    'mode': 'read-only',
    'source': str(path),
    'nodes': nodes,
    'summary': summary,
    'mutations': {'ssh': False, 'serviceRestart': False, 'providerSend': False, 'secretRead': False},
}, ensure_ascii=False, indent=2))
PY
