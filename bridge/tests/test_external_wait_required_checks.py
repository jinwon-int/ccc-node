"""#2507: a PR-checks wait must not report green before required checks register.

ccc-node#2506 (2026-10-24): both CodeQL checks finished at 06:33:44Z and the
main CI workflow registered at 06:33:50Z; a poll in that gap saw "all
registered checks SUCCESS" and woke the conversation with "CI green".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from telegram_bot.core import external_wait_monitor as M
from telegram_bot.core.external_wait import (
    ExternalWaitRegistry,
    default_registry_path,
)
from telegram_bot.core.external_wait_monitor import (
    ExternalWaitMonitor,
    GhCliTransport,
    PrState,
    TransportError,
    _normalize_rollup,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _check(name, conclusion="SUCCESS", status="COMPLETED"):
    return {"name": name, "status": status, "conclusion": conclusion}


CODEQL_ONLY = {"statusCheckRollup": [_check("CodeQL"), _check("codeql-python")]}
REQUIRED = frozenset({"validate-harness", "bridge-tests (3.12)"})


def test_required_context_not_yet_registered_is_pending() -> None:
    assert _normalize_rollup(CODEQL_ONLY) == "success"  # legacy behaviour without a set
    assert _normalize_rollup(CODEQL_ONLY, REQUIRED) == "pending"


def test_all_required_present_and_green_is_success() -> None:
    payload = {"statusCheckRollup": CODEQL_ONLY["statusCheckRollup"]
               + [_check("validate-harness"), _check("bridge-tests (3.12)")]}
    assert _normalize_rollup(payload, REQUIRED) == "success"


def test_required_present_but_running_is_pending_and_failure_still_wins() -> None:
    running = {"statusCheckRollup": [_check("validate-harness", conclusion="", status="IN_PROGRESS"),
                                     _check("bridge-tests (3.12)")]}
    assert _normalize_rollup(running, REQUIRED) == "pending"
    failed = {"statusCheckRollup": [_check("CodeQL", conclusion="FAILURE")]}
    assert _normalize_rollup(failed, REQUIRED) == "failure"


def test_legacy_status_context_state_is_read() -> None:
    pending = {"statusCheckRollup": [_check("CodeQL"), {"context": "legacy-ci", "state": "PENDING"}]}
    assert _normalize_rollup(pending) == "pending"
    errored = {"statusCheckRollup": [{"context": "legacy-ci", "state": "ERROR"}]}
    assert _normalize_rollup(errored) == "failure"
    ok = {"statusCheckRollup": [{"context": "legacy-ci", "state": "SUCCESS"}]}
    assert _normalize_rollup(ok, frozenset({"legacy-ci"})) == "success"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_100.0

    def __call__(self) -> float:
        return self.now


class _Seq:
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)

    async def fetch_pr_state(self, repo, pr_number):
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]


class _Rec:
    def __init__(self) -> None:
        self.notes, self.resumes = [], []

    async def notify(self, chat_id, text):
        self.notes.append(text)
        return True

    async def resume(self, record, prompt):
        self.resumes.append(prompt)
        return True


async def _session(user_id, chat_id):
    return "sess-1"


def _setup(tmp_path, outcomes):
    clock = _Clock()
    reg = ExternalWaitRegistry(default_registry_path(tmp_path), clock=clock)
    reg.register(repo="jinwon-int/ccc-node", pr_number=2506, head_sha="c114a7de", user_id=7, chat_id=70,
                 session_id="sess-1", summary="next", timeout_seconds=100_000, poll_interval_seconds=30, now=1_000.0)
    rec = _Rec()
    mon = ExternalWaitMonitor(reg, transport=_Seq(outcomes), notifier=rec.notify, resumer=rec.resume,
                              session_lookup=_session, clock=clock)
    return clock, rec, mon


@pytest.mark.anyio
async def test_unknown_required_set_needs_two_consecutive_greens(tmp_path: Path) -> None:
    green = PrState("c114a7de", "success", required_known=False)
    clock, rec, mon = _setup(tmp_path, [green, PrState("c114a7de", "pending", required_known=False), green, green])
    await mon._tick()               # green #1 — provisional only
    assert rec.notes == []
    clock.now += 400
    await mon._tick()               # pending resets the streak
    clock.now += 400
    await mon._tick()               # green #1 again
    assert rec.notes == []
    clock.now += 400
    await mon._tick()               # green #2 — settled
    assert len(rec.notes) == 1 and "CI green" in rec.notes[0]
    assert rec.resumes and rec.resumes[0].startswith("[external_event: github_pr_checks terminal=success")


@pytest.mark.anyio
async def test_known_required_set_finishes_on_first_green(tmp_path: Path) -> None:
    clock, rec, mon = _setup(tmp_path, [PrState("c114a7de", "success")])
    await mon._tick()
    assert len(rec.notes) == 1


@pytest.mark.anyio
async def test_required_contexts_union_cache_and_unreadable(monkeypatch) -> None:
    calls = []
    responses = {
        "repos/o/r/branches/main": {"protection": {"required_status_checks": {"contexts": ["validate-harness"]}}},
        "repos/o/r/rules/branches/main": [
            {"type": "pull_request"},
            {"type": "required_status_checks",
             "parameters": {"required_status_checks": [{"context": "bridge-tests (3.12)"}]}},
        ],
    }

    async def fake_gh(*args, timeout=60):
        calls.append(args[1])
        if args[1] not in responses:
            raise TransportError("gh-error")
        return json.dumps(responses[args[1]]).encode()

    monkeypatch.setattr(M, "_run_gh", fake_gh)
    now = [0.0]
    transport = GhCliTransport(clock=lambda: now[0])
    assert await transport.required_contexts("o/r", "main") == REQUIRED
    assert await transport.required_contexts("o/r", "main") == REQUIRED
    assert len(calls) == 2                      # cached
    now[0] += M.REQUIRED_CONTEXTS_TTL_SECONDS + 1
    await transport.required_contexts("o/r", "main")
    assert len(calls) == 4                      # TTL expired, re-read
    assert await transport.required_contexts("o/r", "other") is None   # both sources fail
