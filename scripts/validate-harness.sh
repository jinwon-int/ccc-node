#!/usr/bin/env bash
# Harness self-validation — runnable locally and in CI.
# Validates the Claude Code harness template: settings JSON, hook scripts, hook tests,
# skill/agent frontmatter, and that hooks referenced by settings.json exist.
# Exit non-zero on any failure. shellcheck/bats are optional (skipped with a note if absent).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
# Private scratch dir. Validation writes fixed-name artifacts (rendered.json,
# rendered.json, htest.out, ...); pointing at a SHARED ${TMPDIR:-/tmp} makes
# runs collide with other users' stale copies — with fs.protected_regular the
# open is denied outright — and the run false-FAILs (observed on gwakga:
# /tmp/rendered.json left by another account). Always use a fresh private dir.
TMP="$(mktemp -d 2>/dev/null || mktemp -d -t ccc-validate 2>/dev/null)" \
  || { TMP="$ROOT/.harness-tmp.$$"; mkdir -p "$TMP"; }
trap 'rm -rf "$TMP" 2>/dev/null || true' EXIT
# Child test suites resolve ${TMPDIR:-/tmp} themselves (mktemp, fixed-name
# artifacts like checkpoint-guard.out) — export the private dir so the WHOLE
# validation run, children included, stays clear of hostile shared /tmp state
# (review finding on #565: a stale root-owned checkpoint-guard.out false-FAILed
# checkpoint.test.sh through the shared caller TMPDIR).
export TMPDIR="$TMP"
# Test seam: print the resolved scratch contract and exit (used by
# scripts/validate-harness.test.sh to pin private-TMP + TMPDIR propagation).
if [ "${1:-}" = "--print-scratch" ]; then
  # One path per line: whitespace in a valid scratch root must not break the
  # consumer's parsing (review finding on #565).
  printf '%s\n%s\n' "$TMP" "$TMPDIR"
  exit 0
fi
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
for f in claude/settings.base.json claude/settings.local.template.json \
         claude/hooks/enforcement-overlay.json \
         .claude-plugin/marketplace.json \
         claude/.claude-plugin/plugin.json claude/hooks/hooks.json \
         schemas/auto-distill-evaluation-receipt-v1.schema.json \
         schemas/agent-cron-task-store.schema.json \
         architecture/architecture-contract-v1.json \
         architecture/side-effect-contract-v1.json; do
  [ -f "$f" ] || { say "  (skip $f — absent)"; continue; }
  if jq -e . "$f" >/dev/null 2>&1; then say "  ok $f"; else err "invalid JSON: $f"; fi
done
if jq -e '.permissions.defaultMode == "bypassPermissions"' \
     claude/settings.base.json >/dev/null 2>&1; then
  say "  ok Claude native permission mode bypasses approval prompts (non-root default)"
else
  err "Claude native permission mode is not bypassPermissions"
fi
# Claude Code natively refuses bypassPermissions under root, so setup must
# neutralize the default on a root node. Keep that root-aware install path
# present so the source default cannot brick root-run nodes.
if grep -q "neutralize_bypass_if_root" setup.sh; then
  say "  ok setup neutralizes the bypassPermissions default under root"
else
  err "setup.sh missing root-aware bypassPermissions neutralization"
fi

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

# 1b-1) Wiki log IDs are node/date scoped. The old global max+1 guidance caused
# concurrent fleet writers to allocate duplicate LOG-NNNN identifiers.
say "== Wiki LOG namespace policy =="
WIKI_RULE_FILES=(skills/wiki-record/SKILL.md claude/commands/wiki-log.md \
                 claude/hooks/tools-cheatsheet.md hermes/memories/MEMORY.template.md)
for f in "${WIKI_RULE_FILES[@]}"; do
  grep -q 'LOG-YYYYMMDD-<node>-' "$f" \
    && say "  ok node-scoped LOG guidance $f" || err "missing node-scoped LOG guidance: $f"
