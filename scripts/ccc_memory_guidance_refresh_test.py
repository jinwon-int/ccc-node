#!/usr/bin/env python3
"""Hermetic tests for ``setup.sh --refresh-guidance`` (#2238 item 2).

The refresh rewrites node-owned guidance, so the contract is narrow: back up,
replace only the marker-delimited Memory Stack block — or, on a legacy file
without markers, a span that is stale *and* equals a known old template version
— plus exact known stale lines; refuse everything else untouched; keep every
node-specific section; and be a no-op on a second run.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import io
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ccc_doctor  # noqa: E402
import ccc_memory_guidance as mg  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "stale-memory-guidance"
CLAUDE_TEMPLATE = (REPO / "claude" / "CLAUDE.md.template").read_text(encoding="utf-8")
MEMORY_TEMPLATE = (REPO / "hermes" / "memories" / "MEMORY.template.md").read_text(encoding="utf-8")

NODE_TOP = (
    "## Node Notes (fixture-node)\n- Local bridge runs under systemd --user; keep this line.\n\n"
)
NODE_BOTTOM = "\n## Node Runbook (fixture-node)\n- Restart order: bridge, then poller.\n"


def _block_text() -> str:
    return "\n".join(mg.template_block(CLAUDE_TEMPLATE))


def current_node_claude() -> str:
    """The current template with node-specific sections, as a refreshed node should read."""
    text = CLAUDE_TEMPLATE.replace(
        "## Wiki-first Operating Policy", NODE_TOP + "## Wiki-first Operating Policy", 1
    )
    return text + NODE_BOTTOM


def legacy_node_claude() -> str:
    """A node seeded from the pre-#2163 template: no markers, old span, old reads row."""
    text = current_node_claude()
    text = text.replace(_block_text(), mg.KNOWN_STALE_MEMORY_SPANS[0], 1)
    new_row = next(
        line for line in CLAUDE_TEMPLATE.splitlines() if line.startswith("| Wiki / memory reads |")
    )
    return text.replace(new_row, mg.CLAUDE_KNOWN_LINES[0].old, 1)


