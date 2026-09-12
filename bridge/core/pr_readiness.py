"""Pre-merge pull-request readiness behind the ``pr_readiness`` tool/CLI
(#1694 item 3).

One read-only call aggregates, for one PR of one repository via the node's
authenticated ``gh``, what a merge decision scans for:

- ``pull_request`` — head sha/branch, base, state/draft, mergeability
  (``mergeable``/``mergeStateStatus``), review decision, review requests,
- ``ci`` — the status check rollup at the exact head, counted with the same
  rule the gh-pr-flow relay approval gate uses (SUCCESS/NEUTRAL/SKIPPED are
  acceptable; not COMPLETED, or any other conclusion, is bad; zero checks is
  its own verdict),
- ``reviews`` — the latest review per reviewer together with the commit it
  was submitted on: approvals (non-author, head-matched), changes requested,
  dismissed/commented, stale or commit-unavailable flags,
- ``threads`` — unresolved review threads (GraphQL ``reviewThreads``), with
  outdated flagged and sampling noted.

stdlib-only. Every section carries ``status: ok|unknown`` and its observation
latency; a failed or unparseable ``gh`` call becomes section-scoped
``unknown`` — never a fabricated value. The aggregate is a lookup snapshot
(``readiness``) that informs operators; it never substitutes for approvals,
merge-time re-verification, or the gh-pr-flow gate itself.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import socket
import subprocess
from typing import Any, Callable, Mapping

PR_TIMEOUT = 30.0
THREADS_TIMEOUT = 30.0
REPO_PATTERN = re.compile(r"^[\w.\-]+/[\w.\-]+$")
_OK_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_FAILED_CONCLUSIONS = {
    "FAILURE",
    "TIMED_OUT",
    "STARTUP_FAILURE",
    "CANCELLED",
    "ACTION_REQUIRED",
    "STALE",
    "EXPECTED",
}
_THREAD_SAMPLE = 100

RUNNER = Callable[[list[str], float], "subprocess.CompletedProcess[bytes]"]


class PrReadinessError(ValueError):
    """A structured pr-readiness failure (CLI/ToolError boundary)."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": str(self), **self.details}}


def _gh_prefix(env: Mapping[str, str] | None) -> list[str]:
    environment = os.environ if env is None else env
    override = str(environment.get("CCC_PR_READINESS_GH", "")).strip()
    if override:
        return override.split()
    return ["gh"]


