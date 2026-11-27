#!/usr/bin/env bash
# Canonical tar deploy of the Termux-native A2A worker artifact (#2791 stage 3).
#
# Owner decision 2026-11-27: restarting a redeployed dist/worker.js is the
# DEPLOY side's job (self-update only warns), and the deploy script lives here.
# This replaces the per-rollout ad-hoc scripts that each re-implemented the
# same idle gate / backup / restart / rollback steps.
#
# Usage:
#   a2a-termux-native-worker-deploy.sh --env-file <path> --artifact <dir|tar>
#       [--target-sha <40-hex>] [--backup-dir <dir>] [--smoke-cmd <cmd>]
#       [--no-guard] [--apply]
#
# Dry-run by default: validates the artifact, prints the plan and runs the idle
# gate (`restart-worker --if-idle --dry-run`) — nothing on the node changes.
# --apply performs, in order:
#   1. idle gate: `a2a-termux-native-worker.sh restart-worker --if-idle --dry-run`
#      (busy → exit 10, nothing changed)
#   2. backups of the worker root (without node_modules) and of every
#      node_modules package the artifact replaces; both archives are listed
#      before anything is touched
#   3. copy dist/ scripts/ handlers/ (overwrite) and replace each artifact
#      node_modules package
#   4. pre-restart checks: build revision == target, the artifact's own
#      scripts/worker-artifact-rollout-guard.mjs (when shipped, unless
#      --no-guard), then --smoke-cmd (run with A2A_WORKER_ROOT set). Any
#      failure → rollback, exit 20, no restart
#   5. `restart-worker --if-idle`: SIGTERM only worker.js; the supervise loop
#      relaunches it; the harness verifies "registered with" + health
#      sup_count=1 workers=1 tunnel=UP. Busy now → rollback, exit 10 (the
#      worker was never signaled). Not verified → rollback, restart again on
#      the old files, exit 30
#   6. write <worker_root>/.deploy-source-sha
#
# Exit codes: 0 ok (dry-run: plan ok), 2 usage / invalid artifact, 3 no
# supervisor or worker (nothing changed), 4 another deploy holds the lock,
# 10 busy (nothing changed — or files rolled back before any signal),
# 11 idle state unknown (nothing changed), 20 pre-restart check failed (rolled
# back, no restart), 30 restart not verified (rolled back + restarted),
# 31 rollback failed — MANUAL RESTORE from the printed backups.
#
# Env: A2A_DEPLOY_BACKUP_DIR (default $HOME/a2a-nexus-local-deploy-backups),
#      A2A_NATIVE_WORKER_HARNESS (default: the harness beside this script; the
#      running supervisor must be that same checkout's harness).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS="${A2A_NATIVE_WORKER_HARNESS:-$SCRIPT_DIR/a2a-termux-native-worker.sh}"
DEPLOY_DIRS=(dist scripts handlers)

ENV_FILE=""; ARTIFACT=""; TARGET_SHA=""; SMOKE_CMD=""; APPLY=0; GUARD=1
BACKUP_DIR="${A2A_DEPLOY_BACKUP_DIR:-$HOME/a2a-nexus-local-deploy-backups}"
STAGE_TMP=""; STAGE=""; ROOT=""

say() { printf 'deploy: %s\n' "$*"; }
die() { printf 'deploy: %s\n' "$2" >&2; exit "$1"; }

usage() {
    sed -n '2,/^set -uo/{/^set -uo/d;s/^# \{0,1\}//;p}' "${BASH_SOURCE[0]}" >&2
    exit 2
}

cleanup() { if [[ -n "$STAGE_TMP" ]]; then rm -rf "$STAGE_TMP"; fi; }
trap cleanup EXIT

# Same quoting rules as the Python validator.
extract_env_value() {
    python3 - "$1" "$2" <<'PY'
import shlex, sys
key, path = sys.argv[1], sys.argv[2]
try:
    for raw in open(path, encoding='utf-8'):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].strip()
        if '=' not in line:
            continue
        k, v = line.split('=', 1)
        if k.strip() != key:
            continue
        v = v.strip()
        if v.startswith(('"', "'")):
            try:
                parts = shlex.split(v, posix=True)
                if len(parts) == 1:
                    v = parts[0]
            except ValueError:
                pass
        print(v)
        break
except FileNotFoundError:
    pass
PY
}

build_rev() { # <root>: first 40-hex in dist/build-info.json, empty if none
    [[ -r "$1/dist/build-info.json" ]] || return 0
    grep -oE '[0-9a-f]{40}' "$1/dist/build-info.json" 2>/dev/null | head -n 1
}

