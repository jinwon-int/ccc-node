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
# (plus any bridge-ci.in change) in ONE pull request, and let the full CI
# matrix (bridge-tests, python-lint, wheel-smoke, pip check, pip-audit)
# validate the refreshed resolution before merge. Dependabot bumps to
# bridge/requirements.txt lower bounds follow the same rule: regenerate here
# and ship one verified PR unit.
set -euo pipefail

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

echo "== compiling .github/requirements/bridge-ci.txt =="
"$WORKDIR/venv/bin/pip-compile" --quiet --allow-unsafe --extra=dev --generate-hashes \
    --output-file=.github/requirements/bridge-ci.txt \
    .github/requirements/bridge-ci.in bridge/pyproject.toml

echo "== compiling bridge/requirements.lock.txt (constrained to the CI lock) =="
"$WORKDIR/venv/bin/pip-compile" --quiet --allow-unsafe --generate-hashes \
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

echo "✅ Locks regenerated. Commit both lock files (and bridge-ci.in if changed)"
echo "   together in one PR and let CI validate the refreshed resolution."
