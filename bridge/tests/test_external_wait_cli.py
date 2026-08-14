"""Contract tests for the agent-side external-wait CLI (#740).

The CLI is the only way an agent may create a monitor: a successful
registration prints a wait_id, and every failure is a machine-visible
``{"ok": false, ...}`` with a non-zero exit so an agent can never mistake a
failed registration for a promise that will be kept.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from telegram_bot.core import external_wait_cli
from telegram_bot.core.external_wait import (
    ExternalWaitRegistry,
    ExternalWaitValidationError,
    default_registry_path,
    publish_active_turn,
)


_REAL_RESOLVE = external_wait_cli.resolve_full_head_sha


@pytest.fixture(autouse=True)
def _stub_sha_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``gh`` out of unit tests: expand short SHAs deterministically."""

    def _fake(repo: str, head_sha: str) -> str:
        return head_sha if len(head_sha) == 40 else head_sha.ljust(40, "0")

    monkeypatch.setattr(external_wait_cli, "resolve_full_head_sha", _fake)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CCC_EXTERNAL_WAIT_HOME", str(tmp_path))
    return tmp_path


def _register_args(**overrides) -> list[str]:
    args = [
        "register",
        "--repo",
        "jinwon-int/ccc-node",
        "--pr",
        "123",
        "--head-sha",
        "abc1234",
        "--summary",
        "merge when green",
    ]
    for key, value in overrides.items():
        args += [f"--{key.replace('_', '-')}", str(value)]
    return args


def _publish(home: Path, **kwargs) -> None:
    params = {"user_id": 7, "chat_id": 70, "session_id": "sess-1"}
    params.update(kwargs)
    publish_active_turn(home, **params)


def test_register_binds_the_active_route_and_prints_wait_id(
    home: Path, capsys: pytest.CaptureFixture
) -> None:
    _publish(home)

    rc = external_wait_cli.main(_register_args())

    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    assert payload["wait_id"]
    record = ExternalWaitRegistry(default_registry_path(home)).get(payload["wait_id"])
    assert record["user_id"] == 7 and record["chat_id"] == 70
    assert record["session_id"] == "sess-1"
    assert record["repo"] == "jinwon-int/ccc-node"
    assert record["summary"] == "merge when green"


def test_register_is_idempotent_for_the_same_natural_key(
    home: Path, capsys: pytest.CaptureFixture
) -> None:
    _publish(home)
    first = json.loads(
        (lambda rc: capsys.readouterr().out.strip())(external_wait_cli.main(_register_args()))
    )
    second = json.loads(
        (lambda rc: capsys.readouterr().out.strip())(external_wait_cli.main(_register_args()))
    )
    assert first["wait_id"] == second["wait_id"]


def test_new_pr_head_requires_and_creates_a_new_exact_head_wait(
    home: Path, capsys: pytest.CaptureFixture
) -> None:
    """A completed wait is one-shot and never follows a later pushed head."""
    _publish(home)
    first = json.loads(
        (lambda rc: capsys.readouterr().out.strip())(
            external_wait_cli.main(_register_args(head_sha="a" * 40))
        )
    )
    second = json.loads(
        (lambda rc: capsys.readouterr().out.strip())(
            external_wait_cli.main(_register_args(head_sha="b" * 40))
        )
    )

    assert first["wait_id"] != second["wait_id"]
    records = ExternalWaitRegistry(default_registry_path(home)).records()
    assert {record["head_sha"] for record in records} == {"a" * 40, "b" * 40}


def test_register_normalizes_a_short_sha_to_the_full_head(
    home: Path, capsys: pytest.CaptureFixture
) -> None:
    _publish(home)

    rc = external_wait_cli.main(_register_args(head_sha="a8ac2475"))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    # Regression (#961/#949): a short SHA must never be stored as-is — the
    # monitor compares against GitHub's 40-char headRefOid with exact
    # equality, so a raw short SHA supersedes on the first poll and the
    # "CI finishes -> auto-resume" promise is silently dropped.
    assert len(payload["head_sha"]) == 40
    record = ExternalWaitRegistry(default_registry_path(home)).get(payload["wait_id"])
    assert record["head_sha"] == payload["head_sha"]
    assert record["head_sha"].startswith("a8ac2475")


