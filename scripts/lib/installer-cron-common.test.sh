#!/usr/bin/env bash
# Hermetic tests for scripts/lib/installer-cron-common.sh (#1077).
# The lib owns the unified BEGIN/END block strategy and the whole
# install/remove flow for the three crontab installers, so its parser and
# driver get direct unit coverage here — the installer test pairs then only
# pin lane-specific rendering.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LIB="$ROOT/scripts/lib/installer-cron-common.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/ccc-cron-lib-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

# shellcheck source=/dev/null
. "$LIB"

pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

B="# ccc-node:demo:begin"; E="# ccc-node:demo:end"; M="# ccc-node:demo"

# ---- ccc_cron_need_val ------------------------------------------------------
(ccc_cron_need_val --schedule "" ) 2>/dev/null; rc=$?
ok "need_val rejects an empty value with exit 2" '[ "$rc" = 2 ]'
(ccc_cron_need_val --schedule "x") 2>/dev/null; rc=$?
ok "need_val accepts a present value" '[ "$rc" = 0 ]'

# ---- ccc_cron_strip_managed -------------------------------------------------
out="$(printf '%s\n' "0 1 * * * keep" "$M gen=h_aaaaaaaaaaaa" | ccc_cron_strip_managed "$B" "$E" "$M")"
ok "strip drops a legacy bare marker line" '! grep -qF "gen=h_aaaaaaaaaaaa" <<<"$out"'
ok "strip keeps foreign lines" 'grep -qF "keep" <<<"$out"'

out="$(printf '%s\n' "0 1 * * * keep" "$B" "CRON_TZ=Etc/UTC" "1 2 * * * entry  $M gen=h_bbbbbbbbbbbb" "$E" "0 3 * * * keep2" | ccc_cron_strip_managed "$B" "$E" "$M")"
rc=$?
ok "strip removes a whole managed block" '[ "$rc" = 0 ] && ! grep -qF "entry" <<<"$out" && ! grep -qF "CRON_TZ=Etc/UTC" <<<"$out"'
ok "strip preserves lines around the block" 'grep -qF "keep" <<<"$out" && grep -qF "keep2" <<<"$out"'

printf '%s\n' "$B" "1 2 * * * dangling  $M gen=h_bbbbbbbbbbbb" | ccc_cron_strip_managed "$B" "$E" "$M" >/dev/null 2>&1; rc=$?
ok "unterminated block exits 42" '[ "$rc" = 42 ]'
printf '%s\n' "$E" | ccc_cron_strip_managed "$B" "$E" "$M" >/dev/null 2>&1; rc=$?
ok "end without begin exits 42" '[ "$rc" = 42 ]'
printf '%s\n' "$B" "$B" "$E" | ccc_cron_strip_managed "$B" "$E" "$M" >/dev/null 2>&1; rc=$?
ok "nested begin exits 42" '[ "$rc" = 42 ]'

out="$(printf '%s\n' "# ccc-node:other:begin" "9 9 * * * x  # ccc-node:other gen=h_cccccccccccc" "# ccc-node:other:end" | ccc_cron_strip_managed "$B" "$E" "$M")"
ok "another lane's block is untouched" 'grep -qF "ccc-node:other:begin" <<<"$out" && grep -qF "gen=h_cccccccccccc" <<<"$out"'

# ---- ccc_cron_installer_finish ----------------------------------------------
export FAKE_CRON="$TMP/crontab.txt"; : > "$FAKE_CRON"
STUB="$TMP/crontab-stub.sh"
BASH_BIN="$(command -v bash)"
cat > "$STUB" <<STUBEOF
#!$BASH_BIN
f="\${FAKE_CRON:?}"
case "\${1:-}" in
  -l) [ -s "\$f" ] && cat "\$f" || exit 1 ;;
  -)  cat > "\$f" ;;
  *)  exit 2 ;;
esac
STUBEOF
chmod +x "$STUB"
STATE="$TMP/state"; mkdir -p "$STATE"
FIN=(--label demo --marker "$M" --begin "$B" --end "$E"
     --crontab "$STUB" --state-dir "$STATE" --self "$TMP/install-demo.sh" --gen h_dddddddddddd)

out="$(ccc_cron_installer_finish "${FIN[@]}" --apply 0 --remove 0 --schedule-desc "*/5 * * * *" \
  --body "*/5 * * * * run  $M gen=h_dddddddddddd" -- --apply --schedule "*/5 * * * *" 2>&1)"; rc=$?
