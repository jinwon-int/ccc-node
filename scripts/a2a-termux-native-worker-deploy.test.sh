#!/usr/bin/env bash
# Tests for the canonical Termux-native worker deploy (#2791 stage 3).
#
# The harness's restart-worker (tested in a2a-termux-native-worker-restart.test.sh)
# is replaced by a recorder via A2A_NATIVE_WORKER_HARNESS: its gate rc comes
# from $TMP/gate.rc and each real restart pops the next rc from
# $TMP/restart.rcs. A fake `node` on PATH plays the artifact's rollout guard.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY="$ROOT/scripts/a2a-termux-native-worker-deploy.sh"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env

pass=0; fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() {
    if eval "$2"; then
        pass=$((pass+1))
    else
        fail=$((fail+1))
        echo "FAIL: $1"
        echo "  cond: $2"
        [[ -n "${out:-}" ]] && printf '  out: %s\n' "$out"
    fi
}

OLD_SHA=1111111111111111111111111111111111111111
NEW_SHA=2222222222222222222222222222222222222222
WROOT="$TMP/home/a2a-broker-worker"
BK="$TMP/backups"
ENVF="$TMP/canonical.env"
printf 'A2A_WORKER_ROOT=%s\nA2A_TUNNEL_SSH_TARGET=fake\n' "$WROOT" > "$ENVF"

# Live worker root: old build, an extra dist file the artifact does not ship,
# a node_modules package the artifact replaces and one it must not touch.
reset_root() {
    rm -rf "$WROOT"; mkdir -p "$WROOT/dist" "$WROOT/scripts" "$WROOT/node_modules/a2a-core" "$WROOT/node_modules/left-alone"
    echo 'old worker' > "$WROOT/dist/worker.js"
    echo 'old only' > "$WROOT/dist/legacy.js"
    printf '{"revision":"%s"}\n' "$OLD_SHA" > "$WROOT/dist/build-info.json"
    echo 'old script' > "$WROOT/scripts/tool.mjs"
    echo 'old core' > "$WROOT/node_modules/a2a-core/index.js"
    echo 'keep' > "$WROOT/node_modules/left-alone/index.js"
    echo "$OLD_SHA" > "$WROOT/.deploy-source-sha"
    echo 'state' > "$WROOT/worker-state.json"
}
snapshot() { (cd "$WROOT" && find . -type f -print0 | sort -z | xargs -0 sha256sum; find . | sort) | sha256sum; }

# Artifact: new build + a new handlers/ dir + replaced and new packages.
ART="$TMP/stage/artifact"
mkdir -p "$ART/dist" "$ART/scripts" "$ART/handlers" "$ART/node_modules/a2a-core" "$ART/node_modules/a2a-new"
echo 'new worker' > "$ART/dist/worker.js"
printf '{"revision":"%s"}\n' "$NEW_SHA" > "$ART/dist/build-info.json"
echo 'new script' > "$ART/scripts/tool.mjs"
echo 'handler' > "$ART/handlers/h.mjs"
echo 'new core' > "$ART/node_modules/a2a-core/index.js"
echo 'brand new' > "$ART/node_modules/a2a-new/index.js"
tar czf "$TMP/artifact.tgz" -C "$TMP/stage" artifact

cat > "$TMP/fake-harness.sh" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$TMP/harness.calls"
if [[ " \$* " == *" --dry-run "* ]]; then
    rc=\$(cat "$TMP/gate.rc" 2>/dev/null || echo 0)
else
    rc=\$(head -n1 "$TMP/restart.rcs" 2>/dev/null); rc=\${rc:-0}
    sed -i '1d' "$TMP/restart.rcs" 2>/dev/null || true
    printf 'worker.js at restart: %s\n' "\$(cat "$WROOT/dist/worker.js")" >> "$TMP/harness.calls"
fi
echo "restart-worker: result=fake rc=\$rc sup_count=1 workers=1 tunnel=UP"
exit "\$rc"
EOF
chmod +x "$TMP/fake-harness.sh"
mkdir -p "$TMP/bin"
cat > "$TMP/bin/node" <<EOF
#!/usr/bin/env bash
echo "guard: \$* root=\${A2A_WORKER_ROOT:-} target=\${A2A_WORKER_TARGET_REVISION:-}" >> "$TMP/node.calls"
cat "$TMP/guard.out" 2>/dev/null || echo '{"ok":true}'
EOF
chmod +x "$TMP/bin/node"
export PATH="$TMP/bin:$PATH"
export A2A_NATIVE_WORKER_HARNESS="$TMP/fake-harness.sh"
export A2A_DEPLOY_BACKUP_DIR="$BK"

