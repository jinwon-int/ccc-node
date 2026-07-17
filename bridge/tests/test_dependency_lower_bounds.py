"""Guard legacy requirements floors against known vulnerable ranges.

Scorecard GHSA findings pin SECURITY FLOORS for the CCC_DEPS_UNLOCKED fallback
list. The floor is the contract — not the literal ``>=`` spelling — so an exact
pin (``==``) also satisfies it as long as the pinned version is at or above the
floor (review finding on ccc-node#567: the fallback list moved to exact pins
mirroring requirements.lock.txt).
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

# package -> minimum version that fixes the known GHSA findings
SECURITY_FLOORS = {
    "pydantic": (2, 4, 0),
    "python-dotenv": (1, 2, 2),
}


def _requirement_versions() -> dict[str, tuple[str, tuple[int, ...]]]:
    requirements = REPO_ROOT / "bridge" / "requirements.txt"
    versions = {}
    for raw_line in requirements.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        for op in ("==", ">="):
            if op in line:
                name, version = line.split(op, 1)
                versions[name.strip().lower()] = (
                    op,
                    tuple(int(part) for part in version.strip().split(".")),
                )
                break
    return versions


def test_legacy_requirements_do_not_allow_known_vulnerable_lower_bounds():
    """Every floored package must resolve at or above its security floor.

    An exact pin resolves exactly its version; a ``>=`` bound may resolve the
    bound itself — either way, a version below the floor readmits the known
    vulnerable range, so the comparison is on the parsed version, not the
    operator spelling.
    """
    requirements = _requirement_versions()
    for name, floor in SECURITY_FLOORS.items():
        assert name in requirements, f"{name} missing from the fallback list"
        op, version = requirements[name]
        assert version >= floor, (
            f"{name} {op}{'.'.join(map(str, version))} is below the security "
            f"floor {'.'.join(map(str, floor))} (known GHSA range)"
        )