ok "driver dry-run exits 0 and announces install" '[ "$rc" = 0 ] && grep -q "would install" <<<"$out"'
ok "driver dry-run does not write" '[ ! -s "$FAKE_CRON" ]'

ccc_cron_installer_finish "${FIN[@]}" --apply 1 --remove 0 --schedule-desc "*/5 * * * *" \
  --body "*/5 * * * * run  $M gen=h_dddddddddddd" -- --apply --schedule "*/5 * * * *" >/dev/null 2>&1; rc=$?
ok "driver apply installs begin/body/end" \
  '[ "$rc" = 0 ] && grep -qF "$B" "$FAKE_CRON" && grep -qF "gen=h_dddddddddddd" "$FAKE_CRON" && grep -qF "$E" "$FAKE_CRON"'
ok "driver apply writes an install record with the replay argv" \
  'jq -e ".argv == [\"--apply\",\"--schedule\",\"*/5 * * * *\"]" "$STATE/install-demo.json" >/dev/null'

ccc_cron_installer_finish "${FIN[@]}" --apply 1 --remove 0 --schedule-desc "*/5 * * * *" \
  --body "*/6 * * * * run  $M gen=h_dddddddddddd" -- --apply --schedule "*/6 * * * *" >/dev/null 2>&1
ok "driver re-apply keeps exactly one block and one entry" \
  '[ "$(grep -cF "$B" "$FAKE_CRON")" = 1 ] && [ "$(grep -c "gen=h_" "$FAKE_CRON")" = 1 ] && grep -qF "*/6" "$FAKE_CRON"'

ccc_cron_installer_finish "${FIN[@]}" --apply 1 --remove 1 --schedule-desc "*/6 * * * *" >/dev/null 2>&1; rc=$?
ok "driver remove strips the block and the record" \
  '[ "$rc" = 0 ] && [ "$(grep -c "gen=h_" "$FAKE_CRON")" = 0 ] && ! grep -qF "$B" "$FAKE_CRON" && [ ! -f "$STATE/install-demo.json" ]'

printf '%s\n' "$B" "*/5 * * * * dangling  $M gen=h_dddddddddddd" > "$FAKE_CRON"
(ccc_cron_installer_finish "${FIN[@]}" --apply 1 --remove 0 --schedule-desc "x" --body "x  $M gen=h_dddddddddddd") >/dev/null 2>&1; rc=$?
ok "driver maps a corrupt block to exit 4" '[ "$rc" = 4 ]'

(ccc_cron_installer_finish --label demo) 2>/dev/null; rc=$?
ok "driver rejects missing required args with exit 2" '[ "$rc" = 2 ]'

NOCRON=(--label demo --marker "$M" --begin "$B" --end "$E"
        --crontab "$TMP/no-such-crontab" --state-dir "$STATE" --self "$TMP/install-demo.sh" --gen h_dddddddddddd)
(ccc_cron_installer_finish "${NOCRON[@]}" --apply 1 --remove 0 --schedule-desc x --body x) >/dev/null 2>&1; rc=$?
ok "driver exits 3 when the crontab command is absent" '[ "$rc" = 3 ]'

# ---- ccc_cron_root_scope_warning (#1079 generalization) ---------------------
# Root on a service-account node writes a second, dead install into root's
# crontab (the gongmyoung ghost class). Warning-only; euid/home args are seams.
mkdir -p "$TMP/home/gongmyoung/.claude" "$TMP/noroothome"
out="$(ccc_cron_root_scope_warning demo 0 "$TMP/noroothome" "$TMP/home" 2>&1)"
ok "root + no root harness + service-account harness warns and names the account" \
  'grep -q "WARNING (demo): running as root" <<<"$out" && grep -q "gongmyoung" <<<"$out"'

mkdir -p "$TMP/roothome/.claude"
out="$(ccc_cron_root_scope_warning demo 0 "$TMP/roothome" "$TMP/home" 2>&1)"
ok "root with a real root harness stays silent" '[ -z "$out" ]'

out="$(ccc_cron_root_scope_warning demo 1000 "$TMP/noroothome" "$TMP/home" 2>&1)"
ok "non-root never warns" '[ -z "$out" ]'

mkdir -p "$TMP/emptyhome"
out="$(ccc_cron_root_scope_warning demo 0 "$TMP/noroothome" "$TMP/emptyhome" 2>&1)"
ok "root with no service-account harness anywhere stays silent" '[ -z "$out" ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]

