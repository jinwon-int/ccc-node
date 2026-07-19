#!/usr/bin/env bash
# Tests for ccc-bridge-locate.sh — serving-checkout detection from a scripted
# process table (CCC_BRIDGE_LOCATE_PS seam), candidate scan over fake checkout
# dirs, JSON validity, restartCmd correctness, and exit codes 0/2/4.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SUT="$HERE/ccc-bridge-locate.sh"
pass=0; fail=0
ok()  { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
okc() { if [ "$1" = "$2" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $3 (rc=$1 want=$2)"; fi; }

command -v jq >/dev/null 2>&1 || { echo "SKIP: jq not available"; echo "PASS=0 FAIL=0"; exit 0; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ── fixtures: two fake checkouts (one clean on a branch, one dirty) ──────────
mk_checkout() { # <dir> <branch>
    mkdir -p "$1/bridge"
    printf '#!/usr/bin/env bash\n' > "$1/bridge/start.sh"
    git -C "$1" -c init.defaultBranch="$2" init -q
    git -C "$1" add -A
    git -C "$1" -c user.email=t@t -c user.name=t commit -q -m init
}
CO_A="$TMP/co-a"; mk_checkout "$CO_A" main
CO_B="$TMP/co-b"; mk_checkout "$CO_B" feat/x
echo dirty > "$CO_B/uncommitted.txt"   # 1 untracked file → dirty=1
HEAD_A="$(git -C "$CO_A" rev-parse --short HEAD)"
# shellcheck disable=SC2034  # referenced inside eval'd ok() assertions
HEAD_B="$(git -C "$CO_B" rev-parse --short HEAD)"
# not-a-checkout: missing bridge/start.sh → must be excluded from candidates
mkdir -p "$TMP/co-nogit/bridge"

# ── scripted process tables ──────────────────────────────────────────────────
PS_ONE="$TMP/ps-one"
cat > "$PS_ONE" <<EOF
    1 /sbin/init
  777 $CO_A/bridge/venv/bin/python -m telegram_bot --path /root
  888 grep --color python -m nothing
EOF
PS_NONE="$TMP/ps-none"
printf '    1 /sbin/init\n  999 sshd: root@pts/0\n' > "$PS_NONE"
PS_TWO="$TMP/ps-two"
cat > "$PS_TWO" <<EOF
  777 $CO_A/bridge/venv/bin/python -m telegram_bot --path /root
  778 $CO_B/bridge/venv/bin/python3 -m telegram_bot --path /home/u
EOF

run() { # <ps-fixture> <args...> → stdout; rc in $RC
    local fixture="$1"; shift
    RC=0
    OUT="$(CCC_BRIDGE_LOCATE_PS="cat $fixture" \
           CCC_BRIDGE_LOCATE_CANDIDATES="$CO_A:$CO_B:$TMP/co-nogit:$CO_A" \
           bash "$SUT" "$@")" || RC=$?
}

# ── single serving bridge → exit 0 ───────────────────────────────────────────
run "$PS_ONE" --json
okc "$RC" 0 "single bridge: exit 0"
ok "single bridge: valid JSON" 'printf "%s" "$OUT" | jq -e . >/dev/null'
ok "single bridge: running=true multi=false" \
   '[ "$(printf "%s" "$OUT" | jq -r ".running,.multi" | paste -sd, -)" = "true,false" ]'
ok "single bridge: pid parsed" '[ "$(printf "%s" "$OUT" | jq -r ".bridges[0].pid")" = 777 ]'
ok "single bridge: checkout resolved from venv python path" \
   '[ "$(printf "%s" "$OUT" | jq -r ".bridges[0].checkout")" = "$CO_A" ]'
ok "single bridge: projectPath parsed from --path" \
   '[ "$(printf "%s" "$OUT" | jq -r ".bridges[0].projectPath")" = "/root" ]'
ok "single bridge: head+branch reported for serving checkout" \
   '[ "$(printf "%s" "$OUT" | jq -r ".bridges[0].head")" = "$HEAD_A" ] && [ "$(printf "%s" "$OUT" | jq -r ".bridges[0].branch")" = "main" ]'
ok "single bridge: restartCmd is the serving checkout start.sh (#611 form)" \
   '[ "$(printf "%s" "$OUT" | jq -r ".restartCmd")" = "$CO_A/bridge/start.sh --path /root --restart -d" ]'
ok "single bridge (no --all): candidates omitted" \
   '[ "$(printf "%s" "$OUT" | jq -r ".candidates | length")" = 0 ]'

# --all: candidates appear even while running; bogus + duplicate roots excluded
run "$PS_ONE" --json --all
ok "--all: candidates scanned while running (2 real checkouts, deduped, no bogus)" \
   '[ "$(printf "%s" "$OUT" | jq -r ".candidates | length")" = 2 ]'
ok "--all: dirty count and branch reported for dirty candidate" \
   '[ "$(printf "%s" "$OUT" | jq -r ".candidates[1].dirty")" = 1 ] && [ "$(printf "%s" "$OUT" | jq -r ".candidates[1].branch")" = "feat/x" ]'
ok "--all: clean candidate reports dirty=0 and its head" \
   '[ "$(printf "%s" "$OUT" | jq -r ".candidates[0].dirty")" = 0 ] && [ "$(printf "%s" "$OUT" | jq -r ".candidates[0].head")" = "$HEAD_A" ]'

# ── no running bridge → exit 2, candidate scan runs ──────────────────────────
run "$PS_NONE" --json
okc "$RC" 2 "no bridge: exit 2"
ok "no bridge: running=false, restartCmd null, candidates scanned" \
   '[ "$(printf "%s" "$OUT" | jq -r ".running,.restartCmd,(.candidates|length)" | paste -sd, -)" = "false,null,2" ]'
run "$PS_NONE"
ok "no bridge (human): says no running bridge and lists candidates" \
   'printf "%s" "$OUT" | grep -q "no running bridge" && printf "%s" "$OUT" | grep -q "checkout=$CO_B"'

# ── multiple bridges → exit 4, each reported ─────────────────────────────────
run "$PS_TWO" --json
okc "$RC" 4 "multiple bridges: exit 4"
ok "multiple bridges: multi=true and both reported" \
   '[ "$(printf "%s" "$OUT" | jq -r ".multi,(.bridges|length)" | paste -sd, -)" = "true,2" ]'
ok "multiple bridges: second checkout+path resolved (python3 venv)" \
   '[ "$(printf "%s" "$OUT" | jq -r ".bridges[1].checkout")" = "$CO_B" ] && [ "$(printf "%s" "$OUT" | jq -r ".bridges[1].projectPath")" = "/home/u" ]'
ok "multiple bridges: restartCmd null (ambiguous — never guess)" \
   '[ "$(printf "%s" "$OUT" | jq -r ".restartCmd")" = "null" ]'

# ── read-only guarantee (static): no process/file mutation verbs ─────────────
ok "read-only: script never kills/writes (no kill/rm/mv/tee/> redirection to files)" \
   '! grep -nE "(^|[^a-zA-Z_-])(kill|pkill|rm -|mv |tee )" "$SUT"'
ok "shebang is env-based (runs on VPS + Termux)" '[ "$(head -1 "$SUT")" = "#!/usr/bin/env bash" ]'

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
