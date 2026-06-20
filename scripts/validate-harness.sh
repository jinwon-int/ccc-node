#!/usr/bin/env bash
# Harness self-validation — runnable locally and in CI.
# Validates the Claude Code harness template: settings JSON, hook scripts, hook tests,
# skill/agent frontmatter, and that hooks referenced by settings.json exist.
# Exit non-zero on any failure. shellcheck/bats are optional (skipped with a note if absent).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
fail=0
say() { printf '%s\n' "$*"; }
err() { printf 'FAIL: %s\n' "$*"; fail=1; }

# 1) JSON validity
say "== settings JSON =="
for f in claude/settings.json claude/settings.local.json \
         .claude-plugin/plugin.json .claude-plugin/marketplace.json hooks/hooks.json; do
  [ -f "$f" ] || { say "  (skip $f — absent)"; continue; }
  if jq -e . "$f" >/dev/null 2>&1; then say "  ok $f"; else err "invalid JSON: $f"; fi
done

# 1b) plugin manifest: name present + referenced component paths exist
if [ -f .claude-plugin/plugin.json ]; then
  say "== plugin manifest =="
  jq -e '.name' .claude-plugin/plugin.json >/dev/null 2>&1 && say "  ok plugin.json has name" || err "plugin.json missing name"
  mapfile -t PPATHS < <(jq -r '[.skills, .hooks, (.commands[]?), (.agents[]?)] | .[] | select(type=="string")' .claude-plugin/plugin.json 2>/dev/null)
  for p in "${PPATHS[@]}"; do
    rel="${p#./}"
    if [ -e "$rel" ]; then say "  ok path $rel"; else err "plugin.json path missing: $p"; fi
  done
  jq -e '.plugins[0].name and .plugins[0].source' .claude-plugin/marketplace.json >/dev/null 2>&1 \
    && say "  ok marketplace.json catalog" || err "marketplace.json malformed"
fi

# 2) shell syntax (bash -n) on all hook + top-level scripts
say "== bash -n =="
mapfile -t SH < <(find claude/hooks scripts -name '*.sh' 2>/dev/null; echo setup.sh; echo claude/mcp-setup.sh)
for f in "${SH[@]}"; do
  [ -f "$f" ] || continue
  if bash -n "$f" 2>/dev/null; then say "  ok $f"; else err "bash -n: $f"; fi
done

# 3) shellcheck — scoped to reviewed scripts (blocking); others get bash -n only above.
say "== shellcheck =="
SC_SCOPE=(claude/hooks/guard.sh claude/hooks/audit.sh claude/hooks/redact.sh \
          claude/hooks/notify.sh claude/hooks/guard.test.sh \
          claude/hooks/observability.test.sh scripts/validate-harness.sh)
if command -v shellcheck >/dev/null 2>&1; then
  for f in "${SC_SCOPE[@]}"; do
    [ -f "$f" ] || continue
    if shellcheck --severity=warning -e SC2155,SC1090,SC1091 "$f"; then say "  ok $f"; else err "shellcheck: $f"; fi
  done
else
  say "  (shellcheck absent — skipped)"
fi

# 4) hook tests
say "== hook tests =="
for t in claude/hooks/guard.test.sh claude/hooks/observability.test.sh; do
  [ -f "$t" ] || { err "missing test: $t"; continue; }
  if bash "$t" >/tmp/htest.out 2>&1; then say "  ok $(grep -E 'PASS=' /tmp/htest.out | tail -1) $t";
  else err "test failed: $t"; tail -5 /tmp/htest.out; fi
done

# 5) skill + agent frontmatter (must start with --- and carry name: + description:)
say "== frontmatter =="
fm_check() { # <file>
  local f="$1"
  head -1 "$f" | grep -q '^---' || { err "no frontmatter: $f"; return; }
  awk 'NR>1 && /^---/{exit} {print}' "$f" | grep -q '^name:'        || err "no name: in $f"
  awk 'NR>1 && /^---/{exit} {print}' "$f" | grep -q '^description:' || err "no description: in $f"
}
for f in claude/skills/*/SKILL.md; do [ -f "$f" ] && fm_check "$f" && say "  ok $f"; done
for f in claude/agents/*.md;      do [ -f "$f" ] && fm_check "$f" && say "  ok $f"; done
# slash commands: frontmatter must carry description: (command name = filename, so no name:)
for f in claude/commands/*.md; do
  [ -f "$f" ] || continue
  head -1 "$f" | grep -q '^---' || { err "no frontmatter: $f"; continue; }
  awk 'NR>1 && /^---/{exit} {print}' "$f" | grep -q '^description:' || err "no description: in $f"
  say "  ok $f"
done

# 6) hooks referenced by settings.json must exist on disk
say "== referenced hooks exist =="
mapfile -t REFS < <(jq -r '.. | .command? // empty' claude/settings.json 2>/dev/null | grep -oE '/root/.claude/hooks/[A-Za-z0-9_.-]+\.sh' | sort -u)
for r in "${REFS[@]}"; do
  base="claude/hooks/$(basename "$r")"
  if [ -f "$base" ]; then say "  ok $base"; else err "settings.json references missing hook: $r ($base)"; fi
done

say "===================="
if [ "$fail" = "0" ]; then say "HARNESS VALIDATION: PASS"; else say "HARNESS VALIDATION: FAIL"; fi
exit "$fail"
