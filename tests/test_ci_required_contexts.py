"""Guard ccc-node CI check identity and governance policy against drift."""

from __future__ import annotations

import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / ".github" / "required-checks.json"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CODEQL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "codeql.yml"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"
POLICY = REPO_ROOT / "docs" / "ci-governance.md"

EXPECTED_CONTEXTS = [
    "validate-harness",
    "python-lint",
    "secret-scan",
    "bridge-tests (3.11)",
    "bridge-tests (3.12)",
    "wheel-smoke",
    "codeql-python",
]


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _job_block(workflow: str, job_id: str) -> str:
    marker = f"  {job_id}:\n"
    assert marker in workflow, f"missing workflow job {job_id}"
    tail = workflow.split(marker, 1)[1]
    next_job = re.search(r"(?m)^  [a-zA-Z0-9_-]+:\n", tail)
    return tail[: next_job.start()] if next_job else tail


def test_required_check_manifest_declares_exact_main_contexts():
    manifest = _manifest()

    assert manifest["branch"] == "main"
    assert manifest["strict"] is True
    assert manifest["protection_api"] == "legacy-branch-protection"
    assert manifest["app_id"] == 15368
    assert [check["context"] for check in manifest["checks"]] == EXPECTED_CONTEXTS


def test_required_contexts_match_explicit_workflow_job_names():
    manifest = _manifest()
    workflows = {
        "ci.yml": CI_WORKFLOW.read_text(encoding="utf-8"),
        "codeql.yml": CODEQL_WORKFLOW.read_text(encoding="utf-8"),
    }

    for check in manifest["checks"]:
        block = _job_block(workflows[check["workflow"]], check["job_id"])
        assert f'    name: {check["job_name"]}\n' in block


def test_codeql_required_context_is_not_matrix_suffixed():
    workflow = CODEQL_WORKFLOW.read_text(encoding="utf-8")
    block = _job_block(workflow, "analyze")

    assert "matrix:" not in block
    assert "languages: python" in block


def test_codeql_action_family_is_atomic_in_workflow_and_dependabot():
    workflow = CODEQL_WORKFLOW.read_text(encoding="utf-8")
    action_refs = re.findall(
        r"uses: github/codeql-action/(init|analyze)@([0-9a-f]{40})", workflow
    )

    assert [name for name, _ in action_refs] == ["init", "analyze"]
    assert len({sha for _, sha in action_refs}) == 1

    dependabot = DEPENDABOT.read_text(encoding="utf-8")
    assert "groups:\n      codeql-action-family:\n        patterns:\n          - github/codeql-action/*" in dependabot


def test_ci_policy_records_required_and_infrastructure_failure_boundaries():
    policy = " ".join(POLICY.read_text(encoding="utf-8").split())

    for phrase in [
        "legacy branch protection",
        "ruleset `18203378`",
        "CodeQL is required",
        "dismiss stale reviews",
        "conversation resolution remains disabled",
        "unassigned infrastructure failure",
        "do not merge",
        "rollback",
    ]:
        assert phrase in policy
