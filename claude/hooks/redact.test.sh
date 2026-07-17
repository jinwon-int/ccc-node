#!/usr/bin/env bash
# Tests for redact.sh — the UserPromptSubmit secret-awareness hook.
# Usage: bash redact.test.sh   (exit 0 = all pass)
#
# Hermetic: audit output is routed to a throwaway CCC_AUDIT_LOG and HOME points
# at a temp dir, so runs never touch the node's real ~/.claude/state. Credential
# fixtures are ASSEMBLED AT RUNTIME (prefix + generated body) so no literal that
# secret scanners flag ever appears in this file or in a transcript.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="$HERE/redact.sh"
pass=0; fail=0

TDIR="$(mktemp -d 2>/dev/null || mktemp -d -t ccc-redact-test)"
trap 'rm -rf "$TDIR" 2>/dev/null || true' EXIT
export HOME="$TDIR/home"
export CCC_AUDIT_LOG="$TDIR/audit.jsonl"
mkdir -p "$HOME"

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# run_hook <prompt> -> captures stdout in $out, rc in $rc
run_hook() {
  local prompt="$1" payload
  payload="$(jq -nc --arg p "$prompt" '{prompt:$p}')"
  out="$(printf '%s' "$payload" | bash "$HOOK" 2>/dev/null)"; rc=$?
}

# Runtime-assembled fixtures (pattern-matching, obviously fake).
body_alnum="$(printf 'A%.0s' $(seq 1 30))"
gh_tok="ghp_${body_alnum}"
sk_tok="sk-${body_alnum}"
aws_tok="AKIA$(printf 'B%.0s' $(seq 1 16))"
key_marker="$(printf -- '-----BEGIN %s PRIVATE KEY-----' RSA)"

# 1) GitHub-style token trips the warning
rm -f "$CCC_AUDIT_LOG"
run_hook "please use ${gh_tok} for the deploy"
ok "github-style token produces a warning" \
  '[ "$rc" = 0 ] && jq -e ".hookSpecificOutput.hookEventName == \"UserPromptSubmit\"" <<<"$out" >/dev/null'
ok "warning text never echoes the credential" \
  '! grep -Fq "$gh_tok" <<<"$out"'
ok "detection is audited to CCC_AUDIT_LOG" \
  '[ -s "$CCC_AUDIT_LOG" ] && jq -e ".flag == \"possible-raw-credential\"" "$CCC_AUDIT_LOG" >/dev/null'
ok "audit record never contains the credential" \
  '! grep -Fq "$gh_tok" "$CCC_AUDIT_LOG"'

# 2) Other credential shapes also trip it
for tok in "$sk_tok" "$aws_tok" "$key_marker"; do
  run_hook "context: $tok end"
  ok "credential shape flagged: ${tok:0:9}..." \
    '[ "$rc" = 0 ] && [ -n "$out" ] && jq -e ".hookSpecificOutput.additionalContext" <<<"$out" >/dev/null'
done

# 3) Benign prompt stays silent (no context injection, no audit entry)
rm -f "$CCC_AUDIT_LOG"
run_hook "please review the PR and summarize the diff"
ok "benign prompt produces no output" '[ "$rc" = 0 ] && [ -z "$out" ]'
ok "benign prompt is not audited" '[ ! -s "$CCC_AUDIT_LOG" ]'

# 4) Robustness: empty and non-JSON stdin are silent successes
out="$(printf '' | bash "$HOOK" 2>/dev/null)"; rc=$?
ok "empty stdin exits 0 silently" '[ "$rc" = 0 ] && [ -z "$out" ]'
out="$(printf 'not json at all' | bash "$HOOK" 2>/dev/null)"; rc=$?
ok "non-JSON stdin exits 0 silently" '[ "$rc" = 0 ] && [ -z "$out" ]'

# 5) The hook never mutates or blocks: output (when present) is pure JSON context
run_hook "token check ${gh_tok}"
ok "warning output is a single valid JSON object" \
  'jq -e . <<<"$out" >/dev/null'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = "0" ]
