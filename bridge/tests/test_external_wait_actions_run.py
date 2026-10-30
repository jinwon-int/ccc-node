"""GitHub Actions run watch for durable external waits (#2590).

A dispatched build or release upload used to be watched with an ad-hoc
polling loop that died with the turn, so nothing showed the run was still
being waited on and nothing resumed the conversation durably. These tests pin
the ``github_actions_run`` source: registration by run id, the live-status
mapping (an environment approval gate is *not* terminal), the terminal
mapping, the wake/resume path, and the display strings.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from telegram_bot.core import external_wait_cli
from telegram_bot.core.external_wait import (
    SOURCE_GITHUB_ACTIONS_RUN,
    STATE_MONITORING,
    TERMINAL_CANCELLED,
    TERMINAL_FAILURE,
    TERMINAL_MONITOR_ERROR,
    TERMINAL_SUCCESS,
    ExternalWaitRegistry,
    ExternalWaitValidationError,
    default_registry_path,
    publish_active_turn,
    render_waits,
    validate_run_id,
)
from telegram_bot.core.external_wait_monitor import (
    ActionsRunState,
    ExternalWaitMonitor,
    TransportError,
    _actions_run_state_from,
    _classify_gh_error,
    actions_run_terminal,
    resume_prompt_text,
    wake_notification_text,
)
from telegram_bot.core.external_wait_status import render_wait_status

RUN_ID = 41058887647
HEAD = "7ab76f87341058287fd111a0cf1bd9b0beca105b"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Clock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RunTransport:
    """Scripted Actions-run transport: each fetch pops one outcome (last repeats)."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, int]] = []

    async def fetch_pr_state(self, repo: str, pr_number: int):  # pragma: no cover - must not run
        raise AssertionError("Actions-run waits must not read a PR")

    async def fetch_actions_run(self, repo: str, run_id: int) -> ActionsRunState:
        self.calls.append((repo, run_id))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Recorder:
    def __init__(self) -> None:
        self.notifications: list[tuple[int, str]] = []
        self.resumes: list[tuple[dict, str]] = []
        self.status_syncs: list[tuple[int, int]] = []

    async def notify(self, chat_id: int, text: str) -> bool:
        self.notifications.append((chat_id, text))
        return True

    async def resume(self, record: dict, prompt: str) -> bool:
        self.resumes.append((record, prompt))
        return True

    async def sync(self, user_id: int, chat_id: int) -> None:
        self.status_syncs.append((user_id, chat_id))


async def _session_of(user_id: int, chat_id: int) -> str:
    return "sess-1"


def _run(status: str, conclusion: str = "") -> ActionsRunState:
    return ActionsRunState(status=status, conclusion=conclusion, head_sha=HEAD)


def _setup(tmp_path: Path, transport: RunTransport, clock: Clock, *, summary: str = "verify the bundle"):
    registry = ExternalWaitRegistry(default_registry_path(tmp_path), clock=clock)
    wait_id = registry.register(
        repo="jinwon-int/family-messenger",
        pr_number=None,
        head_sha=HEAD,
        user_id=7,
        chat_id=70,
        session_id="sess-1",
        summary=summary,
        timeout_seconds=100_000,
        poll_interval_seconds=30,
        now=clock(),
        source=SOURCE_GITHUB_ACTIONS_RUN,
        run_id=RUN_ID,
        run_label="Native MLS track — Android host",
        run_number=28,
    )
    recorder = Recorder()
    monitor = ExternalWaitMonitor(
        registry,
        transport=transport,
        notifier=recorder.notify,
        resumer=recorder.resume,
        session_lookup=_session_of,
        status_syncer=recorder.sync,
        clock=clock,
    )
    return registry, wait_id, recorder, monitor


async def _drive(monitor: ExternalWaitMonitor, clock: Clock, ticks: int, step: float = 130) -> None:
    for _ in range(ticks):
        await monitor._tick()
        clock.advance(step)


# --- registry ---------------------------------------------------------------------


def test_register_keys_by_run_id_and_is_idempotent(tmp_path: Path) -> None:
    clock = Clock()
    registry, wait_id, _, _ = _setup(tmp_path, RunTransport([_run("queued")]), clock)
    record = registry.get(wait_id)
    assert record["source"] == SOURCE_GITHUB_ACTIONS_RUN
    assert record["run_id"] == RUN_ID and record["pr_number"] is None
    assert record["run_label"] == "Native MLS track — Android host" and record["run_number"] == 28
    assert record["head_sha"] == HEAD and record["state"] == STATE_MONITORING
    again = registry.register(
        repo="jinwon-int/family-messenger",
        pr_number=None,
        head_sha="",
        user_id=7,
        chat_id=70,
        session_id="sess-1",
        summary="other",
        timeout_seconds=60,
        poll_interval_seconds=30,
        source=SOURCE_GITHUB_ACTIONS_RUN,
        run_id=str(RUN_ID),
    )
    assert again == wait_id