# shellcheck disable=SC2034  # BEFORE is read by the eval-based ok() assertions.
fresh() { reset_root; rm -rf "$BK" "$TMP/harness.calls" "$TMP/restart.rcs" "$TMP/gate.rc" "$TMP/guard.out" "$TMP/node.calls"; BEFORE=$(snapshot); }
mkdir -p "$TMP/stagetmp"
deploy() { TMPDIR="$TMP/stagetmp" bash "$DEPLOY" --env-file "$ENVF" "$@" 2>&1; }
restart_calls() { grep -c -- '--if-idle --env-file' "$TMP/harness.calls" 2>/dev/null | head -n1; }
gate_calls() { grep -c -- '--if-idle --dry-run --env-file' "$TMP/harness.calls" 2>/dev/null | head -n1; }

# ---- 1. usage / artifact validation (rc=2, nothing touched) ------------------
fresh
out=$(bash "$DEPLOY" 2>&1); rc=$?
ok "no args prints usage rc=2" '[ "$rc" = 2 ] && grep -q "Usage:" <<<"$out"'
out=$(deploy --artifact "$TMP/nope"); rc=$?
ok "missing artifact rc=2" '[ "$rc" = 2 ] && grep -q "artifact not found" <<<"$out"'
mkdir -p "$TMP/empty-art"
out=$(deploy --artifact "$TMP/empty-art"); rc=$?
ok "artifact without dist/worker.js rc=2" '[ "$rc" = 2 ] && grep -q "no dist/worker.js" <<<"$out"'
out=$(deploy --artifact "$ART" --target-sha "$OLD_SHA"); rc=$?
ok "artifact revision != --target-sha rc=2" '[ "$rc" = 2 ] && grep -q "!= --target-sha" <<<"$out"'
out=$(deploy --artifact "$ART" --target-sha nothex); rc=$?
ok "malformed --target-sha rc=2" '[ "$rc" = 2 ]'
python3 - "$TMP/evil.tgz" <<'PY'
import io, sys, tarfile
with tarfile.open(sys.argv[1], "w:gz") as tf:
    data = b"x"
    info = tarfile.TarInfo("../escape.js"); info.size = len(data)
    tf.addfile(info, io.BytesIO(data))
PY
out=$(deploy --artifact "$TMP/evil.tgz"); rc=$?
ok "tar member with .. is refused rc=2" '[ "$rc" = 2 ] && grep -q "unsafe member" <<<"$out" && [ ! -e "$TMP/escape.js" ]'
ok "validation failures never reach the gate or the root" '[ ! -e "$TMP/harness.calls" ] && [ "$(snapshot)" = "$BEFORE" ]'

# ---- 2. dry-run is the default ----------------------------------------------
fresh
out=$(deploy --artifact "$ART"); rc=$?
ok "dry-run (default) exits 0 with a plan" \
    '[ "$rc" = 0 ] && grep -q "plan mode=dry-run" <<<"$out" && grep -q "plan rev $OLD_SHA -> $NEW_SHA" <<<"$out" && grep -q "DRY-RUN OK" <<<"$out"'
ok "dry-run plan lists dirs and replaced packages" \
    'grep -q "plan copy dist/ scripts/ handlers/ node_modules=\[a2a-core a2a-new\]" <<<"$out"'
ok "dry-run runs only the gate (restart-worker --if-idle --dry-run)" \
    '[ "$(gate_calls)" = 1 ] && [ "$(restart_calls)" = 0 ]'
ok "dry-run changes nothing and writes no backup" '[ "$(snapshot)" = "$BEFORE" ] && [ ! -e "$BK" ]'
ok "dry-run reports the preview checks" 'grep -q "dry-run checks ok on staged preview (revision, guard=auto, smoke=no)" <<<"$out"'