done
if grep -Fq 'LOG-<max+1>' "${WIKI_RULE_FILES[@]}" \
   || grep -Fq 'grep -hoE "\[LOG-[0-9]+\]"' "${WIKI_RULE_FILES[@]}"; then
  err "legacy global numeric LOG allocation guidance remains"
else
  say "  ok no legacy global numeric LOG allocation guidance"
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
  for d in claude/agents claude/commands skills; do
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

# 2) shell syntax (bash -n) on hooks, skill helpers, and top-level scripts
say "== bash -n =="
mapfile -t SH < <(find claude/hooks skills scripts codex crush -name '*.sh' 2>/dev/null; echo setup.sh; echo claude/mcp-setup.sh; echo claude/headless.sh)
for f in "${SH[@]}"; do
  [ -f "$f" ] || continue
  if bash -n "$f" 2>/dev/null; then say "  ok $f"; else err "bash -n: $f"; fi
done

# 3) shellcheck — scoped to reviewed scripts (blocking); others get bash -n only above.
say "== shellcheck =="
SC_SCOPE=(claude/hooks/audit.sh claude/hooks/redact.sh claude/hooks/lifecycle-feed.sh \
          claude/hooks/lib/lifecycle-common.sh \
          claude/hooks/notify.sh claude/hooks/statusline.sh claude/headless.sh codex/headless.sh crush/headless.sh \
          scripts/ccc-service-control.sh \
          scripts/a2a-intent-dispatcher.sh scripts/skills-intake-review-handler.sh \
          scripts/install-a2a-review-handler.sh \
          scripts/ccc-service-control.test.sh \
          scripts/ccc-broker-reconcile.sh scripts/ccc-broker-reconcile.test.sh \
          claude/hooks/observability.test.sh scripts/validate-harness.sh \
          scripts/bridge-watchdog.sh scripts/bridge-watchdog.test.sh \
          scripts/resource-pressure-guard.sh scripts/resource-pressure-guard.test.sh \
          scripts/git-hooks/managed-checkout-guard scripts/managed-checkout-guard.test.sh \
          skills/nclex-a2a-content-pipeline/watch-task.sh)
if command -v shellcheck >/dev/null 2>&1; then
  SC_PRESENT=()
  for f in "${SC_SCOPE[@]}"; do
    [ -f "$f" ] && SC_PRESENT+=("$f")
  done
  # Batch the whole scope into one shellcheck spawn first; only when that
  # fails re-run per file so the failing script is named next to its findings.
  # Both paths emit the same per-file ok/FAIL lines and tally into $fail.
  if [ "${#SC_PRESENT[@]}" -gt 0 ] \
     && shellcheck --severity=warning -e SC2155,SC1090,SC1091 "${SC_PRESENT[@]}" >/dev/null 2>&1; then
    for f in "${SC_PRESENT[@]}"; do say "  ok $f"; done
  else
    for f in "${SC_PRESENT[@]}"; do
      if shellcheck --severity=warning -e SC2155,SC1090,SC1091 "$f"; then say "  ok $f"; else err "shellcheck: $f"; fi
    done
  fi
  # 3a) Repo-wide error-severity sweep — every tracked script gets at least
  # error-level lint, so a new script cannot escape shellcheck entirely
  # (previously anything outside SC_SCOPE only got bash -n). SC_SCOPE keeps
  # the stricter warning-level bar for reviewed scripts; this pass is
  # currently clean repo-wide, so it only ever catches new real bugs.
  if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    mapfile -t ALL_SH < <(git ls-files '*.sh')
    if [ "${#ALL_SH[@]}" -gt 0 ]; then
      if shellcheck --severity=error "${ALL_SH[@]}"; then
        say "  ok repo-wide shellcheck (severity=error, ${#ALL_SH[@]} scripts)"
      else
        err "repo-wide shellcheck (severity=error)"
      fi
    fi
  else
    say "  (git unavailable — repo-wide shellcheck sweep skipped)"
  fi
else
  say "  (shellcheck absent — skipped)"
fi

