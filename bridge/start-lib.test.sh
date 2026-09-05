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

# --- detached spawn names the interpreter (#1151) ---------------------------
# The daemon path spawns this checkout's start.sh. Executing it directly made
# the spawn depend on the shebang resolving, which it does not on Termux, and
# nohup swallowed the failure into one log line while the caller reported a
# PID. A target whose shebang cannot resolve stands in for that node: it runs
# only if the call site names the interpreter itself.
source_seam "$proj"
fake_dir="$TMP/fake-checkout"; mkdir -p "$fake_dir"
spawn_marker="$TMP/spawned.args"
cat > "$fake_dir/start.sh" <<EOF
#!$TMP/no-such-interpreter
printf '%s\n' "\$*" > "$spawn_marker"
EOF
chmod +x "$fake_dir/start.sh"
spawn_log="$TMP/spawn.log"; : > "$spawn_log"
# SCRIPT_DIR is overridden inside a subshell so the seam's own value, which the
# assertions above still rely on, is left intact. `wait` reaps the backgrounded
# spawn before the marker is read.
# shellcheck disable=SC2034  # read by the sourced spawn helper, not this file
( SCRIPT_DIR="$fake_dir"
  spawn_start_sh_detached "$spawn_log" --path "$proj" --_daemon_supervisor
  wait ) >/dev/null 2>&1
ok "detached spawn runs a target whose shebang does not resolve" \
  '[ -f "$spawn_marker" ]'
ok "detached spawn forwards its arguments" \
  'grep -q -- "--_daemon_supervisor" "$spawn_marker" 2>/dev/null'
ok "detached spawn leaves no interpreter error in the log" \
  '! grep -qi "no such file\|bad interpreter" "$spawn_log"'

# --- proxy banner redaction (#1480) -------------------------------------------
# In daemon mode the startup banner lands in supervisor.log/restart.log, so the
# proxy URL's userinfo must never reach it. The env vars themselves stay intact.
ok "redact: userinfo is masked, host/port/path survive" \
  '[ "$(redact_url_userinfo "http://alice:s3cret@proxy.example:7890/p?q=1")" = "http://<redacted>@proxy.example:7890/p?q=1" ]'
ok "redact: a raw @ inside the password does not leak" \
  '[ "$(redact_url_userinfo "socks5://u:p@ss@proxy.example:1080")" = "socks5://<redacted>@proxy.example:1080" ]'
ok "redact: @ in the path is not userinfo" \
  '[ "$(redact_url_userinfo "http://proxy.example:7890/a@b")" = "http://proxy.example:7890/a@b" ]'
ok "redact: URL without userinfo passes through unchanged" \
  '[ "$(redact_url_userinfo "http://127.0.0.1:7890")" = "http://127.0.0.1:7890" ]'
ok "redact: scheme-less authority is masked too" \
  '[ "$(redact_url_userinfo "user:pw@proxy.example:7890")" = "<redacted>@proxy.example:7890" ]'
# load_optional_env is defined below the seam (run-flow only), so the banner
# call site is pinned statically: the echo must go through the redactor while
# the exports keep the raw value.
ok "proxy banner call site routes through redact_url_userinfo" \
  'grep -q "Proxy configured: \$(redact_url_userinfo \"\$proxy_url\")" "$ROOT/bridge/start.sh"'
ok "proxy exports keep the raw URL (no redaction on the env path)" \
  'grep -q "export https_proxy=\"\$proxy_url\"" "$ROOT/bridge/start.sh"'

# --- atomic token lock (#1480) ------------------------------------------------
# The old claim was read+kill-0 then printf: two concurrent starts both passed
# and both wrote the file. acquire_token_lock must let exactly one contender
# through, refuse the rest while the holder lives, and reclaim a dead holder
# (stale pid) — in both the flock path and the flock-less mkdir fallback.
# TOKEN_LOCK_FILE is pinned directly so init_token_lock's token lookup is
# bypassed; each contender runs in its own subshell (the lock fd is per process).
lock_root="$TMP/locks"; mkdir -p "$lock_root"
# shellcheck disable=SC2034  # read by the sourced lock helpers, not this file
TOKEN_LOCK_FILE="$lock_root/token.pid"
wait_for_file() { # <path> — bounded poll
  for _ in $(seq 1 100); do [ -e "$1" ] && return 0; sleep 0.1; done
  return 1
}
hold_lock() { # <name> — background holder until $TMP/<name>.release exists
  ( acquire_token_lock "$BASHPID" >"$TMP/$1.out" 2>&1 || exit 1
    printf '%s' "$BASHPID" > "$TMP/$1.pid"
    while [ ! -e "$TMP/$1.release" ]; do sleep 0.1; done ) &
  wait_for_file "$TMP/$1.pid"
}
release_lock() { touch "$TMP/$1.release"; wait 2>/dev/null; }
contend() { # <name> — one-shot contender; rc in <name>.rc, output in <name>.out
  ( acquire_token_lock "$BASHPID" >"$TMP/$1.out" 2>&1; echo $? > "$TMP/$1.rc" )
}
race() { # <name> <n> — n contenders released together; winners append their pid
  local name="$1" n="$2"
  rm -f "$TMP/$name.go" "$TMP/$name.winners"
  for _ in $(seq 1 "$n"); do
    ( while [ ! -e "$TMP/$name.go" ]; do sleep 0.05; done
      if acquire_token_lock "$BASHPID" >/dev/null 2>&1; then
        echo "$BASHPID" >> "$TMP/$name.winners"; sleep 3
      fi ) &
  done
  sleep 0.3; touch "$TMP/$name.go"; wait
}

