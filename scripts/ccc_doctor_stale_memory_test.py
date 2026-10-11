#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's stale-memory-guidance check (#2238).

setup.sh seeds ~/.claude/CLAUDE.md and the MEMORY.md files once and never
overwrites them, so nodes seeded before the Honcho retirement (2026-09-01,
#1436) kept presenting Honcho as the current memory stack: recall endpoint,
``~/.hermes/honcho.json``, Routing -> Honcho. These tests pin that the doctor
flags those shapes, stays quiet on text that names Honcho as retired, and
never rewrites the files.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import hashlib
import shutil
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import Doctor, memory_stack_span, stale_memory_guidance_hits  # noqa: E402

ITEM = "stale-memory-guidance"
REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "stale-memory-guidance"


def labels_of(text: str) -> set[str]:
    return {label for _, found in stale_memory_guidance_hits(text) for label in found}


class StaleMemoryPatterns(unittest.TestCase):
    """The scanner on its own: stale shapes flag, retirement notes do not."""

    def test_stale_claude_md_flags_every_old_template_shape(self) -> None:
        text = (FIXTURES / "stale-CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(
            labels_of(text),
            {
                "stack-list",
                "cached-layer",
                "layer-heading",
                "recall-endpoint",
                "honcho-config",
                "routing",
                "current-stack",
                "read-source",
            },
        )

    def test_issue_counted_patterns_hit_three_lines_in_claude_md(self) -> None:
        """The issue's survey (`+ Honcho +`, `cached Honcho`, `Honcho — working`) counted 3."""
        text = (FIXTURES / "stale-CLAUDE.md").read_text(encoding="utf-8")
        survey = [
            n
            for n, found in stale_memory_guidance_hits(text)
            if {"stack-list", "cached-layer", "layer-heading"} & set(found)
        ]
        self.assertEqual(len(survey), 3, survey)

    def test_old_memory_line_flags_despite_supermemory_retired_sentence(self) -> None:
        """'Supermemory is retired.' must not exempt the next sentence's '+ Honcho +'."""
        hits = stale_memory_guidance_hits((FIXTURES / "stale-MEMORY.md").read_text(encoding="utf-8"))
        self.assertEqual(len(hits), 1, hits)
        self.assertIn("stack-list", hits[0][1])
        self.assertIn("current-stack", hits[0][1])

    def test_korean_hermes_memory_flags(self) -> None:
        hits = stale_memory_guidance_hits((FIXTURES / "stale-hermes-MEMORY.md").read_text(encoding="utf-8"))
        self.assertEqual([n for n, _ in hits], [2, 3], hits)

    def test_clean_fixtures_have_no_hits(self) -> None:
        for name in ("clean-CLAUDE.md", "clean-MEMORY.md"):
            with self.subTest(name=name):
                text = (FIXTURES / name).read_text(encoding="utf-8")
                self.assertEqual(stale_memory_guidance_hits(text), [])

    def test_current_repo_templates_are_clean(self) -> None:
        """The remediation source itself must never trip the check."""
        for rel in ("claude/CLAUDE.md.template", "hermes/memories/MEMORY.template.md"):
            with self.subTest(rel=rel):
                text = (REPO / rel).read_text(encoding="utf-8")
                self.assertEqual(stale_memory_guidance_hits(text), [])

    def test_retired_mentions_are_exempt(self) -> None:
        for line in (
            "- **Honcho is retired fleet-wide** — do not query it or treat a missing `~/.hermes/honcho.json` as a fault.",
            "- Honcho 퇴역(2026-09-01): cached Honcho 스냅샷은 더 이상 없다.",
            "- Legacy stack (MEMORY + Honcho + Wiki) was removed in #1436.",
            "- Routing → Honcho is deprecated; use nunchi.",
        ):
            with self.subTest(line=line):
                self.assertEqual(stale_memory_guidance_hits(line), [])

    def test_unrelated_arrows_and_pluses_are_ignored(self) -> None:
        text = "- Durable update: `wiki-agent write-path` → edit → `wiki-agent pr`.\n- MEMORY + nunchi + Wiki.\n"
        self.assertEqual(stale_memory_guidance_hits(text), [])

    def test_memory_span_matches_template_and_detects_old_span(self) -> None:
        template = memory_stack_span((REPO / "claude/CLAUDE.md.template").read_text(encoding="utf-8"))
        self.assertIsNotNone(template)
        clean = memory_stack_span((FIXTURES / "clean-CLAUDE.md").read_text(encoding="utf-8"))
        stale = memory_stack_span((FIXTURES / "stale-CLAUDE.md").read_text(encoding="utf-8"))
        self.assertEqual(clean, template)
        self.assertNotEqual(stale, template)
        self.assertIsNone(memory_stack_span("## Memory Stack\n- no end heading\n"))


class StaleMemoryGuidanceCheck(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.base = Path(self._temp.name)
        self.claude_dir = self.base / ".claude"
        self.claude_dir.mkdir()
        self.hermes = self.base / ".hermes"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def place(self, fixture: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / fixture, dest)
        return dest

    def run_check(self, env: dict[str, str] | None = None) -> Doctor:
        doctor = Doctor(REPO, self.claude_dir, "settings")
        with patch.dict("os.environ", env or {}, clear=True):
            doctor.check_stale_memory_guidance()
        return doctor

    def row(self, doctor: Doctor):
        rows = [r for r in doctor.rows if r.item == ITEM]
        self.assertEqual(len(rows), 1, [r.item for r in doctor.rows])
        return rows[0]

    def test_no_files_is_not_applicable(self) -> None:
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "정상")
        self.assertIn("해당 없음", row.status)

    def test_clean_node_is_normal_and_span_matches_template(self) -> None:
        self.place("clean-CLAUDE.md", self.claude_dir / "CLAUDE.md")
        self.place("clean-MEMORY.md", self.claude_dir / "memories" / "MEMORY.md")
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "정상", row.status)
        self.assertIn("files=2; stale=0", row.status)
        self.assertIn("CLAUDE.md memory-span=template", row.status)

    def test_stale_node_warns_with_lines_labels_and_remedy(self) -> None:
        self.place("stale-CLAUDE.md", self.claude_dir / "CLAUDE.md")
        self.place("stale-MEMORY.md", self.claude_dir / "memories" / "MEMORY.md")
        self.place("stale-hermes-MEMORY.md", self.hermes / "memories" / "MEMORY.md")
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "경고", row.status)
        self.assertIn("in 3 file(s)", row.status)
        self.assertIn("CLAUDE.md:L", row.status)
        self.assertIn("memories/MEMORY.md:L3(stack-list/current-stack)", row.status)
        self.assertIn("hermes:memories/MEMORY.md:L2,L3", row.status)
        self.assertIn("CLAUDE.md memory-span=differs", row.status)
        # Line numbers and labels only: never echo the guidance text itself.
        self.assertNotIn("Family Wiki", row.status)
        self.assertIn("## Memory Stack", row.action)
        self.assertIn("## Fresh Approval Required", row.action)
        self.assertIn("claude/CLAUDE.md.template", row.action)
        self.assertIn("hermes/memories/MEMORY.template.md", row.action)
        self.assertIn("ccc-node#2238", row.action)

    def test_check_never_rewrites_files(self) -> None:
        paths = [
            self.place("stale-CLAUDE.md", self.claude_dir / "CLAUDE.md"),
            self.place("stale-MEMORY.md", self.claude_dir / "memories" / "MEMORY.md"),
        ]
        before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in paths}
        self.run_check()
        after = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in paths}
        self.assertEqual(before, after)
        self.assertEqual(sorted(p.name for p in self.claude_dir.rglob("*") if p.is_file()), ["CLAUDE.md", "MEMORY.md"])

    def test_only_hermes_memory_stale_still_warns(self) -> None:
        """daegyo shape: Claude files clean, Hermes MEMORY.md stale."""
        self.place("clean-CLAUDE.md", self.claude_dir / "CLAUDE.md")
        self.place("stale-hermes-MEMORY.md", self.hermes / "memories" / "MEMORY.md")
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "경고", row.status)
        self.assertIn("in 1 file(s); hermes:memories/MEMORY.md", row.status)

    def test_hermes_home_override(self) -> None:
        other = self.base / "elsewhere"
        self.place("stale-hermes-MEMORY.md", other / "memories" / "MEMORY.md")
        self.assertEqual(self.row(self.run_check()).klass, "정상")
        row = self.row(self.run_check({"CCC_DOCTOR_HERMES_HOME": str(other)}))
        self.assertEqual(row.klass, "경고", row.status)

    def test_customized_clean_span_reports_differs_but_stays_normal(self) -> None:
        dest = self.place("clean-CLAUDE.md", self.claude_dir / "CLAUDE.md")
        dest.write_text(
            dest.read_text(encoding="utf-8").replace(
                "## Fresh Approval Required", "- Node note: nunchi runs in shadow mode.\n\n## Fresh Approval Required"
            ),
            encoding="utf-8",
        )
        row = self.row(self.run_check())
        self.assertEqual(row.klass, "정상", row.status)
        self.assertIn("memory-span=differs", row.status)

    def test_unreadable_file_is_reported_not_fatal(self) -> None:
        self.place("clean-MEMORY.md", self.claude_dir / "memories" / "MEMORY.md")
        self.place("clean-CLAUDE.md", self.claude_dir / "CLAUDE.md")
        real_open = Path.open

        def flaky_open(path: Path, *args, **kwargs):
            if path.name == "CLAUDE.md":
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)

        with patch.object(Path, "open", flaky_open):
            row = self.row(self.run_check())
        self.assertEqual(row.klass, "정상", row.status)
        self.assertIn("unreadable=CLAUDE.md", row.status)


if __name__ == "__main__":
    unittest.main()
