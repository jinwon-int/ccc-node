"""The test suite must never write runtime state into the checkout (#2600).

``tests/conftest.py`` pins ``PROJECT_ROOT`` to the bridge checkout so the real
config validates. Without a separate ``BOT_DATA_DIR`` the global health
reporter and task ledger then wrote ``health.json`` / ``tasks.json`` into
``<checkout>/bridge/.telegram_bot`` — the live bridge's own files on a node
that runs the bridge from that checkout.
"""

from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parents[1]


def _inside_checkout(path: Path) -> bool:
    resolved = path.resolve()
    checkout = BRIDGE_DIR.resolve()
    return resolved == checkout or checkout in resolved.parents


def test_runtime_data_dirs_are_outside_the_checkout() -> None:
    from telegram_bot.utils.config import config

    for name in ("bot_data_dir", "logs_dir", "session_store_path"):
        value = Path(getattr(config, name))
        assert not _inside_checkout(value), f"{name} points into the checkout: {value}"


def test_health_reporter_and_task_ledger_target_the_private_dir() -> None:
    from telegram_bot.core.task_ledger import default_task_ledger_path
    from telegram_bot.utils.config import config

    assert not _inside_checkout(default_task_ledger_path(config.bot_data_dir))
    assert not _inside_checkout(Path(config.bot_data_dir) / "health.json")
