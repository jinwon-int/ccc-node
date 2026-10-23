#!/usr/bin/env python3
"""Hermetic tests for doctor's always-on injection budget (#2384).

Every session pays for CLAUDE.md, the tools cheatsheet, the active output style
and the SessionStart memory snapshot before the task says a word. These tests
pin that doctor reports each source with chars/bytes and share of the cap,
stays 정상 under the cap, and over it names the sources to cut, largest first,
with the char delta — 경고 normally, 수동필요 (exit 1) under --strict. All
fixtures live in a temp HOME; no SessionStart hook is ever executed.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ccc_doctor  # noqa: E402
from ccc_doctor import Doctor  # noqa: E402

SETTINGS = {
    "outputStyle": "ccc-report",
    "hooks": {
        "SessionStart": [
            {
                "hooks": [
                    {"type": "command", "command": "bash /root/.claude/hooks/load-memory.sh SessionStart"},
                    {"type": "command", "command": "bash /root/.claude/hooks/load-tools.sh SessionStart"},
                ]
            }
        ]
    },
}


class AlwaysOnBudget(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.claude = self.home / ".claude"
        (self.claude / "hooks" / "cache").mkdir(parents=True)
        (self.claude / "output-styles").mkdir()
        (self.claude / "memories").mkdir()
        self.write_settings(SETTINGS)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- fixtures -----------------------------------------------------------

    def write_settings(self, data: dict) -> None:
        (self.claude / "settings.json").write_text(json.dumps(data), encoding="utf-8")

    def seed(self, claude_md: int = 0, cheatsheet: int = 0, style: int = 0, memory: int = 0, wiki: int = 0) -> None:
        if claude_md:
            (self.claude / "CLAUDE.md").write_text("c" * claude_md, encoding="utf-8")
        if cheatsheet:
            (self.claude / "hooks" / "tools-cheatsheet.md").write_text("t" * cheatsheet, encoding="utf-8")
        if style:
            (self.claude / "output-styles" / "ccc-report.md").write_text("s" * style, encoding="utf-8")
        if memory:
            (self.claude / "memories" / "MEMORY.md").write_text("m" * memory, encoding="utf-8")
        if wiki:
            (self.claude / "hooks" / "cache" / "wiki.txt").write_text("w" * wiki, encoding="utf-8")

    def run_check(self, env: dict[str, str] | None = None, strict: bool = False) -> Doctor:
        base = {"HOME": str(self.home), "CCC_AGENT_PROVIDER": "claude"}
        base.update(env or {})
        with patch.dict("os.environ", base, clear=True):
            doctor = Doctor(Path.cwd(), self.claude, "settings")
            doctor.strict = strict
            doctor.check_always_on_budget()
        return doctor

    def source_rows(self, doctor: Doctor) -> dict[str, str]:
        return {
            row.item.removeprefix("always-on: "): row.status
            for row in doctor.rows
            if row.item.startswith("always-on: ")
        }

    def verdict(self, doctor: Doctor):
        row = doctor.rows[-1]
        self.assertEqual(row.item, "always-on budget")
        return row

    # --- under cap ------------------------------------------------------------

    def test_under_cap_is_normal_with_per_source_table(self) -> None:
        self.seed(claude_md=8000, cheatsheet=3000, style=2000, memory=1000)
        doctor = self.run_check()
        rows = self.source_rows(doctor)
        self.assertEqual(
            set(rows), {"CLAUDE.md", "tools-cheatsheet.md", "output-style ccc-report", "memory snapshot"}
        )
        self.assertIn("8,000 chars / 8,000 B; 33% of cap; measured", rows["CLAUDE.md"])
        self.assertIn("3,000 chars", rows["tools-cheatsheet.md"])
        self.assertIn("2,000 chars", rows["output-style ccc-report"])
        verdict = self.verdict(doctor)
        self.assertEqual(verdict.klass, "정상")
        self.assertIn("total 14,000 chars", verdict.status)
        self.assertIn("58% of cap 24,000 chars", verdict.status)
        self.assertEqual(doctor.report_exit_code(), 0)

    def test_chars_and_bytes_differ_for_multibyte_text(self) -> None:
        (self.claude / "CLAUDE.md").write_text("가" * 100, encoding="utf-8")
        rows = self.source_rows(self.run_check())
        self.assertIn("100 chars / 300 B", rows["CLAUDE.md"])

    # --- over cap ---------------------------------------------------------------

    def test_over_cap_warns_and_names_biggest_source_with_delta(self) -> None:
        self.seed(claude_md=20000, cheatsheet=3000, style=2000)
        doctor = self.run_check()
        verdict = self.verdict(doctor)
        self.assertEqual(verdict.klass, "경고")
        self.assertIn("over by 1,000 chars", verdict.status)
        self.assertIn("cut 1,000 chars, largest first: CLAUDE.md -1,000 (of 20,000", verdict.action)
        self.assertNotIn("tools-cheatsheet.md -", verdict.action)
        self.assertIn("docs/harness.md", verdict.action)
        # 경고 never changes the exit code.
        self.assertEqual(doctor.report_exit_code(), 0)

    def test_delta_larger_than_biggest_source_spills_to_next(self) -> None:
        self.seed(claude_md=5000, cheatsheet=4000, style=1000)
        doctor = self.run_check({"CCC_ALWAYS_ON_MAX_CHARS": "3000"})
        action = self.verdict(doctor).action
        self.assertIn("cut 7,000 chars", action)
        self.assertIn("CLAUDE.md -5,000 (of 5,000", action)
        self.assertIn("tools-cheatsheet.md -2,000 (of 4,000", action)
        self.assertLess(action.index("CLAUDE.md"), action.index("tools-cheatsheet.md"))
        self.assertNotIn("ccc-report -", action)

    def test_strict_over_cap_fails(self) -> None:
        self.seed(claude_md=30000)
        doctor = self.run_check(strict=True)
        self.assertEqual(self.verdict(doctor).klass, "수동필요")
        self.assertEqual(doctor.report_exit_code(), 1)

    def test_strict_under_cap_stays_normal(self) -> None:
        self.seed(claude_md=100)
        doctor = self.run_check(strict=True)
        self.assertEqual(self.verdict(doctor).klass, "정상")
        self.assertEqual(doctor.report_exit_code(), 0)

    def test_cap_env_override_and_invalid_value_falls_back(self) -> None:
        self.seed(claude_md=1000)
        self.assertEqual(self.verdict(self.run_check({"CCC_ALWAYS_ON_MAX_CHARS": "500"})).klass, "경고")
        fallback = self.verdict(self.run_check({"CCC_ALWAYS_ON_MAX_CHARS": "lots"}))
        self.assertEqual(fallback.klass, "정상")
        self.assertIn("cap 24,000 chars", fallback.status)

    # --- missing / unwired sources ---------------------------------------------

    def test_missing_sources_are_absent_not_errors(self) -> None:
        doctor = self.run_check()
        rows = self.source_rows(doctor)
        self.assertIn("0 chars / 0 B; 0% of cap; absent", rows["CLAUDE.md"])
        self.assertIn("absent", rows["tools-cheatsheet.md"])
        self.assertIn("no-file", rows["output-style ccc-report"])
        self.assertEqual(self.verdict(doctor).klass, "정상")

    def test_unwired_hooks_are_not_counted(self) -> None:
        self.seed(cheatsheet=30000, memory=3000)
        self.write_settings({"outputStyle": "ccc-report", "hooks": {"SessionStart": []}})
        doctor = self.run_check()
        rows = self.source_rows(doctor)
        self.assertIn("not-wired", rows["tools-cheatsheet.md"])
        self.assertIn("not-wired", rows["memory snapshot"])
        self.assertEqual(self.verdict(doctor).klass, "정상")

    def test_output_style_resolved_by_frontmatter_name(self) -> None:
        (self.claude / "output-styles" / "renamed.md").write_text(
            "---\nname: ccc-report\n---\n" + "x" * 500, encoding="utf-8"
        )
        rows = self.source_rows(self.run_check())
        self.assertIn("measured", rows["output-style ccc-report"])

    def test_default_output_style_has_no_file(self) -> None:
        self.write_settings({"hooks": SETTINGS["hooks"]})
        rows = self.source_rows(self.run_check())
        self.assertIn("no-file (default style)", rows["output-style"])

    # --- memory snapshot estimate --------------------------------------------

    def test_snapshot_is_labelled_estimated_and_capped(self) -> None:
        self.seed(memory=10000, wiki=10000)
        doctor = self.run_check()
        snapshot = self.source_rows(doctor)["memory snapshot"]
        # MEMORY capped at 4,000 B + wiki at 5,000 B + one joining newline.
        self.assertIn("9,001 chars", snapshot)
        self.assertIn("estimated", snapshot)
        self.assertIn("(includes estimates)", self.verdict(doctor).status)
        capped = self.source_rows(self.run_check({"CCC_MEMORY_MAX_BYTES": "2000"}))["memory snapshot"]
        self.assertIn("2,000 chars", capped)

    def test_snapshot_skips_wiki_when_disabled(self) -> None:
        self.seed(memory=100, wiki=10000)
        snapshot = self.source_rows(self.run_check({"CCC_WIKI_MEMORY_ENABLED": "0"}))["memory snapshot"]
        self.assertIn("100 chars", snapshot)

    def test_snapshot_never_runs_load_memory(self) -> None:
        with patch.object(ccc_doctor.subprocess, "run", side_effect=AssertionError("hook executed")):
            self.run_check()

    # --- settings.local.json hooks: nunchi, canary, unknown ---------------------

    def write_local(self, commands: list[str]) -> None:
        (self.claude / "settings.local.json").write_text(json.dumps({
            "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": c} for c in commands]}]}
        }), encoding="utf-8")

    def arm_nunchi(self, snapshot: str = "가" * 10 + "a" * 10) -> Path:
        nunchi = self.home / ".nunchi"
        nunchi.mkdir(exist_ok=True)
        (nunchi / "snapshot.md").write_text(snapshot, encoding="utf-8")
        (self.claude / "state").mkdir(exist_ok=True)
        (self.claude / "state" / "nunchi.mode").write_text("on\n", encoding="utf-8")
        return nunchi

    def test_nunchi_hook_in_settings_local_is_counted_at_budget_ceiling(self) -> None:
        self.arm_nunchi()  # 20 chars / 40 bytes -> char/byte ratio 0.5
        self.write_local(["bash '/root/.claude/hooks/nunchi/sessionstart.sh'"])
        doctor = self.run_check()
        row = self.source_rows(doctor)["nunchi working memory"]
        self.assertIn("7,500 chars / 15,000 B", row)
        self.assertIn("estimated (budget ceiling)", row)
        self.assertIn("total 7,500 chars", self.verdict(doctor).status)

    def test_nunchi_budget_env_overrides(self) -> None:
        self.arm_nunchi("a" * 10)
        self.write_local(["bash /root/.claude/hooks/nunchi/sessionstart.sh"])
        row = self.source_rows(self.run_check({
            "CCC_NUNCHI_CONSTRAINT_BUDGET": "1000", "CCC_NUNCHI_ASSEMBLE_BUDGET": "2500",
        }))["nunchi working memory"]
        self.assertIn("3,500 chars / 3,500 B", row)
        legacy = self.source_rows(self.run_check({"CCC_NUNCHI_ASSEMBLE": "0"}))["nunchi working memory"]
        self.assertIn("10 chars", legacy)
        self.assertIn("cached (snapshot.md head -c 3000)", legacy)

    def test_nunchi_inactive_when_mode_off_or_no_snapshot(self) -> None:
        self.write_local(["bash /root/.claude/hooks/nunchi/sessionstart.sh"])
        self.assertIn("inactive (nunchi mode off)", self.source_rows(self.run_check())["nunchi working memory"])
        row = self.source_rows(self.run_check({"CCC_NUNCHI_MODE": "on"}))["nunchi working memory"]
        self.assertIn("inactive (no snapshot.md)", row)

    def test_nunchi_over_cap_names_it_in_the_cut_plan(self) -> None:
        self.arm_nunchi("a" * 10)
        self.seed(claude_md=15000)
        self.write_local(["bash /root/.claude/hooks/nunchi/sessionstart.sh"])
        verdict = self.verdict(self.run_check())
        self.assertEqual(verdict.klass, "경고")
        self.assertIn("cut 6,000 chars, largest first: CLAUDE.md -6,000", verdict.action)
        verdict = self.verdict(self.run_check({"CCC_ALWAYS_ON_MAX_CHARS": "5000"}))
        self.assertIn("nunchi working memory -", verdict.action)

    def test_unknown_stdout_hook_is_listed_unmeasured_not_counted(self) -> None:
        self.seed(claude_md=100)
        self.write_local([
            "bash /root/.claude/hooks/custom-banner.sh SessionStart",
            "bash /root/.claude/hooks/distill/pending-drain.sh >/dev/null 2>&1 &",
            "bash /root/.claude/hooks/quiet.sh 1>/dev/null",
            "bash /root/.claude/hooks/bg.sh &",
        ])
        doctor = self.run_check()
        rows = self.source_rows(doctor)
        self.assertIn("unmeasured hook custom-banner.sh", rows)
        self.assertIn("unmeasured (not counted)", rows["unmeasured hook custom-banner.sh"])
        for skipped in ("pending-drain.sh", "quiet.sh", "bg.sh"):
            self.assertNotIn(f"unmeasured hook {skipped}", rows)
        self.assertIn("total 100 chars", self.verdict(doctor).status)

    def test_stderr_only_redirect_still_counts_as_stdout_hook(self) -> None:
        self.write_local(["bash /root/.claude/hooks/noisy.sh 2>/dev/null"])
        self.assertIn("unmeasured hook noisy.sh", self.source_rows(self.run_check()))

    def test_hooks_merged_from_both_files_without_duplicates(self) -> None:
        self.seed(cheatsheet=500)
        self.write_settings({"outputStyle": "ccc-report", "hooks": {"SessionStart": []}})
        self.write_local(["bash /root/.claude/hooks/load-tools.sh SessionStart"])
        self.assertIn("500 chars", self.source_rows(self.run_check())["tools-cheatsheet.md"])

    def test_canary_counts_only_when_armed(self) -> None:
        self.write_settings({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "bash /root/.claude/hooks/report-style-canary.sh SessionStart"},
        ]}]}})
        rows = self.source_rows(self.run_check())
        self.assertIn("0 chars", rows["report-style canary"])
        self.assertIn("conditional (canary flag not armed)", rows["report-style canary"])
        (self.claude / "hooks" / "lib").mkdir()
        (self.claude / "hooks" / "lib" / "report-style-ste.txt").write_text("r" * 700, encoding="utf-8")
        (self.claude / "state").mkdir(exist_ok=True)
        flag = self.claude / "state" / "report-style-canary.flag"
        flag.write_text("", encoding="utf-8")
        flag.chmod(0o600)
        (self.claude / "hooks" / "lib" / "report-style-ste.txt").chmod(0o600)
        rows = self.source_rows(self.run_check())
        self.assertIn("700 chars", rows["report-style canary"])
        self.assertIn("conditional (canary flag armed)", rows["report-style canary"])

    # --- codex / other providers -----------------------------------------------

    def test_codex_splits_managed_memory_block(self) -> None:
        codex = self.home / ".codex"
        codex.mkdir()
        block = f"{ccc_doctor.CODEX_MEMORY_BEGIN_MARKER}\n{'b' * 3000}\n{ccc_doctor.CODEX_MEMORY_END_MARKER}"
        (codex / "AGENTS.md").write_text("a" * 6000 + "\n" + block + "\n", encoding="utf-8")
        doctor = self.run_check({"CCC_AGENT_PROVIDER": "codex", "CODEX_HOME": str(codex)})
        rows = self.source_rows(doctor)
        self.assertIn("6,002 chars", rows["AGENTS.md"])
        self.assertIn("memory snapshot (managed block)", rows)
        verdict = self.verdict(doctor)
        self.assertEqual(verdict.klass, "경고")
        self.assertIn("cap 8,000 chars", verdict.status)
        self.assertIn("AGENTS.md -", verdict.action)

    def test_codex_override_file_wins_when_non_empty(self) -> None:
        codex = self.home / ".codex"
        codex.mkdir()
        (codex / "AGENTS.md").write_text("a" * 50, encoding="utf-8")
        (codex / "AGENTS.override.md").write_text("o" * 70, encoding="utf-8")
        rows = self.source_rows(self.run_check({"CCC_AGENT_PROVIDER": "codex", "CODEX_HOME": str(codex)}))
        self.assertIn("70 chars", rows["AGENTS.override.md"])

    def test_codex_markers_mirror_materializer(self) -> None:
        source = (Path(__file__).resolve().parent / "ccc_codex_memory.py").read_text(encoding="utf-8")
        self.assertIn(f'BEGIN_MARKER = "{ccc_doctor.CODEX_MEMORY_BEGIN_MARKER}"', source)
        self.assertIn(f'END_MARKER = "{ccc_doctor.CODEX_MEMORY_END_MARKER}"', source)

    def test_unknown_provider_is_not_measured(self) -> None:
        doctor = self.run_check({"CCC_AGENT_PROVIDER": "piri"})
        verdict = self.verdict(doctor)
        self.assertEqual(verdict.klass, "정상")
        self.assertIn("not-measured", verdict.status)

    # --- CLI ---------------------------------------------------------------

    def test_summary_flag_prints_one_line_and_strict_exit(self) -> None:
        self.seed(claude_md=30000)
        env = {"HOME": str(self.home), "CCC_DOCTOR_CLAUDE_DIR": str(self.claude), "CCC_AGENT_PROVIDER": "claude"}
        for argv, rc in ((["--always-on-summary"], 0), (["--always-on-summary", "--strict"], 1)):
            out = io.StringIO()
            with patch.dict("os.environ", env, clear=True), redirect_stdout(out):
                self.assertEqual(ccc_doctor.main(argv), rc)
            lines = out.getvalue().strip().splitlines()
            self.assertEqual(len(lines), 1, lines)
            self.assertTrue(lines[0].startswith("always-on: OVER"), lines[0])
            self.assertIn("CLAUDE.md -6,000", lines[0])
        self.assertTrue(os.path.isfile(self.claude / "CLAUDE.md"))


if __name__ == "__main__":
    unittest.main()