# ---- 2b. dry-run runs the pre-restart checks on a temp preview ----------------
# Owner decision 2026-11-27: the dry-run includes the handler smoke (and the
# artifact's rollout guard) the --apply path runs before its restart. They see
# the post-deploy layout in a temp dir; the live root and backups stay untouched.
SMOKE_OUT="$TMP/smoke.seen"
# shellcheck disable=SC2016  # the smoke command is expanded by the deploy's bash -c.
PROBE='{ echo "root=$A2A_WORKER_ROOT"; echo "worker=$(cat "$A2A_WORKER_ROOT/dist/worker.js")"; echo "legacy=$(cat "$A2A_WORKER_ROOT/dist/legacy.js")"; echo "handler=$(cat "$A2A_WORKER_ROOT/handlers/h.mjs")"; echo "core=$(cat "$A2A_WORKER_ROOT/node_modules/a2a-core/index.js")"; echo "kept=$(cat "$A2A_WORKER_ROOT/node_modules/left-alone/index.js")"; echo "state=$(cat "$A2A_WORKER_ROOT/worker-state.json")"; } > '"$SMOKE_OUT"'; echo scribble > "$A2A_WORKER_ROOT/dist/worker.js"; echo junk > "$A2A_WORKER_ROOT/node_modules/a2a-core/index.js"'
for kind in dir tar; do
    fresh; rm -f "$SMOKE_OUT"
    src="$ART"; [[ "$kind" = tar ]] && src="$TMP/artifact.tgz"
    out=$(deploy --artifact "$src" --smoke-cmd "$PROBE"); rc=$?
    ok "$kind dry-run: smoke runs and the dry-run succeeds" \
        '[ "$rc" = 0 ] && [ -f "$SMOKE_OUT" ] && grep -q "dry-run checks ok on staged preview (revision, guard=auto, smoke=yes)" <<<"$out" && grep -q "DRY-RUN OK" <<<"$out"'
    ok "$kind dry-run: smoke sees the post-deploy layout (new files, kept files, kept packages)" \
        'grep -qx "worker=new worker" "$SMOKE_OUT" && grep -qx "legacy=old only" "$SMOKE_OUT" && grep -qx "handler=handler" "$SMOKE_OUT" && grep -qx "core=new core" "$SMOKE_OUT" && grep -qx "kept=keep" "$SMOKE_OUT" && grep -qx "state=state" "$SMOKE_OUT"'
    ok "$kind dry-run: smoke root is a temp preview, not the live root" \
        'r=$(sed -n "s/^root=//p" "$SMOKE_OUT"); [ -n "$r" ] && [ "$r" != "$WROOT" ] && [[ "$r" == "$TMP/stagetmp/"* ]]'
    ok "$kind dry-run: live root, backups and preview dir untouched / removed" \
        '[ "$(snapshot)" = "$BEFORE" ] && [ ! -e "$BK" ] && [ -z "$(ls -A "$TMP/stagetmp")" ] && [ "$(restart_calls)" = 0 ] && [ "$(gate_calls)" = 1 ]'
done

fresh
out=$(deploy --artifact "$ART" --smoke-cmd 'test "$(cat "$A2A_WORKER_ROOT/dist/worker.js")" = "never"'); rc=$?
ok "dry-run smoke failure: exit 20, nothing changed, gate never reached" \
    '[ "$rc" = 20 ] && grep -q "dry-run pre-restart check failed on the staged preview: smoke command failed — nothing changed" <<<"$out" && [ "$(snapshot)" = "$BEFORE" ] && [ ! -e "$BK" ] && [ ! -e "$TMP/harness.calls" ] && [ -z "$(ls -A "$TMP/stagetmp")" ]'

fresh; : > "$ART/scripts/worker-artifact-rollout-guard.mjs"; echo '{"ok":false}' > "$TMP/guard.out"
out=$(deploy --artifact "$ART"); rc=$?
ok "dry-run guard ok=false: exit 20 on the preview, nothing changed" \
    '[ "$rc" = 20 ] && grep -q "staged preview: rollout guard failed" <<<"$out" && [ "$(snapshot)" = "$BEFORE" ] && [ ! -e "$TMP/harness.calls" ]'
ok "dry-run guard runs --deployed against the preview root" \
    'grep -q -- "--deployed root=$TMP/stagetmp/" "$TMP/node.calls" && ! grep -q "root=$WROOT\$" "$TMP/node.calls"'
