#!/usr/bin/env bash
# ccc security audit — read-only metadata-only security diagnostics.
#
# Issue #53 follow-up slice after the memory injection scanner: classify file
# permissions, settings allowlist posture, scanner integrity, and spool/cache
# redaction risk without printing raw secrets or file contents.
set -uo pipefail

FIX=0
for arg in "$@"; do
  case "$arg" in
    --fix) FIX=1 ;;
    -h|--help)
      cat <<'EOF'
Usage: ccc-security-audit.sh [--fix]

Read-only ccc-node security diagnostics. Reports metadata only and never prints
matched secret text or file contents. Classifies checks as:
정상 / 경고 / 위험 / 수동필요.

This implementation slice is diagnostic-only: --fix is not implemented and
makes no filesystem changes.
EOF
      exit 0
      ;;
    *) echo "Unknown flag: $arg" >&2; exit 2 ;;
  esac
done

if [ "$FIX" = 1 ]; then
  echo "ccc security audit --fix is not implemented in this diagnostic-only slice; no filesystem changes were made." >&2
  exit 2
fi

export CCC_SECURITY_AUDIT_REPO_DIR="${CCC_SECURITY_AUDIT_REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
export CCC_SECURITY_AUDIT_CLAUDE_DIR="${CCC_SECURITY_AUDIT_CLAUDE_DIR:-${HOME:-/root}/.claude}"
export CCC_SECURITY_AUDIT_HERMES_DIR="${CCC_SECURITY_AUDIT_HERMES_DIR:-${HOME:-/root}/.hermes}"
export CCC_SECURITY_AUDIT_SPOOL_DIR="${CCC_SECURITY_AUDIT_SPOOL_DIR:-$CCC_SECURITY_AUDIT_CLAUDE_DIR/state/telegram-spool}"
export CCC_SECURITY_AUDIT_CACHE_DIR="${CCC_SECURITY_AUDIT_CACHE_DIR:-$CCC_SECURITY_AUDIT_CLAUDE_DIR/hooks/cache}"

python3 - <<'PY'
import json
import os
import re
import stat
import subprocess
from pathlib import Path

repo = Path(os.environ.get('CCC_SECURITY_AUDIT_REPO_DIR') or Path(__file__).resolve().parents[1])
claude_dir = Path(os.environ.get('CCC_SECURITY_AUDIT_CLAUDE_DIR') or os.path.expanduser('~/.claude'))
hermes_dir = Path(os.environ.get('CCC_SECURITY_AUDIT_HERMES_DIR') or os.path.expanduser('~/.hermes'))
spool_dir = Path(os.environ.get('CCC_SECURITY_AUDIT_SPOOL_DIR') or claude_dir / 'state' / 'telegram-spool')
cache_dir = Path(os.environ.get('CCC_SECURITY_AUDIT_CACHE_DIR') or claude_dir / 'hooks' / 'cache')

rows = []
counts = {'정상': 0, '경고': 0, '위험': 0, '수동필요': 0}

def add(cls, item, status, action):
    rows.append((cls, item, status, action))
    counts[cls] = counts.get(cls, 0) + 1

