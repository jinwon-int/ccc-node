#!/usr/bin/env bash
# Write state/resume.md from distill/extract JSON.
# Input schema: {resume:{last_activity,pending_action,awaiting_user,open_question,next_step,evidence}}
# Fail-open: invalid/empty resume preserves the previous file.
set -uo pipefail

STATE_DIR="${CCC_STATE_DIR:-${HOME:-/root}/.claude/state}"
RESUME_FILE="${CCC_RESUME_FILE:-$STATE_DIR/resume.md}"
MAX_BYTES="${CCC_RESUME_WRITE_MAX_BYTES:-4000}"
mkdir -p "$STATE_DIR" 2>/dev/null || exit 0

RAW="$(cat 2>/dev/null || true)"
[ -n "$RAW" ] || exit 0

rendered="$(RESUME_JSON="$RAW" python3 - "$MAX_BYTES" 2>/dev/null <<'PY'
import json, os, re, sys
max_bytes = int(sys.argv[1])
try:
    doc = json.loads(os.environ.get("RESUME_JSON", ""))
except Exception:
    sys.exit(0)
resume = doc.get("resume")
if not isinstance(resume, dict):
    sys.exit(0)

def clean(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else ""
    if isinstance(value, (list, tuple)):
        return ", ".join(clean(v) for v in value if clean(v))
    text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    # Belt-and-suspenders redaction; extract input is already redacted.
    text = re.sub(r"(?:ghp|gho|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}", "[REDACTED:gh-token]", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{20,}", "[REDACTED:api-key]", text)
    text = re.sub(r"AKIA[A-Z0-9]{16}", "[REDACTED:aws-key]", text)
    text = re.sub(r"Bearer [A-Za-z0-9._-]{20,}", "Bearer [REDACTED]", text)
    text = text.replace("-----BEGIN PRIVATE KEY-----", "[REDACTED:pem-begin]")
    return text[:1000]

fields = [
    ("last_activity", "마지막 작업"),
    ("pending_action", "다음 액션"),
    ("awaiting_user", "사용자 대기"),
    ("open_question", "열린 질문"),
    ("next_step", "다음 한 수"),
]
lines = []
for key, label in fields:
    val = clean(resume.get(key))
    if val:
        lines.append(f"- {label}: {val}")
evidence = clean(resume.get("evidence"))
if evidence:
    lines.append(f"- 근거: {evidence}")
# No substantive resume: preserve previous file by emitting nothing.
if not lines:
    sys.exit(0)
out = "\n".join(lines) + "\n"
data = out.encode("utf-8")
if len(data) > max_bytes:
    data = data[:max_bytes]
    out = data.decode("utf-8", errors="ignore") + "\n… [truncated by CCC resume budget]\n"
sys.stdout.write(out)
PY
)"

[ -n "$rendered" ] || exit 0

tmp="$RESUME_FILE.tmp.$$"
printf '%s' "$rendered" > "$tmp" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; exit 0; }
chmod 600 "$tmp" 2>/dev/null || true
mv "$tmp" "$RESUME_FILE" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; exit 0; }
exit 0