# 3b) python hook helpers — a syntax error would break the statusline helper, so
# compile the shipped python here.
say "== python hook helpers =="
PY_COMPILE_FILES=(claude/hooks/statusline-usage.py \
                  claude/hooks/lib/memory_render.py \
                  claude/hooks/distill/pending_journal.py \
                  claude/hooks/skill-review/ownership.py \
                  claude/hooks/skill-review/curator.py \
                  scripts/ccc_codex_github_policy.py \
                  scripts/ccc-skill-registry.py \
                  scripts/ccc-skill-promotion.py \
                  scripts/ccc-fleet-skills-sync.py \
                  scripts/ccc_memory_probe.py \
                  scripts/cost-ledger-weekly.py \
                  scripts/ccc_memory_timeparse.py \
                  scripts/ccc_memory_timeparse_test.py \
                  bridge/runtime_config_check.py \
                  scripts/ccc_script_interpreter_check.py \
                  scripts/ccc_script_interpreter_check_test.py \
                  scripts/ccc_architecture_contract.py \
                  scripts/ccc_architecture_contract_test.py \
                  scripts/ccc_side_effect_contract.py \
                  scripts/ccc_side_effect_contract_test.py)
if command -v python3 >/dev/null 2>&1; then
  # One interpreter compiles the whole list (was one python3 spawn per file);
  # only when the batch fails re-run per file so the broken/missing file is
  # attributed. The per-file "ok ... compiles" lines are printed either way.
  if python3 -m py_compile "${PY_COMPILE_FILES[@]}" 2>/dev/null; then
    for f in "${PY_COMPILE_FILES[@]}"; do say "  ok $f compiles"; done
  else
    for f in "${PY_COMPILE_FILES[@]}"; do
      if python3 -m py_compile "$f" 2>/dev/null; then say "  ok $f compiles"; else err "py_compile: $f"; fi
    done
  fi
else
  say "  (python3 absent — skipped)"
fi

# 3c) interpreter-named invocation of repo scripts (#1160). Exec'ing a repo
# .sh through its shebang silently dies on Termux (no /bin/bash, no /usr) and
# five defects of that shape shipped silently (#472/#663/#1151/#1157/#1159),
# each fixed individually. This static check fails CI on any NEW call site
# that execs a repo script without naming an interpreter; override seams
# (CCC_SCAN_INJECTION_BIN, CCC_BRIDGE_RESTART_SPAWN) and deliberate exceptions
# (`# ccc:interpreter-ok: <reason>`) are honored. The repo baseline is clean.
say "== interpreter-named script invocation =="
if command -v python3 >/dev/null 2>&1; then
  if python3 scripts/ccc_script_interpreter_check.py --repo-root . >"$TMP/interp-check.out" 2>&1; then
    say "  ok repo script call sites name an interpreter or a declared seam"
  else
    err "interpreter-less repo script invocation(s) detected"
    tail -10 "$TMP/interp-check.out"
  fi
else
  say "  (python3 absent — skipped)"
fi

# 4) hook tests
say "== hook tests =="
if python3 scripts/ccc_codex_skills.py validate --repo-root . >"$TMP/codex-skills-validate.out" 2>&1 \
   && python3 scripts/ccc_codex_skills_test.py >"$TMP/codex-skills-test.out" 2>&1; then
  say "  ok Codex compatibility catalog + managed-skill transaction tests"
else
  err "Codex managed-skill validation/tests failed"
  tail -10 "$TMP/codex-skills-validate.out" "$TMP/codex-skills-test.out" 2>/dev/null
fi
if python3 scripts/ccc-skill-registry.py validate --repo-root . >"$TMP/skill-registry.out" 2>&1; then
  say "  ok Skill registry freshness (#1338)"
else
  err "skill registry validation failed"
  tail -10 "$TMP/skill-registry.out" 2>/dev/null
fi
if python3 scripts/ccc_memory_timeparse_test.py >"$TMP/timeparse-test.out" 2>&1; then
  say "  ok NL as_of time-reference estimation tests (#871)"