# Keep patterns aligned with scan-injection.sh, but report only metadata.
patterns = [
    ('credential-pattern', re.compile(r'(ghp_|gho_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,}')),
    ('credential-pattern', re.compile(r'(sk-)[A-Za-z0-9_-]{20,}')),
    ('credential-pattern', re.compile(r'(AKIA|ASIA)[A-Z0-9]{16}')),
    ('credential-pattern', re.compile(r'(xox[baprs]-)[A-Za-z0-9-]{20,}')),
    ('credential-pattern', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----', re.S)),
    ('credential-pattern', re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b')),
    ('credential-pattern', re.compile(r'(?i)\b(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/=-]{12,}')),
    ('credential-pattern', re.compile(r'(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*[^\s"\'&|;]{8,}')),
    ('prompt-injection', re.compile(r'(?i)\b(ignore|disregard) (all )?(previous|prior|above) (instructions|directives|messages)\b')),
    ('prompt-injection', re.compile(r'(?i)\byou are now (system|developer|root|admin)\b')),
    ('prompt-injection', re.compile(r'(?i)\btreat this as (a )?(system|developer) message\b')),
    ('prompt-injection', re.compile(r'(?i)\breveal (the )?(system prompt|developer message|secrets?|tokens?)\b')),
]
INVISIBLE = {0x00AD, 0x034F, 0x061C, 0x180E, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F,
             0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2060, 0x2061, 0x2062, 0x2063,
             0x2064, 0x2066, 0x2067, 0x2068, 0x2069, 0xFEFF}
INVISIBLE.update(range(0xE0000, 0xE0080))

def scan_tree(label, root, globs=('*.json', '*.txt', '*.md', '*.jsonl')):
    if not root.exists():
        add('정상', label, 'not present', 'none')
        return
    matched_files = 0
    cat_counts = {}
    seen = set()
    for glob in globs:
        for p in root.rglob(glob):
            if not p.is_file() or p in seen:
                continue
            seen.add(p)
            try:
                text = p.read_text(encoding='utf-8', errors='replace')
            except OSError:
                add('수동필요', label, 'unreadable file encountered', 'inspect permissions manually; contents not printed')
                continue
            file_cats = set()
            for cat, rx in patterns:
                if rx.search(text):
                    file_cats.add(cat)
            if any(ord(ch) in INVISIBLE for ch in text):
                file_cats.add('invisible-unicode')
            if file_cats:
                matched_files += 1
                for cat in file_cats:
                    cat_counts[cat] = cat_counts.get(cat, 0) + 1
    if matched_files:
        cats = ', '.join(f'{k}:{v}' for k, v in sorted(cat_counts.items()))
        add('위험', label, f'{matched_files} file(s) matched categories: {cats}', 'redact/rotate outside this read-only audit; matched text not printed')
    else:
        add('정상', label, 'no credential/prompt-injection/invisible-unicode patterns detected', 'none')

def check_sensitive_perms(label, path, expected_max=0o600):
    if not path.exists():
        add('정상', f'permissions:{label}', 'not present', 'none')
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        add('수동필요', f'permissions:{label}', 'stat failed', 'inspect file permissions manually')
        return
    if mode & 0o077:
        add('교정가능' if '교정가능' in counts else '위험', f'permissions:{label}', f'mode {mode:04o} allows group/other access', 'future --fix should chmod 0600 after backup')
    elif mode <= expected_max:
        add('정상', f'permissions:{label}', f'mode {mode:04o}', 'none')
    else:
        add('경고', f'permissions:{label}', f'mode {mode:04o}', 'review permission policy')

# File permission posture. Labels avoid printing full sensitive paths.
check_sensitive_perms('Claude credentials', claude_dir / '.credentials.json')
check_sensitive_perms('Honcho config', hermes_dir / 'honcho.json')
check_sensitive_perms('node MEMORY', claude_dir / 'memories' / 'MEMORY.md')
check_sensitive_perms('node USER', claude_dir / 'memories' / 'USER.md')

# Hook executable/world-writable posture.
hook_dir = claude_dir / 'hooks'
if hook_dir.exists():
    bad = 0
    missing_exec = 0
    for p in hook_dir.glob('*.sh'):
        try:
            mode = stat.S_IMODE(p.stat().st_mode)
        except OSError:
            bad += 1
            continue
        if mode & 0o002:
            bad += 1
        if not (mode & 0o111):
            missing_exec += 1
    if bad:
        add('위험', 'hook permissions', f'{bad} hook file(s) world-writable or unreadable', 'repair permissions after backup; filenames not printed')
    elif missing_exec:
        add('경고', 'hook permissions', f'{missing_exec} hook file(s) are not executable', 'setup.sh normally chmod +x hooks/*.sh')
    else:
        add('정상', 'hook permissions', 'hook shell files are executable and not world-writable', 'none')
else:
    add('경고', 'hook permissions', 'hook directory not present', 'node may not be installed as ccc-node')

# Settings allowlist posture. Operator decision TM-1306 (2026-07-18): the fleet
# runs the NATIVE Claude Code posture — no semantic PreToolUse guard, no native
# permissions.deny backstop. Remaining safety layers are behavioral policy,
# the bridge single-owner allowlist, and the audit trail. A guard hook or deny
# entries still present indicate a pre-TM-1306 install that needs setup rerun.
settings = claude_dir / 'settings.json'
if settings.exists():
    try:
        data = json.loads(settings.read_text(encoding='utf-8'))
        allow = data.get('permissions', {}).get('allow', []) or []
        deny = data.get('permissions', {}).get('deny', []) or []
        hooks = data.get('hooks', {}) or {}
        has_guard = any('guard.sh' in h.get('command', '') for ev in hooks.values() for group in ([ev] if isinstance(ev, dict) else ev) for h in group.get('hooks', []))
        broad = any(x in allow for x in ['Bash(*)', 'Read(*)', 'Write(*)', 'Edit(*)', 'MultiEdit(*)'])
        if has_guard or deny:
            add('경고', 'settings allowlist', 'legacy enforcement remnants present (guard hook and/or native deny entries)', 'rerun setup.sh to apply the native posture (TM-1306)')
        elif broad:
            add('정상', 'settings allowlist', 'native posture: broad allowlist without semantic guard or native deny (operator decision TM-1306)', 'none')
        else:
            add('경고', 'settings allowlist', 'nonstandard permissions posture', 'review against ccc-node policy')
    except Exception:
        add('수동필요', 'settings allowlist', 'settings JSON unreadable/invalid', 'repair settings manually; contents not printed')
else:
    add('경고', 'settings allowlist', 'settings.json not present', 'node may not be installed as ccc-node')

# Scanner integrity.
scanner = repo / 'claude' / 'hooks' / 'scan-injection.sh'
if scanner.exists():
    rc = subprocess.run(['bash', '-n', str(scanner)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    if rc == 0:
        add('정상', 'scan-injection.sh', 'present and bash -n passes', 'none')
    else:
        add('위험', 'scan-injection.sh', 'bash -n failed', 'fix scanner syntax before release')
else:
    add('위험', 'scan-injection.sh', 'missing from repo', 'restore memory-injection scanner')

setup = repo / 'setup.sh'
setup_text = setup.read_text(encoding='utf-8', errors='replace') if setup.exists() else ''
# setup.sh deploys the whole claude/hooks tree via the shared hook-tree walk
# (ccc_hook_tree_files, #569); an explicit per-file cp is the legacy form.
if 'ccc_hook_tree_files' in setup_text or 'scan-injection.sh' in setup_text:
    add('정상', 'scanner install wiring', 'setup.sh installs scan-injection.sh (hook-tree walk)', 'none')
else:
    add('위험', 'scanner install wiring', 'setup.sh does not install scan-injection.sh', 'restore setup.sh hook-tree deployment')

# Metadata-only content scans.
scan_tree('push spool redaction', spool_dir)
scan_tree('memory cache redaction', cache_dir)

print('# ccc security audit\n')
print(f'- repo: `{repo}`')
print(f'- claude dir: `{claude_dir}`')
print('- output policy: metadata-only; matched text and file contents are never printed\n')
print('## 진단 요약\n')
for key in ['정상', '경고', '위험', '수동필요']:
    print(f'- {key}: {counts.get(key, 0)}')
print('\n| 분류 | 항목 | 상태 | 조치 |')
print('|---|---|---|---|')
for cls, item, status, action in rows:
    print(f'| {cls} | `{item}` | {status} | {action} |')
print('\n## 경계\n')
print('- This command is read-only in the current slice.')
print('- It does not print raw secrets, matched text, or scanned file contents.')
print('- No permission changes, rotations, restarts, provider sends, DB/ACK/replay, or remote-node actions are performed.')
print('- `--fix` is reserved for a later backup + dry-run + idempotent repair slice.')

raise SystemExit(1 if counts.get('위험', 0) or counts.get('수동필요', 0) else 0)
PY
