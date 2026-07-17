"""Runtime dependency hash-lock contract (issue #349).

Same-SHA clean installs must resolve identical versions/hashes, the runtime
install must reject unhashed transitive dependencies, and the wheel smoke +
audit gates must stay wired in CI. The two locks share one generation source
(bridge/pyproject.toml) and are regenerated together by scripts/ccc-deps-lock.sh.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_LOCK = REPO_ROOT / "bridge" / "requirements.lock.txt"
UNLOCKED_FALLBACK = REPO_ROOT / "bridge" / "requirements.txt"
CI_LOCK = REPO_ROOT / ".github" / "requirements" / "bridge-ci.txt"
PYPROJECT = REPO_ROOT / "bridge" / "pyproject.toml"
START_SH = REPO_ROOT / "bridge" / "start.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
LOCK_SCRIPT = REPO_ROOT / "scripts" / "ccc-deps-lock.sh"

PIN = re.compile(
    r"^([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_.,-]+\])?==([A-Za-z0-9_.!+-]+)", re.M
)


def _pins(path: Path) -> dict[str, str]:
    return dict(PIN.findall(path.read_text(encoding="utf-8")))


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_runtime_lock_pins_every_requirement_with_hashes():
    text = RUNTIME_LOCK.read_text(encoding="utf-8")
    assert "--hash=sha256:" in text

    requirement_lines = [
        line
        for line in text.splitlines()
        if line
        and not line.startswith(("#", " ", "\t"))
        and not line.startswith("--")
    ]
    assert requirement_lines, "expected pinned requirements in the runtime lock"
    for line in requirement_lines:
        assert PIN.match(line), f"unpinned requirement line: {line!r}"
        # Hashes continue on following lines; every pin must open a
        # continuation so pip --require-hashes has something to verify.
        assert line.rstrip().endswith("\\"), f"pin without hash continuation: {line!r}"


def test_unlocked_fallback_pins_match_the_runtime_lock():
    """CCC_DEPS_UNLOCKED=1 installs bridge/requirements.txt directly; a lower
    bound there could pull a breaking major the locked flow never vetted. Every
    fallback entry must be an exact pin, at the same version the runtime lock
    resolved."""
    lock = {_canonical(name): ver for name, ver in _pins(RUNTIME_LOCK).items()}
    lines = [
        line.strip()
        for line in UNLOCKED_FALLBACK.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines, "expected requirements in the unlocked fallback list"
    for line in lines:
        match = PIN.match(line)
        assert match, f"unlocked fallback requirement is not exactly pinned: {line!r}"
        name, version = match.group(1), match.group(2)
        locked = lock.get(_canonical(name))
        assert locked == version, (
            f"unlocked fallback pin {name}=={version} diverges from "
            f"requirements.lock.txt ({locked}); regenerate them together"
        )


def test_runtime_lock_headers_record_canonical_generation_command():
    header = RUNTIME_LOCK.read_text(encoding="utf-8").split("annotated-types", 1)[0]
    assert "pip-compile" in header
    assert "--generate-hashes" in header
    assert "--allow-unsafe" in header
    assert "--constraint=.github/requirements/bridge-ci.txt" in header
    assert "bridge/pyproject.toml" in header


def test_runtime_lock_is_version_consistent_subset_of_ci_lock():
    runtime = _pins(RUNTIME_LOCK)
    ci = _pins(CI_LOCK)
    assert runtime, "runtime lock has no pins"
    drift = {
        name: (version, ci.get(name))
        for name, version in runtime.items()
        if ci.get(name) != version
    }
    assert not drift, f"runtime lock drifted from CI lock: {drift}"


def test_runtime_lock_covers_all_pyproject_runtime_dependencies():
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    locked = {_canonical(name) for name in _pins(RUNTIME_LOCK)}
    for spec in project["dependencies"]:
        name = re.split(r"[<>=!~\[; ]", spec, 1)[0]
        assert _canonical(name) in locked, f"pyproject dependency missing from lock: {spec}"


def test_runtime_lock_excludes_dev_and_ci_tooling():
    locked = {_canonical(name) for name in _pins(RUNTIME_LOCK)}
    for tool in ["pytest", "pytest-cov", "ruff", "mypy", "build", "pip-audit"]:
        assert tool not in locked, f"CI/dev tool leaked into the runtime lock: {tool}"


def test_start_sh_installs_hash_locked_by_default():
    script = START_SH.read_text(encoding="utf-8")
    assert 'LOCK_FILE="$SCRIPT_DIR/requirements.lock.txt"' in script
    assert '--require-hashes -r "$LOCK_FILE"' in script
    # The editable first-party install must not pull unhashed transitives.
    assert '--no-deps -e "$SCRIPT_DIR"' in script
    # The escape hatch exists, is explicit, and defaults to locked. It must
    # honor the project/global .env path, not just the process environment
    # (PR #431 review finding).
    assert "CCC_DEPS_UNLOCKED:-" in script
    assert 'read_env_with_fallback "CCC_DEPS_UNLOCKED"' in script


def test_start_sh_locked_path_has_no_unpinned_pip_upgrade():
    script = START_SH.read_text(encoding="utf-8")
    locked_branch = script.split('deps_install_mode)" = "locked"', 1)[1].split("else", 1)[0]
    assert "--upgrade pip" not in locked_branch


def test_start_sh_dependency_fingerprint_covers_lock_and_mode():
    script = START_SH.read_text(encoding="utf-8")
    fingerprint = script.split("get_requirements_hash()", 1)[1].split("}", 1)[0]
    assert '"$LOCK_FILE"' in fingerprint
    assert '"$PYPROJECT_FILE"' in fingerprint
    assert "deps_install_mode" in fingerprint


def test_ci_wheel_smoke_job_builds_installs_and_audits():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "  wheel-smoke:\n    name: wheel-smoke\n" in workflow

    job = workflow.split("  wheel-smoke:\n", 1)[1]
    # --no-isolation: the build backend comes hash-pinned from the CI lock
    # instead of an unhashed index fetch at build time.
    assert "python -m build --wheel --no-isolation" in job
    assert "python -m venv" in job
    assert "--require-hashes -r bridge/requirements.lock.txt" in job
    assert "--no-deps" in job and ".whl" in job
    assert "pip\" check" in job or "pip check" in job
    assert "pip-audit --require-hashes --disable-pip -r bridge/requirements.lock.txt" in job
    # The smoke must import the installed wheel, not the checkout.
    assert "site-packages" in job


def test_ci_lock_pins_wheel_smoke_toolchain():
    ci = _pins(CI_LOCK)
    for tool in ["build", "pip-audit", "pip", "setuptools"]:
        assert tool in ci, f"wheel-smoke toolchain missing from CI lock: {tool}"


def test_lock_regeneration_script_regenerates_both_locks_together():
    script = LOCK_SCRIPT.read_text(encoding="utf-8")
    assert "pip-compile" in script
    assert "--output-file=.github/requirements/bridge-ci.txt" in script
    assert "--output-file=bridge/requirements.lock.txt" in script
    assert "--constraint=.github/requirements/bridge-ci.txt" in script
    # Platform policy must stay documented next to the generation commands.
    for phrase in ["Termux", "macOS", "sdist", "environment marker"]:
        assert phrase in script, f"platform lock policy lost from script: {phrase}"
