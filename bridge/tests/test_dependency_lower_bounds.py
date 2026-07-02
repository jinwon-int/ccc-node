"""Guard legacy requirements lower bounds against known vulnerable ranges."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _requirement_lines() -> dict[str, str]:
    requirements = REPO_ROOT / "bridge" / "requirements.txt"
    lines = {}
    for raw_line in requirements.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split(">=", 1)[0].split("==", 1)[0].strip().lower()
        lines[name] = line
    return lines


def test_legacy_requirements_do_not_allow_known_vulnerable_lower_bounds():
    """Scorecard GHSA findings must stay fixed in requirements.txt too."""
    requirements = _requirement_lines()
    assert requirements["pydantic"] == "pydantic>=2.4.0"
    assert requirements["python-dotenv"] == "python-dotenv>=1.2.2"