def sha(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


class TemplateMarkers(unittest.TestCase):
    def test_template_has_one_marker_pair_around_the_memory_span(self) -> None:
        self.assertEqual(CLAUDE_TEMPLATE.count(mg.MARKER_BEGIN), 1)
        self.assertEqual(CLAUDE_TEMPLATE.count(mg.MARKER_END), 1)
        block = mg.template_block(CLAUDE_TEMPLATE)
        self.assertEqual(block[1], mg.MEMORY_STACK_START)
        inner = "\n".join(block[1:-1]).strip()
        self.assertEqual(inner, mg.memory_stack_span(CLAUDE_TEMPLATE))

    def test_known_stale_variants_are_stale_and_differ_from_template(self) -> None:
        for span in mg.KNOWN_STALE_MEMORY_SPANS:
            self.assertTrue(mg.stale_memory_guidance_hits(span))
            self.assertNotEqual(span, mg.memory_stack_span(CLAUDE_TEMPLATE))
        for known in mg.CLAUDE_KNOWN_LINES + mg.MEMORY_KNOWN_LINES:
            self.assertTrue(mg.stale_memory_guidance_hits(known.old), known.old)

    def test_old_fixture_span_is_a_known_variant(self) -> None:
        """The doctor's stale fixture was cut from 89314d75^; its span must be recognized."""
        fixture = (FIXTURES / "stale-CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn(
            mg.memory_stack_span(fixture), [s.strip() for s in mg.KNOWN_STALE_MEMORY_SPANS]
        )

    def test_doctor_reuses_the_shared_detection(self) -> None:
        self.assertIs(ccc_doctor.stale_memory_guidance_hits, mg.stale_memory_guidance_hits)
        self.assertIs(ccc_doctor.memory_stack_span, mg.memory_stack_span)


class RefreshText(unittest.TestCase):
    """Pure text transforms."""

    def test_legacy_stale_file_becomes_current_template_with_node_sections(self) -> None:
        out = mg.refresh_claude_md(legacy_node_claude(), CLAUDE_TEMPLATE)
        self.assertFalse(out.refused)
        self.assertEqual(out.text, current_node_claude())
        self.assertEqual(out.remaining, [])
        self.assertEqual(len(out.changes), 2, out.changes)

    def test_customized_stale_span_is_refused(self) -> None:
        text = legacy_node_claude().replace(
            "**Routing:** durable facts",
            "- Node note: recall via the local proxy.\n\n**Routing:** durable facts",
            1,
        )
        out = mg.refresh_claude_md(text, CLAUDE_TEMPLATE)
        self.assertIn("differs from every known template version", out.refused)
        self.assertEqual(out.text, text)
        self.assertTrue(out.remaining)

    def test_unknown_clean_span_is_refused(self) -> None:
        text = current_node_claude().replace(
            _block_text(), "## Memory Stack\n- Local layout, hand-written.\n", 1
        )
        out = mg.refresh_claude_md(text, CLAUDE_TEMPLATE)
        self.assertTrue(out.refused)
        self.assertEqual(out.text, text)

    def test_marker_block_is_replaced_and_outside_text_kept(self) -> None:
        stale_block = "\n".join(
            [
                mg.MARKER_BEGIN,
                "## Memory Stack",
                "- Current stack: MEMORY + Honcho + Wiki.",
                mg.MARKER_END,
            ]
        )
        text = current_node_claude().replace(_block_text(), stale_block, 1)
        out = mg.refresh_claude_md(text, CLAUDE_TEMPLATE)
        self.assertFalse(out.refused)
        self.assertEqual(out.text, current_node_claude())
        self.assertIn(NODE_TOP, out.text)
        self.assertTrue(out.text.endswith(NODE_BOTTOM))

    def test_unbalanced_markers_are_refused(self) -> None:
        text = current_node_claude().replace(mg.MARKER_END + "\n", "", 1)
        out = mg.refresh_claude_md(text, CLAUDE_TEMPLATE)
        self.assertIn("unbalanced", out.refused)
        self.assertEqual(out.text, text)

    def test_current_files_are_no_ops(self) -> None:
        current = current_node_claude()
        self.assertEqual(mg.refresh_claude_md(current, CLAUDE_TEMPLATE).text, current)
        # Hand-fixed to the current text without markers: nothing stale to replace.
        hand_fixed = current.replace(mg.MARKER_BEGIN + "\n", "").replace(mg.MARKER_END + "\n", "")
        out = mg.refresh_claude_md(hand_fixed, CLAUDE_TEMPLATE)
        self.assertEqual((out.text, out.refused, out.changes), (hand_fixed, "", []))
        clean_memory = (FIXTURES / "clean-MEMORY.md").read_text(encoding="utf-8")
        self.assertEqual(mg.refresh_memory_md(clean_memory, MEMORY_TEMPLATE).text, clean_memory)

    def test_memory_line_rewritten_only_on_exact_match(self) -> None:
        stale = (FIXTURES / "stale-MEMORY.md").read_text(encoding="utf-8")
        out = mg.refresh_memory_md(stale, MEMORY_TEMPLATE)
        self.assertFalse(out.refused)
        self.assertEqual(out.remaining, [])
        self.assertIn("Node-specific fact: the bridge runs under systemd --user.", out.text)
        self.assertIn("+ nunchi +", out.text)
        variant = (FIXTURES / "stale-hermes-MEMORY.md").read_text(encoding="utf-8")
        out = mg.refresh_memory_md(variant, MEMORY_TEMPLATE)
        self.assertIn("do not match the known template line exactly", out.refused)
        self.assertEqual(out.text, variant)


class RefreshFiles(unittest.TestCase):
    """run_refresh on a scratch node: backups, refusals, idempotency, dry-run."""

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        base = Path(self._temp.name)
        self.claude = base / ".claude"
        self.hermes = base / ".hermes"
        (self.claude / "memories").mkdir(parents=True)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def run_refresh(self, dry_run: bool = False) -> tuple[int, str]:
        buf = io.StringIO()
        rc = mg.run_refresh(REPO, self.claude, self.hermes, dry_run=dry_run, out=buf)
        return rc, buf.getvalue()

    def backups(self, path: Path) -> list[Path]:
        return sorted(path.parent.glob(path.name + ".bak-refresh-guidance-*"))

    def test_stale_legacy_node_is_refreshed_with_backups(self) -> None:
        claude_md = self.claude / "CLAUDE.md"
        memory_md = self.claude / "memories" / "MEMORY.md"
        claude_md.write_text(legacy_node_claude(), encoding="utf-8")
        shutil.copyfile(FIXTURES / "stale-MEMORY.md", memory_md)
        claude_md.chmod(0o640)
        rc, out = self.run_refresh()
        self.assertEqual(rc, 0, out)
        self.assertEqual(claude_md.read_text(encoding="utf-8"), current_node_claude())
        self.assertEqual(claude_md.stat().st_mode & 0o777, 0o640)
        [claude_bak] = self.backups(claude_md)
        [memory_bak] = self.backups(memory_md)
        self.assertRegex(claude_bak.name, r"\.bak-refresh-guidance-\d{8}T\d{6}Z$")
        self.assertEqual(claude_bak.read_text(encoding="utf-8"), legacy_node_claude())
        self.assertEqual(memory_bak.read_bytes(), (FIXTURES / "stale-MEMORY.md").read_bytes())
        self.assertEqual(mg.stale_memory_guidance_hits(memory_md.read_text(encoding="utf-8")), [])
        self.assertNotIn("Family Wiki", out)  # never echoes guidance text

    def test_second_run_is_a_no_op(self) -> None:
        claude_md = self.claude / "CLAUDE.md"
        claude_md.write_text(legacy_node_claude(), encoding="utf-8")
        self.assertEqual(self.run_refresh()[0], 0)
        before = sha(claude_md)
        rc, out = self.run_refresh()
        self.assertEqual(rc, 0, out)
        self.assertIn("current", out)
        self.assertEqual(sha(claude_md), before)
        self.assertEqual(len(self.backups(claude_md)), 1)

    def test_customized_span_is_refused_untouched(self) -> None:
        claude_md = self.claude / "CLAUDE.md"
        text = legacy_node_claude().replace(
            "- Cleanup policy:", "- Node override: keep Honcho notes.\n- Cleanup policy:", 1
        )
        claude_md.write_text(text, encoding="utf-8")
        before = sha(claude_md)
        rc, out = self.run_refresh()
        self.assertEqual(rc, 1)
        self.assertIn("refused", out)
        self.assertEqual(sha(claude_md), before)
        self.assertEqual(self.backups(claude_md), [])

    def test_marker_file_span_replaced(self) -> None:
        claude_md = self.claude / "CLAUDE.md"
        stale_block = "\n".join(
            [mg.MARKER_BEGIN, "## Memory Stack", "- old managed text", mg.MARKER_END]
        )
        claude_md.write_text(
            current_node_claude().replace(_block_text(), stale_block, 1), encoding="utf-8"
        )
        rc, out = self.run_refresh()
        self.assertEqual(rc, 0, out)
        self.assertIn("marker block", out)
        self.assertEqual(claude_md.read_text(encoding="utf-8"), current_node_claude())
        self.assertEqual(len(self.backups(claude_md)), 1)

    def test_dry_run_writes_nothing(self) -> None:
        claude_md = self.claude / "CLAUDE.md"
        claude_md.write_text(legacy_node_claude(), encoding="utf-8")
        before = sha(claude_md)
        rc, out = self.run_refresh(dry_run=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("would", out)
        self.assertEqual(sha(claude_md), before)
        self.assertEqual(self.backups(claude_md), [])

    def test_unmatched_hermes_memory_refused_while_others_refresh(self) -> None:
        memory_md = self.claude / "memories" / "MEMORY.md"
        shutil.copyfile(FIXTURES / "stale-MEMORY.md", memory_md)
        hermes_md = self.hermes / "memories" / "MEMORY.md"
        hermes_md.parent.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "stale-hermes-MEMORY.md", hermes_md)
        before = sha(hermes_md)
        rc, out = self.run_refresh()
        self.assertEqual(rc, 1)
        self.assertEqual(sha(hermes_md), before)
        self.assertIn("refreshed memories/MEMORY.md", out)
        self.assertIn("refused   hermes:memories/MEMORY.md", out)

    def test_symlinked_memory_is_refreshed_once_and_link_kept(self) -> None:
        hermes_md = self.hermes / "memories" / "MEMORY.md"
        hermes_md.parent.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "stale-MEMORY.md", hermes_md)
        link = self.claude / "memories" / "MEMORY.md"
        link.symlink_to(hermes_md)
        rc, out = self.run_refresh()
        self.assertEqual(rc, 0, out)
        self.assertTrue(link.is_symlink())
        self.assertEqual(len(self.backups(hermes_md)), 1)
        self.assertIn("same-file", out)
        self.assertEqual(mg.stale_memory_guidance_hits(hermes_md.read_text(encoding="utf-8")), [])

    def test_no_files_is_clean(self) -> None:
        rc, out = self.run_refresh()
        self.assertEqual(rc, 0)
        self.assertIn("absent", out)


