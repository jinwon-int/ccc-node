#!/usr/bin/env bash
# Harness self-validation — runnable locally and in CI.
# Validates the Claude Code harness template: settings JSON, hook scripts, hook tests,
# skill/agent frontmatter, and that hooks referenced by settings.json exist.
# Exit non-zero on any failure. shellcheck/bats are optional (skipped with a note if absent).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
TMP="${TMPDIR:-/tmp}"; mkdir -p "$TMP" 2>/dev/null || TMP="$ROOT/.harness-tmp"; mkdir -p "$TMP" 2>/dev/null
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
for f in claude/settings.base.json claude/settings.local.json \
         claude/hooks/enforcement-overlay.json \
         .claude-plugin/marketplace.json \
         claude/.claude-plugin/plugin.json claude/hooks/hooks.json \
         schemas/agent-cron-task-store.schema.json; do
  [ -f "$f" ] || { say "  (skip $f — absent)"; continue; }
  if jq -e . "$f" >/dev/null 2>&1; then say "  ok $f"; else err "invalid JSON: $f"; fi
done

# 1a) Fail closed if OpenClaw runtime/bootstrap context files are tracked.
say "== OpenClaw context guard =="
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  offenders=()
  while IFS= read -r f; do
    base="${f##*/}"
    case "$f" in
      .openclaw/*|*/.openclaw/*) offenders+=("$f") ;;
      *)
        case "$base" in
          AGENTS.md|SOUL.md|USER.md|TOOLS.md|HEARTBEAT.md|IDENTITY.md) offenders+=("$f") ;;
        esac
        ;;
    esac
  done < <(git ls-files)
  if [ "${#offenders[@]}" -eq 0 ]; then
    say "  ok no OpenClaw runtime/bootstrap context files tracked"
  else
    err "OpenClaw runtime/bootstrap context files tracked: ${offenders[*]}"
  fi
else
  say "  (git unavailable or not a worktree — skipped)"
fi

# 1b) CLAUDE.md template policy blocks
say "== CLAUDE.md template policy =="
if [ -f claude/CLAUDE.md.template ]; then
  grep -q '^## Standing Orders$' claude/CLAUDE.md.template \
    && say "  ok Standing Orders section" || err "CLAUDE.md.template missing Standing Orders section"
  grep -q '| Workstream | Autonomy scope | Trigger | Approval gate | Escalation |' claude/CLAUDE.md.template \
    && say "  ok Standing Orders table columns" || err "Standing Orders table missing required columns"
  grep -q 'Fresh Approval Required always wins' claude/CLAUDE.md.template \
    && say "  ok Fresh Approval precedence stated" || err "Standing Orders must state Fresh Approval precedence"
  grep -q '| Fresh-approval operations |' claude/CLAUDE.md.template \
    && say "  ok Fresh-approval operations row" || err "Standing Orders missing Fresh-approval operations row"
else
  err "missing claude/CLAUDE.md.template"
fi

# 1c) plugin manifest + marketplace catalog + runtime hook-path resolution
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

# 1d) hooks.json runtime-path resolution — the check that catches broken ${CLAUDE_PLUGIN_ROOT}
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

# 1e) best-effort real load check via the Claude CLI (non-blocking if absent)
if command -v claude >/dev/null 2>&1; then
  say "== claude plugin validate =="
  if claude plugin validate . >"$TMP/pluginval.out" 2>&1; then
    say "  ok claude plugin validate (see $TMP/pluginval.out)"
  else
    say "  (claude plugin validate reported issues — review $TMP/pluginval.out; non-blocking)"
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
          claude/hooks/guard.test.sh scripts/ccc-service-control.sh \
          scripts/ccc-service-control.test.sh \
          claude/hooks/observability.test.sh scripts/validate-harness.sh)
if command -v shellcheck >/dev/null 2>&1; then
  for f in "${SC_SCOPE[@]}"; do
    [ -f "$f" ] || continue
    if shellcheck --severity=warning -e SC2155,SC1090,SC1091 "$f"; then say "  ok $f"; else err "shellcheck: $f"; fi
  done
else
  say "  (shellcheck absent — skipped)"
fi

# 3b) guard.py — the PreToolUse enforcement logic behind the guard.sh shim. A
# syntax error would make the shim fail OPEN (guard silently unenforced), so
# compile it here and confirm setup.sh installs it alongside the shim.
say "== guard.py (python enforcement) =="
if command -v python3 >/dev/null 2>&1; then
  if python3 -m py_compile claude/hooks/guard.py 2>/dev/null; then say "  ok claude/hooks/guard.py compiles"; else err "py_compile: claude/hooks/guard.py"; fi
else
  say "  (python3 absent — skipped)"
fi
if grep -Fq 'run cp "$SRC/claude/hooks/guard.py"' setup.sh 2>/dev/null; then
  say "  ok setup.sh installs guard.py"
else
  err "setup.sh does not install guard.py (guard.sh shim would fail open)"
fi

# 4) hook tests
say "== hook tests =="
for t in claude/hooks/guard.test.sh claude/hooks/observability.test.sh claude/hooks/security-scan.test.sh \
         claude/hooks/checkpoint.test.sh claude/hooks/distill-scope.test.sh claude/hooks/skill-review.test.sh \
         claude/hooks/distill/extract.test.sh claude/hooks/distill/honcho-push.test.sh \
         claude/hooks/distill/queue-drain.test.sh claude/hooks/distill/wiki-queue.test.sh \
         claude/hooks/distill/local-facts.test.sh claude/hooks/memory-hooks.test.sh \
         scripts/ccc-doctor.test.sh scripts/ccc-memory.test.sh scripts/ccc-distill-check.test.sh scripts/ccc-security-audit.test.sh \
         scripts/ccc-fleet-matrix.test.sh scripts/ccc-wiki-triage.test.sh scripts/setup.test.sh \
         scripts/harness-paths.test.sh \
         scripts/agent-cron.test.sh scripts/agent-cron-lib.test.sh scripts/a2a-termux-native-worker.test.sh \
         scripts/a2a-termux-native-worker-health.test.sh \
         scripts/install-memory-refresh-cron.test.sh scripts/ccc-skill-autosave.test.sh \
         scripts/ccc-self-update.test.sh scripts/ccc-provenance.test.sh \
         scripts/ccc-service-control.test.sh; do
  [ -f "$t" ] || { err "missing test: $t"; continue; }
  if bash "$t" >"$TMP/htest.out" 2>&1; then say "  ok $(grep -E 'PASS=' "$TMP/htest.out" | tail -1) $t";
  else err "test failed: $t"; tail -5 "$TMP/htest.out"; fi
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
# A2A subagent cost-tier metadata (#54): advisory only; no hard-coded model routing.
for f in claude/agents/a2a-*.md; do
  [ -f "$f" ] || continue
  fm="$(awk 'NR>1 && /^---/{exit} {print}' "$f")"
  if grep -q '^model_tier:[[:space:]]*\(low-cost\|upper\)$' <<<"$fm"; then say "  ok model_tier $f";
  else err "missing/invalid model_tier in $f (expected low-cost or upper)"; fi
  grep -q '^model_tier_default:[[:space:]]*inherit-parent-unless-overridden$' <<<"$fm" \
    && say "  ok model_tier_default $f" || err "missing safe model_tier_default in $f"
  grep -qi 'cost/token' "$f" \
    && say "  ok cost/token reporting note $f" || err "missing cost/token reporting note in $f"
done
for f in claude/output-styles/*.md; do [ -f "$f" ] && fm_check "$f" && say "  ok $f"; done
# slash commands: frontmatter must carry description: (command name = filename, so no name:)
for f in claude/commands/*.md; do
  [ -f "$f" ] || continue
  head -1 "$f" | grep -q '^---' || { err "no frontmatter: $f"; continue; }
  awk 'NR>1 && /^---/{exit} {print}' "$f" | grep -q '^description:' || err "no description: in $f"
  say "  ok $f"
done

# 6) hooks referenced by settings (base + overlay) must exist on disk
say "== referenced hooks exist =="
mapfile -t REFS < <(jq -r '.. | .command? // empty' claude/settings.base.json claude/hooks/enforcement-overlay.json 2>/dev/null | grep -oE '/root/.claude/hooks/[A-Za-z0-9_.-]+\.sh' | sort -u)
for r in "${REFS[@]}"; do
  base="claude/hooks/$(basename "$r")"
  if [ -f "$base" ]; then say "  ok $base"; else err "settings references missing hook: $r ($base)"; fi
done

# 6a) every referenced hook must ALSO be installed by setup.sh — a hook that exists in the
# repo but is not copied to ~/.claude would be referenced-but-missing on a real install
# (e.g. evidence-gate.sh was added to the Stop hook but initially omitted from setup.sh).
say "== referenced hooks installed by setup.sh =="
for r in "${REFS[@]}"; do
  hook="$(basename "$r")"
  if grep -Fq "run cp \"\$SRC/claude/hooks/$hook\"" setup.sh 2>/dev/null; then
    say "  ok setup.sh installs $hook"
  else
    err "setup.sh does not install referenced hook: $hook"
  fi
done

# 6b) Single-owner invariant: base (node-local) and overlay (portable) must NOT share any
# hook event, or a standalone install would double-register; and the overlay must match the
# plugin's hooks/hooks.json modulo the path prefix (same events, matchers, script basenames),
# so the two registration paths (setup.sh vs plugin) stay in sync.
say "== settings base/overlay/plugin parity =="
shared="$(jq -rn --slurpfile b claude/settings.base.json --slurpfile o claude/hooks/enforcement-overlay.json \
  '($b[0].hooks|keys) as $bk | ($o[0].hooks|keys) as $ok | ($bk - ($bk - $ok)) | .[]' 2>/dev/null)"
[ -z "$shared" ] && say "  ok base/overlay hook events disjoint" \
  || err "base and overlay share hook event(s) — would double-fire standalone: $shared"
# normalize: event -> sorted "matcher|basename(cmd)" set, comparing overlay vs plugin hooks.json
norm() { jq -S '.hooks | to_entries | map({event:.key, items:(.value|map({m:(.matcher//""),
          c:(.hooks|map(.command|capture("/(?<b>[A-Za-z0-9_.-]+\\.sh)").b // .)|sort)})|sort)})' "$1" 2>/dev/null; }
if diff <(norm claude/hooks/enforcement-overlay.json) <(norm claude/hooks/hooks.json) >/dev/null 2>&1; then
  say "  ok overlay ≡ plugin hooks.json (events/matchers/scripts match modulo path)"
else
  err "overlay and plugin hooks/hooks.json diverged — setup.sh and plugin would enforce differently"
fi
# 6c) Rendered standalone settings (base + overlay) must be valid and carry all hook events.
if jq -s '.[0] as $b | .[1] as $o | $b | .hooks = ($b.hooks + $o.hooks)' \
     claude/settings.base.json claude/hooks/enforcement-overlay.json >"$TMP/rendered.json" 2>/dev/null \
   && jq -e '.hooks.PreToolUse and .hooks.SessionStart and .statusLine and .outputStyle' "$TMP/rendered.json" >/dev/null 2>&1; then
  say "  ok rendered standalone settings valid (node-local + portable + statusLine + outputStyle)"
else
  err "rendered standalone settings (base+overlay) invalid or missing expected keys"
fi

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
# settings statusLine command must point at an installed script that exists in-repo
SL_CMD="$(jq -r '.statusLine.command // empty' claude/settings.base.json 2>/dev/null)"
if [ -n "$SL_CMD" ]; then
  base="claude/hooks/$(basename "${SL_CMD##* }")"
  [ -f "$base" ] && say "  ok statusLine -> $base" || err "settings statusLine references missing script: $SL_CMD ($base)"
fi
# settings outputStyle must name a shipped output-style file
OS="$(jq -r '.outputStyle // empty' claude/settings.base.json 2>/dev/null)"
if [ -n "$OS" ]; then
  if grep -rqi "^name:[[:space:]]*$OS\b" claude/output-styles/*.md 2>/dev/null \
     || [ -f "claude/output-styles/$OS.md" ]; then say "  ok outputStyle -> $OS";
  else err "settings outputStyle '$OS' has no matching claude/output-styles/*.md"; fi
fi

say "===================="
if [ "$fail" = "0" ]; then say "HARNESS VALIDATION: PASS"; else say "HARNESS VALIDATION: FAIL"; fi
exit "$fail"