def test_register_fails_closed_when_the_short_sha_does_not_resolve(
    home: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish(home)

    def _unresolvable(repo: str, head_sha: str) -> str:
        raise ExternalWaitValidationError("short head SHA does not resolve to a commit")

    monkeypatch.setattr(external_wait_cli, "resolve_full_head_sha", _unresolvable)

    rc = external_wait_cli.main(_register_args(head_sha="deadbeef"))

    assert rc == 2
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["code"] == "validation"
    # The honest failure must not leave a watch that can never fire.
    assert ExternalWaitRegistry(default_registry_path(home)).records() == []


def test_full_sha_passes_through_without_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    full = "a8ac2475" * 5

    def _boom(*args, **kwargs):
        raise AssertionError("gh must not run for an already-full SHA")

    monkeypatch.setattr(external_wait_cli.subprocess, "run", _boom)
    assert _REAL_RESOLVE("jinwon-int/ccc-node", full) == full


def test_register_fails_closed_without_an_active_route(
    home: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = external_wait_cli.main(_register_args())

    assert rc == 3
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["code"] == "route-unavailable"
    # The contract: the failure itself tells the agent how to fall back, so a
    # missing monitor can never be reported as a kept promise.
    assert "foreground" in payload["message"] or "unavailable" in payload["message"]


def test_register_rejects_invalid_fields(home: Path, capsys: pytest.CaptureFixture) -> None:
    _publish(home)

    rc = external_wait_cli.main(
        ["register", "--repo", "bad", "--pr", "0", "--head-sha", "xyz"]
    )

    assert rc == 2
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False


def test_list_and_cancel_round_trip(home: Path, capsys: pytest.CaptureFixture) -> None:
    _publish(home)
    external_wait_cli.main(_register_args())
    capsys.readouterr()

    assert external_wait_cli.main(["list"]) == 0
    listed = json.loads(capsys.readouterr().out.strip())
    wait_id = listed["waits"][0]["wait_id"]
    assert listed["waits"][0]["state"] == "monitoring"

    assert external_wait_cli.main(["cancel", wait_id]) == 0
    assert json.loads(capsys.readouterr().out.strip())["cancelled"] is True
    assert external_wait_cli.main(["cancel", wait_id]) == 2


def test_register_fails_closed_with_ambiguous_routes(
    home: Path, capsys: pytest.CaptureFixture
) -> None:
    _publish(home)
    _publish(home, user_id=8, chat_id=80, session_id="other")

    rc = external_wait_cli.main(_register_args())

    assert rc == 3
    assert json.loads(capsys.readouterr().out.strip())["ok"] is False


# ---------------------------------------------------------------------------
# Stale-head supersede on registration (#1110)
# ---------------------------------------------------------------------------


def _wait_ids(home: Path) -> dict[str, dict]:
    registry = ExternalWaitRegistry(default_registry_path(home))
    return {rec["wait_id"]: rec for rec in registry.records()}


def test_register_supersedes_this_conversations_other_head_waits(
    home: Path, capsys: pytest.CaptureFixture
) -> None:
    """A pushed head must not leave the old watch alive to fire stale results."""
    _publish(home)
    first = json.loads(
        (lambda rc: capsys.readouterr().out.strip())(
            external_wait_cli.main(_register_args(head_sha="a" * 40))
        )
    )

    second = json.loads(
        (lambda rc: capsys.readouterr().out.strip())(
            external_wait_cli.main(_register_args(head_sha="b" * 40))
        )
    )

    assert second["ok"] is True
    assert second["superseded"] == [first["wait_id"]]
    records = _wait_ids(home)
    old = records[first["wait_id"]]
    assert old["state"] == "superseded"
    # The journal is preserved (audit), but the wait leaves the poll set and
    # its wake can never resume the conversation (non-terminal rollup).
    assert old["wake"] is not None
    assert old["head_sha"] == "a" * 40
    new = records[second["wait_id"]]
    assert new["state"] == "monitoring"
    due_ids = {
        rec["wait_id"]
        for rec in ExternalWaitRegistry(default_registry_path(home)).due(now=10**12)
    }
    assert first["wait_id"] not in due_ids
    assert second["wait_id"] in due_ids


def test_register_keep_previous_leaves_other_head_waits_monitoring(
    home: Path, capsys: pytest.CaptureFixture
) -> None:
    _publish(home)
    first = json.loads(
        (lambda rc: capsys.readouterr().out.strip())(
            external_wait_cli.main(_register_args(head_sha="a" * 40))
        )
    )

    second = json.loads(
        (lambda rc: capsys.readouterr().out.strip())(
            external_wait_cli.main(_register_args(head_sha="b" * 40, keep_previous="1"))
        )
    )

    assert second["superseded"] == []
    records = _wait_ids(home)
    assert records[first["wait_id"]]["state"] == "monitoring"
    assert records[second["wait_id"]]["state"] == "monitoring"


def test_reregistering_the_same_head_supersedes_nothing(
    home: Path, capsys: pytest.CaptureFixture
) -> None:
    """Idempotent re-registration must never supersede itself (#1110 guard)."""
    _publish(home)
    first = json.loads(
        (lambda rc: capsys.readouterr().out.strip())(
            external_wait_cli.main(_register_args(head_sha="a" * 40))
        )
    )
    second = json.loads(
        (lambda rc: capsys.readouterr().out.strip())(
            external_wait_cli.main(_register_args(head_sha="a" * 40))
        )
    )

    assert first["wait_id"] == second["wait_id"]
    assert second["superseded"] == []
    assert _wait_ids(home)[first["wait_id"]]["state"] == "monitoring"


def test_register_never_supersedes_another_conversations_wait(
    home: Path, capsys: pytest.CaptureFixture
) -> None:
    """Supersede is scoped to the registering conversation (#1110 guard)."""
    _publish(home)  # user 7 / chat 70
    registry = ExternalWaitRegistry(default_registry_path(home))
    other_id = registry.register(
        repo="jinwon-int/ccc-node",
        pr_number=123,
        head_sha="c" * 40,
        user_id=9,
        chat_id=90,
        session_id="sess-other",
        summary="another conversation's watch",
        timeout_seconds=600,
        poll_interval_seconds=30,
    )

    payload = json.loads(
        (lambda rc: capsys.readouterr().out.strip())(
            external_wait_cli.main(_register_args(head_sha="b" * 40))
        )
    )

    assert payload["ok"] is True
    assert payload["superseded"] == []
    assert _wait_ids(home)[other_id]["state"] == "monitoring"