else
  err "NL as_of timeparse tests failed"
  tail -10 "$TMP/timeparse-test.out" 2>/dev/null
fi
if python3 scripts/ccc_architecture_contract.py --repo-root . >"$TMP/architecture-contract.out" 2>&1 \
   && python3 scripts/ccc_architecture_contract_test.py >"$TMP/architecture-contract-test.out" 2>&1; then
  say "  ok executable architecture import contract (#872)"
else
  err "architecture contract validation/tests failed"
  tail -10 "$TMP/architecture-contract.out" "$TMP/architecture-contract-test.out" 2>/dev/null
fi
if python3 scripts/ccc_side_effect_contract.py --repo-root . >"$TMP/side-effect-contract.out" 2>&1 \
   && python3 scripts/ccc_side_effect_contract_test.py >"$TMP/side-effect-contract-test.out" 2>&1; then
  say "  ok typed side-effect inventory and recovery drills (#872)"
else
  err "side-effect contract validation/tests failed"
  tail -10 "$TMP/side-effect-contract.out" "$TMP/side-effect-contract-test.out" 2>/dev/null
fi
if python3 scripts/ccc_doctor_bootpath_test.py >"$TMP/doctor-bootpath-test.out" 2>&1; then
  say "  ok doctor bridge boot-path guard tests"
else
  err "doctor bridge boot-path guard tests failed"
  tail -10 "$TMP/doctor-bootpath-test.out" 2>/dev/null
fi
if python3 scripts/ccc_doctor_bridge_status_test.py >"$TMP/doctor-bridge-status-test.out" 2>&1; then
  say "  ok doctor bridge status verdict tests"
else
  err "doctor bridge status verdict tests failed"
  tail -10 "$TMP/doctor-bridge-status-test.out" 2>/dev/null
fi
if python3 scripts/ccc_doctor_hookfiles_test.py >"$TMP/doctor-hookfiles-test.out" 2>&1; then
  say "  ok doctor hook-tree walk tests"
else
  err "doctor hook-tree walk tests failed"
  tail -10 "$TMP/doctor-hookfiles-test.out" 2>/dev/null
fi
if python3 scripts/ccc_doctor_selfupdate_test.py >"$TMP/doctor-selfupdate-test.out" 2>&1; then
  say "  ok doctor self-update stall verdict tests (#1328)"
else
  err "doctor self-update stall verdict tests failed"
  tail -10 "$TMP/doctor-selfupdate-test.out" 2>/dev/null
fi
if python3 scripts/ccc_doctor_marker_registry_test.py >"$TMP/doctor-marker-registry-test.out" 2>&1; then
  say "  ok doctor cron marker registry covers every install-*-cron.sh"
else
  err "doctor cron marker registry guard failed (add the installer to CRON_MARKER_INSTALLERS / CRON_AUX_MARKERS)"
  tail -10 "$TMP/doctor-marker-registry-test.out" 2>/dev/null
fi
if python3 scripts/a2a_piri_memory_snapshot_test.py >"$TMP/a2a-piri-memory-test.out" 2>&1; then
  say "  ok A2A Piri shared memory snapshot producer tests"
else
  err "A2A Piri shared memory snapshot producer tests failed"
  tail -10 "$TMP/a2a-piri-memory-test.out" 2>/dev/null
fi
# A suite must not inherit the harness environment of the node it runs on
# (#1064). The per-suite guard `ccc_test_reset_hook_env` (#1023) only reaches
# suites that source test-stub.sh, so Python-driven suites fell outside it and
# three of them failed on EVERY live node while CI — which has no CCC_*/NUNCHI_*
# set — stayed green. That is the wrong way round for a gate whose whole job is
# to be trustworthy on a node. Scrub at the runner instead of relying on each
# suite to remember: it covers the suites that exist and the ones added later.
# Only CCC_*/NUNCHI_* are removed, so TMPDIR (deliberately exported above) and
# the rest of the environment still reach the child.
run_suite() { # <suite-path>
  local v
  local -a scrub=()
  while IFS= read -r v; do
    [ -n "$v" ] && scrub+=(-u "$v")
  done < <(env | sed -n 's/^\(CCC_[A-Za-z0-9_]*\|NUNCHI_[A-Za-z0-9_]*\)=.*/\1/p' | sort -u)
  env ${scrub[@]+"${scrub[@]}"} bash "$1"
}

