"""Guard the required owner-operator production contract CI gate."""

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
OWNER_CONTRACT_COMMAND = "python -m pytest -q tests/test_owner_operator_contract.py"
CI_HARDENING_COMMAND = (
    "python -m pytest -q tests/test_ci_pip_hash_pinning.py "
    "tests/test_workflow_permissions.py tests/test_codeowners_branch_protection.py "
    "tests/test_ci_required_contexts.py tests/test_owner_operator_ci_gate.py "
    "tests/test_runtime_deps_lock.py tests/test_coverage_floor_gate.py"
)


def _job_block(workflow: str, job_id: str) -> str:
    marker = f"  {job_id}:\n"
    assert marker in workflow, f"missing workflow job {job_id}"
    tail = workflow.split(marker, 1)[1]
    next_job = re.search(r"(?m)^  [a-zA-Z0-9_-]+:\n", tail)
    return tail[: next_job.start()] if next_job else tail


def _step_block(job_block: str, step_name: str) -> str:
    marker = f"      - name: {step_name}\n"
    assert marker in job_block, f"missing workflow step {step_name}"
    tail = job_block.split(marker, 1)[1]
    next_step = re.search(r"(?m)^      - (?:name:|uses:)", tail)
    return tail[: next_step.start()] if next_step else tail


def _block_scalar_commands(step_block: str) -> list[str]:
    marker = "        run: |\n"
    assert marker in step_block, "workflow step must use a block run command"
    body = step_block.split(marker, 1)[1]
    commands = []
    for line in body.splitlines():
        if not line.strip():
            continue
        assert line.startswith("          "), "unexpected run-command indentation"
        commands.append(line[10:])
    return commands


def _assert_owner_contract_step(workflow: str) -> None:
    job = _job_block(workflow, "python-lint")
    step = _step_block(job, "Owner-operator production contract")
    lines = {line.strip() for line in step.splitlines()}

    assert "working-directory: bridge" in lines
    assert "PYTHONPATH: ${{ github.workspace }}/.github/pythonpath" in lines
    assert _block_scalar_commands(step) == [
        'mkdir -p "$TMPDIR"',
        OWNER_CONTRACT_COMMAND,
    ]


def _assert_ci_hardening_step(workflow: str) -> None:
    job = _job_block(workflow, "python-lint")
    step = _step_block(job, "CI hardening regressions")
    run_lines = [
        line.strip() for line in step.splitlines() if line.strip().startswith("run:")
    ]

    assert run_lines == [f"run: {CI_HARDENING_COMMAND}"]


def test_python_lint_job_runs_owner_operator_production_contract():
    _assert_owner_contract_step(CI_WORKFLOW.read_text(encoding="utf-8"))


def test_ci_hardening_suite_guards_owner_operator_gate():
    _assert_ci_hardening_step(CI_WORKFLOW.read_text(encoding="utf-8"))


@pytest.mark.parametrize("prefix", ["echo ", "# "])
def test_owner_contract_oracle_rejects_nonexecuting_command(prefix: str):
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    mutated = workflow.replace(
        f"          {OWNER_CONTRACT_COMMAND}",
        f"          {prefix}{OWNER_CONTRACT_COMMAND}",
        1,
    )
    assert mutated != workflow

    with pytest.raises(AssertionError):
        _assert_owner_contract_step(mutated)


def test_ci_hardening_oracle_rejects_echoed_command():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    mutated = workflow.replace(
        f"        run: {CI_HARDENING_COMMAND}",
        f"        run: echo {CI_HARDENING_COMMAND}",
        1,
    )
    assert mutated != workflow

    with pytest.raises(AssertionError):
        _assert_ci_hardening_step(mutated)
