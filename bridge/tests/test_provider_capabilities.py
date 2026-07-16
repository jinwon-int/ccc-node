"""Drift checkers for the provider capability matrix (#387).

The matrix in ``core/provider_capabilities.py`` is the single source of
truth.  These tests pin it against three things that can silently drift:

* the committed rendered document ``docs/provider-capability-matrix.md``,
* the session layer's set of valid providers,
* the runtime code the ``supported`` claims are grounded in (adapter
  modules, browsing/usage surfaces, memory hook files, and the executable
  conformance coverage).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING
import unittest

if TYPE_CHECKING:
    from core.provider_capabilities import (
        CAPABILITY_AXES,
        PROVIDER_CAPABILITY_MATRIX,
        SUPPORTED_PROVIDERS,
        CapabilityState,
        CapabilityStatus,
        capability_status,
        render_capability_matrix_markdown,
    )
    from session.manager import SessionManager
    from tests import runtime_conformance as conformance
else:
    from telegram_bot.core.provider_capabilities import (
        CAPABILITY_AXES,
        PROVIDER_CAPABILITY_MATRIX,
        SUPPORTED_PROVIDERS,
        CapabilityState,
        CapabilityStatus,
        capability_status,
        render_capability_matrix_markdown,
    )
    from telegram_bot.session.manager import SessionManager
    import runtime_conformance as conformance

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX_DOCUMENT = REPO_ROOT / "docs" / "provider-capability-matrix.md"


class CapabilityDeclarationTests(unittest.TestCase):
    def test_matrix_covers_exactly_the_session_layer_providers(self) -> None:
        self.assertEqual(set(SUPPORTED_PROVIDERS), set(SessionManager.VALID_PROVIDERS))
        self.assertEqual(set(PROVIDER_CAPABILITY_MATRIX), set(SUPPORTED_PROVIDERS))

    def test_every_axis_declares_every_provider_exactly_once(self) -> None:
        keys = [axis.key for axis in CAPABILITY_AXES]
        self.assertEqual(len(keys), len(set(keys)), "axis keys must be unique")
        for axis in CAPABILITY_AXES:
            self.assertEqual(set(axis.statuses), set(SUPPORTED_PROVIDERS))
        for provider in SUPPORTED_PROVIDERS:
            self.assertEqual(set(PROVIDER_CAPABILITY_MATRIX[provider]), set(keys))

    def test_statuses_are_machine_readable(self) -> None:
        for axis in CAPABILITY_AXES:
            for provider, status in axis.statuses.items():
                with self.subTest(axis=axis.key, provider=provider):
                    self.assertIsInstance(status.state, CapabilityState)
                    self.assertTrue(status.reason.strip())
                    for dependency in status.dependencies:
                        self.assertRegex(dependency, r"^#\d+$")

    def test_status_validation_rejects_malformed_declarations(self) -> None:
        with self.assertRaises(ValueError):
            CapabilityStatus(CapabilityState.SUPPORTED, "")
        with self.assertRaises(ValueError):
            CapabilityStatus(CapabilityState.DEGRADED, "reason", ("issue-465",))

    def test_capability_status_accessor_rejects_unknown_keys(self) -> None:
        self.assertIs(
            capability_status("codex", "runtime_adapter").state,
            CapabilityState.SUPPORTED,
        )
        with self.assertRaises(KeyError):
            capability_status("gemini", "runtime_adapter")
        with self.assertRaises(KeyError):
            capability_status("codex", "not_an_axis")


class CapabilityDocumentDriftTests(unittest.TestCase):
    def test_committed_document_matches_the_rendered_matrix(self) -> None:
        self.assertTrue(
            MATRIX_DOCUMENT.exists(),
            f"missing {MATRIX_DOCUMENT}; regenerate it with "
            "`python -m telegram_bot.core.provider_capabilities > "
            "docs/provider-capability-matrix.md`",
        )
        committed = MATRIX_DOCUMENT.read_text(encoding="utf-8")
        self.assertEqual(
            committed,
            render_capability_matrix_markdown(),
            "docs/provider-capability-matrix.md drifted from "
            "core/provider_capabilities.py; regenerate it with "
            "`python -m telegram_bot.core.provider_capabilities > "
            "docs/provider-capability-matrix.md`",
        )

    def test_render_is_deterministic_and_lists_every_axis(self) -> None:
        first = render_capability_matrix_markdown()
        self.assertEqual(first, render_capability_matrix_markdown())
        for axis in CAPABILITY_AXES:
            self.assertIn(f"`{axis.key}`", first)
        for state in CapabilityState:
            self.assertIn(f"`{state.value}`", first)


class CapabilityRuntimeDriftTests(unittest.TestCase):
    """Pin `supported` claims to the code they are grounded in."""

    def test_conformance_covered_axes_are_declared_supported_for_codex(self) -> None:
        # test_runtime_conformance.py binds CodexRuntime to the suite; the
        # axes the suite exercises must therefore be declared supported, and
        # downgrading one requires consciously dropping the behavior coverage.
        for axis_key in conformance.CONFORMANCE_COVERED_AXES:
            with self.subTest(axis=axis_key):
                self.assertIs(
                    capability_status("codex", axis_key).state,
                    CapabilityState.SUPPORTED,
                )

    def test_claude_runtime_adapter_state_tracks_the_adapter_module(self) -> None:
        # When a ClaudeRuntime adapter lands, this trips: bind the adapter to
        # the conformance suite and promote `claude`/`runtime_adapter` in the
        # capability matrix in the same change.
        adapter_exists = (
            importlib.util.find_spec("telegram_bot.core.claude_runtime") is not None
        )
        declared_supported = (
            capability_status("claude", "runtime_adapter").state
            is CapabilityState.SUPPORTED
        )
        self.assertEqual(adapter_exists, declared_supported)

    def test_codex_session_browsing_claim_matches_the_runtime_surface(self) -> None:
        from telegram_bot.core.codex_runtime import CodexRuntime

        declared = capability_status("codex", "session_browsing").state
        self.assertIs(declared, CapabilityState.SUPPORTED)
        for member in ("list_sessions", "read_session", "supports_session_browsing"):
            self.assertTrue(hasattr(CodexRuntime, member))

    def test_usage_metering_claims_match_the_parser_surface(self) -> None:
        from telegram_bot.core import usage

        surfaces = {
            "claude": ("parse_claude_rate_limit_event", "parse_claude_result"),
            "codex": (
                "parse_codex_rate_limits",
                "parse_codex_account_usage",
                "parse_codex_thread_usage",
            ),
        }
        for provider, members in surfaces.items():
            with self.subTest(provider=provider):
                self.assertIs(
                    capability_status(provider, "usage_metering").state,
                    CapabilityState.SUPPORTED,
                )
                for member in members:
                    self.assertTrue(hasattr(usage, member))

    def test_claude_memory_claims_match_the_hook_layout(self) -> None:
        grounded_files = {
            "memory_read_bootstrap": ("claude/hooks/load-memory.sh",),
            "memory_writeback_distill": ("claude/hooks/distill.sh",),
            "memory_sink_local": (
                "claude/hooks/distill/resume-write.sh",
                "claude/hooks/distill/local-facts.sh",
            ),
            "memory_sink_honcho": (
                "claude/hooks/distill/honcho-push.sh",
                "claude/hooks/distill/queue-drain.sh",
            ),
            "memory_sink_wiki_candidate": ("claude/hooks/distill/wiki-queue.sh",),
        }
        for axis_key, relative_paths in grounded_files.items():
            with self.subTest(axis=axis_key):
                self.assertIs(
                    capability_status("claude", axis_key).state,
                    CapabilityState.SUPPORTED,
                )
                for relative_path in relative_paths:
                    self.assertTrue(
                        (REPO_ROOT / relative_path).is_file(),
                        f"{relative_path} backs the claude {axis_key} claim",
                    )

    def test_codex_memory_gaps_track_issue_465(self) -> None:
        for axis_key in (
            "memory_writeback_distill",
            "memory_sink_local",
            "memory_sink_honcho",
            "memory_sink_wiki_candidate",
            "memory_roundtrip",
        ):
            status = capability_status("codex", axis_key)
            with self.subTest(axis=axis_key):
                self.assertIsNot(status.state, CapabilityState.SUPPORTED)
                self.assertIn("#465", status.dependencies)


if __name__ == "__main__":
    unittest.main()