@pytest.mark.parametrize("bad", [None, "", "0", "-3", "abc", 10**15])
def test_run_id_validation(bad) -> None:
    with pytest.raises(ExternalWaitValidationError):
        validate_run_id(bad)


def test_head_sha_is_optional_but_validated(tmp_path: Path) -> None:
    registry = ExternalWaitRegistry(default_registry_path(tmp_path))
    kwargs = dict(
        repo="o/r",
        pr_number=None,
        user_id=1,
        chat_id=2,
        session_id=None,
        summary="",
        timeout_seconds=60,
        poll_interval_seconds=30,
        source=SOURCE_GITHUB_ACTIONS_RUN,
        run_id=5,
    )
    wait_id = registry.register(head_sha="", **kwargs)
    assert registry.get(wait_id)["head_sha"] == ""
    with pytest.raises(ExternalWaitValidationError):
        registry.register(head_sha="not-a-sha", **{**kwargs, "run_id": 6})


def test_note_run_status_rewrites_only_on_change(tmp_path: Path) -> None:
    clock = Clock()
    registry, wait_id, _, _ = _setup(tmp_path, RunTransport([_run("queued")]), clock)
    assert registry.note_run_status(wait_id, "waiting") is True
    assert registry.note_run_status(wait_id, "waiting") is False
    assert registry.note_run_status(wait_id, "Bad Status!") is False
    assert registry.get(wait_id)["run_status"] == "waiting"


# --- state mapping ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "conclusion", "terminal"),
    [
        ("queued", "", None),
        ("in_progress", "", None),
        ("waiting", "", None),  # environment approval gate — the run continues once approved
        ("requested", "", None),
        ("pending", "", None),
        ("completed", "success", TERMINAL_SUCCESS),
        ("completed", "neutral", TERMINAL_SUCCESS),
        ("completed", "cancelled", TERMINAL_CANCELLED),
        ("completed", "failure", TERMINAL_FAILURE),
        ("completed", "timed_out", TERMINAL_FAILURE),
        ("completed", "startup_failure", TERMINAL_FAILURE),
        ("completed", "action_required", TERMINAL_FAILURE),
        ("completed", "skipped", TERMINAL_FAILURE),
        ("completed", "some_future_value", TERMINAL_FAILURE),
    ],
)
def test_actions_run_terminal_mapping(status: str, conclusion: str, terminal) -> None:
    assert actions_run_terminal(ActionsRunState(status=status, conclusion=conclusion)) == terminal


def test_state_parser_is_body_free_and_strict() -> None:
    state = _actions_run_state_from(
        {"status": "completed", "conclusion": "success", "head_sha": HEAD.upper(), "name": "x", "logs_url": "y"}
    )
    assert state == ActionsRunState(status="completed", conclusion="success", head_sha=HEAD)
    for bad in (None, [], {}, {"status": ""}, {"status": "x" * 40}):
        with pytest.raises(TransportError):
            _actions_run_state_from(bad)


def test_not_found_is_classified() -> None:
    assert _classify_gh_error(b"gh: Not Found (HTTP 404)") == "not-found"
    assert _classify_gh_error(b"API rate limit exceeded") == "rate-limit"


# --- monitor ------------------------------------------------------------------------


@pytest.mark.anyio
async def test_success_wakes_once_and_resumes_with_the_run(tmp_path: Path) -> None:
    clock = Clock()
    transport = RunTransport([_run("queued"), _run("in_progress"), _run("completed", "success")])
    registry, wait_id, recorder, monitor = _setup(tmp_path, transport, clock)

    await _drive(monitor, clock, 8)

    record = registry.get(wait_id)
    assert record["state"] == TERMINAL_SUCCESS
    assert record["terminal_detail"] == {"conclusion": "success"}
    assert all(call == ("jinwon-int/family-messenger", RUN_ID) for call in transport.calls)
    assert len(recorder.notifications) == 1
    text = recorder.notifications[0][1]
    assert text.startswith("✅ Actions run succeeded")
    assert f"Actions run {RUN_ID}" in text and "#28" in text
    assert "Continuing automatically." in text
    assert len(recorder.resumes) == 1
    prompt = recorder.resumes[0][1]
    assert f"run={RUN_ID}" in prompt and "github_actions_run" in prompt and "pr=" not in prompt