HARNESS_SUITES=(claude/hooks/observability.test.sh claude/hooks/security-scan.test.sh \
         claude/hooks/skill-usage-log.test.sh \
         scripts/validate-harness.test.sh \
         claude/hooks/redact.test.sh claude/hooks/scan-injection.test.sh \
         claude/hooks/checkpoint.test.sh claude/hooks/distill-scope.test.sh claude/hooks/skill-review.test.sh \
         claude/hooks/skill-review/ownership.test.sh \
         claude/hooks/skill-review/ownership-incremental.test.sh \
         claude/hooks/skill-review/ownership-nolink-fallback.test.sh \
         claude/hooks/skill-review/curator.test.sh \
         claude/hooks/skill-review/autoinstall.test.sh \
         claude/hooks/skill-review/autoinstall-incremental.test.sh \
         claude/hooks/skill-review/codex-autoinstall.test.sh \
         claude/hooks/lib/mtime-prune.test.sh \
         claude/hooks/lib/memory_render.test.sh \
         claude/hooks/lib/pending_promises.test.sh \
         claude/hooks/lib/detached_jobs.test.sh \
         claude/hooks/lib/test-stub.test.sh \
         claude/hooks/lib/hook-common.test.sh \
         claude/hooks/statusline.test.sh \
         claude/hooks/distill/extract.test.sh claude/hooks/distill/pending-drain.test.sh claude/hooks/distill/wiki-queue.test.sh \
         claude/hooks/distill/local-facts.test.sh claude/hooks/memory-hooks.test.sh \
         claude/hooks/refresh-memory-freshness.test.sh \
         claude/hooks/nunchi/nunchi.test.sh claude/hooks/nunchi/bench.test.sh \
         claude/hooks/nunchi/sessionstart.test.sh \
         claude/hooks/nunchi/judge-batch.test.sh \
         claude/hooks/nunchi/bridge-journal.test.sh claude/hooks/nunchi/codex-feed.test.sh \
         scripts/ccc-doctor.test.sh scripts/ccc-memory.test.sh scripts/ccc-codex-memory.test.sh scripts/ccc-codex.test.sh scripts/ccc-piri.test.sh piri/skills/web/web_tools.test.sh scripts/ccc-codex-github-policy.test.sh scripts/ccc-distill-check.test.sh scripts/ccc-distill-fleet-matrix.test.sh scripts/ccc-security-audit.test.sh \
         scripts/ccc-script-interpreter-check.test.sh \
         scripts/managed-checkout-guard.test.sh \
         scripts/ccc-fleet-matrix.test.sh scripts/ccc-wiki-triage.test.sh scripts/setup.test.sh \
         scripts/harness-paths.test.sh scripts/canonical-paths.test.sh \
         scripts/installer-gen-stamp.test.sh \
         scripts/agent-cron.test.sh scripts/agent-cron-lib.test.sh scripts/a2a-termux-native-worker.test.sh \
         scripts/a2a-termux-native-worker-health.test.sh \
         scripts/resource-pressure-guard.test.sh \
         scripts/install-memory-refresh-cron.test.sh scripts/install-nunchi.test.sh scripts/auto-distill.test.sh scripts/install-termux-mempalace.test.sh scripts/ccc-skill-autosave.test.sh \
         scripts/codex-rollout-normalize.test.sh \
         scripts/cost-ledger.test.sh \
         scripts/cost-ledger-weekly.test.sh \
         scripts/ccc-skill-promotion.test.sh \
         scripts/rescreen-rotation.test.sh \
         scripts/a2a-review-handler.test.sh \
         scripts/a2a-rescreen-rotation.test.sh \
         scripts/nclex-a2a-watch-task.test.sh \
         scripts/ccc-skill-registry.test.sh \
         scripts/ccc-fleet-skills-sync.test.sh \
         scripts/gh-pr-flow-seoseo-merge.test.sh \
         scripts/ccc-self-update.test.sh scripts/self-update-check.test.sh scripts/ccc-provenance.test.sh \
         scripts/bridge-watchdog.test.sh \
         scripts/ccc-bridge-locate.test.sh \
         bridge/service-install.test.sh \
         bridge/restart.test.sh \
         bridge/start-lib.test.sh \
         scripts/install-agent-cron-systemd.test.sh \
         codex/headless.test.sh crush/headless.test.sh \
         scripts/install-skill-autosave-cron.test.sh \
         scripts/gh-pr-flow-jinon86.test.sh \
         scripts/gh-pr-flow-seoseo-ai.test.sh \
         scripts/ccc-service-control.test.sh \
         scripts/ccc-broker-reconcile.test.sh \
         claude/hooks/lib/autonomy-guard.test.sh \
         claude/hooks/skill-review/gate-sim.test.sh \
         claude/mcp-setup.test.sh \
         scripts/ccc-live-backups-rotate.test.sh \
         scripts/ccc-pr-status-poll.test.sh \
         scripts/fleet-bridge-watch.test.sh \
         scripts/install-pr-status-poll-cron.test.sh \
         scripts/install-fleet-skills-sync-cron.test.sh \
         scripts/tunnel-audit.test.sh \
         scripts/tunnel-audit-fleet.test.sh \
         scripts/install-tunnel-audit-cron.test.sh \
         scripts/lib/installer-cron-common.test.sh)