def _now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_runner(cmd: list[str], timeout: float) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def _run_gh(
    cmd: list[str],
    timeout: float,
    runner: RUNNER | None,
) -> tuple[dict[str, Any] | None, int, dict[str, Any] | None]:
    """Run one gh call; return (document, latency_ms, error)."""

    started = _dt.datetime.now(_dt.timezone.utc)
    try:
        completed = runner(cmd, timeout) if runner is not None else _default_runner(cmd, timeout)
    except subprocess.TimeoutExpired:
        return None, 0, {"error": "timeout"}
    except OSError as error:
        return None, 0, {"error": f"unavailable: {error}"}
    latency_ms = int((_dt.datetime.now(_dt.timezone.utc) - started).total_seconds() * 1000)
    if completed.returncode != 0:
        return None, latency_ms, {
            "error": f"exit {completed.returncode}",
            "latency_ms": latency_ms,
            "stderr_tail": completed.stderr.decode("utf-8", "replace")[-300:],
        }
    try:
        document = json.loads(completed.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None, latency_ms, {"error": "unparseable gh output", "latency_ms": latency_ms}
    return document, latency_ms, None


_PR_VIEW_FIELDS = (
    "number,title,author,baseRefName,state,isDraft,headRefName,headRefOid,"
    "mergeable,mergeStateStatus,reviewDecision,reviewRequests,statusCheckRollup,"
    "reviews,url"
)


def _pull_request_section(
    env: Mapping[str, str] | None,
    runner: RUNNER | None,
    repo: str,
    pr: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    cmd = _gh_prefix(env) + ["pr", "view", str(pr), "--repo", repo, "--json", _PR_VIEW_FIELDS]
    document, latency_ms, failure = _run_gh(cmd, PR_TIMEOUT, runner)
    if failure is not None:
        return {"status": "unknown", **failure}, None
    if not isinstance(document, dict) or not isinstance(document.get("headRefOid"), str):
        return {"status": "unknown", "error": "pull request output shape invalid"}, None
    author = document.get("author")
    review_requests: list[str] = []
    for request in document.get("reviewRequests") or []:
        if not isinstance(request, dict):
            continue
        if isinstance(request.get("login"), str):
            review_requests.append(request["login"])
        elif isinstance(request.get("slug"), str):
            review_requests.append(f"team:{request['slug']}")
    section = {
        "status": "ok",
        "latency_ms": latency_ms,
        "number": document.get("number"),
        "title": document.get("title"),
        "url": document.get("url"),
        "author": author.get("login", "unknown") if isinstance(author, dict) else "unknown",
        "state": document.get("state"),
        "draft": bool(document.get("isDraft")),
        "head_branch": document.get("headRefName"),
        "head_sha": document.get("headRefOid"),
        "base_branch": document.get("baseRefName"),
        "mergeable": document.get("mergeable"),
        "merge_state": document.get("mergeStateStatus"),
        "review_decision": document.get("reviewDecision"),
        "review_requests": review_requests,
    }
    return section, document


def _check_is_good(entry: dict[str, Any]) -> tuple[bool, str]:
    """Classify one rollup entry with the gh-pr-flow relay gate's rule."""

    state = entry.get("state")
    if isinstance(state, str) and state != "":
        # CheckSuite-style entry: only the aggregate state exists.
        return state == "SUCCESS", "check_suite_state"
    status = entry.get("status")
    conclusion = entry.get("conclusion") or ""
    if status != "COMPLETED":
        return False, "pending"
    if conclusion in _OK_CONCLUSIONS:
        return True, conclusion.lower()
    if conclusion in _FAILED_CONCLUSIONS:
        return False, "failed"
    return False, "pending"


def _ci_section(document: dict[str, Any] | None) -> dict[str, Any]:
    """Derive the CI section from the rollup embedded in the PR document."""

    if document is None:
        return {"status": "unknown", "error": "pull request data unavailable"}
    rollup = document.get("statusCheckRollup")
    if not isinstance(rollup, list):
        return {"status": "unknown", "error": "statusCheckRollup shape invalid"}
    good = 0
    failed: list[str] = []
    pending: list[str] = []
    for entry in rollup:
        if not isinstance(entry, dict):
            failed.append("malformed-entry")
            continue
        is_good, bucket = _check_is_good(entry)
        if is_good:
            good += 1
            continue
        name = str(entry.get("name") or entry.get("context") or "unnamed-check")
        (failed if bucket == "failed" else pending).append(name)
    if not rollup:
        verdict = "no_checks"
    elif failed:
        verdict = "fail"
    elif pending:
        verdict = "pending"
    else:
        verdict = "ok"
    return {
        "status": "ok",
        "counting_rule": (
            "gh-pr-flow relay gate (SUCCESS/NEUTRAL/SKIPPED ok; not COMPLETED or any"
            " other conclusion bad; zero checks = no_checks)"
        ),
        "total": len(rollup),
        "good": good,
        "failed": failed,
        "pending": pending,
        "verdict": verdict,
    }


def _reviews_section(section: dict[str, Any], document: dict[str, Any] | None) -> dict[str, Any]:
    if document is None:
        return {"status": "unknown", "error": "pull request data unavailable"}
    author = section.get("author")
    head_sha = str(section.get("head_sha") or "").lower()
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for review in document.get("reviews") or []:
        if not isinstance(review, dict):
            continue
        review_author = review.get("author")
        login = review_author.get("login") if isinstance(review_author, dict) else None
        if not isinstance(login, str):
            continue
        if login not in latest:
            order.append(login)
            latest[login] = review
        elif str(review.get("submittedAt") or "") >= str(latest[login].get("submittedAt") or ""):
            latest[login] = review
    approvals: list[dict[str, Any]] = []
    changes_requested: list[dict[str, Any]] = []
    other_states: dict[str, int] = {}
    for login in order:
        review = latest[login]
        state = str(review.get("state") or "unknown")
        commit = review.get("commit")
        commit_oid = str(commit.get("oid")) if isinstance(commit, dict) and commit.get("oid") else None
        entry = {
            "login": login,
            "state": state,
            "submitted_at": review.get("submittedAt"),
            "review_commit": commit_oid,
            "matches_head": (commit_oid.lower() == head_sha) if commit_oid and head_sha else None,
        }
        if state == "APPROVED":
            approvals.append(entry)
        elif state == "CHANGES_REQUESTED":
            changes_requested.append(entry)
        else:
            other_states[state] = other_states.get(state, 0) + 1
    non_author_head_matched = [
        item["login"] for item in approvals if item["login"] != author and item["matches_head"] is True
    ]
    non_author_stale_or_unknown = [
        item["login"] for item in approvals if item["login"] != author and item["matches_head"] is not True
    ]
    return {
        "status": "ok",
        "review_decision": section.get("review_decision"),
        "approvals": approvals,
        "non_author_head_matched_approvals": non_author_head_matched,
        "non_author_stale_or_unknown_approvals": non_author_stale_or_unknown,
        "changes_requested": changes_requested,
        "other_latest_states": other_states,
        "review_requests": section.get("review_requests", []),
    }


_THREADS_QUERY = (
    "query($owner:String!,$name:String!,$num:Int!){repository(owner:$owner,name:$name)"
    "{pullRequest(number:$num){reviewThreads(first:100){totalCount nodes{isResolved isOutdated}}}}}"
)


def _threads_section(
    env: Mapping[str, str] | None,
    runner: RUNNER | None,
    repo: str,
    pr: int,
) -> dict[str, Any]:
    owner, _, name = repo.partition("/")
    cmd = _gh_prefix(env) + [
        "api",
        "graphql",
        "-f",
        f"query={_THREADS_QUERY}",
        "-f",
        f"owner={owner}",
        "-f",
        f"name={name}",
        "-F",
        f"num={pr}",
    ]
    document, latency_ms, failure = _run_gh(cmd, THREADS_TIMEOUT, runner)
    if failure is not None or not isinstance(document, dict):
        return {"status": "unknown", **(failure or {})}
    try:
        threads = document["data"]["repository"]["pullRequest"]["reviewThreads"]
        total = int(threads["totalCount"])
        nodes = list(threads["nodes"])
    except (KeyError, TypeError, ValueError):
        return {"status": "unknown", "error": "reviewThreads output shape invalid"}
    unresolved = [node for node in nodes if node.get("isResolved") is False]
    return {
        "status": "ok",
        "latency_ms": latency_ms,
        "total": total,
        "sampled": len(nodes),
        "partial": total > len(nodes),
        "unresolved": len(unresolved),
        "unresolved_outdated": sum(1 for node in unresolved if node.get("isOutdated") is True),
    }


def _pr_state_reasons(pr_section: dict[str, Any]) -> list[str]:
    if pr_section["status"] != "ok":
        return ["pull_request_unavailable"]
    reasons: list[str] = []
    if pr_section.get("state") != "OPEN":
        reasons.append("not_open")
    if pr_section.get("draft"):
        reasons.append("draft")
    mergeable = pr_section.get("mergeable")
    if mergeable is None:
        reasons.append("mergeable_computing")
    elif mergeable != "MERGEABLE":
        reasons.append("not_mergeable")
    merge_state = pr_section.get("merge_state")
    if merge_state not in (None, "CLEAN"):
        reasons.append(f"merge_state:{merge_state}")
    return reasons


def _review_reasons(
    pr_section: dict[str, Any],
    reviews_section: dict[str, Any],
) -> list[str]:
    if reviews_section["status"] != "ok":
        return ["reviews_unknown"]
    reasons: list[str] = []
    if pr_section["status"] == "ok" and pr_section.get("review_decision") != "APPROVED":
        reasons.append(f"review_decision:{pr_section.get('review_decision')}")
    if not reviews_section.get("non_author_head_matched_approvals"):
        reasons.append("no_non_author_head_matched_approval")
    if reviews_section.get("non_author_stale_or_unknown_approvals"):
        reasons.append("approval_stale_or_head_unknown")
    if reviews_section.get("changes_requested"):
        reasons.append("changes_requested_open")
    return reasons


def _readiness(
    pr_section: dict[str, Any],
    ci_section: dict[str, Any],
    reviews_section: dict[str, Any],
    threads_section: dict[str, Any],
) -> dict[str, Any]:
    reasons = _pr_state_reasons(pr_section)
    if ci_section["status"] != "ok":
        reasons.append("ci_unknown")
    elif ci_section.get("verdict") != "ok":
        reasons.append(f"ci:{ci_section.get('verdict')}")
    reasons.extend(_review_reasons(pr_section, reviews_section))
    if threads_section["status"] != "ok":
        reasons.append("threads_unknown")
    elif threads_section.get("unresolved", 0) > 0:
        reasons.append("unresolved_review_threads")
    return {
        "verdict": "blocked" if reasons else "likely_ready",
        "reasons": reasons,
        "informational_only": True,
    }


def collect(
    repo: str,
    pr: int,
    *,
    env: Mapping[str, str] | None = None,
    runner: RUNNER | None = None,
) -> dict[str, Any]:
    """Collect pre-merge readiness for one PR; failures become unknown."""

    try:
        from telegram_bot.core.skill_lookup import policy_denial
    except ImportError:  # pragma: no cover - worktree aliasing only
        from skill_lookup import policy_denial

    denial = policy_denial(env)
    if denial is not None:
        raise PrReadinessError("policy_denied", "pr readiness denied by node policy", reason=denial)
    if not isinstance(repo, str) or not REPO_PATTERN.match(repo.strip()):
        raise PrReadinessError("invalid_repo", "repo must be OWNER/REPO", repo=repo)
    repo = repo.strip()
    try:
        pr_number = int(pr)
    except (TypeError, ValueError):
        raise PrReadinessError("invalid_pr", "pr must be an integer", pr=pr) from None
    if pr_number <= 0:
        raise PrReadinessError("invalid_pr", "pr must be a positive integer", pr=pr)

    pr_section, document = _pull_request_section(env, runner, repo, pr_number)
    ci_section = _ci_section(document)
    reviews_section = _reviews_section(pr_section, document)
    threads_section = _threads_section(env, runner, repo, pr_number)

    sections = {
        "pull_request": pr_section,
        "ci": ci_section,
        "reviews": reviews_section,
        "threads": threads_section,
    }
    partial = any(section["status"] != "ok" for section in sections.values())
    return {
        "observed_at": _now_utc(),
        "node": socket.gethostname(),
        "repo": repo,
        "pr": pr_number,
        "status": "unknown" if partial else "ok",
        "sections": sections,
        "readiness": _readiness(pr_section, ci_section, reviews_section, threads_section),
    }
