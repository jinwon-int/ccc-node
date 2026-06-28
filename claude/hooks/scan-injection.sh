#!/usr/bin/env bash
# Runtime memory-injection scanner.
#
# Reads a memory/cache block on stdin and writes a redacted/sanitized version to
# stdout. This script is intentionally fail-open at the caller boundary:
# load-memory.sh falls back to the original text if this scanner is missing or
# exits non-zero. Successful scans never print raw secrets to stderr/stdout.
set -uo pipefail

label="${1:-memory-injection}"
input_tmp="$(mktemp)"
trap 'rm -f "$input_tmp"' EXIT
cat > "$input_tmp"
python3 - "$label" "$input_tmp" <<'PY'
import json
import os
import re
import sys
from datetime import datetime, timezone

label = sys.argv[1] if len(sys.argv) > 1 else "memory-injection"
with open(sys.argv[2], encoding='utf-8', errors='replace') as fh:
    text = fh.read()
original = text
categories = []

# Remove invisible / control-format characters that can alter model-visible text
# without being obvious to humans. Keep normal whitespace intact.
INVISIBLE = {
    0x00AD,  # soft hyphen
    0x034F,  # combining grapheme joiner
    0x061C,  # arabic letter mark
    0x180E,  # mongolian vowel separator
    0x200B, 0x200C, 0x200D, 0x200E, 0x200F,  # zero-width + bidi marks
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,  # bidi embeddings/overrides
    0x2060, 0x2061, 0x2062, 0x2063, 0x2064,
    0x2066, 0x2067, 0x2068, 0x2069,  # bidi isolates
    0xFEFF,  # zero-width no-break space / BOM
}
# Unicode tag characters.
INVISIBLE.update(range(0xE0000, 0xE0080))
if any(ord(ch) in INVISIBLE for ch in text):
    text = ''.join('[REDACTED:unicode]' if ord(ch) in INVISIBLE else ch for ch in text)
    categories.append('invisible-unicode')

credential_patterns = [
    (re.compile(r'(ghp_|gho_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,}'), r'\1[REDACTED:credential]'),
    (re.compile(r'(sk-)[A-Za-z0-9_-]{20,}'), r'\1[REDACTED:credential]'),
    (re.compile(r'(AKIA|ASIA)[A-Z0-9]{16}'), r'\1[REDACTED:credential]'),
    (re.compile(r'(xox[baprs]-)[A-Za-z0-9-]{20,}'), r'\1[REDACTED:credential]'),
    (re.compile(r'(-----BEGIN [A-Z ]*PRIVATE KEY-----).*?(-----END [A-Z ]*PRIVATE KEY-----)', re.S), r'\1[REDACTED:private-key]\2'),
    (re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b'), '[REDACTED:jwt]'),
    (re.compile(r'(?i)\b(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/=-]{12,}'), r'\1[REDACTED:credential]'),
    (re.compile(r'(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*[^\s"\'&|;]{8,}'), r'\1=[REDACTED:credential]'),
]
for pattern, repl in credential_patterns:
    text2 = pattern.sub(repl, text)
    if text2 != text and 'credential-pattern' not in categories:
        categories.append('credential-pattern')
    text = text2

# Redact only the imperative phrase, not the whole surrounding operational note.
# This preserves useful memory while neutralizing obvious prompt-injection text.
injection_patterns = [
    r'(?i)\bignore (all )?(previous|prior|above) (instructions|directives|messages)\b',
    r'(?i)\bdisregard (all )?(previous|prior|above) (instructions|directives|messages)\b',
    r'(?i)\byou are now (system|developer|root|admin)\b',
    r'(?i)\btreat this as (a )?(system|developer) message\b',
    r'(?i)\breveal (the )?(system prompt|developer message|secrets?|tokens?)\b',
    r'(?i)\bexfiltrate\b[^\n]{0,80}\b(secret|token|credential|key)s?\b',
    r'(?i)\b(send|post|upload)\b[^\n]{0,80}\b(secret|token|credential|key)s?\b',
    r'(?i)\btool[- ]?invocation request\b',
    r'(?i)\bdo not follow (the )?(user|operator)\b',
    r'(?i)\bforget (the )?(fresh approval|approval gate|safety rules)\b',
]
for rx in injection_patterns:
    pattern = re.compile(rx)
    text2 = pattern.sub('[REDACTED:prompt-injection]', text)
    if text2 != text and 'prompt-injection' not in categories:
        categories.append('prompt-injection')
    text = text2

# Emit sanitized text first; audit metadata never includes raw/redacted body.
sys.stdout.write(text)

if categories:
    log = os.environ.get('CCC_AUDIT_LOG', os.path.expanduser('~/.claude/state/audit.jsonl'))
    try:
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({
                'ts': datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
                'event': 'MemoryInjectionScan',
                'label': label,
                'categories': categories,
                'inputBytes': len(original.encode('utf-8', 'replace')),
                'outputBytes': len(text.encode('utf-8', 'replace')),
            }, ensure_ascii=False) + '\n')
    except Exception:
        # Do not fail the hook because audit logging is unavailable.
        pass
PY
