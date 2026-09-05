#!/usr/bin/env bash
# Canonical dependency-lock regeneration for ccc-node (issue #349).
#
# Regenerates BOTH hash locks from their single generation source
# (bridge/pyproject.toml) in one run so they can never drift apart:
#
#   1. .github/requirements/bridge-ci.txt   — CI toolchain + bridge dev set
#   2. bridge/requirements.lock.txt         — runtime set, constrained to (1)
#
# The runtime lock is compiled with the CI lock as a pip constraint, so every
# package the bridge installs at runtime is exactly the version CI tested.
# A consistency check at the end fails the script if any pin still differs;
# tests/test_runtime_deps_lock.py enforces the same invariant in CI.
#
# Platform / marker policy (Termux, Linux, macOS):
#   Locks are compiled on CPython 3.11 / Linux. All supported bridge platforms
#   (glibc Linux, macOS, Termux/Android) install from this single lock:
#   --generate-hashes records hashes for EVERY published artifact of a pinned
#   version (all wheels plus the sdist), so hosts that must build from source
#   (for example Termux) still verify against the same lock. A dependency that
#   is only needed on one platform must be declared in bridge/pyproject.toml
#   with an explicit environment marker and the locks recompiled here — never
#   hand-edited into a lock. The optional voice extra (requirements-voice.txt)
#   intentionally stays outside the lock: its native `tos`/`crcmod` build is
#   host-specific and opt-in.
#
# Refresh policy: run this script on a clean checkout, commit BOTH lock files
# (plus bridge/requirements.txt, which this script re-pins to the runtime lock,
# and any bridge-ci.in change) in ONE pull request, and let the full CI matrix
# (bridge-tests, python-lint, wheel-smoke, pip check, pip-audit) validate the
# refreshed resolution before merge. Routine bumps arrive the same way: the
# weekly deps-lock workflow (.github/workflows/deps-lock.yml, driven by
# scripts/ccc-deps-lock-pr.sh) runs this script with --upgrade and opens one
# bot PR per round. Dependabot pip version PRs are disabled because Dependabot
# cannot perform this derivation and moved one lock without the other (#1483).
#
# Targeted upgrades: by default pip-compile PRESERVES every pin that still
# satisfies the inputs, so a plain run only re-derives a consistent lock pair and
# never raises a version. To raise one, name it explicitly:
#
#   scripts/ccc-deps-lock.sh --upgrade-package mypy --upgrade-package librt
#
# Only the named packages may move; everything else stays pinned, keeping the
# diff reviewable. Name the transitive dependencies that gate the bump too — a
# package pinned at an older version silently caps its dependents (mypy 2.3.0
# needs librt>=0.13.0, so upgrading mypy alone stops at 2.2.0). A spec such as
# `--upgrade-package ruff==0.14.0` pins the named package to that exact
# version. `--upgrade` (no name) lets EVERY pin move to the newest version the
# inputs permit — the weekly bot round uses it; humans should prefer naming.
set -euo pipefail

usage() {
    cat >&2 <<'USAGE'
usage: scripts/ccc-deps-lock.sh [--upgrade] [--upgrade-package NAME[==VER]]...

Regenerates both hash locks from bridge/pyproject.toml. Without arguments every
existing pin that still satisfies the inputs is preserved. Each
--upgrade-package NAME allows that one package to move to the newest version the
inputs permit (NAME==VER pins it to that version); --upgrade lets every pin
move. bridge/requirements.txt is re-pinned to the regenerated runtime lock.
USAGE
}

UPGRADE_ARGS=()
UPGRADE_NAMES=()
UPGRADE_ALL=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --upgrade)
            UPGRADE_ARGS+=(--upgrade)
            UPGRADE_ALL=1
            shift
            ;;
        --upgrade-package)
            [ "$#" -ge 2 ] || { echo "❌ --upgrade-package requires a package name" >&2; exit 2; }
            UPGRADE_ARGS+=(--upgrade-package "$2")
            UPGRADE_NAMES+=("$2")
            shift 2
            ;;
        --upgrade-package=*)
            value="${1#*=}"
            [ -n "$value" ] || { echo "❌ --upgrade-package requires a package name" >&2; exit 2; }
            UPGRADE_ARGS+=(--upgrade-package "$value")
            UPGRADE_NAMES+=("$value")
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "❌ unknown argument: $1" >&2
            usage
            exit 2
            ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${CCC_DEPS_LOCK_PYTHON:-python3.11}"
