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
# Plugin layout: marketplace at .claude-plugin/marketplace.json (source ./claude);
# the plugin root is claude/, so its manifest is claude/.claude-plugin/plugin.json and
# its hook config is the auto-discovered claude/hooks/hooks.json. Components are
# auto-discovered from claude/{agents,commands,skills} — the manifest carries NO path
# fields, because this CLI silently loads 0 components from custom agents/commands path
# arrays (verified on 2.1.183); only default-location discovery is honoured.
say "== settings JSON =="
for f in claude/settings.json claude/settings.local.json \
         .claude-plugin/marketplace.json \
         claude/.claude-plugin/plugin.json claude/hooks/hooks.json; do
  [ -f "$f" ] || { say "  (skip $f — absent)"; continue; }
  if jq -e . "$f" >/dev/null 2>&1; then say "  ok $f"; else err "invalid JSON: $f"; fi
done

# 1b) plugin manifest + marketplace catalog + runtime hook-path resolution
if [ -f claude/.claude-plugin/plugin.json ]; then
  say "== plugin manifest =="
  jq -e '.name' claude/.claude-plugin/plugin.json >/dev/null 2>&1 && say "  ok plugin.json has name" || err "plugin.json missing name"
  # Guard against the silent-load trap: agents/commands custom-path fields don't load.
  if jq -e 'has("agents") or has("commands") or has("hooks")' claude/.claude-plugin/plugin.json >/dev/null 2>&1; then
    err "plugin.json must NOT set agents/commands/hooks path fields — they silently load 0 components; rely on default-location discovery under claude/"
  else
    say "  ok plugin.json has no silent-load path fields"
  fi
  # marketplace source must point the plugin root at ./claude (where the components live)
  src="$(jq -r '.plugins[0].source // empty' .claude-plugin/marketplace.json 2>/dev/null)"
  jq -e '.plugins[0].name' .claude-plugin/marketplace.json >/dev/null 2>&1 \
    && say "  ok marketplace.json catalog" || err "marketplace.json malformed"
  [ "$src" = "./claude" ] && say "  ok marketplace source -> ./claude" \
    || err "marketplace plugin source must be \"./claude\" (got: ${src:-<unset>})"
  # default-discovery dirs must exist under the plugin root
  for d in claude/agents claude/commands claude/skills; do
    [ -d "$d" ] && say "  ok component dir $d" || err "missing component dir: $d"
  done
fi

# 1c) hooks.json runtime-path resolution — the check that catches broken ${CLAUDE_PLUGIN_ROOT}
# references (plugin root = claude/, so ${CLAUDE_PLUGIN_ROOT}/X resolves to claude/X).
if [ -f claude/hooks/hooks.json ]; then
  say "== hook-path resolution =="
  mapfile -t HK < <(jq -r '.. | .command? // empty' claude/hooks/hooks.json 2>/dev/null \
    | grep -oE '\$\{CLAUDE_PLUGIN_ROOT\}"?/[A-Za-z0-9_./-]+\.sh' | sed -E 's#.*\}"?/##' | sort -u)
  [ "${#HK[@]}" -gt 0 ] || err "hooks.json references no \${CLAUDE_PLUGIN_ROOT} scripts"
  for rel in "${HK[@]}"; do
    if [ -f "claude/$rel" ]; then say "  ok \${CLAUDE_PLUGIN_ROOT}/$rel -> claude/$rel"
    else err "hooks.json references missing script: \${CLAUDE_PLUGIN_ROOT}/$rel (expected claude/$rel)"; fi
  done
fi

# 1d) best-effort real load check via the Claude CLI (non-blocking if absent)
if command -v claude >/dev/null 2>&1; then
  say "== claude plugin validate =="
  if claude plugin validate . >/tmp/pluginval.out 2>&1; then
    say "  ok claude plugin validate (see /tmp/pluginval.out)"
  else
    say "  (claude plugin validate reported issues — review /tmp/pluginval.out; non-blocking)"
  fi
fi

# 2) shell syntax (bash -n) on all hook + top-level scripts
say "== bash -n =="
mapfile -t SH < <(find claude/hooks scripts -name '*.sh' 2>/dev/null; echo setup.sh; echo claude/mcp-setup.sh; echo claude/headless.sh)
for f in "${SH[@]}"; do
  [ -f "$f" ] || continue
  if bash -n "$f" 2>/dev/null; then say "  ok $f"; else err "bash -n: $f"; fi
done

# 3) shellcheck — scoped to reviewed scripts (blocking); others get bash -n only above.
say "== shellcheck =="
SC_SCOPE=(claude/hooks/guard.sh claude/hooks/audit.sh claude/hooks/redact.sh \
          claude/hooks/notify.sh claude/hooks/statusline.sh claude/headless.sh \
          claude/hooks/guard.test.sh \
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
for f in claude/output-styles/*.md; do [ -f "$f" ] && fm_check "$f" && say "  ok $f"; done
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

# 7) Tier 3: statusline smoke + settings wiring
say "== statusline + settings wiring =="
if [ -f claude/hooks/statusline.sh ]; then
  SAMPLE='{"model":{"display_name":"T"},"context_window":{"used_percentage":42.5},"cost":{"total_cost_usd":1.2},"exceeds_200k_tokens":true,"output_style":{"name":"ccc-report"},"workspace":{"current_dir":"'"$ROOT"'"}}'
  if out="$(printf '%s' "$SAMPLE" | CCC_NODE=ci bash claude/hooks/statusline.sh 2>/dev/null)" && [ -n "$out" ]; then
    say "  ok statusline.sh emits output"
  else err "statusline.sh produced no output / non-zero"; fi
  # empty input must not crash (fail-open to a usable bar)
  printf '%s' '' | CCC_NODE=ci bash claude/hooks/statusline.sh >/dev/null 2>&1 \
    && say "  ok statusline.sh survives empty input" || err "statusline.sh crashed on empty input"
fi
# settings.json statusLine command must point at an installed script that exists in-repo
SL_CMD="$(jq -r '.statusLine.command // empty' claude/settings.json 2>/dev/null)"
if [ -n "$SL_CMD" ]; then
  base="claude/hooks/$(basename "${SL_CMD##* }")"
  [ -f "$base" ] && say "  ok statusLine -> $base" || err "settings statusLine references missing script: $SL_CMD ($base)"
fi
# settings.json outputStyle must name a shipped output-style file
OS="$(jq -r '.outputStyle // empty' claude/settings.json 2>/dev/null)"
if [ -n "$OS" ]; then
  if grep -rqi "^name:[[:space:]]*$OS\b" claude/output-styles/*.md 2>/dev/null \
     || [ -f "claude/output-styles/$OS.md" ]; then say "  ok outputStyle -> $OS";
  else err "settings outputStyle '$OS' has no matching claude/output-styles/*.md"; fi
fi

say "===================="
if [ "$fail" = "0" ]; then say "HARNESS VALIDATION: PASS"; else say "HARNESS VALIDATION: FAIL"; fi
exit "$fail"