@pytest.mark.anyio
async def test_failure_records_the_conclusion_and_still_resumes(tmp_path: Path) -> None:
    clock = Clock()
    transport = RunTransport([_run("completed", "timed_out")])
    registry, wait_id, recorder, monitor = _setup(tmp_path, transport, clock)

    await _drive(monitor, clock, 2)

    record = registry.get(wait_id)
    assert record["state"] == TERMINAL_FAILURE
    assert "Run conclusion: timed_out" in recorder.notifications[0][1]
    assert "conclusion=timed_out" in recorder.resumes[0][1]


@pytest.mark.anyio
async def test_cancelled_run(tmp_path: Path) -> None:
    clock = Clock()
    registry, wait_id, recorder, monitor = _setup(tmp_path, RunTransport([_run("completed", "cancelled")]), clock)
    await _drive(monitor, clock, 2)
    assert registry.get(wait_id)["state"] == TERMINAL_CANCELLED
    assert recorder.notifications[0][1].startswith("⚠️ Actions run cancelled")


@pytest.mark.anyio
async def test_approval_gate_stays_open_and_refreshes_the_status_line(tmp_path: Path) -> None:
    clock = Clock()
    transport = RunTransport([_run("waiting"), _run("waiting"), _run("in_progress")])
    registry, wait_id, recorder, monitor = _setup(tmp_path, transport, clock)

    await _drive(monitor, clock, 2)
    record = registry.get(wait_id)
    assert record["state"] == STATE_MONITORING and record["run_status"] == "waiting"
    assert recorder.notifications == []
    assert recorder.status_syncs == [(7, 70)], "status line refreshed once, on the change"
    line = render_wait_status([record], clock())
    assert "Actions run Native MLS track — Android host #28 (awaiting approval)" in line

    await _drive(monitor, clock, 2)
    record = registry.get(wait_id)
    assert record["run_status"] == "in_progress"
    assert "awaiting approval" not in render_wait_status([record], clock())
    assert len(recorder.status_syncs) == 2


@pytest.mark.anyio
async def test_polls_back_off_to_the_actions_ceiling(tmp_path: Path) -> None:
    clock = Clock()
    registry, wait_id, _, monitor = _setup(tmp_path, RunTransport([_run("in_progress")]), clock)
    for _ in range(8):
        await monitor._tick()
        clock.advance(400)
    assert registry.get(wait_id)["poll_interval_seconds"] == 120.0


@pytest.mark.anyio
async def test_deleted_run_ends_in_monitor_error_after_bounded_retries(tmp_path: Path) -> None:
    clock = Clock()
    transport = RunTransport([TransportError("not-found")])
    registry, wait_id, recorder, monitor = _setup(tmp_path, transport, clock)

    await _drive(monitor, clock, 6)

    assert registry.get(wait_id)["state"] == TERMINAL_MONITOR_ERROR
    assert len(transport.calls) == 3
    assert recorder.resumes == [], "a read error says nothing about the run"
    assert "NOT continued" in recorder.notifications[0][1]


@pytest.mark.anyio
async def test_transport_without_actions_support_fails_closed(tmp_path: Path) -> None:
    class PrOnly:
        async def fetch_pr_state(self, repo, pr_number):  # pragma: no cover
            raise AssertionError

    clock = Clock()
    registry, wait_id, _, monitor = _setup(tmp_path, PrOnly(), clock)  # type: ignore[arg-type]
    await _drive(monitor, clock, 4)
    assert registry.get(wait_id)["state"] == TERMINAL_MONITOR_ERROR


# --- display ------------------------------------------------------------------------


def test_render_waits_and_texts_name_the_run(tmp_path: Path) -> None:
    clock = Clock()
    registry, wait_id, _, _ = _setup(tmp_path, RunTransport([_run("queued")]), clock)
    record = registry.get(wait_id)
    listing = render_waits([record])
    assert f"jinwon-int/family-messenger Actions run {RUN_ID} (Native MLS track — Android host)" in listing
    assert "#None" not in listing
    line = render_wait_status([record], clock())
    assert line.startswith("⏳ Waiting for results · Actions run Native MLS track — Android host #28 — verify the bundle")
    record = {**record, "state": TERMINAL_SUCCESS, "terminal_status": TERMINAL_SUCCESS}
    assert "pr=" not in resume_prompt_text(record)
    assert "#None" not in wake_notification_text(record, resumed=True)