fresh; echo '{"ok":false}' > "$TMP/guard.out"
out=$(deploy --artifact "$ART" --no-guard); rc=$?
ok "dry-run --no-guard skips the shipped guard" '[ "$rc" = 0 ] && [ ! -e "$TMP/node.calls" ] && grep -q "guard=off" <<<"$out"'
# A shipped guard together with --target-sha (the way an operator runs a real rollout): the guard gets the target
# revision in its environment. Before this fix the assignment ran as the command (rc 127 on every such run).
fresh; : > "$ART/scripts/worker-artifact-rollout-guard.mjs"
out=$(deploy --artifact "$ART" --target-sha "$NEW_SHA"); rc=$?
ok "dry-run guard + --target-sha: the guard runs and passes" \
    '[ "$rc" = 0 ] && grep -q "DRY-RUN OK" <<<"$out" && ! grep -q "command not found" <<<"$out"'
ok "dry-run guard + --target-sha: the guard sees the target revision" \
    'grep -q -- "--deployed root=$TMP/stagetmp/.* target=$NEW_SHA\$" "$TMP/node.calls"'
rm -f "$ART/scripts/worker-artifact-rollout-guard.mjs"

# A symlinked live dist/ (release-dir layout) must not let the preview overlay
# write through to its target.
fresh
REL="$TMP/release-dist"; rm -rf "$REL"; mv "$WROOT/dist" "$REL"; ln -s "$REL" "$WROOT/dist"
# shellcheck disable=SC2034  # REL_BEFORE is read by the eval-based ok() assertion.
REL_BEFORE=$( (cd "$REL" && find . -type f -print0 | sort -z | xargs -0 sha256sum; find . | sort) | sha256sum)
out=$(deploy --artifact "$ART" --smoke-cmd 'grep -qx "new worker" "$A2A_WORKER_ROOT/dist/worker.js" && [ -f "$A2A_WORKER_ROOT/dist/legacy.js" ] && [ ! -L "$A2A_WORKER_ROOT/dist" ]'); rc=$?
ok "symlinked live dist/: dry-run preview is a dereferenced copy; the link target is untouched" \
    '[ "$rc" = 0 ] && [ "$( (cd "$REL" && find . -type f -print0 | sort -z | xargs -0 sha256sum; find . | sort) | sha256sum)" = "$REL_BEFORE" ] && [ "$(cat "$REL/worker.js")" = "old worker" ] && [ -L "$WROOT/dist" ]'
rm -f "$WROOT/dist"; rm -rf "$REL"

# ---- 3. idle refusal ----------------------------------------------------------
fresh; echo 10 > "$TMP/gate.rc"
out=$(deploy --artifact "$ART" --apply); rc=$?
ok "busy at the gate: --apply exits 10 with the reason" '[ "$rc" = 10 ] && grep -q "BUSY" <<<"$out" && grep -q "nothing changed" <<<"$out"'
ok "busy at the gate: no restart, no backup, root untouched" \
    '[ "$(restart_calls)" = 0 ] && [ ! -e "$BK" ] && [ "$(snapshot)" = "$BEFORE" ]'
fresh; echo 3 > "$TMP/gate.rc"
out=$(deploy --artifact "$ART" --apply); rc=$?
ok "no supervisor/worker at the gate: exit 3, nothing changed" '[ "$rc" = 3 ] && [ "$(snapshot)" = "$BEFORE" ]'

# ---- 4. successful apply (directory and tar artifacts) ----------------------
for kind in dir tar; do
    fresh
    src="$ART"; [[ "$kind" = tar ]] && src="$TMP/artifact.tgz"
    out=$(deploy --artifact "$src" --target-sha "$NEW_SHA" --apply); rc=$?
    ok "$kind artifact: --apply succeeds" '[ "$rc" = 0 ] && grep -q "OK rev $OLD_SHA -> $NEW_SHA" <<<"$out"'
    ok "$kind artifact: files and packages replaced, others kept" \
        '[ "$(cat "$WROOT/dist/worker.js")" = "new worker" ] && [ -f "$WROOT/handlers/h.mjs" ] && [ "$(cat "$WROOT/node_modules/a2a-core/index.js")" = "new core" ] && [ -f "$WROOT/node_modules/a2a-new/index.js" ] && [ "$(cat "$WROOT/node_modules/left-alone/index.js")" = keep ] && [ -f "$WROOT/dist/legacy.js" ]'
    ok "$kind artifact: .deploy-source-sha names the new revision" '[ "$(cat "$WROOT/.deploy-source-sha")" = "$NEW_SHA" ]'
    ok "$kind artifact: one gate + one restart, restart saw the new files" \
        '[ "$(gate_calls)" = 1 ] && [ "$(restart_calls)" = 1 ] && grep -q "worker.js at restart: new worker" "$TMP/harness.calls"'
    ok "$kind artifact: root + node_modules backups exist" \
        '[ "$(compgen -G "$BK/a2a-broker-worker.pre-222222222222.*.tgz" | wc -l)" = 1 ] && [ "$(compgen -G "$BK/a2a-broker-worker-node_modules.pre-*.tgz" | wc -l)" = 1 ]'
