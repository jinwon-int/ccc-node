"""Incident evidence search behind the ``incident_find`` tool/CLI (#1694
item 5, the roadmap's final item).

One read-only call combines the node's existing search sources for one
symptom/error query:

- ``wiki`` — ``wiki-agent find --json`` over the whole Family Wiki
  (incidents, logs, runbooks, decisions): top semantic sections plus text
  matches, with the abstention flag preserved. wiki-agent itself marks
  results as *candidates* — so does this tool.
- ``issues`` / ``pull_requests`` — authenticated ``gh search`` against one
  repository (optional): matched issues and PRs with title/url/state, so a
  "fixing PR" or a confirmed-cause thread is one lookup away.

This is distinct from ``family-skills``' ``skill_search``/``skill_read``
(skill catalog lookup): incident_find searches operational evidence, not the
skill catalog.

stdlib-only. Every section carries ``status: ok|unknown`` and its observation
latency; a failed search becomes section-scoped ``unknown`` — never a
fabricated value. Results are search candidates that inform operators; they
never substitute for reading the underlying evidence or for any approval.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import socket
import subprocess
from typing import Any, Callable, Mapping

WIKI_TIMEOUT = 60.0
GH_TIMEOUT = 30.0
MAX_QUERY_CHARS = 512
RESULT_CAP = 5
GH_RESULT_CAP = 10
SNIPPET_CHARS = 240
_REPO_PATTERN = re.compile(r"^[\w.\-]+/[\w.\-]+$")

RUNNER = Callable[[list[str], float], "subprocess.CompletedProcess[bytes]"]


class IncidentFindError(ValueError):
    """A structured incident-find failure (CLI/ToolError boundary)."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": str(self), **self.details}}


def _cmd_prefix(env: Mapping[str, str] | None, var: str, default: list[str]) -> list[str]:
    environment = os.environ if env is None else env
    override = str(environment.get(var, "")).strip()
    if override:
        return override.split()
    return list(default)


def _now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_runner(cmd: list[str], timeout: float) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def _run(
    cmd: list[str],
    timeout: float,
    runner: RUNNER | None,
) -> tuple["subprocess.CompletedProcess[bytes] | None", int, dict[str, Any] | None]:
    started = _dt.datetime.now(_dt.timezone.utc)
    try:
        completed = runner(cmd, timeout) if runner is not None else _default_runner(cmd, timeout)
    except subprocess.TimeoutExpired:
        return None, 0, {"error": "timeout"}
    except OSError as error:
        return None, 0, {"error": f"unavailable: {error}"}
    latency_ms = int((_dt.datetime.now(_dt.timezone.utc) - started).total_seconds() * 1000)
    if completed.returncode != 0:
        return completed, latency_ms, {
            "error": f"exit {completed.returncode}",
            "latency_ms": latency_ms,
            "stderr_tail": completed.stderr.decode("utf-8", "replace")[-300:],
        }
    return completed, latency_ms, None


