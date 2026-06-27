"""Direct unit tests for the extracted path-scope classification (core/paths.py).

This is the security-relevant logic that decides which tool-call paths fall
outside PROJECT_ROOT and therefore require explicit user approval. Before the
extraction it lived on the TelegramBot god object with no direct tests. These
tests pin the behavior against an arbitrary temp project root.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from telegram_bot.core import paths


class IsWithinProjectRootTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)

    def test_inside(self):
        self.assertTrue(paths.is_within_project_root(self.root / "a" / "b.txt", self.root))

    def test_the_root_itself(self):
        self.assertTrue(paths.is_within_project_root(self.root, self.root))

    def test_outside(self):
        self.assertFalse(paths.is_within_project_root(Path("/etc/passwd"), self.root))

    def test_dotdot_escape_is_outside(self):
        escaped = self.root / ".." / "sibling" / "x"
        self.assertFalse(paths.is_within_project_root(escaped, self.root))


class ResolveCandidatePathTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)

    def test_relative_resolves_against_root(self):
        self.assertEqual(
            paths.resolve_candidate_path("sub/file.txt", self.root),
            self.root / "sub" / "file.txt",
        )

    def test_absolute_kept(self):
        self.assertEqual(
            paths.resolve_candidate_path("/etc/hosts", self.root), Path("/etc/hosts")
        )

    def test_strips_surrounding_quotes(self):
        self.assertEqual(
            paths.resolve_candidate_path('"sub/x"', self.root), self.root / "sub" / "x"
        )


class IterStringsTest(unittest.TestCase):
    def test_nested_structure(self):
        value = {"a": "one", "b": ["two", {"c": "three"}], "d": ("four",)}
        self.assertEqual(sorted(paths.iter_strings(value)), ["four", "one", "three", "two"])

    def test_plain_string(self):
        self.assertEqual(list(paths.iter_strings("solo")), ["solo"])


class ExtractPathsFromCommandTest(unittest.TestCase):
    def test_picks_path_like_tokens(self):
        got = paths.extract_paths_from_command("cat /etc/passwd ./local ../up file")
        self.assertIn("/etc/passwd", got)
        self.assertIn("./local", got)
        self.assertIn("../up", got)
        self.assertNotIn("file", got)  # no slash, not path-like
        self.assertNotIn("cat", got)

    def test_skips_flags_and_urls(self):
        got = paths.extract_paths_from_command("curl -X GET https://e.com/a /tmp/out")
        self.assertNotIn("-X", got)
        self.assertNotIn("https://e.com/a", got)
        self.assertIn("/tmp/out", got)

    def test_malformed_quotes_fall_back_to_split(self):
        # Unbalanced quote makes shlex.split raise; falls back to .split(), which
        # keeps the raw (still-quoted) token. Quote stripping happens later in
        # resolve_candidate_path, not here.
        got = paths.extract_paths_from_command('echo "/tmp/x')
        self.assertIn('"/tmp/x', got)


class ExtractPathCandidatesTest(unittest.TestCase):
    def test_keyword_keyed_strings(self):
        tool_input = {"file_path": "/a/b.txt", "note": "not a path"}
        got = paths.extract_path_candidates("Read", tool_input)
        self.assertEqual(got, ["/a/b.txt"])

    def test_bash_command_paths(self):
        tool_input = {"command": "cat /etc/shadow"}
        got = paths.extract_path_candidates("Bash", tool_input)
        self.assertIn("/etc/shadow", got)

    def test_dedupes(self):
        tool_input = {"command": "cat /etc/x /etc/x"}
        got = paths.extract_path_candidates("Bash", tool_input)
        self.assertEqual(got.count("/etc/x"), 1)


class ExtractOutsidePathsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)

    def test_unguarded_tool_returns_empty(self):
        self.assertEqual(
            paths.extract_outside_paths(
                "WebFetch", {"file_path": "/etc/passwd"}, project_root=self.root
            ),
            [],
        )

    def test_inside_path_not_flagged(self):
        inside = str(self.root / "sub" / "f.txt")
        self.assertEqual(
            paths.extract_outside_paths(
                "Read", {"file_path": inside}, project_root=self.root
            ),
            [],
        )

    def test_outside_path_flagged(self):
        got = paths.extract_outside_paths(
            "Read", {"file_path": "/etc/passwd"}, project_root=self.root
        )
        self.assertEqual(got, ["/etc/passwd"])

    def test_relative_inside_is_safe(self):
        # Relative path resolves under root -> not flagged.
        got = paths.extract_outside_paths(
            "Edit", {"file_path": "sub/inside.txt"}, project_root=self.root
        )
        self.assertEqual(got, [])

    def test_bash_escape_flagged(self):
        got = paths.extract_outside_paths(
            "Bash", {"command": "cat /etc/passwd"}, project_root=self.root
        )
        self.assertEqual(got, ["/etc/passwd"])


class SplitPathsByScopeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)

    def test_partition(self):
        inside = self.root / "a.txt"
        outside = Path("/etc/hosts")
        in_root, out = paths.split_paths_by_scope([inside, outside], self.root)
        self.assertEqual(in_root, [inside])
        self.assertEqual(out, [outside])


if __name__ == "__main__":
    unittest.main()