done
ok "tar artifact staging is cleaned up" '[ -z "$(ls -A "$TMP/stagetmp")" ]'

# ---- 5. rollback paths --------------------------------------------------------
# 5a. pre-restart smoke failure -> exact rollback, no restart.
fresh
out=$(deploy --artifact "$ART" --apply --smoke-cmd 'test "$(cat "$A2A_WORKER_ROOT/dist/worker.js")" = "never"'); rc=$?
ok "smoke failure: exit 20, rolled back" '[ "$rc" = 20 ] && grep -q "smoke command failed (no restart); rolled back" <<<"$out"'
ok "smoke failure: root restored exactly (added files removed) and no restart" \
    '[ "$(snapshot)" = "$BEFORE" ] && [ "$(restart_calls)" = 0 ]'

# 5b. the artifact's own rollout guard says not ok -> rollback.
fresh; mkdir -p "$ART/scripts"; : > "$ART/scripts/worker-artifact-rollout-guard.mjs"; echo '{"ok":false}' > "$TMP/guard.out"
out=$(deploy --artifact "$ART" --apply); rc=$?
ok "guard ok=false: exit 20, rolled back, no restart" \
    '[ "$rc" = 20 ] && grep -q "rollout guard failed" <<<"$out" && [ "$(snapshot)" = "$BEFORE" ] && [ "$(restart_calls)" = 0 ] && grep -q -- "--deployed" "$TMP/node.calls"'
fresh; echo '{"ok":false}' > "$TMP/guard.out"
out=$(deploy --artifact "$ART" --apply --no-guard); rc=$?
ok "--no-guard skips the shipped guard" '[ "$rc" = 0 ] && [ ! -e "$TMP/node.calls" ]'
rm -f "$ART/scripts/worker-artifact-rollout-guard.mjs"

# 5c. restart not verified -> rollback + a second restart on the old files.
fresh; printf '30\n0\n' > "$TMP/restart.rcs"
out=$(deploy --artifact "$ART" --apply); rc=$?
ok "unverified restart: exit 30" '[ "$rc" = 30 ] && grep -q "restart not verified (rc=30); rolled back to $OLD_SHA, restart on old files rc=0" <<<"$out"'
ok "unverified restart: root restored and re-restarted on old code" \
    '[ "$(snapshot)" = "$BEFORE" ] && [ "$(restart_calls)" = 2 ] && [ "$(grep "worker.js at restart" "$TMP/harness.calls" | tail -n1)" = "worker.js at restart: old worker" ]'

# 5d. worker became busy between gate and restart -> rollback, never signaled.
fresh; printf '10\n' > "$TMP/restart.rcs"
out=$(deploy --artifact "$ART" --apply); rc=$?
ok "busy at restart: exit 10, rolled back, single (refused) restart call" \
    '[ "$rc" = 10 ] && grep -q "became busy" <<<"$out" && [ "$(snapshot)" = "$BEFORE" ] && [ "$(restart_calls)" = 1 ]'

# ---- 6. concurrent deploys are refused -----------------------------------------
fresh; mkdir -p "$BK"
exec 8>"$BK/.deploy.lock"; flock -n 8
out=$(deploy --artifact "$ART" --apply)
# shellcheck disable=SC2034  # rc is read by the eval-based ok() assertion.
rc=$?
ok "a held deploy lock: exit 4, nothing changed" '[ "$rc" = 4 ] && [ "$(snapshot)" = "$BEFORE" ] && [ "$(restart_calls)" = 0 ]'
exec 8>&-

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