class SetupFlag(unittest.TestCase):
    """setup.sh --refresh-guidance dispatches to the refresh and installs nothing else."""

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.home = Path(self._temp.name) / "home"
        (self.home / ".claude").mkdir(parents=True)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def setup_sh(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("CCC_")}
        env.update(
            HOME=str(self.home),
            CCC_CLAUDE_DIR=str(self.home / ".claude"),
            CCC_HERMES_DIR=str(self.home / ".hermes"),
        )
        return subprocess.run(
            ["bash", str(REPO / "setup.sh"), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_flag_refreshes_only_guidance(self) -> None:
        claude_md = self.home / ".claude" / "CLAUDE.md"
        claude_md.write_text(legacy_node_claude(), encoding="utf-8")
        dry = self.setup_sh("--refresh-guidance", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("dry-run", dry.stdout)
        self.assertEqual(claude_md.read_text(encoding="utf-8"), legacy_node_claude())
        done = self.setup_sh("--refresh-guidance")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(claude_md.read_text(encoding="utf-8"), current_node_claude())
        # Standalone mode: no settings.json, hooks or seeded templates appear.
        names = sorted(p.name for p in (self.home / ".claude").iterdir())
        self.assertEqual(names[0], "CLAUDE.md")
        self.assertTrue(all(n.startswith("CLAUDE.md") for n in names), names)

    def test_flag_refuses_install_flags(self) -> None:
        res = self.setup_sh("--refresh-guidance", "--with-plugin")
        self.assertEqual(res.returncode, 2)
        self.assertIn("runs alone", res.stderr)


if __name__ == "__main__":
    unittest.main()
