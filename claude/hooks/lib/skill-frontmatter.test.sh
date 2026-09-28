#!/usr/bin/env bash
# Tests for lib/skill-frontmatter.sh (#2032): the awk decoder shell readers use
# for YAML-quoted SKILL.md description lines must agree with the canonical
# Python helper (bridge/utils/skill_frontmatter.py) on everything the renderer
# emits for printable text. Also runs the script-reader agreement suite
# (scripts/ccc_skill_frontmatter_readers_test.py).
# shellcheck disable=SC2034  # got/want/expected/listing are read via eval inside ok()
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
FM_PY="$ROOT/bridge/utils/skill_frontmatter.py"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# shellcheck source=claude/hooks/lib/skill-frontmatter.sh
. "$HERE/skill-frontmatter.sh"

ok "sourcing defines the awk decoder and ccc_fm_field" \
  '[ -n "${CCC_YAML_UNQUOTE_AWK:-}" ] && declare -f ccc_fm_field >/dev/null'

# Parity: render each value with the Python helper, write a SKILL.md, and
# compare ccc_fm_field (awk) with the Python unquote of the same raw line.
values=(
  'Use when X: do Y'
  'Use when a PR fixes issue #42 and the changelog must follow'
  'Use when quoting "inner" text and a \ backslash'
  '- starts with a sequence indicator'
  "'single quote start and it's inner"
  '"double quote start'
  'ends with a colon:'
  '  padded value  '
  'yes'
  '3 steps to recover'
  "tab	inside"
  'emoji 😀 and 한국어: 설명'
  'Use when the plain description has no YAML hazards at all'
)
i=0
for value in "${values[@]}"; do
  i=$((i + 1))
  raw="$(printf '%s' "$value" | python3 "$FM_PY" render)"
  printf -- '---\nname: demo\ndescription: %s\n---\nbody\n' "$raw" > "$TMP/$i.md"
  want="$(printf '%s' "$raw" | python3 "$FM_PY" unquote)"
  got="$(ccc_fm_field "$TMP/$i.md" description)"
  ok "awk decode matches python for case $i ($raw)" '[ "$got" = "$want" ] && [ "$got" = "$value" ]'
  # LC_ALL=C (autoinstall.sh) byte-mode awk decodes identically.
  got_c="$(LC_ALL=C ccc_fm_field "$TMP/$i.md" description)"
  ok "awk decode is locale-independent for case $i" '[ "$got_c" = "$want" ]'
done

check_raw() { # <label> <raw line value> <expected decoded>
  printf -- '---\nname: demo\ndescription: %s\n---\nbody\n' "$2" > "$TMP/raw.md"
  got="$(ccc_fm_field "$TMP/raw.md" description)"
  expected="$3"
  ok "$1" '[ "$got" = "$expected" ]'
}
check_raw "single-quoted value decodes '' escapes" "'it''s here'" "it's here"
check_raw "trailing comment after a quoted value is dropped" '"quoted" # note' "quoted"
check_raw "plain value keeps a # tail verbatim" "plain value # kept" "plain value # kept"
check_raw "unterminated quote is returned as-is" '"unterminated' '"unterminated'
check_raw "junk after the closing quote is returned as-is" '"closed" junk' '"closed" junk'
check_raw "slash and space escapes decode" '"a\/b\ c"' "a/b c"
check_raw "control escapes stay literal (display-safe)" '"bell\x07 esc\e"' 'bell\x07 esc\e'

printf -- '---\nname: demo\n---\ndescription: "not frontmatter"\n' > "$TMP/outside.md"
ok "keys after the closing fence are ignored" '[ -z "$(ccc_fm_field "$TMP/outside.md" description)" ]'
printf -- '---\nname: "demo-quoted"\ndescription: x\n---\n' > "$TMP/name.md"
ok "any key decodes, not only description" '[ "$(ccc_fm_field "$TMP/name.md" name)" = "demo-quoted" ]'

# extract.sh lists existing skills for the drafting model; it must show the
# decoded description.
mkdir -p "$TMP/skills/quoted"
printf -- '---\nname: quoted\ndescription: "Use when X: do Y"\n---\nbody\n' > "$TMP/skills/quoted/SKILL.md"
listing="$(SKILLS_DIR="$TMP/skills" bash -c '
  eval "$(sed -n "/^existing_skills() {/,/^}/p" "$1")"
  . "$2"
  existing_skills' _ "$HERE/../skill-review/extract.sh" "$HERE/skill-frontmatter.sh")"
ok "extract.sh existing-skill listing shows the decoded description" \
  '[ "$listing" = "- quoted — Use when X: do Y" ]'

# Script readers (fleet-skills sync, listing policy, registry, codex skills,
# promotion) agree with the renderer.
if python3 "$ROOT/scripts/ccc_skill_frontmatter_readers_test.py" >"$TMP/readers.log" 2>&1; then
  pass=$((pass+1))
else
  fail=$((fail+1)); echo "FAIL: scripts/ccc_skill_frontmatter_readers_test.py"; cat "$TMP/readers.log"
fi

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
