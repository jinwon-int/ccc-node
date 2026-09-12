"""pr_readiness collector tests (#1694 item 3): relay-gate CI counting,
head-matched non-author approvals, review-thread aggregation, partial
failures as section-scoped unknowns, and the node policy gate.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from telegram_bot.core.pr_readiness import PrReadinessError, collect

FLEET_ENV = {"CCC_NODE_ISOLATION_PROFILE": "fleet"}


def _completed(cmd: list[str], stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=cmd, returncode=returncode, stdout=stdout.encode(), stderr=stderr.encode()
    )


def _pr_view_payload(**overrides) -> dict:
    payload = {
        "number": 1701,
        "title": "feat: something",
        "url": "https://github.com/jinwon-int/ccc-node/pull/1701",
        "author": {"login": "seoseo-ai"},
        "baseRefName": "main",
        "state": "OPEN",
        "isDraft": False,
        "headRefName": "feat/x",
        "headRefOid": "aa11bb33" * 5,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
        "reviewRequests": [{"login": "jinon86"}],
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "python-lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "CodeQL", "status": "COMPLETED", "conclusion": "NEUTRAL"},
            {"__typename": "CheckRun", "name": "skipped-thing", "status": "COMPLETED", "conclusion": "SKIPPED"},
        ],
        "reviews": [
            {
                "author": {"login": "jinon86"},
                "state": "APPROVED",
                "submittedAt": "2026-09-12T03:44:55Z",
                "commit": {"oid": "aa11bb33" * 5},
            }
        ],
    }
    payload.update(overrides)
    return payload


def _threads_payload(*nodes, total: int | None = None) -> dict:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "totalCount": len(nodes) if total is None else total,
                        "nodes": list(nodes),
                    }
                }
            }
        }
    }


def _fake_runner(pr_payload: dict, threads_payload: dict | None = None, calls: list | None = None):
    def runner(cmd: list[str], timeout: float):
        if calls is not None:
            calls.append(list(cmd))
        if "graphql" in cmd:
            if threads_payload is None:
                return _completed(cmd, returncode=1, stderr="graphql boom")
            return _completed(cmd, json.dumps(threads_payload))
        return _completed(cmd, json.dumps(pr_payload))

    return runner


def test_collect_aggregates_ready_pr() -> None:
    calls: list = []
    runner = _fake_runner(_pr_view_payload(), _threads_payload({"isResolved": True, "isOutdated": False}), calls)
    result = collect("jinwon-int/ccc-node", 1701, env=FLEET_ENV, runner=runner)
    assert result["status"] == "ok"
    assert result["repo"] == "jinwon-int/ccc-node"
    assert result["observed_at"].endswith("Z")
    sections = result["sections"]
    assert sections["pull_request"]["head_sha"] == "aa11bb33" * 5
    assert sections["ci"]["verdict"] == "ok"
    assert sections["ci"]["good"] == 3
    assert sections["reviews"]["non_author_head_matched_approvals"] == ["jinon86"]
    assert sections["threads"]["unresolved"] == 0
    assert result["readiness"]["verdict"] == "likely_ready"
    assert result["readiness"]["reasons"] == []
    assert "graphql" in calls[1]
    # relay-gate field set is present so lookups match the gate's own method
    assert "statusCheckRollup" in calls[0][-1]


def test_ci_counting_matches_relay_gate_rule() -> None:
    rollup = [
        {"name": "ok-run", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "neutral-run", "status": "COMPLETED", "conclusion": "NEUTRAL"},
        {"name": "skipped-run", "status": "COMPLETED", "conclusion": "SKIPPED"},
        {"name": "failed-run", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"name": "timeout-run", "status": "COMPLETED", "conclusion": "TIMED_OUT"},
        {"name": "still-running", "status": "IN_PROGRESS", "conclusion": None},
        {"name": "suite-state", "state": "PENDING"},
        {"name": "suite-ok", "state": "SUCCESS"},
    ]
    runner = _fake_runner(_pr_view_payload(statusCheckRollup=rollup), _threads_payload())
    result = collect("jinwon-int/ccc-node", 1701, env=FLEET_ENV, runner=runner)
    ci = result["sections"]["ci"]
    assert ci["total"] == 8
    assert ci["good"] == 4
    assert sorted(ci["failed"]) == ["failed-run", "timeout-run"]
    assert sorted(ci["pending"]) == ["still-running", "suite-state"]
    assert ci["verdict"] == "fail"
    assert result["readiness"]["reasons"] >= ["ci:fail"] and "ci:fail" in result["readiness"]["reasons"]


def test_no_checks_verdict_and_mergeable_computing() -> None:
    runner = _fake_runner(
        _pr_view_payload(statusCheckRollup=[], mergeable=None, mergeStateStatus="UNKNOWN"),
        _threads_payload(),
    )
    result = collect("jinwon-int/ccc-node", 1701, env=FLEET_ENV, runner=runner)
    assert result["sections"]["ci"]["verdict"] == "no_checks"
    reasons = result["readiness"]["reasons"]
    assert "ci:no_checks" in reasons
    assert "mergeable_computing" in reasons


def test_approval_staleness_and_author_exclusion() -> None:
    head = "aa11bb33" * 5
    reviews = [
        # author self-approval on head: never counts as non-author
        {"author": {"login": "seoseo-ai"}, "state": "APPROVED", "submittedAt": "2026-09-12T00:00:00Z", "commit": {"oid": head}},
        # non-author approval on an older commit: stale
        {"author": {"login": "jinon86"}, "state": "APPROVED", "submittedAt": "2026-09-11T00:00:00Z", "commit": {"oid": "old" * 10 + "aaaa"}},
        # latest per reviewer wins: jinon86's second review is a stale approval
        {"author": {"login": "jinon86"}, "state": "APPROVED", "submittedAt": "2026-09-12T01:00:00Z", "commit": {"oid": "old2" * 10 + "bbbb"}},
        # commit info missing: matches_head unknown
        {"author": {"login": "relay-bot"}, "state": "COMMENTED", "submittedAt": "2026-09-12T02:00:00Z"},
    ]
    runner = _fake_runner(_pr_view_payload(reviews=reviews), _threads_payload())
    result = collect("jinwon-int/ccc-node", 1701, env=FLEET_ENV, runner=runner)
    reviews_section = result["sections"]["reviews"]
    assert reviews_section["approvals"][0]["login"] == "seoseo-ai"
    assert reviews_section["non_author_head_matched_approvals"] == []
    assert reviews_section["non_author_stale_or_unknown_approvals"] == ["jinon86"]
    assert "no_non_author_head_matched_approval" in result["readiness"]["reasons"]
    assert "approval_stale_or_head_unknown" in result["readiness"]["reasons"]


def test_review_requests_teams_and_changes_requested() -> None:
    reviews = [
        {"author": {"login": "jinon86"}, "state": "CHANGES_REQUESTED", "submittedAt": "2026-09-12T00:00:00Z", "commit": {"oid": "aa11bb33" * 5}}
    ]
    runner = _fake_runner(
        _pr_view_payload(
            reviewDecision="CHANGES_REQUESTED",
            reviewRequests=[{"login": "someone"}, {"slug": "core-team"}],
            reviews=reviews,
        ),
        _threads_payload(),
    )
    result = collect("jinwon-int/ccc-node", 1701, env=FLEET_ENV, runner=runner)
    reviews_section = result["sections"]["reviews"]
    assert reviews_section["review_requests"] == ["someone", "team:core-team"]
    assert reviews_section["changes_requested"][0]["login"] == "jinon86"
    assert "review_decision:CHANGES_REQUESTED" in result["readiness"]["reasons"]
    assert "changes_requested_open" in result["readiness"]["reasons"]


def test_threads_unresolved_and_partial_sampling() -> None:
    nodes = [
        {"isResolved": False, "isOutdated": True},
        {"isResolved": False, "isOutdated": False},
        {"isResolved": True, "isOutdated": False},
    ]
    runner = _fake_runner(_pr_view_payload(), _threads_payload(*nodes, total=150))
    result = collect("jinwon-int/ccc-node", 1701, env=FLEET_ENV, runner=runner)
    threads = result["sections"]["threads"]
    assert threads["total"] == 150
    assert threads["partial"] is True
    assert threads["unresolved"] == 2
    assert threads["unresolved_outdated"] == 1
    assert "unresolved_review_threads" in result["readiness"]["reasons"]


def test_threads_failure_is_section_scoped_unknown() -> None:
    runner = _fake_runner(_pr_view_payload(), None)
    result = collect("jinwon-int/ccc-node", 1701, env=FLEET_ENV, runner=runner)
    assert result["sections"]["threads"]["status"] == "unknown"
    assert result["sections"]["pull_request"]["status"] == "ok"
    assert result["sections"]["ci"]["verdict"] == "ok"
    assert result["status"] == "unknown"
    assert "threads_unknown" in result["readiness"]["reasons"]


def test_pr_view_failure_degrades_dependent_sections() -> None:
    def runner(cmd: list[str], timeout: float):
        if "graphql" not in cmd:
            return _completed(cmd, returncode=1, stderr="GraphQL: Could not resolve to a PullRequest")
        return _completed(cmd, json.dumps(_threads_payload()))

    result = collect("jinwon-int/ccc-node", 404, env=FLEET_ENV, runner=runner)
    sections = result["sections"]
    assert sections["pull_request"]["status"] == "unknown"
    assert "Could not resolve" in sections["pull_request"]["stderr_tail"]
    assert sections["ci"]["status"] == "unknown"
    assert sections["reviews"]["status"] == "unknown"
    assert sections["threads"]["status"] == "ok"
    assert result["readiness"]["reasons"][0] == "pull_request_unavailable"


def test_gh_command_env_override(tmp_path) -> None:
    script = tmp_path / "fake-gh.sh"
    script.write_text(
        "#!/bin/sh\n"
        'if printf "%s " "$@" | grep -q graphql; then\n'
        f"  echo '{json.dumps(_threads_payload())}'\n"
        "else\n"
        f"  echo '{json.dumps(_pr_view_payload())}'\n"
        "fi\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    env = {**FLEET_ENV, "CCC_PR_READINESS_GH": f"bash {script}"}
    result = collect("jinwon-int/ccc-node", 1701, env=env)
    assert result["status"] == "ok"
    assert result["readiness"]["verdict"] == "likely_ready"


def test_input_validation() -> None:
    with pytest.raises(PrReadinessError) as exc:
        collect("jinwon-int", 1, env=FLEET_ENV, runner=_fake_runner(_pr_view_payload()))
    assert exc.value.code == "invalid_repo"
    with pytest.raises(PrReadinessError) as exc:
        collect("jinwon-int/ccc-node", 0, env=FLEET_ENV, runner=_fake_runner(_pr_view_payload()))
    assert exc.value.code == "invalid_pr"
    with pytest.raises(PrReadinessError) as exc:
        collect("jinwon-int/ccc-node", "many", env=FLEET_ENV, runner=_fake_runner(_pr_view_payload()))
    assert exc.value.code == "invalid_pr"


def test_policy_denied() -> None:
    with pytest.raises(PrReadinessError) as exc:
        collect("jinwon-int/ccc-node", 1, env={"CCC_NODE_ISOLATION_PROFILE": "external"})
    assert exc.value.code == "policy_denied"
    with pytest.raises(PrReadinessError) as exc:
        collect("jinwon-int/ccc-node", 1, env={"CCC_MEMORY_AUDIENCE": "shared"})
    assert exc.value.code == "policy_denied"
