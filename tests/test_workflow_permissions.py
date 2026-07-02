"""Guard GitHub workflow permissions against broad writable scopes."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


def _read_workflow(name: str) -> str:
    return (WORKFLOW_DIR / name).read_text(encoding="utf-8")


def test_codeql_security_events_write_is_scoped_to_analyze_job():
    """CodeQL upload needs security-events: write, but not as top-level permission."""
    workflow = _read_workflow("codeql.yml")
    top_level_permissions = workflow.split("jobs:", 1)[0]
    jobs_section = workflow.split("jobs:", 1)[1]

    assert "security-events: write" not in top_level_permissions
    assert "  analyze:\n    permissions:\n      contents: read\n      security-events: write" in jobs_section


def test_release_contents_write_stays_job_scoped_for_tag_releases():
    """Release creation still needs contents: write, but only in the release job."""
    workflow = _read_workflow("release.yml")
    top_level_permissions = workflow.split("jobs:", 1)[0]
    jobs_section = workflow.split("jobs:", 1)[1]

    assert "contents: write" not in top_level_permissions
    assert "  github-release:\n    permissions:\n      contents: write" in jobs_section
