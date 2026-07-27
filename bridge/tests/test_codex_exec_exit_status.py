"""The provider exit status must reach diagnostics without widening the boundary (#760).

`codex_exec_backend` deliberately discards provider stdout/stderr — the module
contract is that they "never enter exceptions or diagnostics", and the doctor
suite asserts no auth markers leak. So a nonzero exit arrived as one opaque
`codex_distill_nonzero_exit` whatever the cause: #760 was an HTTP 400 schema
rejection and could only be identified by reproducing the invocation by hand.

The exit status is carried on a separate attribute rather than folded into
`code`, because `_RETRYABLE_BACKEND_CODES` matches `code` exactly — encoding the
status there would silently drop nonzero exits out of the retryable set.
"""

from __future__ import annotations

import logging

from telegram_bot.memory.codex_exec_backend import CodexDistillBackendError
from telegram_bot.memory.distill_worker import (
    _RETRYABLE_BACKEND_CODES,
    _body_free_error_code,
)
from telegram_bot.memory.skill_candidate_backend import SkillCandidateBackendError


class TestExitStatusIsCarried:
    def test_defaults_to_none(self) -> None:
        assert CodexDistillBackendError("codex_distill_timeout").exit_status is None

    def test_carries_exit_status(self) -> None:
        error = CodexDistillBackendError("codex_distill_nonzero_exit", exit_status=1)
        assert error.exit_status == 1
        assert error.code == "codex_distill_nonzero_exit"

    def test_negative_status_is_signal_semantics(self) -> None:
        # subprocess convention: -9 is SIGKILL, which the timeout path produces.
        assert CodexDistillBackendError("x", exit_status=-9).exit_status == -9

    def test_str_stays_body_free(self) -> None:
        # The message must remain the bare code — logs built from str(error)
        # must not start carrying extra fields.
        assert str(CodexDistillBackendError("codex_distill_nonzero_exit", exit_status=1)) == (
            "codex_distill_nonzero_exit"
        )


class TestClassificationIsUnchanged:
    """The reason the status is not part of `code`."""

    def test_nonzero_exit_is_still_retryable(self) -> None:
        error = CodexDistillBackendError("codex_distill_nonzero_exit", exit_status=1)
        code, terminal = _body_free_error_code(error)
        assert code == "codex_distill_nonzero_exit"
        assert terminal is False
        assert code in _RETRYABLE_BACKEND_CODES

    def test_recorded_code_does_not_embed_the_status(self) -> None:
        code, _ = _body_free_error_code(
            CodexDistillBackendError("codex_distill_nonzero_exit", exit_status=124)
        )
        assert "124" not in code


class TestRelabelPreservesStatus:
    def test_skill_candidate_relabel_keeps_exit_status(self) -> None:
        origin = CodexDistillBackendError("codex_distill_nonzero_exit", exit_status=1)
        relabelled = SkillCandidateBackendError(
            origin.code.replace("codex_distill_", "skill_candidate_", 1),
            exit_status=origin.exit_status,
        )
        assert relabelled.code == "skill_candidate_nonzero_exit"
        assert relabelled.exit_status == 1

    def test_skill_candidate_defaults_to_none(self) -> None:
        assert SkillCandidateBackendError("skill_candidate_output_invalid").exit_status is None


class TestLoggingSurfacesStatusOnly:
    def test_warning_carries_status_and_no_body(self, caplog) -> None:
        error = CodexDistillBackendError("codex_distill_nonzero_exit", exit_status=1)
        code, terminal = _body_free_error_code(error)
        logger = logging.getLogger("telegram_bot.memory.distill_worker")
        with caplog.at_level(logging.WARNING, logger=logger.name):
            logger.warning(
                "Distill extraction failed: code=%s terminal=%s provider_exit_status=%d",
                code,
                terminal,
                error.exit_status,
            )
        message = caplog.text
        assert "provider_exit_status=1" in message
        assert "codex_distill_nonzero_exit" in message