# Extract a tar artifact into a private staging dir, refusing absolute paths,
# `..`, device nodes and links that escape the stage.
extract_tar() { # <tar> <dest>
    python3 - "$1" "$2" <<'PY'
import os
import sys
import tarfile

src, dest = sys.argv[1], os.path.realpath(sys.argv[2])


def inside(path):
    real = os.path.realpath(path)
    return real == dest or real.startswith(dest + os.sep)


try:
    with tarfile.open(src) as tf:
        for m in tf.getmembers():
            parts = m.name.split("/")
            if m.name.startswith("/") or ".." in parts:
                sys.exit("unsafe member path: %s" % m.name)
            if m.isdev() or m.isfifo():
                sys.exit("device/fifo member refused: %s" % m.name)
            if m.issym() or m.islnk():
                base = dest if m.islnk() else os.path.dirname(os.path.join(dest, m.name))
                if os.path.isabs(m.linkname) or not inside(os.path.join(base, m.linkname)):
                    sys.exit("link escapes the artifact: %s -> %s" % (m.name, m.linkname))
        try:
            tf.extractall(dest, filter="data")
        except TypeError:  # Python < 3.12 has no extraction filters
            tf.extractall(dest)
except (OSError, tarfile.TarError) as exc:
    sys.exit("unreadable artifact: %s" % exc)
PY
}

# Resolve the artifact to a directory whose dist/worker.js exists.
stage_artifact() {
    local src="$ARTIFACT" sub
    if [[ -f "$src" ]]; then
        STAGE_TMP=$(mktemp -d "${TMPDIR:-/tmp}/a2a-worker-deploy.XXXXXX") || die 2 "mktemp failed"
        extract_tar "$src" "$STAGE_TMP" || die 2 "artifact tar rejected: $src"
        src="$STAGE_TMP"
    elif [[ ! -d "$src" ]]; then
        die 2 "artifact not found: $src"
    fi
    if [[ -f "$src/dist/worker.js" ]]; then
        STAGE="$(cd "$src" && pwd)"
        return 0
    fi
    # Accept one wrapping directory (e.g. a tar of artifact/).
    local -a subs=()
    for sub in "$src"/*/; do
        [[ -f "$sub/dist/worker.js" ]] && subs+=("${sub%/}")
    done
    (( ${#subs[@]} == 1 )) || die 2 "artifact has no dist/worker.js (top level or one wrapping dir): $ARTIFACT"
    STAGE="$(cd "${subs[0]}" && pwd)"
}

# Top-level node_modules entries the artifact ships (scoped dirs as a unit;
# dot entries such as .bin are left alone, as the ad-hoc rollouts did).
artifact_packages() {
    [[ -d "$STAGE/node_modules" ]] || return 0
    local p
    for p in "$STAGE/node_modules"/*; do
        [[ -e "$p" ]] || continue
        printf '%s\n' "${p##*/}"
    done
}

# Run the gate/restart through the harness; echo its output, return its rc.
harness() { bash "$HARNESS" "$@" --env-file "$ENV_FILE"; }

ROOT_BACKUP=""; NM_BACKUP=""; NM_REPLACED=(); NM_NEW=(); ROOT_MEMBERS=()