# Registration guard — a suite that exists but is not listed above never runs,
# which is worse than having no suite at all: the tree looks covered and CI is
# green while the assertions are dead. Seven suites (139 assertions) had drifted
# into exactly that state before this guard existed. Mirrors the repo-wide
# lint sweep above (the one that stops a new script escaping shellcheck): the
# explicit list keeps its ordering and grouping, and this pass only ever
# catches a NEW suite that forgot to join it.
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  unregistered=0
  while IFS= read -r suite; do
    [ -n "$suite" ] || continue
    case " ${HARNESS_SUITES[*]} " in
      *" $suite "*) ;;
      *) err "unregistered test suite (never runs): $suite"; unregistered=$((unregistered+1)) ;;
    esac
  done < <(git ls-files '*.test.sh')
  [ "$unregistered" -eq 0 ] && say "  ok every tracked *.test.sh is registered (${#HARNESS_SUITES[@]} suites)"
else
  say "  (git unavailable — test-suite registration guard skipped)"
fi

# A suite reports its own tally on a final `PASS=<n> FAIL=<n>` line, which is
# what gets echoed next to its name below. A suite that omits it still shows
# "ok", just with a blank count — so a suite that silently asserted nothing
# would be indistinguishable from one that asserted a hundred things. Six
# suites printed a lowercase `pass=`/`fail=` variant and read as blank here.
# Require the line, so the tally can be trusted as evidence.
suite_summary() { # <output-file> <suite> [label]
  local s; s="$(grep -E '^PASS=[0-9]+ FAIL=[0-9]+$' "$1" | tail -1)"
  if [ -n "$s" ]; then say "  ok $s $2${3:+ $3}"
  else err "no 'PASS=<n> FAIL=<n>' summary line: $2${3:+ $3}"; fi
}

for t in "${HARNESS_SUITES[@]}"; do
  [ -f "$t" ] || { err "missing test: $t"; continue; }
  if run_suite "$t" >"$TMP/htest.out" 2>&1; then suite_summary "$TMP/htest.out" "$t";
  else err "test failed: $t"; tail -5 "$TMP/htest.out"; fi
done

