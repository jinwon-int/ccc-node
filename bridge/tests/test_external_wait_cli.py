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
    default_registry_path,
    publish_active_turn,
)


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