# Tiers: util-linux flock(1); python fcntl on the same inherited fd (Termux
# without util-linux); mkdir claim dir (no flock AND no python3). The same seam
# as refresh-memory.sh forces past flock(1); a PATH without python3 forces the
# last resort. The subshells only need these tools beyond bash builtins.
lock_bin_min="$TMP/bin-min"; mkdir -p "$lock_bin_min"
for tool in sleep cat mkdir rm mv rmdir find seq touch wc grep; do
  ln -s "$(command -v "$tool")" "$lock_bin_min/$tool"
done
LOCK_TEST_PATH="$PATH"
for lock_mode in flock python mkdir; do
  rm -rf "$lock_root"; mkdir -p "$lock_root"
  PATH="$LOCK_TEST_PATH"
  case "$lock_mode" in
    flock) unset CCC_FLOCK_CLI ;;
    python) export CCC_FLOCK_CLI="$TMP/no-such-flock" ;;
    mkdir) export CCC_FLOCK_CLI="$TMP/no-such-flock"; PATH="$lock_bin_min" ;;
  esac
  hold_lock "h-$lock_mode"
  ok "[$lock_mode] holder acquires and records its pid" \
    '[ "$(cat "$TOKEN_LOCK_FILE")" = "$(cat "$TMP/h-$lock_mode.pid")" ]'
  contend "c-$lock_mode"
  ok "[$lock_mode] contender is refused while the holder lives" \
    '[ "$(cat "$TMP/c-$lock_mode.rc")" = 1 ] && grep -q "already using the same Bot Token (PID: $(cat "$TMP/h-$lock_mode.pid"))" "$TMP/c-$lock_mode.out"'
  ok "[$lock_mode] refused contender leaves the holder's pid in place" \
    '[ "$(cat "$TOKEN_LOCK_FILE")" = "$(cat "$TMP/h-$lock_mode.pid")" ]'
  # Dead holder: SIGKILL runs no cleanup, so the pid file (and, in fallback,
  # the claim dir) is left behind exactly as a crashed bridge would leave it.
  # (The holder's last `sleep` child inherited the lock fd; give it its 100 ms
  # to exit — in production that inheritance is the daemon child, by design.)
  kill -9 "$(cat "$TMP/h-$lock_mode.pid")" 2>/dev/null; wait 2>/dev/null; sleep 0.3
  contend "s-$lock_mode"
  ok "[$lock_mode] stale claim of a dead holder is reclaimed" \
    '[ "$(cat "$TMP/s-$lock_mode.rc")" = 0 ]'
  # The one-shot reclaimer above exited without cleanup, so this race starts
  # against a stale claim: the hardest case for the mkdir tier (all five see a
  # dead holder and all try to reclaim at once).
  race "r-$lock_mode" 5
  ok "[$lock_mode] concurrent launch against a stale claim: exactly one of five wins" \
    '[ "$(wc -l < "$TMP/r-$lock_mode.winners")" -eq 1 ]'
  ok "[$lock_mode] the recorded pid is the winner's" \
    '[ "$(cat "$TOKEN_LOCK_FILE")" = "$(cat "$TMP/r-$lock_mode.winners")" ]'
  ( cleanup_token_lock )
  race "q-$lock_mode" 5
  ok "[$lock_mode] concurrent launch from a clean state: exactly one of five wins" \
    '[ "$(wc -l < "$TMP/q-$lock_mode.winners")" -eq 1 ]'
done
PATH="$LOCK_TEST_PATH"
ok "mkdir tier announces itself" 'grep -q "flock unavailable; using mkdir fallback" "$TMP/h-mkdir.out"'
ok "flock and python tiers are silent about the fallback" \
  '! grep -q "mkdir fallback" "$TMP/h-flock.out" "$TMP/h-python.out"'
ok "mkdir tier leaves no reclaim token behind" '[ ! -e "$TOKEN_LOCK_FILE.d.reclaim" ]'
( cleanup_token_lock )
ok "cleanup removes the pid file and the fallback claim dir" \
  '[ ! -e "$TOKEN_LOCK_FILE" ] && [ ! -e "$TOKEN_LOCK_FILE.d" ]'
unset CCC_FLOCK_CLI
# Call-site pins: both run paths claim atomically BEFORE prepare_runtime, and
# nothing else writes the pid into the lock file any more.
ok "daemon supervisor claims the lock atomically before prepare_runtime" \
  'grep -A1 "acquire_token_lock \"\$\$\" || exit 1" "$ROOT/bridge/start.sh" | grep -q prepare_runtime'
ok "no bare write_token_lock claim remains outside acquire_token_lock" \
  '[ "$(grep -c "^ *write_token_lock \"\\$\\$\"" "$ROOT/bridge/start.sh")" = 0 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