# Umask-0002 variant (#770): the ownership contract fail-closes on
# group/other-writable skill dirs, so the umask-sensitive suites must also
# pass on nodes whose default umask is 0002. CI runs 0022 — run these twice.
for t in claude/hooks/skill-review.test.sh \
         claude/hooks/distill-scope.test.sh \
         claude/hooks/distill/pending-drain.test.sh \
         claude/hooks/skill-review/autoinstall.test.sh \
         claude/hooks/skill-review/autoinstall-incremental.test.sh \
         claude/hooks/skill-review/codex-autoinstall.test.sh \
         scripts/ccc-codex-github-policy.test.sh \
         scripts/ccc-codex-memory.test.sh \
         scripts/install-nunchi.test.sh \
         scripts/setup.test.sh; do
  [ -f "$t" ] || { err "missing test: $t"; continue; }
  if ( umask 0002; run_suite "$t" ) >"$TMP/htest.out" 2>&1; then suite_summary "$TMP/htest.out" "$t" "(umask 0002)";
  else err "test failed (umask 0002): $t"; tail -5 "$TMP/htest.out"; fi
done

# 5) skill + agent frontmatter (must start with --- and carry name: + description:)
say "== frontmatter =="
fm_check() { # <file> — nonzero on any finding so the caller's `&& say ok`
  # line stays truthful (err only sets fail=1 and returns 0, so a bare
  # `return` after it used to print a contradictory "ok" after the FAIL).
  local f="$1" bad=0
  head -1 "$f" | grep -q '^---' || { err "no frontmatter: $f"; return 1; }
  awk 'NR>1 && /^---/{exit} {print}' "$f" | grep -q '^name:'        || { err "no name: in $f"; bad=1; }
  awk 'NR>1 && /^---/{exit} {print}' "$f" | grep -q '^description:' || { err "no description: in $f"; bad=1; }
  return "$bad"
}
for f in skills/*/SKILL.md; do [ -f "$f" ] && fm_check "$f" && say "  ok $f"; done
for f in codex/skills/*/SKILL.md; do [ -f "$f" ] && fm_check "$f" && say "  ok $f"; done
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
# setup.sh no longer hand-lists hook cps: it deploys the shared hook-tree walk
# (ccc_hook_tree_files, scripts/lib/harness-paths.sh). Derive the expected set from
# the SAME walk so installer and validator share one convention (#569; the 3-way
# hand-list drift behind the #564 mtime-prune miss).
say "== referenced hooks installed by setup.sh (shared hook-tree walk) =="
# shellcheck source=lib/harness-paths.sh
. scripts/lib/harness-paths.sh
if grep -q 'ccc_hook_tree_files "\$SRC"' setup.sh 2>/dev/null; then
  say "  ok setup.sh deploys hooks via the shared ccc_hook_tree_files walk"
else
  err "setup.sh does not deploy hooks via the shared ccc_hook_tree_files walk"
fi
mapfile -t DEPLOYED < <(ccc_hook_tree_files "$ROOT")
[ "${#DEPLOYED[@]}" -gt 0 ] || err "hook-tree walk found no deployable hooks under claude/hooks"
for r in "${REFS[@]}"; do
  hook="$(basename "$r")"
  if printf '%s\n' "${DEPLOYED[@]}" | grep -Fxq -- "$hook"; then
    say "  ok setup.sh installs $hook"
  else
    err "setup.sh does not install referenced hook: $hook (excluded from the hook-tree walk)"
  fi
