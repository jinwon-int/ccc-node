#!/usr/bin/env bash
# lib/skill-frontmatter.sh — shell side of the YAML-safe SKILL.md frontmatter
# contract (#2032). Sourced, never executed.
#
# Writers render single-line frontmatter values through
# bridge/utils/skill_frontmatter.py (installed as hooks/ccc_skill_frontmatter.py):
# plain when YAML reads the text back unchanged, otherwise ONE double-quoted
# line (fleet-skills#328 rejects unquoted values containing ": " or " #").
# Shell line readers must therefore decode a quoted value instead of keeping
# the quotes, or lengths, dedup tokens, and listings see `"Use when ..."`.
#
# CCC_YAML_UNQUOTE_AWK defines ccc_yaml_unquote(s) for inline awk programs:
#   awk "$CCC_YAML_UNQUOTE_AWK"'{ print ccc_yaml_unquote($0) }'
# It mirrors unquote_scalar() in the Python helper for everything the
# renderer emits for printable text: "..." with \" \\ \/ \<space> \t escapes,
# '...' with '' escapes, and an optional trailing " # comment". Escapes of
# control or non-printable characters (\xHH, \uHHHH, \e, ...) stay literal —
# display-safe for listings — and malformed quoting returns the value as-is.
# Plain values are returned verbatim (a " #..." tail is kept, as before).
#
# ccc_fm_field <skill.md> <key> prints the first single-line value of <key>
# in the frontmatter, decoded.

# shellcheck disable=SC2016  # awk program text, expanded by awk not the shell
CCC_YAML_UNQUOTE_AWK='
function ccc_yaml_unquote(s,    q, n, i, c, e, out) {
  sub(/^[ \t]+/, "", s)
  sub(/[ \t\r]+$/, "", s)
  q = substr(s, 1, 1)
  if (q != "\"" && q != "\047") return s
  n = length(s)
  out = ""
  i = 2
  while (i <= n) {
    c = substr(s, i, 1)
    if (c == q) {
      if (q == "\047" && substr(s, i + 1, 1) == "\047") { out = out c; i += 2; continue }
      if (substr(s, i + 1) ~ /^([ \t]+#.*)?$/) return out
      return s
    }
    if (c == "\\" && q == "\"") {
      if (i == n) return s
      e = substr(s, i + 1, 1)
      if (e == "\"" || e == "\\" || e == "/" || e == " ") out = out e
      else if (e == "t") out = out "\t"
      else out = out c e
      i += 2
      continue
    }
    out = out c
    i++
  }
  return s
}
'

ccc_fm_field() { # <skill.md> <key> — first single-line frontmatter value, YAML-unquoted
  awk -v k="$2" "$CCC_YAML_UNQUOTE_AWK"'
    NR==1 { next }
    /^---/ { exit }
    $0 ~ "^" k ":" { sub("^" k ":[[:space:]]*", ""); print ccc_yaml_unquote($0); exit }
  ' "$1" 2>/dev/null
}