make_backups() { # <tag>
    local parent base ts nm_existing=() p
    parent="$(dirname "$ROOT")"; base="$(basename "$ROOT")"
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$BACKUP_DIR" || return 1
    ROOT_BACKUP="$BACKUP_DIR/$base.pre-$1.$ts.tgz"
    NM_BACKUP="$BACKUP_DIR/$base-node_modules.pre-$1.$ts.tgz"
    tar czf "$ROOT_BACKUP" -C "$parent" --exclude="$base/node_modules" \
        --exclude="$base/dist.bak*" "$base" || return 1
    tar tzf "$ROOT_BACKUP" >/dev/null || return 1
    for p in "${NM_REPLACED[@]}"; do
        if [[ -e "$ROOT/node_modules/$p" ]]; then nm_existing+=("$p"); else NM_NEW+=("$p"); fi
    done
    if (( ${#nm_existing[@]} )); then
        tar czf "$NM_BACKUP" -C "$ROOT/node_modules" -- "${nm_existing[@]}" || return 1
        tar tzf "$NM_BACKUP" >/dev/null || return 1
    else
        NM_BACKUP=""
    fi
    # Which deploy dirs (and the sha marker) existed before: rollback restores
    # exactly these and removes what the artifact added.
    local d
    for d in "${DEPLOY_DIRS[@]}" .deploy-source-sha; do
        [[ -e "$ROOT/$d" ]] && ROOT_MEMBERS+=("$base/$d")
    done
    return 0
}

rollback() {
    local parent d p rc=0
    parent="$(dirname "$ROOT")"
    say "ROLLBACK from $ROOT_BACKUP${NM_BACKUP:+ + $NM_BACKUP}"
    for d in "${DEPLOY_DIRS[@]}" .deploy-source-sha; do rm -rf "${ROOT:?}/$d" || rc=1; done
    if (( ${#ROOT_MEMBERS[@]} )); then
        tar xzf "$ROOT_BACKUP" -C "$parent" -- "${ROOT_MEMBERS[@]}" || rc=1
    fi
    for p in "${NM_REPLACED[@]}"; do rm -rf "${ROOT:?}/node_modules/$p" || rc=1; done
    if [[ -n "$NM_BACKUP" ]]; then
        tar xzf "$NM_BACKUP" -C "$ROOT/node_modules" || rc=1
    fi
    return "$rc"
}

rollback_or_die() { # <exit_rc> <message>
    if ! rollback; then
        die 31 "ROLLBACK FAILED after: $2 — MANUAL RESTORE from $ROOT_BACKUP${NM_BACKUP:+ and $NM_BACKUP}"
    fi
    die "$1" "$2; rolled back"
}

copy_artifact() {
    local d p
    for d in "${DEPLOY_DIRS[@]}"; do
        [[ -d "$STAGE/$d" ]] || continue
        mkdir -p "$ROOT/$d" && cp -a "$STAGE/$d/." "$ROOT/$d/" || return 1
    done
    if (( ${#NM_REPLACED[@]} )); then
        mkdir -p "$ROOT/node_modules" || return 1
        for p in "${NM_REPLACED[@]}"; do
            rm -rf "${ROOT:?}/node_modules/$p" && cp -a "$STAGE/node_modules/$p" "$ROOT/node_modules/$p" || return 1
        done
    fi
}

run_guard() { # the artifact's own rollout guard, when it ships one
    local guard="$ROOT/scripts/worker-artifact-rollout-guard.mjs" out
    (( GUARD )) && [[ -f "$guard" ]] || return 0
    out=$(A2A_WORKER_ROOT="$ROOT" ${TARGET_SHA:+A2A_WORKER_TARGET_REVISION="$TARGET_SHA"} \
        node "$guard" --deployed 2>&1) || { say "guard rc!=0: ${out##*$'\n'}"; return 1; }
    python3 - "$out" <<'PY'
import json, sys
s = sys.argv[1]
try:
    d = json.loads(s[s.index("{"):])
except ValueError:
    sys.exit("guard output is not JSON")
sys.exit(0 if d.get("ok") is True else "guard ok=%s" % d.get("ok"))
PY
}

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --env-file|--artifact|--target-sha|--backup-dir|--smoke-cmd)
                [[ $# -ge 2 ]] || die 2 "$1 requires a value"
                case "$1" in
                    --env-file) ENV_FILE="$2" ;;
                    --artifact) ARTIFACT="$2" ;;
                    --target-sha) TARGET_SHA="$2" ;;
                    --backup-dir) BACKUP_DIR="$2" ;;
                    --smoke-cmd) SMOKE_CMD="$2" ;;
                esac
                shift 2 ;;
            --apply) APPLY=1; shift ;;
            --dry-run) APPLY=0; shift ;;
            --no-guard) GUARD=0; shift ;;
            -h|--help) usage ;;
            *) printf 'deploy: unknown arg: %s\n' "$1" >&2; usage ;;
        esac
    done
    [[ -n "$ENV_FILE" && -n "$ARTIFACT" ]] || { printf 'deploy: --env-file and --artifact are required\n' >&2; usage; }
    [[ -f "$ENV_FILE" ]] || die 2 "env file not found: $ENV_FILE"
    [[ -z "$TARGET_SHA" || "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || die 2 "--target-sha must be 40 lowercase hex"
    [[ -f "$HARNESS" ]] || die 2 "harness not found: $HARNESS"

    ROOT=$(extract_env_value A2A_WORKER_ROOT "$ENV_FILE")
    ROOT="${ROOT/#\~/$HOME}"
    [[ -n "$ROOT" && -d "$ROOT" ]] || die 2 "A2A_WORKER_ROOT missing or not a directory in $ENV_FILE"
    ROOT="$(cd "$ROOT" && pwd)"
    local script
    script=$(extract_env_value A2A_WORKER_SCRIPT "$ENV_FILE")
    if [[ -n "$script" && "$(realpath -m "${script/#\~/$HOME}")" != "$(realpath -m "$ROOT/dist/worker.js")" ]]; then
        die 2 "A2A_WORKER_SCRIPT is not <A2A_WORKER_ROOT>/dist/worker.js; this deploy only handles the default layout"
    fi

    stage_artifact
    local staged_rev current_rev source_sha
    staged_rev=$(build_rev "$STAGE"); current_rev=$(build_rev "$ROOT")
    if [[ -n "$TARGET_SHA" && -n "$staged_rev" && "$staged_rev" != "$TARGET_SHA" ]]; then
        die 2 "artifact revision $staged_rev != --target-sha $TARGET_SHA"
    fi
    source_sha="${TARGET_SHA:-$staged_rev}"
    [[ -n "$source_sha" ]] || die 2 "no source revision: artifact has no dist/build-info.json revision; pass --target-sha"
    mapfile -t NM_REPLACED < <(artifact_packages)
    local -a present=()
    local d
    for d in "${DEPLOY_DIRS[@]}"; do [[ -d "$STAGE/$d" ]] && present+=("$d/"); done

    say "plan mode=$([[ $APPLY = 1 ]] && echo apply || echo dry-run) root=$ROOT"
    say "plan rev ${current_rev:-unknown} -> $source_sha (artifact ${staged_rev:-no build-info})"
    say "plan copy ${present[*]} node_modules=[${NM_REPLACED[*]}] guard=$([[ $GUARD = 1 ]] && echo auto || echo off) smoke=$([[ -n $SMOKE_CMD ]] && echo yes || echo no)"
    say "plan backups -> $BACKUP_DIR; restart via $(basename "$HARNESS") restart-worker --if-idle"

    # 1. idle gate (nothing changes on refusal).
    local out rc
    out=$(harness restart-worker --if-idle --dry-run 2>&1); rc=$?
    printf '%s\n' "$out"
    case "$rc" in
        0) ;;
        10) die 10 "BUSY: worker is running a task — nothing changed, retry later" ;;
        11) die 11 "idle state unknown — nothing changed" ;;
        3) die 3 "no canonical supervisor / worker — nothing changed" ;;
        *) die 2 "idle gate failed rc=$rc — nothing changed" ;;
    esac
    if (( ! APPLY )); then
        say "DRY-RUN OK — nothing changed (re-run with --apply)"
        return 0
    fi

    mkdir -p "$BACKUP_DIR" || die 2 "cannot create backup dir $BACKUP_DIR"
    exec 9>"$BACKUP_DIR/.deploy.lock"
    flock -n 9 || die 4 "another deploy holds $BACKUP_DIR/.deploy.lock"

    # 2. backups.
    make_backups "${source_sha:0:12}" || die 20 "backup failed — nothing changed"
    say "backup $ROOT_BACKUP${NM_BACKUP:+ + $NM_BACKUP}"

    # 3. copy.
    copy_artifact || rollback_or_die 20 "artifact copy failed (no restart)"

    # 4. pre-restart checks.
    local new_rev
    new_rev=$(build_rev "$ROOT")
    if [[ -n "$staged_rev" && "$new_rev" != "$staged_rev" ]]; then
        rollback_or_die 20 "deployed revision ${new_rev:-none} != artifact $staged_rev (no restart)"
    fi
    run_guard || rollback_or_die 20 "rollout guard failed (no restart)"
    if [[ -n "$SMOKE_CMD" ]]; then
        A2A_WORKER_ROOT="$ROOT" bash -c "$SMOKE_CMD" || rollback_or_die 20 "smoke command failed (no restart)"
    fi

    # 5. restart worker.js only; the harness verifies registration + health.
    out=$(harness restart-worker --if-idle 2>&1); rc=$?
    printf '%s\n' "$out"
    case "$rc" in
        0) ;;
        10|11) rollback_or_die 10 "worker became busy before the restart (never signaled)" ;;
        3) rollback_or_die 30 "no worker to restart" ;;
        *)
            rollback || die 31 "ROLLBACK FAILED after unverified restart — MANUAL RESTORE from $ROOT_BACKUP${NM_BACKUP:+ and $NM_BACKUP}"
            local again arc
            again=$(harness restart-worker --if-idle 2>&1); arc=$?
            printf '%s\n' "$again"
            die 30 "restart not verified (rc=$rc); rolled back to ${current_rev:-previous}, restart on old files rc=$arc"
            ;;
    esac

    # 6. record what is now serving.
    printf '%s\n' "$source_sha" > "$ROOT/.deploy-source-sha.tmp" &&
        mv -f "$ROOT/.deploy-source-sha.tmp" "$ROOT/.deploy-source-sha" ||
        say "WARN could not write $ROOT/.deploy-source-sha"
    say "OK rev ${current_rev:-unknown} -> $source_sha | ${out##*$'\n'}"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
