#!/usr/bin/env bash
# Tests for scripts/lib/canonical_paths.py — the canonical-path rewrite shared by
# setup.sh (install) and ccc_doctor.py (diagnosis). These two used to carry
# separate copies of the transform, and the copy doctor did not have produced
# fleet-wide phantom drift (2026-07-30). The contract these tests pin is the one
# both callers depend on: a single non-cascading pass, byte-identical results.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIB="$ROOT/scripts/lib/canonical_paths.py"
pass=0; fail=0
TMP_BASE="${TMPDIR:-$(dirname "$ROOT")}"; mkdir -p "$TMP_BASE"
TMP="$(mktemp -d "$TMP_BASE/ccc-canonical-paths-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

py() { PYTHONDONTWRITEBYTECODE=1 python3 -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('cp', '$LIB')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
$1
"; }

ok "module is readable" '[ -r "$LIB" ]'

# The literals both callers agree on. If these ever change, setup.sh's own
# rewrite invocation must change with them.
ok "canonical repo constant matches setup.sh" \
  'py "print(m.CANONICAL_REPO)" | grep -Fxq "/opt/ccc-node" && grep -Fq "\"/opt/ccc-node\" \"\$SRC\"" "$ROOT/setup.sh"'
ok "canonical harness-dir constant matches setup.sh" \
  'py "print(m.CANONICAL_CLAUDE_DIR)" | grep -Fxq "/root/.claude" && grep -Fq "\"/root/.claude\" \"\$CLAUDE_DIR\"" "$ROOT/setup.sh"'

# A canonical install must yield no substitutions at all, so its installed files
# stay byte-identical to the templates (doctor then compares byte-exact).
ok "canonical install pair produces no rewrite pairs" \
  'py "print(m.rewrite_pairs(\"/opt/ccc-node\", \"/root/.claude\"))" | grep -Fxq "{}"'
ok "non-canonical checkout produces only the differing pair" \
  'py "print(m.rewrite_pairs(\"/root/ccc-node\", \"/root/.claude\"))" | grep -Fxq "{'"'"'/opt/ccc-node'"'"': '"'"'/root/ccc-node'"'"'}"'

ok "empty pairs leave text untouched" \
  'py "print(m.rewrite_text(\"/opt/ccc-node/x\", {}))" | grep -Fxq "/opt/ccc-node/x"'
ok "repo path is substituted" \
  'py "print(m.rewrite_text(\"/opt/ccc-node/bridge/venv/bin/python\", {\"/opt/ccc-node\": \"/root/ccc-node\"}))" \
     | grep -Fxq "/root/ccc-node/bridge/venv/bin/python"'

# Non-cascading pass: a replacement value containing the other pair's search
# token must not be rescanned, or a checkout under /root/.claude would have its
# freshly inserted path corrupted by the harness-dir pair.
ok "replacement values are never rescanned" \
  'py "print(m.rewrite_text(\"/opt/ccc-node|/root/.claude\", {\"/opt/ccc-node\": \"/root/.claude/src\", \"/root/.claude\": \"/home/n/.claude\"}))" \
     | grep -Fxq "/root/.claude/src|/home/n/.claude"'

# The CLI shape setup.sh calls with, and its refusal to guess on odd arg counts.
printf '/opt/ccc-node/a and /root/.claude/b\n' > "$TMP/f"
python3 "$LIB" "$TMP/f" "/opt/ccc-node" "/root/ccc-node" "/root/.claude" "/root/.claude" >/dev/null 2>&1
ok "CLI rewrites only the pairs that differ" \
  'grep -Fxq "/root/ccc-node/a and /root/.claude/b" "$TMP/f"'

printf 'x\n' > "$TMP/g"
out="$(python3 "$LIB" "$TMP/g" "/opt/ccc-node" 2>&1)"; rc=$?
ok "CLI refuses an unpaired argument list" \
  '[ "$rc" = 2 ] && grep -q "usage:" <<<"$out" && grep -Fxq "x" "$TMP/g"'

# This module holds the canonical literals, so an installed copy would have its
# own constants rewritten by the transform it defines.
ok "module is not installed into the harness directory" \
  '! grep -nE "canonical_paths\.py\"? +\"?\$CLAUDE_DIR" "$ROOT/setup.sh"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
