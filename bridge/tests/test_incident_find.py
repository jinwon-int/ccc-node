"""incident_find collector tests (#1694 item 5): wiki candidates via
wiki-agent, optional GitHub issue/PR search, section-scoped unknowns, input
validation, and the node policy gate.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from telegram_bot.core.incident_find import IncidentFindError, collect

FLEET_ENV = {"CCC_NODE_ISOLATION_PROFILE": "fleet"}

WIKI_JSON = json.dumps(
    {
        "abstained": False,
        "confidence": 0.91,
        "query": "fence ownership conflict",
        "schema": "wiki-find-v2",
        "semantic": {
            "results": [
                {
                    "path": "pages/incidents/2026-09-11-broker-rollback.md",
                    "heading": "fence ownership_conflict 크래시롭",
                    "snippet": "브로커 재시작 중 fence 토큰 미해제로 ownership_conflict — " + "상세 " * 200,
                    "score": 0.87,
                    "loadCommand": "wiki-agent load pages/incidents/2026-09-11-broker-rollback.md --lines 40-67",
                },
                {"path": "pages/runbooks/broker-restart.md", "heading": "브로커 재시작", "snippet": "docker stop -t 30", "score": 0.71},
            ]
        },
        "textMatches": [
            {
                "path": "pages/log.md",
                "line": 1234,
                "text": "fence 초기화 절차: 브로커 정지 확인 → fence sqlite 백업 후 삭제",
                "loadCommand": "wiki-agent load pages/log.md --lines 1230-1240",
            }
        ],
    }
)
ISSUES_JSON = json.dumps(
    [
        {"title": "broker restart crash: ownership_conflict", "url": "https://github.com/jinwon-int/a2a-nexus/issues/2081", "state": "open"}
    ]
)
PRS_JSON = json.dumps(
    [
        {"title": "fix(broker): release fence token on SIGTERM", "url": "https://github.com/jinwon-int/a2a-nexus/pull/2089", "state": "merged"}
    ]
)


def _completed(cmd: list[str], stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=cmd, returncode=returncode, stdout=stdout.encode(), stderr=stderr.encode()
    )


class FakeSearch:
    def __init__(
        self,
        *,
        wiki_json: str = WIKI_JSON,
        issues_json: str = ISSUES_JSON,
        prs_json: str = PRS_JSON,
        wiki_fails: bool = False,
        issues_fails: bool = False,
    ) -> None:
        self.wiki_json = wiki_json
        self.issues_json = issues_json
        self.prs_json = prs_json
        self.wiki_fails = wiki_fails
        self.issues_fails = issues_fails
        self.commands: list[list[str]] = []

    def __call__(self, cmd: list[str], timeout: float):
        self.commands.append(list(cmd))
        joined = " ".join(cmd)
        if "find" in joined:
            if self.wiki_fails:
                return _completed(cmd, returncode=1, stderr="wiki agent boom")
            return _completed(cmd, self.wiki_json)
        if "issues" in joined:
            if self.issues_fails:
                return _completed(cmd, returncode=1, stderr="gh boom")
            return _completed(cmd, self.issues_json)
        if "prs" in joined:
            return _completed(cmd, self.prs_json)
        return _completed(cmd, returncode=1, stderr=f"unhandled: {joined}")


def test_collect_combines_wiki_and_github() -> None:
    search = FakeSearch()
    result = collect("fence ownership conflict", "jinwon-int/a2a-nexus", env=FLEET_ENV, runner=search)
    assert result["status"] == "ok"
    assert result["query"] == "fence ownership conflict"
    assert result["repo"] == "jinwon-int/a2a-nexus"
    assert result["results_are_candidates_only"] is True
    sections = result["sections"]
    wiki = sections["wiki"]
    assert wiki["status"] == "ok"
    assert wiki["abstained"] is False
    assert wiki["results"][0]["path"] == "pages/incidents/2026-09-11-broker-rollback.md"
    # snippet is bounded
    assert len(wiki["results"][0]["snippet"]) <= 240
    assert wiki["text_matches"][0]["line"] == 1234
    assert sections["issues"]["count"] == 1
    assert sections["issues"]["results"][0]["state"] == "open"
    assert sections["pull_requests"]["results"][0]["state"] == "merged"
    # both gh searches are scoped to the repo
    gh_cmds = [cmd for cmd in search.commands if "gh" in cmd]
    assert all("--repo" in cmd and "jinwon-int/a2a-nexus" in cmd for cmd in gh_cmds)


def test_repo_omitted_skips_github_sections(tmp_path=None) -> None:
    result = collect("fence ownership conflict", env=FLEET_ENV, runner=FakeSearch())
    assert result["repo"] is None
    sections = result["sections"]
    assert sections["issues"] == {"status": "ok", "skipped": True, "reason": "no repo given"}
    assert sections["pull_requests"]["skipped"] is True
    assert sections["wiki"]["status"] == "ok"
    assert result["status"] == "ok"


def test_wiki_failure_is_section_scoped(tmp_path=None) -> None:
    result = collect("fence conflict", "jinwon-int/a2a-nexus", env=FLEET_ENV, runner=FakeSearch(wiki_fails=True))
    assert result["sections"]["wiki"]["status"] == "unknown"
    assert result["sections"]["issues"]["status"] == "ok"
    assert result["status"] == "unknown"


def test_gh_failure_keeps_wiki(tmp_path=None) -> None:
    result = collect("fence conflict", "jinwon-int/a2a-nexus", env=FLEET_ENV, runner=FakeSearch(issues_fails=True))
    sections = result["sections"]
    assert sections["wiki"]["status"] == "ok"
    assert sections["issues"]["status"] == "unknown"
    assert sections["pull_requests"]["status"] == "ok"
    assert result["status"] == "unknown"


def test_wiki_abstention_is_preserved(tmp_path=None) -> None:
    abstained = json.dumps({"abstained": True, "confidence": 0.2, "semantic": {"results": []}, "textMatches": []})
    result = collect("obscure symptom", env=FLEET_ENV, runner=FakeSearch(wiki_json=abstained))
    assert result["sections"]["wiki"]["abstained"] is True
    assert result["sections"]["wiki"]["results"] == []


def test_command_env_overrides(tmp_path: Path) -> None:
    wiki_fake = tmp_path / "wiki.sh"
    wiki_fake.write_text(f"#!/bin/sh\necho '{WIKI_JSON}'\n", encoding="utf-8")
    gh_fake = tmp_path / "gh.sh"
    gh_fake.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        "  issues) echo '" + ISSUES_JSON + "';;\n"
        "  prs) echo '" + PRS_JSON + "';;\n"
        "  *) exit 1;;\n"
        "esac\n",
        encoding="utf-8",
    )
    wiki_fake.chmod(0o755)
    gh_fake.chmod(0o755)
    env = {
        **FLEET_ENV,
        "CCC_INCIDENT_FIND_WIKI": f"bash {wiki_fake}",
        "CCC_INCIDENT_FIND_GH": f"bash {gh_fake}",
    }
    result = collect("fence conflict", "jinwon-int/a2a-nexus", env=env)
    assert result["status"] == "ok"
    assert result["sections"]["pull_requests"]["count"] == 1


def test_input_validation() -> None:
    with pytest.raises(IncidentFindError) as exc:
        collect("   ", env=FLEET_ENV, runner=FakeSearch())
    assert exc.value.code == "invalid_query"
    with pytest.raises(IncidentFindError) as exc:
        collect("fence", "jinwon-int", env=FLEET_ENV, runner=FakeSearch())
    assert exc.value.code == "invalid_repo"
    with pytest.raises(IncidentFindError) as exc:
        collect("fence", 123, env=FLEET_ENV, runner=FakeSearch())
    assert exc.value.code == "invalid_repo"


def test_policy_denied() -> None:
    with pytest.raises(IncidentFindError) as exc:
        collect("fence", env={"CCC_NODE_ISOLATION_PROFILE": "external"})
    assert exc.value.code == "policy_denied"
    with pytest.raises(IncidentFindError) as exc:
        collect("fence", env={"CCC_MEMORY_AUDIENCE": "shared"})
    assert exc.value.code == "policy_denied"
