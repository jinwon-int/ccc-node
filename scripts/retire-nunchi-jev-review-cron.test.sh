#!/usr/bin/env bash
# shellcheck disable=SC2016  # ok() assertions are eval-ed single-quoted strings
# Tests for retire-nunchi-jev-review-cron.sh (#2905) — hermetic: a stubbed
# crontab backed by a temp file (CCC_CRONTAB_CMD). Covers dry-run vs --apply,
# idempotency, untouched neighbours and already-commented lines, the private
# backup, --status, --restore round trip, an empty crontab and the missing
# crontab command. No real cron.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SC="$HERE/retire-nunchi-jev-review-cron.sh"
pass=0; fail=0
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = "$2" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $3 (rc=$1 want=$2)"; fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export CCC_STATE_DIR="$TMP/state"
export CCC_JEV_REVIEW_RETIRE_DATE=20261125
export CRON_STORE="$TMP/crontab.store"
STUB="$TMP/crontab"
# Absolute bash shebang: `/usr/bin/env` does not exist on Termux nodes.
printf '#!%s\n' "$(command -v bash)" > "$STUB"
cat >> "$STUB" <<'STUBEOF'
case "${1:-}" in
  -l) [ -f "$CRON_STORE" ] || exit 1; cat "$CRON_STORE" ;;
  -)  cat > "$CRON_STORE" ;;
  *)  exit 2 ;;
esac
STUBEOF
chmod +x "$STUB"
OUT="$TMP/out"; RC=0
run() { RC=0; CCC_CRONTAB_CMD="$STUB" bash "$SC" "$@" >"$OUT" 2>&1 || RC=$?; }

# shellcheck disable=SC2034  # read via eval in ok()
JEV='5,15,25,35,45,55 * * * * NUNCHI_JEV_REVIEW=1 python3 /home/u/.claude/hooks/nunchi/jev-review.py >> /home/u/.nunchi/jev-review.py.log 2>&1 # nunchi:repair-jev-review.py'
# shellcheck disable=SC2034
OLD='#unified-20261015#5,15,25,35,45,55 * * * * NUNCHI_JEV_REVIEW=1 python3 /home/u/.claude/hooks/nunchi/jev-review.py >> /home/u/.nunchi/jev-review.py.log 2>&1'
# shellcheck disable=SC2034
NEIGH='41 * * * * NUNCHI_JUDGE_APPLY=1 python3 /home/u/.claude/hooks/nunchi/judge-batch.py >> /home/u/.nunchi/judge.log 2>&1'
printf '%s\n' "CRON_TZ=Asia/Seoul" "$NEIGH" "$JEV" "$OLD" > "$CRON_STORE"
cp "$CRON_STORE" "$TMP/original"

# ---- status ---------------------------------------------------------------
run --status
okc "$RC" 0 "status exits 0"
ok "status counts one active, none retired" 'grep -qx "jev-review cron: active=1 retired=0" "$OUT"'

# ---- dry-run changes nothing ---------------------------------------------
run
okc "$RC" 0 "dry-run exits 0"
ok "dry-run reports one line" 'grep -q "dry-run: 1 line(s) would change (mode=retire)" "$OUT"'
ok "dry-run leaves crontab byte-identical" 'cmp -s "$CRON_STORE" "$TMP/original"'
ok "dry-run writes no backup" '[ ! -d "$CCC_STATE_DIR" ] || [ -z "$(ls -A "$CCC_STATE_DIR")" ]'

# ---- apply ---------------------------------------------------------------
run --apply
okc "$RC" 0 "apply exits 0"
ok "active line is now behind the marker" 'grep -qxF "#jev-review-retired-20261125# $JEV" "$CRON_STORE"'
ok "no active jev-review line remains" '! grep -v "^[[:space:]]*#" "$CRON_STORE" | grep -q "hooks/nunchi/jev-review.py"'
ok "neighbour judge line untouched" 'grep -qxF "$NEIGH" "$CRON_STORE"'
ok "older commented line untouched" 'grep -qxF "$OLD" "$CRON_STORE"'
ok "CRON_TZ kept" 'grep -qx "CRON_TZ=Asia/Seoul" "$CRON_STORE"'
ok "line count unchanged" '[ "$(wc -l < "$CRON_STORE")" = "$(wc -l < "$TMP/original")" ]'
BK="$(find "$CCC_STATE_DIR" -maxdepth 1 -name "crontab.bak-jev-review-retire-*" | head -1)"; export BK
ok "backup written" '[ -n "$BK" ] && [ -f "$BK" ]'
ok "backup equals the original" 'cmp -s "$BK" "$TMP/original"'
ok "backup is private" '[ "$(stat -c %a "$BK" 2>/dev/null || stat -f %Lp "$BK" 2>/dev/null)" = 600 ]'

run --status
ok "status after apply" 'grep -qx "jev-review cron: active=0 retired=1" "$OUT"'

# ---- idempotent ----------------------------------------------------------
cp "$CRON_STORE" "$TMP/after-apply"
run --apply
okc "$RC" 0 "second apply exits 0"
ok "second apply is a no-op" 'grep -q "nothing to do" "$OUT" && cmp -s "$CRON_STORE" "$TMP/after-apply"'

# ---- restore round trip --------------------------------------------------
run --restore
okc "$RC" 0 "restore dry-run exits 0"
ok "restore dry-run leaves crontab alone" 'cmp -s "$CRON_STORE" "$TMP/after-apply"'
run --restore --apply
okc "$RC" 0 "restore apply exits 0"
ok "restore gives back the original crontab" 'cmp -s "$CRON_STORE" "$TMP/original"'

# ---- empty / missing crontab ---------------------------------------------
rm -f "$CRON_STORE"
run --apply
okc "$RC" 0 "no crontab is nothing to do"
ok "no crontab reports nothing to do" 'grep -q "nothing to do" "$OUT"'
ok "no crontab is not created" '[ ! -f "$CRON_STORE" ]'

RC=0; CCC_CRONTAB_CMD="$TMP/no-such-crontab" bash "$SC" --dry-run >"$OUT" 2>&1 || RC=$?
okc "$RC" 3 "missing crontab command exits 3"

# ---- usage ---------------------------------------------------------------
run --bogus
okc "$RC" 2 "unknown option exits 2"
RC=0; CCC_JEV_REVIEW_RETIRE_DATE=2026-11-25 CCC_CRONTAB_CMD="$STUB" bash "$SC" >"$OUT" 2>&1 || RC=$?
okc "$RC" 2 "malformed date stamp exits 2"

echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
