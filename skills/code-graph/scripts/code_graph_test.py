#!/usr/bin/env python3
"""Hermetic tests for code_graph.py (ccc-node#2707 stage 1): temp git repos only."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True  # keep the skill tree free of __pycache__ (registry hash)
spec = importlib.util.spec_from_file_location("code_graph", HERE / "code_graph.py")
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)

FIXTURE = {
    ".ccc-code-graph.toml": 'include = ["**/*.py"]\nexclude = ["vendor/**"]\n',
    "pkg/__init__.py": "from .impl import exported\n",
    "pkg/impl.py": (
        "from . import util\n"
        "from .util import helper as h\n"
        "def exported():\n    return h()\n"
        "def helper():\n    return 1\n"
        "def local_caller():\n    return helper()\n"
        "class K:\n"
        "    def m(self):\n        return self.n()\n"
        "    def n(self):\n        return util.helper()\n"
    ),
    "pkg/util.py": (
        "from pkg import impl  # circular on purpose\n"
        "def helper():\n    return 2\n"
        "def other(obj):\n    return obj.helper()\n"
    ),
    "pkg/broken.py": "def oops(:\n",
    "app.py": "import pkg\nfrom pkg import exported\ndef main():\n    exported()\n    pkg.exported()\n",
    "vendor/skip.py": "def vendored():\n    pass\n",
}


def sh(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class CodeGraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        env = {"HOME": str(self.tmp), "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
               "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        patcher = unittest.mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.tmp / "repo"
        self.cache = self.tmp / "cache"
        for rel, text in FIXTURE.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text)
        sh(self.repo, "init", "-q")
        sh(self.repo, "remote", "add", "origin", "https://github.com/jinwon-int/fixture.git")
        self.commit("init")

    def commit(self, msg: str) -> None:
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-q", "-m", msg)

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = cg.main([*argv, "--cache-root", str(self.cache)])
        return rc, out.getvalue()

    def build(self, *extra: str) -> dict:
        rc, out = self.run_cli("build", str(self.repo), "--json", *extra)
        self.assertEqual(rc, 0, out)
        return json.loads(out)

    def calls(self) -> list[dict]:
        lines = Path(self.build()["cache_file"]).read_text().splitlines()
        return [r for r in map(json.loads, lines) if r.get("type") == "calls"]

    def test_syntax_error_file_skipped_and_excludes_respected(self):
        info = self.build()
        self.assertEqual(info["files"], 4)  # 6 .py minus broken minus vendor
        self.assertEqual([s["path"] for s in info["skipped"]], ["pkg/broken.py"])
        self.assertIn("syntax", info["skipped"][0]["reason"])
        self.assertTrue(info["cache_file"].endswith("jinwon-int__fixture@" + info["sha"][:12] + ".jsonl"))

    def test_same_name_functions_never_become_exact_by_name(self):
        edges = {(e["src"], e["dst"], e["confidence"]) for e in self.calls()}
        self.assertIn(("pkg.impl.local_caller", "pkg.impl.helper", "exact"), edges)
        self.assertIn(("pkg.impl.exported", "pkg.util.helper", "exact"), edges)  # aliased import
        self.assertIn(("pkg.util.other", "helper", "name-match"), edges)  # obj.helper(): two candidates
        self.assertFalse([e for e in edges if e[0] == "pkg.util.other" and e[2] == "exact"])
        self.assertIn(("pkg.impl.K.m", "pkg.impl.K.n", "name-match"), edges)  # self.x(): no dispatch

    def test_relative_imports_and_reexports_resolve(self):
        edges = {(e["src"], e["dst"], e["confidence"]) for e in self.calls()}
        self.assertIn(("pkg.impl.K.n", "pkg.util.helper", "exact"), edges)  # from . import util
        self.assertIn(("app.main", "pkg.impl.exported", "exact"), edges)  # via pkg/__init__ re-export
        rc, out = self.run_cli("imports-of", "pkg/impl.py", "--path", str(self.repo), "--json")
        self.assertEqual(rc, 0)
        self.assertEqual({e["dst"] for e in json.loads(out)}, {"pkg.util"})

    def test_circular_import_impact_terminates(self):
        rc, out = self.run_cli("impact", "pkg/util.py", "--path", str(self.repo), "--json")
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        paths = [r["path"] for r in rows]
        self.assertEqual(sorted(paths), ["pkg/__init__.py", "pkg/impl.py"])  # util<->impl cycle
        self.assertNotIn("pkg/util.py", paths)
        rc, out = self.run_cli("impact", "pkg/util.py", "--depth", "1", "--path", str(self.repo))
        self.assertIn("pkg/impl.py", out)
        self.assertNotIn("pkg/__init__.py", out)
        rc, out = self.run_cli("impact", "./pkg/util.py", "--depth", "3", "--path", str(self.repo), "--json")
        self.assertIn({"depth": 3, "path": "app.py", "via": "pkg/__init__.py"}, json.loads(out))

    def test_callers_table_output(self):
        rc, out = self.run_cli("callers", "pkg.util.helper", "--path", str(self.repo))
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("CONFIDENCE"))
        self.assertIn("pkg.impl.K.n", out)
        self.assertIn("pkg/util.py:5", out)  # the name-match obj.helper() site is shown too

    def test_cache_reused_then_invalidated_on_sha_change(self):
        first = self.build()
        self.assertTrue(self.build()["cached"])
        (self.repo / "pkg/util.py").write_text(FIXTURE["pkg/util.py"] + "def added():\n    helper()\n")
        self.commit("change")
        second = self.build()
        self.assertFalse(second["cached"])
        self.assertNotEqual(first["cache_file"], second["cache_file"])
        self.assertIn(("pkg.util.added", "pkg.util.helper"), {(e["src"], e["dst"]) for e in self.calls()})

    def test_dirty_worktree_refused_unless_flagged(self):
        (self.repo / "app.py").write_text(FIXTURE["app.py"] + "x = 1\n")
        rc, _ = self.run_cli("build", str(self.repo))
        self.assertEqual(rc, 3)
        one = self.build("--worktree")
        self.assertIn("+dirty-", one["cache_file"])
        (self.repo / "app.py").write_text(FIXTURE["app.py"] + "x = 2\n")
        two = self.build("--worktree")
        self.assertNotEqual(one["cache_file"], two["cache_file"])
        self.assertFalse(two["cached"])

    def test_untracked_files_do_not_dirty_a_clean_build(self):
        (self.repo / "scratch.py").write_text("def s():\n    pass\n")
        self.assertNotIn("+dirty-", self.build()["cache_file"])

    def test_foreign_or_missing_origin_refused(self):
        for url in ("https://github.com/someone-else/fixture.git", "https://gitlab.com/jinwon-int/x.git",
                    "https://github.com/jinwon-int-evil/fixture.git"):
            sh(self.repo, "remote", "set-url", "origin", url)
            self.assertEqual(self.run_cli("build", str(self.repo))[0], 3, url)
        sh(self.repo, "remote", "remove", "origin")
        self.assertEqual(self.run_cli("build", str(self.repo))[0], 3)

    def test_opt_in_required(self):
        (self.repo / ".ccc-code-graph.toml").unlink()
        self.commit("drop config")
        self.assertEqual(self.run_cli("build", str(self.repo))[0], 3)
        info = self.build("--repo")
        self.assertEqual(info["files"], 5)  # no excludes without the config

    def test_setuptools_package_dir_maps_installed_name(self):
        files = {"lib/pyproject.toml": '[tool.setuptools]\npackage-dir = {"instname" = "."}\n',
                 "lib/__init__.py": "", "lib/mod.py": "def f():\n    pass\n",
                 "use.py": "from instname.mod import f\nimport instname.mod as m\ndef g():\n    f()\n    m.f()\n"}
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text)
        self.commit("installed package")
        got = [(e["dst"], e["confidence"]) for e in self.calls() if e["src"] == "use.g"]
        self.assertEqual(got, [("lib.mod.f", "exact")] * 2)

    def test_invalid_config_refused(self):
        (self.repo / ".ccc-code-graph.toml").write_text("include = [\n")
        self.commit("bad config")
        self.assertEqual(self.run_cli("build", str(self.repo))[0], 3)

    def test_origin_url_shapes(self):
        for url in ("git@github.com:jinwon-int/x.git", "ssh://git@github.com/jinwon-int/x",
                    "https://token@github.com/jinwon-int/x.git"):
            self.assertEqual(cg.ORIGIN_RE.match(url)["owner"], "jinwon-int", url)


if __name__ == "__main__":
    unittest.main()
