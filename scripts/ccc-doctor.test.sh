#!/usr/bin/env bash
# Tests for ccc doctor — diagnostic-only harness drift classification.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCTOR="$ROOT/scripts/ccc-doctor.sh"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

make_fixture() { # <name> <mode:standalone|plugin>
  local name="$1" mode="$2" dir
  dir="$TMP/$name"
  mkdir -p "$dir/repo/claude/hooks" "$dir/repo/claude/output-styles" "$dir/repo/bridge" \
           "$dir/home/.claude/hooks" "$dir/home/.claude/output-styles"
  cp "$ROOT/claude/settings.base.json" "$dir/repo/claude/settings.base.json"
  cp "$ROOT/claude/settings.local.json" "$dir/repo/claude/settings.local.json"
  cp "$ROOT/claude/hooks/enforcement-overlay.json" "$dir/repo/claude/hooks/enforcement-overlay.json"
  cp "$ROOT/claude/hooks/hooks.json" "$dir/repo/claude/hooks/hooks.json"
  cp "$ROOT/claude/hooks/load-memory.sh" "$dir/home/.claude/hooks/load-memory.sh"
  cp "$ROOT/claude/hooks/load-tools.sh" "$dir/home/.claude/hooks/load-tools.sh"
  cp "$ROOT/claude/hooks/checkpoint.sh" "$dir/home/.claude/hooks/checkpoint.sh"
  cp "$ROOT/claude/hooks/statusline.sh" "$dir/home/.claude/hooks/statusline.sh"
  cp "$ROOT/claude/hooks/guard.sh" "$dir/home/.claude/hooks/guard.sh"
  cp "$ROOT/claude/hooks/audit.sh" "$dir/home/.claude/hooks/audit.sh"
  cp "$ROOT/claude/hooks/redact.sh" "$dir/home/.claude/hooks/redact.sh"
  cp "$ROOT/claude/hooks/notify.sh" "$dir/home/.claude/hooks/notify.sh"
  cp "$ROOT/claude/hooks/evidence-gate.sh" "$dir/home/.claude/hooks/evidence-gate.sh"
  cp "$ROOT/claude/output-styles/ccc-report.md" "$dir/home/.claude/output-styles/ccc-report.md"
  printf '#!/usr/bin/env bash\n[ "$1" = "--status" ] || [ "$3" = "--status" ] || true\necho bridge status ok\n' > "$dir/repo/bridge/start.sh"
  chmod +x "$dir/repo/bridge/start.sh"
  if [ "$mode" = standalone ]; then
    jq -s '.[0] as $b | .[1] as $o | $b | .hooks = ($b.hooks + $o.hooks)' \
      "$ROOT/claude/settings.base.json" "$ROOT/claude/hooks/enforcement-overlay.json" > "$dir/home/.claude/settings.json"
  else
    cp "$ROOT/claude/settings.base.json" "$dir/home/.claude/settings.json"
  fi
  cp "$ROOT/claude/settings.local.json" "$dir/home/.claude/settings.local.json"
  printf '%s\n' "$dir"
}

run_doctor() { # <fixture-dir> [args...]
  local dir="$1"; shift
  CCC_DOCTOR_REPO_DIR="$dir/repo" CCC_DOCTOR_CLAUDE_DIR="$dir/home/.claude" \
    bash "$DOCTOR" "$@"
}

clean="$(make_fixture clean standalone)"
out="$(run_doctor "$clean")"; rc=$?
ok "clean standalone exits 0" '[ "$rc" = 0 ]'
ok "clean output reports 정상" 'grep -q "정상" <<<"$out"'
ok "clean output reports standalone mode" 'grep -q "mode.*standalone" <<<"$out"'

plugin="$(make_fixture plugin plugin)"
out="$(run_doctor "$plugin")"; rc=$?
ok "plugin base-only settings exits 0" '[ "$rc" = 0 ]'
ok "plugin output reports plugin mode" 'grep -q "mode.*plugin" <<<"$out"'
ok "plugin mode does not require portable hooks in settings.json" '! grep -q "PreToolUse.*교정가능" <<<"$out"'

drift="$(make_fixture drift standalone)"
rm -f "$drift/home/.claude/hooks/statusline.sh"
out="$(run_doctor "$drift")"; rc=$?
ok "missing installed hook exits 1" '[ "$rc" = 1 ]'
ok "missing installed hook classified fixable" 'grep -q "교정가능.*statusline.sh" <<<"$out"'

before="$(find "$drift" -type f -printf '%P %s %T@\n' | sort)"
out="$(run_doctor "$drift" --fix 2>&1)"; rc=$?
after="$(find "$drift" -type f -printf '%P %s %T@\n' | sort)"
ok "--fix is explicitly not implemented in diagnostic slice" '[ "$rc" = 2 ] && grep -q "not implemented" <<<"$out"'
ok "--fix made no filesystem changes" '[ "$before" = "$after" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