done
# Full-tree exclusion parity: every file the walk SKIPS must match a documented
# non-deployable pattern (tests/fixtures, bytecode, docs, settings-compose wiring).
# An accidental new exclusion in ccc_hook_tree_files — or a deployable file that
# a future pattern mistakenly swallows — fails here instead of silently shipping
# an install that is missing the file.
tree_parity_ok=1
while IFS= read -r f; do
  printf '%s\n' "${DEPLOYED[@]}" | grep -Fxq -- "$f" && continue
  case "$f" in
    *.test.sh|lib/test-stub.sh|__pycache__/*|*/__pycache__/*|*.pyc|*.md|hooks.json|enforcement-overlay.json)
      : ;;  # documented exclusion
    *)
      tree_parity_ok=0
      err "claude/hooks/$f is not deployed by the hook-tree walk and matches no documented exclusion" ;;
  esac
done < <(cd claude/hooks && find . -type f | sed 's|^\./||' | LC_ALL=C sort)
[ "$tree_parity_ok" = 1 ] && say "  ok hook-tree exclusions all match documented non-deployable patterns"

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
   && jq -e '.hooks.SessionStart and .statusLine and .outputStyle' "$TMP/rendered.json" >/dev/null 2>&1; then
  say "  ok rendered standalone settings valid (node-local + portable + statusLine + outputStyle)"
else
  err "rendered standalone settings (base+overlay) invalid or missing expected keys"
fi

# 7) Tier 3: statusline smoke + settings wiring
say "== statusline + settings wiring =="
if [ -f claude/hooks/statusline.sh ]; then
  SAMPLE='{"session_id":"validate-session","model":{"display_name":"T"},"context_window":{"used_percentage":42.5,"total_input_tokens":850,"context_window_size":200000},"cost":{"total_cost_usd":1.2},"rate_limits":{"five_hour":{"used_percentage":12,"resets_at":2000000000}},"exceeds_200k_tokens":true,"output_style":{"name":"ccc-report"},"workspace":{"current_dir":"'"$ROOT"'"}}'
  if out="$(printf '%s' "$SAMPLE" | CCC_NODE=ci CCC_STATE_DIR="$TMP/status-state" CCC_STATUSLINE_USAGE_COLLECTOR="$ROOT/claude/hooks/statusline-usage.py" bash claude/hooks/statusline.sh 2>/dev/null)" && [ -n "$out" ]; then
    say "  ok statusline.sh emits output"
  else err "statusline.sh produced no output / non-zero"; fi
  # The collector runs detached from the render path (its interpreter start
  # must not tax every status-line render), so give its snapshot a bounded
  # moment to land before asserting on it.
  collector_ok=0
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if find "$TMP/status-state/usage" -type f -name '*.json' -perm 0600 2>/dev/null | grep -q .; then
      collector_ok=1
      break
    fi
    sleep 0.25
  done
  if [ "$collector_ok" = 1 ]; then
    say "  ok statusline usage collector writes owner-only snapshot"
  else err "statusline usage collector did not write owner-only snapshot"; fi
  # empty input must not crash (fail-open to a usable bar)
  printf '%s' '' | CCC_NODE=ci bash claude/hooks/statusline.sh >/dev/null 2>&1 \
    && say "  ok statusline.sh survives empty input" || err "statusline.sh crashed on empty input"
  # git TTL cache must be a valid, non-empty record for a CLEAN repo. The
  # historical regression class: a malformed write truncated the cache so
  # clean repos re-ran git status on every render. The cache is a one-line
  # TSV (ts<TAB>branch<TAB>dirty) read by the bash builtin — statusline.test.sh
  # covers hit/miss behavior; this smoke check pins the persisted shape.
  clean_repo="$TMP/status-clean-repo"
  if git init -q "$clean_repo" 2>/dev/null \
     && git -C "$clean_repo" -c user.email=ci@local -c user.name=ci commit -q --allow-empty -m init 2>/dev/null; then
    printf '{"workspace":{"current_dir":"%s"}}' "$clean_repo" \
      | CCC_NODE=ci HOME="$TMP/status-home" bash claude/hooks/statusline.sh >/dev/null 2>&1 || true
    cache_tsv="$(find "$TMP/status-home/.claude/cache/git-status" -name '*.tsv' 2>/dev/null | head -1)"
    cache_line=""
    [ -n "$cache_tsv" ] && IFS= read -r cache_line < "$cache_tsv" 2>/dev/null
    if [ -n "$cache_line" ] \
       && printf '%s' "$cache_line" | grep -Eq $'^[0-9]+\t[^\t]+\t0$'; then
      say "  ok statusline git cache records a clean repo"
    else err "statusline git cache invalid/empty for a clean repo"; fi
  fi
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