def _wiki_section(
    env: Mapping[str, str] | None,
    runner: RUNNER | None,
    query: str,
) -> dict[str, Any]:
    cmd = _cmd_prefix(env, "CCC_INCIDENT_FIND_WIKI", ["wiki-agent", "find", "--json", "--top", str(RESULT_CAP)])
    cmd.append(query)
    completed, latency_ms, failure = _run(cmd, WIKI_TIMEOUT, runner)
    if failure is not None or completed is None:
        return {"status": "unknown", **(failure or {})}
    try:
        document = json.loads(completed.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {"status": "unknown", "error": "unparseable wiki output", "latency_ms": latency_ms}
    semantic = document.get("semantic") if isinstance(document, dict) else None
    results = semantic.get("results") if isinstance(semantic, dict) else None
    sections: list[dict[str, Any]] = []
    if isinstance(results, list):
        for result in results[:RESULT_CAP]:
            if not isinstance(result, dict):
                continue
            sections.append(
                {
                    "path": result.get("path"),
                    "heading": result.get("heading"),
                    "snippet": str(result.get("snippet") or "")[:SNIPPET_CHARS],
                    "score": result.get("score"),
                    "load_command": result.get("loadCommand"),
                }
            )
    text_matches: list[dict[str, Any]] = []
    raw_matches = document.get("textMatches") if isinstance(document, dict) else None
    if isinstance(raw_matches, list):
        for match in raw_matches[:RESULT_CAP]:
            if not isinstance(match, dict):
                continue
            text_matches.append(
                {
                    "path": match.get("path"),
                    "line": match.get("line"),
                    "text": str(match.get("text") or "")[:SNIPPET_CHARS],
                    "load_command": match.get("loadCommand"),
                }
            )
    return {
        "status": "ok",
        "latency_ms": latency_ms,
        "abstained": bool(document.get("abstained")) if isinstance(document, dict) else None,
        "confidence": document.get("confidence") if isinstance(document, dict) else None,
        "results": sections,
        "text_matches": text_matches,
        "candidates_note": "wiki-agent results are candidates — verify with the load commands before operational claims",
    }


def _gh_section(
    env: Mapping[str, str] | None,
    runner: RUNNER | None,
    kind: str,
    query: str,
    repo: str,
) -> dict[str, Any]:
    cmd = _cmd_prefix(env, "CCC_INCIDENT_FIND_GH", ["gh"])
    cmd += ["search", kind, query, "--limit", str(GH_RESULT_CAP), "--json", "title,url,state"]
    cmd += ["--repo", repo]
    completed, latency_ms, failure = _run(cmd, GH_TIMEOUT, runner)
    if failure is not None or completed is None:
        return {"status": "unknown", **(failure or {})}
    try:
        items = json.loads(completed.stdout.decode("utf-8", "replace"))
        if not isinstance(items, list):
            raise TypeError("search output not a list")
    except json.JSONDecodeError:
        return {"status": "unknown", "error": "unparseable gh output", "latency_ms": latency_ms}
    except TypeError:
        return {"status": "unknown", "error": "gh search output shape invalid", "latency_ms": latency_ms}
    entries = [
        {"title": item.get("title"), "url": item.get("url"), "state": item.get("state")}
        for item in items[:GH_RESULT_CAP]
        if isinstance(item, dict)
    ]
    return {"status": "ok", "latency_ms": latency_ms, "count": len(entries), "results": entries}


def collect(
    query: str,
    repo: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    runner: RUNNER | None = None,
) -> dict[str, Any]:
    """Combine wiki and (optional) GitHub searches for one symptom query."""

    try:
        from telegram_bot.core.skill_lookup import policy_denial
    except ImportError:  # pragma: no cover - worktree aliasing only
        from skill_lookup import policy_denial

    denial = policy_denial(env)
    if denial is not None:
        raise IncidentFindError("policy_denied", "incident find denied by node policy", reason=denial)
    if not isinstance(query, str) or not query.strip():
        raise IncidentFindError("invalid_query", "query must be a non-empty string", query=query)
    query = query.strip()[:MAX_QUERY_CHARS]
    repo_clean: str | None = None
    if repo is not None:
        if not isinstance(repo, str) or not _REPO_PATTERN.match(repo.strip()):
            raise IncidentFindError("invalid_repo", "repo must be OWNER/REPO", repo=repo)
        repo_clean = repo.strip()

    wiki = _wiki_section(env, runner, query)
    if repo_clean is None:
        issues: dict[str, Any] = {"status": "ok", "skipped": True, "reason": "no repo given"}
        pull_requests: dict[str, Any] = dict(issues)
    else:
        issues = _gh_section(env, runner, "issues", query, repo_clean)
        pull_requests = _gh_section(env, runner, "prs", query, repo_clean)

    sections = {"wiki": wiki, "issues": issues, "pull_requests": pull_requests}
    partial = any(section["status"] != "ok" for section in sections.values())
    return {
        "observed_at": _now_utc(),
        "node": socket.gethostname(),
        "query": query,
        "repo": repo_clean,
        "status": "unknown" if partial else "ok",
        "sections": sections,
        "results_are_candidates_only": True,
    }