def test_label_without_a_workflow_name_falls_back_to_the_run_id(tmp_path: Path) -> None:
    registry = ExternalWaitRegistry(default_registry_path(tmp_path))
    wait_id = registry.register(
        repo="o/r",
        pr_number=None,
        head_sha="",
        user_id=1,
        chat_id=2,
        session_id=None,
        summary="",
        timeout_seconds=60,
        poll_interval_seconds=30,
        source=SOURCE_GITHUB_ACTIONS_RUN,
        run_id=99,
    )
    record = registry.get(wait_id)
    assert "Actions run 99" in render_wait_status([record], 1_000.0)
    assert wake_notification_text({**record, "terminal_status": TERMINAL_SUCCESS}, resumed=False).count("@") == 0


# --- CLI ----------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CCC_EXTERNAL_WAIT_HOME", str(tmp_path))
    publish_active_turn(tmp_path, user_id=7, chat_id=70, session_id="sess-1")
    return tmp_path


def _fake_run(name: str = "iOS TestFlight release", number: int = 12, head: str = HEAD):
    def _resolve(repo: str, run_id: int) -> dict:
        return {"name": name, "run_number": number, "head_sha": head, "status": "queued"}

    return _resolve


def _cli(*extra: str) -> list[str]:
    return [
        "register",
        "--repo",
        "jinwon-int/family-messenger",
        "--source",
        "actions-run",
        "--run-id",
        str(RUN_ID),
        "--summary",
        "verify the upload",
        *extra,
    ]


def test_cli_registers_an_actions_run(home: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(external_wait_cli, "resolve_actions_run", _fake_run())

    assert external_wait_cli.main(_cli()) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True and payload["source"] == SOURCE_GITHUB_ACTIONS_RUN
    assert payload["run_id"] == RUN_ID and payload["run_number"] == 12 and payload["head_sha"] == HEAD
    assert "Actions run" in payload["message"]
    record = ExternalWaitRegistry(default_registry_path(home)).get(payload["wait_id"])
    assert record["run_label"] == "iOS TestFlight release" and record["user_id"] == 7

    assert external_wait_cli.main(["list"]) == 0
    listed = json.loads(capsys.readouterr().out.strip())["waits"][0]
    assert listed["run_id"] == RUN_ID and listed["pr"] is None


def test_cli_refuses_a_head_that_is_not_the_runs(home: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(external_wait_cli, "resolve_actions_run", _fake_run())
    assert external_wait_cli.main(_cli("--head-sha", "deadbeef")) == 2
    assert "does not match" in json.loads(capsys.readouterr().out.strip())["message"]
    assert external_wait_cli.main(_cli("--head-sha", HEAD[:8])) == 0


def test_cli_fails_closed_when_the_run_does_not_resolve(home: Path, capsys, monkeypatch) -> None:
    def _boom(repo: str, run_id: int) -> dict:
        raise ExternalWaitValidationError("Actions run does not resolve in the repo; check --repo and --run-id")

    monkeypatch.setattr(external_wait_cli, "resolve_actions_run", _boom)
    assert external_wait_cli.main(_cli()) == 2
    assert json.loads(capsys.readouterr().out.strip())["code"] == "validation"
    assert ExternalWaitRegistry(default_registry_path(home)).records() == []


@pytest.mark.parametrize("run_id", ["", "abc", "0"])
def test_cli_rejects_a_bad_run_id(home: Path, capsys, monkeypatch, run_id: str) -> None:
    monkeypatch.setattr(external_wait_cli, "resolve_actions_run", _fake_run())
    args = _cli()
    args[args.index("--run-id") + 1] = run_id
    assert external_wait_cli.main(args) == 2


def test_resolve_actions_run_parses_gh_output(monkeypatch) -> None:
    out = json.dumps({"name": "  Build\n release ", "run_number": 28, "head_sha": HEAD, "status": "in_progress"})

    def _fake(cmd, capture_output, timeout):
        assert cmd[:3] == ["gh", "api", f"repos/o/r/actions/runs/{RUN_ID}"]
        return subprocess.CompletedProcess(cmd, 0, stdout=out.encode(), stderr=b"")

    monkeypatch.setattr(external_wait_cli.subprocess, "run", _fake)
    assert external_wait_cli.resolve_actions_run("o/r", RUN_ID) == {
        "name": "Build release",
        "run_number": 28,
        "head_sha": HEAD,
        "status": "in_progress",
    }

    def _missing(cmd, capture_output, timeout):
        return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"Not Found")

    monkeypatch.setattr(external_wait_cli.subprocess, "run", _missing)
    with pytest.raises(ExternalWaitValidationError):
        external_wait_cli.resolve_actions_run("o/r", RUN_ID)
