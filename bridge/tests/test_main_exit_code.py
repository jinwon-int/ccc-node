"""Exit-code contract for ``telegram_bot.__main__.main``.

`main()` wraps `bot.run()` so that an orderly shutdown stays orderly. Rewriting
every SystemExit to 1 told systemd an orderly stop had failed, so a
`Restart=on-failure` unit bounced a bridge that meant to stop, and the operator
saw a failed unit for a successful stop (#875, commit 9070053).

That fix shipped without a test — `git show 9070053 --stat` touches only
`test_distill_worker.py` and `test_lifecycle_observation.py`, which cover the
reservation-refund and redaction fixes. The only test that drives `main()` at
all (`test_session_composition.py`) asserts on the *pre*-`bot.run()` path and
never reaches the handler, and `test_connection_resilience.py` calls
`bot.run()` directly, bypassing `main()` entirely. `service-install.test.sh`
pins the unit's `Restart=` line — the complementary half, and precisely why
the process side needs pinning too.

These tests drive the handler itself.
"""

import logging
import sys

import pytest

import telegram_bot.__main__ as main_module


class _StubBot:
    """Minimal stand-in for TelegramBot: run() raises whatever we hand it."""

    def __init__(self, raises: BaseException) -> None:
        self._raises = raises
        self.validated = False

    def validate_runtime_paths(self) -> None:
        self.validated = True

    def run(self) -> None:
        raise self._raises


class _StubSettings:
    def hook_policy_environment(self) -> dict[str, str]:
        return {}


@pytest.fixture
def run_main(monkeypatch, tmp_path):
    """Drive main() with a bot whose run() raises `exc`; return the stub bot."""

    def _run(exc: BaseException) -> _StubBot:
        bot = _StubBot(exc)
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        # argparse in main() reads the real sys.argv.
        monkeypatch.setattr(sys, "argv", ["telegram_bot"])
        monkeypatch.setattr(
            main_module, "load_runtime_settings", lambda: _StubSettings()
        )
        monkeypatch.setattr(main_module, "create_bot", lambda settings: bot)
        monkeypatch.setattr(main_module, "setup_logging", lambda settings: None)
        return bot

    return _run


# --- clean exits must stay clean -------------------------------------------
# These are the regression cases: each one, before the fix, became SystemExit(1)
# and a spuriously failed systemd unit.


@pytest.mark.parametrize("code", [0, "0", None])
def test_clean_exit_is_not_rewritten(run_main, caplog, code):
    run_main(SystemExit(code))
    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as caught:
            main_module.main()
    # Re-raised unchanged — not replaced with SystemExit(1).
    assert caught.value.code == code
    # A clean stop must not be logged as an error.
    assert caplog.records == []


def test_clean_exit_preserves_the_original_exception(run_main):
    original = SystemExit(0)
    run_main(original)
    with pytest.raises(SystemExit) as caught:
        main_module.main()
    # `raise` (bare) re-raises the same object, so systemd sees the same code
    # rather than a lookalike constructed by the handler.
    assert caught.value is original


# --- genuine failures must surface as exit 1 -------------------------------


@pytest.mark.parametrize("code", [2, 1, "boom", True])
def test_failing_exit_is_normalised_to_one_and_logged(run_main, caplog, code):
    run_main(SystemExit(code))
    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as caught:
            main_module.main()
    assert caught.value.code == 1
    assert [r.getMessage() for r in caplog.records] == [str(code)]
    # `from exc` keeps the original reachable for post-mortems.
    assert isinstance(caught.value.__cause__, SystemExit)
    assert caught.value.__cause__.code == code


def test_unexpected_exception_becomes_exit_one_with_traceback(run_main, caplog):
    boom = RuntimeError("kaboom")
    run_main(boom)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as caught:
            main_module.main()
    assert caught.value.code == 1
    assert caught.value.__cause__ is boom
    (record,) = caplog.records
    assert "kaboom" in record.getMessage()
    # exc_info=True — without it the traceback is lost and the operator gets a
    # one-line message for an unexpected crash.
    assert record.exc_info is not None


# --- the boundary between the two branches ---------------------------------


def test_exit_code_zero_string_takes_the_clean_branch(run_main, caplog):
    """`str(exc.code) == "0"` is deliberate: `sys.exit("0")` is a clean stop.

    Pinned because the obvious "simplification" — comparing `exc.code == 0` —
    silently reclassifies it as a failure.
    """
    run_main(SystemExit("0"))
    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as caught:
            main_module.main()
    assert caught.value.code == "0"
    assert caplog.records == []


def test_run_is_reached_only_after_paths_are_validated(run_main):
    """The handler must not mask a validation failure as a clean exit."""
    bot = run_main(SystemExit(0))
    with pytest.raises(SystemExit):
        main_module.main()
    assert bot.validated is True
