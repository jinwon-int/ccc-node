#!/usr/bin/env bash
# Unit tests for start.sh's process-identification predicates (#584 P3-2).
#
# These decide which PIDs --status calls "running", which --stop and
# reap_competing_pollers terminate, and which process a restart may not kill.
# A false positive kills an unrelated process; a false negative launches a
# second poller and self-inflicts a getUpdates Conflict (#446). They had no
# direct coverage: service-install.test.sh and restart.test.sh drive start.sh
# as a subprocess and never reach these predicates with adversarial input.
#
# The suite sources start.sh through its CCC_START_SH_LIB_ONLY seam and feeds
# the predicates fixture cmdline files, so no real process is inspected,
# signalled, or created.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
# Fixtures supply every input; ambient harness variables must not reach the
# script under test (#1023).
ccc_test_reset_hook_env
pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# The predicates compare against $PROJECT_ROOT, so the seam is sourced once per
# project root under test. `--path` requires an existing directory.
proj="$TMP/proj"; mkdir -p "$proj"
source_seam() { # <project-root>
  # start.sh is not written against `set -u`; sourcing it under this suite's
  # options would abort on the first optional variable. Relax only for the
  # source itself, then restore, so the assertions keep the strict setting.
  set +u
  # shellcheck disable=SC1091  # resolved at runtime from $ROOT
  CCC_START_SH_LIB_ONLY=1 . "$ROOT/bridge/start.sh" --path "$1" >/dev/null 2>&1
  local rc=$?
  set -u
  return "$rc"
}
source_seam "$proj"
# shellcheck disable=SC2034  # consumed via eval in ok()
src_rc=$?
ok "start.sh sources cleanly through the lib-only seam" '[ "$src_rc" = 0 ]'
ok "seam does not dispatch an action" '[ "$ACTION" = "run" ]'
ok "seam still parses --path" '[ "$PROJECT_ROOT" = "$proj" ]'
ok "predicates are defined after sourcing" \
  '[ "$(type -t _cmdline_is_project_bot)" = function ] && [ "$(type -t find_project_bot_pids)" = function ]'

# Build a NUL-delimited /proc/<pid>/cmdline fixture from literal argv words.
mk_cmdline() { # <name> <argv...>
  local name="$1"; shift
  local f="$TMP/cmdline.$name"
  : > "$f"
  local a
  for a in "$@"; do printf '%s\0' "$a" >> "$f"; done
  printf '%s' "$f"
}

# --- positive: the exact process this project owns ---------------------------
f="$(mk_cmdline exact python3 -m telegram_bot --path "$proj")"
ok "matches this project's bot" '_cmdline_is_project_bot "$f"'

f="$(mk_cmdline extra_args python3 -m telegram_bot --path "$proj" -d --debug)"
ok "matches with trailing flags after --path" '_cmdline_is_project_bot "$f"'

f="$(mk_cmdline reordered python3 -m telegram_bot -d --path "$proj")"
ok "matches when --path is not the last option" '_cmdline_is_project_bot "$f"'

# --- negative: prefix confusion, the #446 failure class -----------------------
# A sibling root sharing a prefix must never be claimed. Killing it would take
# down another node's bridge.
sibling="${proj}X"; mkdir -p "$sibling"
f="$(mk_cmdline sibling python3 -m telegram_bot --path "$sibling")"
ok "does NOT match a project root that merely shares a prefix" '! _cmdline_is_project_bot "$f"'

child="$proj/nested"; mkdir -p "$child"
f="$(mk_cmdline nested python3 -m telegram_bot --path "$child")"
ok "does NOT match a project root nested under this one" '! _cmdline_is_project_bot "$f"'

f="$(mk_cmdline parent python3 -m telegram_bot --path "$TMP")"
ok "does NOT match this project root's parent" '! _cmdline_is_project_bot "$f"'

# --- negative: right path, wrong program -------------------------------------
f="$(mk_cmdline no_module python3 -m something_else --path "$proj")"
ok "does NOT match a different module on the same path" '! _cmdline_is_project_bot "$f"'

f="$(mk_cmdline editor vim --path "$proj")"
ok "does NOT match an unrelated command holding the same --path" '! _cmdline_is_project_bot "$f"'

# The root appearing as some other option's value is not ownership.
f="$(mk_cmdline other_opt python3 -m telegram_bot --log-dir "$proj")"
ok "does NOT match when the root is another option's value" '! _cmdline_is_project_bot "$f"'

# --- boundary: malformed argv must not crash or over-match -------------------
f="$(mk_cmdline dangling python3 -m telegram_bot --path)"
ok "does NOT match a dangling --path with no value (no out-of-bounds read)" \
  '! _cmdline_is_project_bot "$f"'

f="$(mk_cmdline empty_file)"
ok "does NOT match an empty cmdline" '! _cmdline_is_project_bot "$f"'

ok "does NOT match a cmdline file that does not exist" \
  '! _cmdline_is_project_bot "$TMP/cmdline.absent"'
# A process can exit between pgrep listing it and this read, so the vanished
# cmdline is an expected race, not an error worth printing on every poll.
# shellcheck disable=SC2034  # consumed via eval in ok()
absent_err="$(_cmdline_is_project_bot "$TMP/cmdline.absent" 2>&1 >/dev/null)"
ok "a vanished cmdline is silent (no stderr leak from the read)" '[ -z "$absent_err" ]'

f="$(mk_cmdline module_only python3 -m telegram_bot)"
ok "does NOT match the module with no --path at all" '! _cmdline_is_project_bot "$f"'

# --- metacharacter paths: matching is literal, never a pattern ---------------
# pgrep gathers candidates by a metacharacter-free literal prefix precisely
# because a raw root inside an ERE mis-judged these (#446); confirm the exact
# check that backs it treats the root as literal bytes.
meta="$TMP/a.b[c]*d"; mkdir -p "$meta"
source_seam "$meta"
ok "seam re-sources with a metacharacter project root" '[ "$PROJECT_ROOT" = "$meta" ]'
f="$(mk_cmdline meta_exact python3 -m telegram_bot --path "$meta")"
ok "matches a root containing regex/glob metacharacters" '_cmdline_is_project_bot "$f"'
# The glob-expanded spelling is a different directory and must not match.
f="$(mk_cmdline meta_glob python3 -m telegram_bot --path "$TMP/a.bc_d")"
ok "does NOT match a path the metacharacters would have globbed to" \
  '! _cmdline_is_project_bot "$f"'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
