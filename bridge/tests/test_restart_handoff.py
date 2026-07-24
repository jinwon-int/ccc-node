from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from telegram_bot.core import restart_handoff as rh
from telegram_bot.core.bot_lifecycle import BotLifecycleMixin
from telegram_bot.utils.config import Config


def completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def test_schedule_persists_private_receipt_and_uses_argv_only(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 8
        return completed()

    result = rh.schedule_restart(
        data_dir=tmp_path,
        chat_id=42,
        unit="ccc-telegram-bridge-nosuk.service",
        runner=runner,
        systemd_run_path="/fake/systemd-run",
        python_executable="/fake/python",
        now=lambda: 100.0,
        origin_pid=111,
        user_scope=True,
    )

    receipt = rh.read_receipt(tmp_path)
    assert receipt == {
        "schema_version": 1,
        "request_id": result.request_id,
        "state": "prepared",
        "unit": "ccc-telegram-bridge-nosuk.service",
        "chat_id": 42,
        "origin_pid": 111,
        "created_at": 100.0,
        "updated_at": 100.0,
        "user_scope": True,
    }
    assert stat_mode(tmp_path) == 0o700
    assert stat_mode(tmp_path / rh.RECEIPT_NAME) == 0o600
    assert calls == [
        [
            "/fake/systemd-run",
            "--user",
            "--quiet",
            "--collect",
            f"--unit=ccc-bridge-restart-{result.request_id}",
            "--on-active=5s",
            "/fake/python",
            str(Path(rh.__file__).resolve()),
            "worker",
            "--data-dir",
            str(tmp_path.resolve()),
            "--request-id",
            result.request_id,
            "--user-scope",
        ]
    ]


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_schedule_rejects_active_request_and_invalid_unit(tmp_path: Path) -> None:
    rh.schedule_restart(
        data_dir=tmp_path,
        chat_id=1,
        unit="ccc-telegram-bridge.service",
        runner=lambda *a, **k: completed(),
        now=lambda: 100.0,
        user_scope=False,
    )
    with pytest.raises(rh.RestartHandoffError, match="restart_already_pending"):
        rh.schedule_restart(
            data_dir=tmp_path,
            chat_id=1,
            unit="ccc-telegram-bridge.service",
            runner=lambda *a, **k: completed(),
            now=lambda: 101.0,
            user_scope=False,
        )
    with pytest.raises(rh.RestartHandoffError, match="invalid_unit"):
        rh.validate_unit("ssh.service")


def test_schedule_does_not_overwrite_an_undelivered_terminal_result(
    tmp_path: Path,
) -> None:
    scheduled = rh.schedule_restart(
        data_dir=tmp_path,
        chat_id=1,
        unit="ccc-telegram-bridge.service",
        runner=lambda *a, **k: completed(),
        user_scope=False,
    )
    record = rh.read_receipt(tmp_path)
    record["state"] = "failed"
    rh._write_at(tmp_path, rh.RECEIPT_NAME, record)

    with pytest.raises(rh.RestartHandoffError, match="restart_result_pending"):
        rh.schedule_restart(
            data_dir=tmp_path,
            chat_id=1,
            unit="ccc-telegram-bridge.service",
            runner=lambda *a, **k: completed(),
            user_scope=False,
        )
    assert rh.read_receipt(tmp_path)["request_id"] == scheduled.request_id


def test_schedule_failure_leaves_bridge_without_pending_receipt(tmp_path: Path) -> None:
    with pytest.raises(rh.RestartHandoffError, match="systemd_run_rejected"):
        rh.schedule_restart(
            data_dir=tmp_path,
            chat_id=1,
            unit="ccc-telegram-bridge.service",
            runner=lambda *a, **k: completed(1),
            user_scope=False,
        )
    assert rh.read_receipt(tmp_path) is None


def test_receipt_reader_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("{}", encoding="utf-8")
    (tmp_path / rh.RECEIPT_NAME).symlink_to(target)
    with pytest.raises((rh.RestartHandoffError, OSError)):
        rh.read_receipt(tmp_path)


def test_worker_verifies_new_main_pid_and_available_health(tmp_path: Path) -> None:
    scheduled = rh.schedule_restart(
        data_dir=tmp_path,
        chat_id=9,
        unit="ccc-telegram-bridge.service",
        runner=lambda *a, **k: completed(),
        now=lambda: 100.0,
        origin_pid=111,
        user_scope=True,
    )
    (tmp_path / "health.json").write_text(
        json.dumps(
            {
                "service": {"state": "available"},
                "process": {"pid": 222},
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if "show" in argv:
            return completed(stdout="222\n")
        return completed()

    assert (
        rh.run_worker(
            data_dir=tmp_path,
            request_id=scheduled.request_id,
            user_scope=True,
            runner=runner,
            systemctl_path="/fake/systemctl",
            now=lambda: 101.0,
            sleep=lambda _: None,
        )
        == 0
    )
    receipt = rh.read_receipt(tmp_path)
    assert receipt["state"] == "completed"
    assert receipt["new_pid"] == 222
    assert calls[0] == [
        "/fake/systemctl",
        "--user",
        "restart",
        "--",
        "ccc-telegram-bridge.service",
    ]


def test_worker_records_body_free_restart_failure(tmp_path: Path) -> None:
    scheduled = rh.schedule_restart(
        data_dir=tmp_path,
        chat_id=9,
        unit="ccc-telegram-bridge.service",
        runner=lambda *a, **k: completed(),
        user_scope=False,
    )
    assert (
        rh.run_worker(
            data_dir=tmp_path,
            request_id=scheduled.request_id,
            user_scope=False,
            runner=lambda *a, **k: completed(1, "secret output"),
        )
        == 1
    )
    receipt = rh.read_receipt(tmp_path)
    assert receipt["state"] == "failed"
    assert receipt["reason_code"] == "restart_failed"
    assert "secret" not in json.dumps(receipt)


def test_terminal_receipt_archives_only_matching_request(tmp_path: Path) -> None:
    scheduled = rh.schedule_restart(
        data_dir=tmp_path,
        chat_id=9,
        unit="ccc-telegram-bridge.service",
        runner=lambda *a, **k: completed(),
        user_scope=False,
    )
    record = rh.read_receipt(tmp_path)
    record["state"] = "failed"
    rh._write_at(tmp_path, rh.RECEIPT_NAME, record)

    assert not rh.archive_receipt(tmp_path, "wrong")
    assert rh.archive_receipt(tmp_path, scheduled.request_id)
    assert rh.read_receipt(tmp_path) is None
    assert (tmp_path / rh.ARCHIVE_NAME).is_file()


def test_worker_rejects_wrong_request_without_restarting(tmp_path: Path) -> None:
    rh.schedule_restart(
        data_dir=tmp_path,
        chat_id=9,
        unit="ccc-telegram-bridge.service",
        runner=lambda *a, **k: completed(),
        user_scope=False,
    )
    called = False

    def runner(*args, **kwargs):
        nonlocal called
        called = True
        return completed()

    assert (
        rh.run_worker(
            data_dir=tmp_path,
            request_id="wrong",
            user_scope=False,
            runner=runner,
        )
        == 2
    )
    assert not called


def test_config_restricts_restart_unit_family() -> None:
    config = Config(
        telegram_bot_token="token",
        CCC_BRIDGE_RESTART_HANDOFF="systemd",
        CCC_BRIDGE_RESTART_UNIT="ccc-telegram-bridge-nosuk.service",
    )
    assert config.restart_handoff == "systemd"
    with pytest.raises(ValueError, match="CCC_BRIDGE_RESTART_UNIT"):
        Config(
            telegram_bot_token="token",
            CCC_BRIDGE_RESTART_UNIT="ssh.service",
        )


@pytest.mark.anyio
async def test_replacement_bridge_delivers_and_archives_terminal_receipt(
    tmp_path: Path,
) -> None:
    scheduled = rh.schedule_restart(
        data_dir=tmp_path,
        chat_id=77,
        unit="ccc-telegram-bridge.service",
        runner=lambda *a, **k: completed(),
        user_scope=False,
    )
    record = rh.read_receipt(tmp_path)
    record.update(state="completed", new_pid=222)
    rh._write_at(tmp_path, rh.RECEIPT_NAME, record)
    stop_event = asyncio.Event()

    async def send_message(**kwargs):
        assert kwargs["chat_id"] == 77
        assert "222" in kwargs["text"]
        stop_event.set()

    class Lifecycle(BotLifecycleMixin):
        pass

    lifecycle = Lifecycle()
    lifecycle._config = SimpleNamespace(bot_data_dir=tmp_path)
    lifecycle.application = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock(side_effect=send_message))
    )

    await lifecycle._restart_receipt_loop(stop_event)

    assert rh.read_receipt(tmp_path) is None
    assert (tmp_path / rh.ARCHIVE_NAME).is_file()
    assert scheduled.request_id in (tmp_path / rh.ARCHIVE_NAME).read_text(encoding="utf-8")
