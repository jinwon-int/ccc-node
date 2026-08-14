"""Contract tests for the agent-side continuation CLI (#1113).

Mirrors the external-wait CLI contract: a successful registration prints a
continuation_id, and every failure is a machine-visible ``{"ok": false}``
with a non-zero exit so an agent never mistakes it for a queued bundle.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from telegram_bot.core import continuation_cli
from telegram_bot.core.continuation import (
    ContinuationQueue,
    default_queue_path,
)
from telegram_bot.core.external_wait import publish_active_turn


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CCC_CONTINUATION_HOME", str(tmp_path / "continuation"))
    monkeypatch.setenv("CCC_EXTERNAL_WAIT_HOME", str(tmp_path / "external-wait"))
    return tmp_path


def _publish(home: Path, **kwargs) -> None:
    params = {"user_id": 7, "chat_id": 70, "session_id": "sess-1"}
    params.update(kwargs)
    publish_active_turn(home / "external-wait", **params)


def _register_args(**overrides) -> list[str]:
    args = ["register", "--prompt", "open the content PR next"]
    for key, value in overrides.items():
        args += [f"--{key.replace('_', '-')}", str(value)]
    return args


def _queue(home: Path) -> ContinuationQueue:
    return ContinuationQueue(default_queue_path(home / "continuation"))


def test_register_binds_the_route_and_prints_continuation_id(
    home: Path, capsys: pytest.CaptureFixture
) -> None:
    _publish(home)

    rc = continuation_cli.main(_register_args())

    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    assert payload["continuation_id"]
    assert payload["replaced"] == []
    record = _queue(home).get(payload["continuation_id"])
    assert record["user_id"] == 7 and record["chat_id"] == 70
    assert record["session_id"] == "sess-1"
    assert record["prompt"] == "open the content PR next"
    assert record["state"] == "pending"


def test_second_registration_replaces_the_pending_one(
    home: Path, capsys: pytest.CaptureFixture
) -> None:
    _publish(home)
    first = json.loads(
        (lambda rc: capsys.readouterr().out.strip())(continuation_cli.main(_register_args()))
    )
    second = json.loads(
        (lambda rc: capsys.readouterr().out.strip())(
            continuation_cli.main(_register_args(prompt="actually do this instead"))
        )
    )

    assert second["replaced"] == [first["continuation_id"]]
    assert _queue(home).get(first["continuation_id"])["state"] == "cancelled"
    assert _queue(home).get(second["continuation_id"])["state"] == "pending"


def test_register_fails_closed_without_an_active_route(
    home: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = continuation_cli.main(_register_args())

    assert rc == 3
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["code"] == "route-unavailable"
    # No continuation is recorded when the route cannot be bound.
    assert _queue(home).records() == []


def test_register_rejects_an_empty_prompt(home: Path, capsys: pytest.CaptureFixture) -> None:
    _publish(home)

    rc = continuation_cli.main(_register_args(prompt="   "))

    assert rc == 2
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["code"] == "validation"


def test_list_and_cancel_round_trip(home: Path, capsys: pytest.CaptureFixture) -> None:
    _publish(home)
    registered = json.loads(
        (lambda rc: capsys.readouterr().out.strip())(continuation_cli.main(_register_args()))
    )

    assert continuation_cli.main(["list"]) == 0
    listed = json.loads(capsys.readouterr().out.strip())
    assert listed["ok"] is True
    ids = [c["continuation_id"] for c in listed["continuations"]]
    assert registered["continuation_id"] in ids

    rc = continuation_cli.main(["cancel", registered["continuation_id"]])
    assert rc == 0
    cancelled = json.loads(capsys.readouterr().out.strip())
    assert cancelled["ok"] is True
    assert (
        _queue(home).get(registered["continuation_id"])["state"] == "cancelled"
    )
