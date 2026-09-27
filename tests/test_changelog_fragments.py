"""Changelog fragments (#2022): validation, ordering, assembly and the release guard."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "changelog_fragments.py"
_spec = importlib.util.spec_from_file_location("changelog_fragments", SCRIPT)
assert _spec is not None and _spec.loader is not None
cf = importlib.util.module_from_spec(_spec)
sys.modules["changelog_fragments"] = cf
_spec.loader.exec_module(cf)

HARNESS = "# Changelog — ccc-node harness\n\nIntro.\n\n## [Unreleased]\n\n- **Old entry (#1).**\n\n## [0.6.0] — 2026-09-24\n\n- shipped\n"
BRIDGE = "# Changelog\n\n- **Old bridge entry (#2).**\n\n## [Unreleased]\n"


def _tree(root: Path) -> None:
    (root / "changelog.d").mkdir()
    (root / "bridge" / "changelog.d").mkdir(parents=True)
    (root / "CHANGELOG.md").write_text(HARNESS, encoding="utf-8")
    (root / "bridge" / "CHANGELOG.md").write_text(BRIDGE, encoding="utf-8")
    (root / "changelog.d" / "README.md").write_text("# docs, not a fragment\n", encoding="utf-8")


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


class FragmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _tree(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.write_text(text, encoding="utf-8")
        return path

    def test_apply_inserts_newest_first_under_each_anchor_and_deletes_fragments(self) -> None:
        self.write("changelog.d/10-older.md", "- **Older (#10).**\n")
        self.write("changelog.d/2022-newer.md", "- **Newer (#2022).**\n  continued line\n")
        self.write("bridge/changelog.d/2001-files.md", "- **Files (#2001).**\n")
        result = _run(self.root, "apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        harness = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(
            "## [Unreleased]\n\n- **Newer (#2022).**\n  continued line\n\n- **Older (#10).**\n\n- **Old entry (#1).**\n",
            harness,
        )
        self.assertIn("## [0.6.0] — 2026-09-24", harness)
        bridge = (self.root / "bridge" / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertTrue(bridge.startswith("# Changelog\n\n- **Files (#2001).**\n\n- **Old bridge entry (#2).**\n"))
        remaining = sorted(p.name for p in (self.root / "changelog.d").iterdir())
        self.assertEqual(remaining, ["README.md"], "fragments are consumed, the README stays")
        self.assertEqual(list((self.root / "bridge" / "changelog.d").iterdir()), [])

    def test_apply_with_nothing_pending_changes_nothing(self) -> None:
        result = _run(self.root, "apply")
        self.assertEqual(result.returncode, 0)
        self.assertEqual((self.root / "CHANGELOG.md").read_text(encoding="utf-8"), HARNESS)

    def test_check_rejects_malformed_fragments(self) -> None:
        cases = {
            "changelog.d/no-number.md": "- entry\n",
            "changelog.d/12-Upper.md": "- entry\n",
            "changelog.d/13-heading.md": "- entry\n## [0.7.0]\n",
            "changelog.d/14-prose.md": "Not a bullet.\n",
            "changelog.d/15-empty.md": "\n\n",
            "changelog.d/16-conflict.md": "- a\n<<<<<<< HEAD\n",
            "changelog.d/17-entry.txt": "- wrong suffix\n",
        }
        for rel, text in cases.items():
            with self.subTest(rel=rel):
                path = self.write(rel, text)
                result = _run(self.root, "check")
                self.assertEqual(result.returncode, 1, f"{rel} should fail: {result.stdout}")
                path.unlink()

    def test_a_failing_fragment_writes_nothing(self) -> None:
        self.write("changelog.d/20-good.md", "- good\n")
        self.write("bridge/changelog.d/21-bad.md", "no bullet\n")
        result = _run(self.root, "apply")
        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.root / "CHANGELOG.md").read_text(encoding="utf-8"), HARNESS)
        self.assertTrue((self.root / "changelog.d" / "20-good.md").exists())

    def test_none_pending_guards_a_release(self) -> None:
        self.assertEqual(_run(self.root, "check", "--none-pending").returncode, 0)
        self.write("changelog.d/30-pending.md", "- pending\n")
        self.assertEqual(_run(self.root, "check").returncode, 0)
        self.assertEqual(_run(self.root, "check", "--none-pending").returncode, 1)

    def test_a_missing_anchor_is_an_error(self) -> None:
        (self.root / "CHANGELOG.md").write_text("# Changelog\n\nno unreleased heading\n", encoding="utf-8")
        self.write("changelog.d/40-x.md", "- x\n")
        self.assertEqual(_run(self.root, "apply").returncode, 1)


class RepositoryTests(unittest.TestCase):
    def test_the_repository_fragments_are_valid(self) -> None:
        result = _run(REPO_ROOT, "check")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_both_changelogs_still_carry_their_anchor(self) -> None:
        for target in cf.targets(REPO_ROOT):
            with self.subTest(target=target.label):
                text = target.changelog.read_text(encoding="utf-8")
                self.assertTrue(
                    any(target.anchor.match(line) for line in text.splitlines()),
                    f"{target.changelog} lost its insertion anchor",
                )

    def test_the_release_workflow_refuses_pending_fragments(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("changelog_fragments.py check --none-pending", workflow)


if __name__ == "__main__":
    unittest.main()
