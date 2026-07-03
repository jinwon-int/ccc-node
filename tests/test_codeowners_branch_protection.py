"""Repository ownership and branch-protection readiness checks."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CODEOWNERS = REPO_ROOT / ".github" / "CODEOWNERS"
RUNBOOK = REPO_ROOT / "docs" / "branch-protection.md"


def test_codeowners_covers_the_whole_repository_with_both_maintainers():
    codeowners = CODEOWNERS.read_text(encoding="utf-8")
    rules = [
        line.split("#", 1)[0].strip()
        for line in codeowners.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert "* @jinon86 @seoseo-ai" in rules


def test_branch_protection_runbook_preserves_operator_approval_boundary():
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert "repository visibility, ownership, branch-protection, or ruleset changes" in runbook
    assert "explicit operator approval" in runbook
    assert "required_approving_review_count" in runbook
    assert "Do not raise" in runbook