PIP_TOOLS_SPEC="${CCC_DEPS_LOCK_PIP_TOOLS:-pip-tools==7.5.3}"

cd "$REPO_ROOT"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "❌ $PYTHON_BIN not found — locks must be compiled with CPython 3.11" >&2
    echo "   (override the interpreter with CCC_DEPS_LOCK_PYTHON if needed)" >&2
    exit 1
fi

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/ccc-deps-lock.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT

echo "== creating lock toolchain venv ($PYTHON_BIN, $PIP_TOOLS_SPEC) =="
"$PYTHON_BIN" -m venv "$WORKDIR/venv"
"$WORKDIR/venv/bin/pip" install -q "$PIP_TOOLS_SPEC"

if [ "$UPGRADE_ALL" -eq 1 ]; then
    echo "== --upgrade: every pin may move to the newest version the inputs permit =="
elif [ "${#UPGRADE_NAMES[@]}" -gt 0 ]; then
    echo "== targeted upgrades: ${UPGRADE_NAMES[*]} =="
else
    echo "== no targeted upgrades: every satisfiable pin is preserved =="
fi

echo "== compiling .github/requirements/bridge-ci.txt =="
"$WORKDIR/venv/bin/pip-compile" --quiet --allow-unsafe --extra=dev --generate-hashes \
    ${UPGRADE_ARGS[@]+"${UPGRADE_ARGS[@]}"} \
    --output-file=.github/requirements/bridge-ci.txt \
    .github/requirements/bridge-ci.in bridge/pyproject.toml

echo "== compiling bridge/requirements.lock.txt (constrained to the CI lock) =="
"$WORKDIR/venv/bin/pip-compile" --quiet --allow-unsafe --generate-hashes \
    ${UPGRADE_ARGS[@]+"${UPGRADE_ARGS[@]}"} \
    --constraint=.github/requirements/bridge-ci.txt \
    --output-file=bridge/requirements.lock.txt \
    bridge/pyproject.toml

echo "== verifying the runtime lock is a version-consistent subset of the CI lock =="
"$WORKDIR/venv/bin/python" - <<'PY'
import re
import sys
from pathlib import Path

pin = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_.,-]+\])?==([A-Za-z0-9_.!+-]+)", re.M)
runtime = dict(pin.findall(Path("bridge/requirements.lock.txt").read_text(encoding="utf-8")))
ci = dict(pin.findall(Path(".github/requirements/bridge-ci.txt").read_text(encoding="utf-8")))
drift = {name: (version, ci.get(name)) for name, version in runtime.items() if ci.get(name) != version}
if drift:
    print(f"❌ lock drift between runtime and CI locks: {drift}", file=sys.stderr)
    sys.exit(1)
print(f"✓ {len(runtime)} runtime pins all match the CI lock ({len(ci)} pins)")
PY

# bridge/requirements.txt is the CCC_DEPS_UNLOCKED=1 fallback and must mirror
# the runtime lock as EXACT pins (tests/test_runtime_deps_lock.py). Re-pin
# every entry it lists to the version the runtime lock just resolved; comments
# and ordering are preserved, and nothing is added or removed.
echo "== re-pinning bridge/requirements.txt to the runtime lock =="
"$WORKDIR/venv/bin/python" - <<'PY'
import re
from pathlib import Path

pin = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_.,-]+\])?==([A-Za-z0-9_.!+-]+)", re.M)
canon = lambda name: re.sub(r"[-_.]+", "-", name).lower()
lock = {canon(n): v for n, v in pin.findall(Path("bridge/requirements.lock.txt").read_text(encoding="utf-8"))}
path = Path("bridge/requirements.txt")
out, moved = [], []
for line in path.read_text(encoding="utf-8").splitlines():
    m = pin.match(line)
    if m and canon(m.group(1)) in lock and lock[canon(m.group(1))] != m.group(2):
        moved.append(f"{m.group(1)} {m.group(2)} -> {lock[canon(m.group(1))]}")
        line = f"{m.group(1)}=={lock[canon(m.group(1))]}"
    out.append(line)
if moved:
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("✓ re-pinned: " + ", ".join(moved))
else:
    print("✓ bridge/requirements.txt already matches the runtime lock")
PY

echo "✅ Locks regenerated. Commit both lock files, bridge/requirements.txt (and"
echo "   bridge-ci.in if changed) together in one PR and let CI validate the"
echo "   refreshed resolution."
