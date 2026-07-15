#!/usr/bin/env bash
# Tests for lib/test-stub.sh — Termux-safe hermetic stub shebangs (#472).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$HERE/test-stub.sh"
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

write_exec_stub "$TMP/s" <<'SH'
printf 'ran:%s\n' "$1"
SH

ok "stub is executable" '[ -x "$TMP/s" ]'
# Termux/Android has no /usr/bin/env — the whole point of #472.
ok "shebang is NOT /usr/bin/env" '! head -1 "$TMP/s" | grep -q "/usr/bin/env"'
ok "shebang points at a real, executable bash interpreter" \
  '[ -x "$(head -1 "$TMP/s" | sed "s|^#!||")" ]'
ok "stub execs directly via its own shebang" '[ "$("$TMP/s" hi)" = "ran:hi" ]'

# Regression pin: the hermetic suites must not reintroduce a raw
# `#!/usr/bin/env bash` stub shebang — the only such line in each file is its
# own line-1 shebang (stubs go through write_exec_stub).
HOOKS="$(cd "$HERE/.." && pwd)"
for t in distill/extract.test.sh distill/honcho-push.test.sh \
         distill/queue-drain.test.sh skill-review.test.sh distill-scope.test.sh; do
  # shellcheck disable=SC2034  # $n is consumed via eval in ok()
  n="$(grep -c '#!/usr/bin/env bash' "$HOOKS/$t")"
  ok "$t writes no /usr/bin/env stub shebang" '[ "$n" = 1 ]'
done

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
