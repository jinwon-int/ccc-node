#!/usr/bin/env python3
"""Procedure docs follow main, the dispatcher stays on the broker checkout (ccc-node#2705).

Field case, 2026-11-06: a2a-nexus#2508 changed `docs/skills-intake-revise.md`,
but the publisher read the worker procedure from `a2a_nexus_dir`, the T1
broker's deploy checkout (detached at an older commit), so the merged text
reached no reviser until the next broker deploy. Owner decision A: an opt-in
`procedure_docs_dir` that follows main, fast-forwarded once per collect, with
every dispatch row recording which procedure text it used.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "promotion_procedure_docs_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}
FILLER = "Step text that keeps the extracted procedure above the minimum size. " * 6


def doc(body: str, rubric: str | None = None) -> str:
    mark = f"Rubric version: `{rubric}`.\n" if rubric else ""
    return f"# Spec\n\n## Worker procedure — x\n\n{mark}{body}\n{FILLER}\n\n## Result schema\n\n{{}}\n"


def git(*argv: str, cwd: Path) -> str:
    return subprocess.run(["git", *argv], cwd=cwd, env=GIT_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def write_docs(root: Path, review: str, revise: str) -> None:
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "skills-intake-review.md").write_text(review, encoding="utf-8")
    (root / "docs" / "skills-intake-revise.md").write_text(revise, encoding="utf-8")


def config(docs_dir: Path | None, nexus_dir: Path) -> types.SimpleNamespace:
    return types.SimpleNamespace(procedure_docs_dir=docs_dir, a2a_nexus_dir=nexus_dir)


class Repos:
    """origin (bare) <- author clone pushes; docs clone tracks origin/main."""

    def __init__(self, tmp: Path) -> None:
        self.origin = tmp / "origin.git"
        self.author = tmp / "author"
        self.docs = tmp / "docs-clone"
        git("init", "--quiet", "--bare", "--initial-branch=main", str(self.origin), cwd=tmp)
        git("clone", "--quiet", str(self.origin), str(self.author), cwd=tmp)
        git("checkout", "--quiet", "-b", "main", cwd=self.author)
        write_docs(self.author, doc("review v1", promotion._INTAKE_RUBRIC_VERSION), doc("revise v1"))
        self.commit("v1")
        git("clone", "--quiet", "--branch", "main", str(self.origin), str(self.docs), cwd=tmp)

    def commit(self, message: str) -> None:
        git("add", "-A", cwd=self.author)
        git("commit", "--quiet", "-m", message, cwd=self.author)
        git("push", "--quiet", "origin", "main", cwd=self.author)


class Ledger:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def __enter__(self) -> "Ledger":
        self._patches = [
            mock.patch.object(promotion, "_ledger_rows", lambda _c: list(self.rows)),
            mock.patch.object(promotion, "_append_ledger", lambda _c, row: self.rows.append(row)),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *_exc) -> None:
        for patch in self._patches:
            patch.stop()

    def docs_rows(self) -> list[dict]:
        return [row for row in self.rows if row.get("kind") == promotion._PROCEDURE_DOCS_KIND]


class ProcedureSource(unittest.TestCase):
    def test_env_opt_in_and_default(self) -> None:
        self.assertIsNone(promotion._config({}).procedure_docs_dir)
        cfg = promotion._config({"CCC_SKILL_PROMOTION_PROCEDURE_DOCS_DIR": "/srv/nexus-docs"})
        self.assertEqual(cfg.procedure_docs_dir, Path("/srv/nexus-docs"))

    def test_unset_reads_the_nexus_dir_exactly_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            nexus = Path(raw)
            write_docs(nexus, doc("review nexus", "1999-01-01.0"), doc("revise nexus"))
            text, meta = promotion._worker_procedure(
                config(None, nexus), doc_name="skills-intake-review.md", end_marker="## Result schema"
            )
            self.assertIn("review nexus", text)  # rubric mismatch tolerated when unset
            self.assertEqual(meta["procedure_source"], "nexus-dir")
            self.assertNotIn("procedure_commit", meta)  # not a git checkout

    def test_docs_dir_wins_and_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repos = Repos(Path(raw))
            nexus = Path(raw) / "nexus"
            write_docs(nexus, doc("review STALE"), doc("revise STALE"))
            text, meta = promotion._worker_procedure(
                config(repos.docs, nexus), doc_name="skills-intake-revise.md", end_marker="## Result schema"
            )
            self.assertIn("revise v1", text)
            self.assertNotIn("STALE", text)
            self.assertEqual(meta["procedure_source"], "docs-dir")
            self.assertEqual(meta["procedure_doc"], "skills-intake-revise.md")
            self.assertEqual(len(meta["procedure_sha256"]), 16)
            self.assertEqual(meta["procedure_commit"], git("rev-parse", "HEAD", cwd=repos.docs)[:12])

    def test_rubric_mismatch_is_refused_only_with_docs_dir(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            docs = Path(raw)
            write_docs(docs, doc("review", "2099-01-01.0"), doc("revise"))
            with self.assertRaises(promotion.PromotionError) as caught:
                promotion._worker_procedure(
                    config(docs, Path(raw) / "unused"), doc_name="skills-intake-review.md",
                    end_marker="## Result schema",
                )
            self.assertEqual(caught.exception.code, "dispatch_docs_rubric_mismatch")
            # the revise doc carries no rubric mark and is never refused for it
            promotion._worker_procedure(
                config(docs, Path(raw) / "unused"), doc_name="skills-intake-revise.md",
                end_marker="## Result schema",
            )

    def test_dispatcher_script_stays_on_the_broker_checkout(self) -> None:
        for fn in (promotion._dispatch_intake_review_attempt, promotion._dispatch_intake_revise):
            source = inspect.getsource(fn)
            self.assertNotIn("procedure_docs", source.replace("procedure_meta", ""))
        self.assertIn('config.a2a_nexus_dir / "scripts" / "a2a-dispatch-round.mjs"',
                      inspect.getsource(promotion._dispatch_intake_revise))

    def test_collect_refreshes_only_on_the_locked_live_path(self) -> None:
        source = inspect.getsource(promotion._collect)
        refresh = source.find("_refresh_procedure_docs(config)")
        self.assertGreater(refresh, 0)
        # after the dry-run early return and inside the promotion lock
        self.assertGreater(refresh, source.find("return collector(config, dry_run=True)"))
        self.assertGreater(refresh, source.find("with _promotion_lock("))
        self.assertLess(refresh, source.find("collector(config, dry_run=False)"))

    def test_dispatch_rows_carry_the_procedure_meta(self) -> None:
        for fn in (promotion._dispatch_intake_review_attempt, promotion._dispatch_intake_revise):
            self.assertIn("**procedure_meta", inspect.getsource(fn))


class Refresh(unittest.TestCase):
    def test_unset_is_a_no_op(self) -> None:
        self.assertIsNone(promotion._refresh_procedure_docs(config(None, Path("/nonexistent"))))

    def test_fast_forward_records_one_row_then_stays_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as raw, Ledger() as ledger:
            repos = Repos(Path(raw))
            old = git("rev-parse", "HEAD", cwd=repos.docs)
            write_docs(repos.author, doc("review v2", promotion._INTAKE_RUBRIC_VERSION), doc("revise v2"))
            repos.commit("v2")
            cfg = config(repos.docs, Path(raw) / "unused")
            result = promotion._refresh_procedure_docs(cfg)
            new = git("rev-parse", "HEAD", cwd=repos.docs)
            self.assertEqual(result["status"], "updated")
            self.assertNotEqual(old, new)
            self.assertEqual((result["old"], result["new"]), (old[:12], new[:12]))
            self.assertIn("revise v2", (repos.docs / "docs" / "skills-intake-revise.md").read_text())
            self.assertEqual(len(ledger.docs_rows()), 1)
            self.assertEqual(promotion._refresh_procedure_docs(cfg)["status"], "current")
            self.assertEqual(len(ledger.docs_rows()), 1)

    def test_dirty_clone_keeps_its_tree_and_records_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw, Ledger() as ledger:
            repos = Repos(Path(raw))
            head = git("rev-parse", "HEAD", cwd=repos.docs)
            write_docs(repos.author, doc("review v2", promotion._INTAKE_RUBRIC_VERSION), doc("revise v2"))
            repos.commit("v2")
            (repos.docs / "docs" / "skills-intake-revise.md").write_text(doc("local edit"), encoding="utf-8")
            cfg = config(repos.docs, Path(raw) / "unused")
            for _ in range(3):
                result = promotion._refresh_procedure_docs(cfg)
                self.assertEqual((result["status"], result["code"]), ("failed", "docs_dirty"))
            self.assertEqual(git("rev-parse", "HEAD", cwd=repos.docs), head)
            self.assertEqual(len(ledger.docs_rows()), 1)  # deduplicated across cycles

    def test_diverged_clone_is_not_forced(self) -> None:
        with tempfile.TemporaryDirectory() as raw, Ledger() as ledger:
            repos = Repos(Path(raw))
            write_docs(repos.author, doc("review v2", promotion._INTAKE_RUBRIC_VERSION), doc("revise v2"))
            repos.commit("v2")
            (repos.docs / "docs" / "local.md").write_text("x", encoding="utf-8")
            git("add", "-A", cwd=repos.docs)
            git("commit", "--quiet", "-m", "local", cwd=repos.docs)
            local = git("rev-parse", "HEAD", cwd=repos.docs)
            result = promotion._refresh_procedure_docs(config(repos.docs, Path(raw) / "unused"))
            self.assertEqual((result["status"], result["code"]), ("failed", "docs_not_ff"))
            self.assertEqual(git("rev-parse", "HEAD", cwd=repos.docs), local)
            self.assertEqual(len(ledger.docs_rows()), 1)

    def test_not_a_repository(self) -> None:
        with tempfile.TemporaryDirectory() as raw, Ledger() as ledger:
            result = promotion._refresh_procedure_docs(config(Path(raw), Path(raw)))
            self.assertEqual((result["status"], result["code"]), ("failed", "docs_not_git"))
            self.assertEqual(len(ledger.docs_rows()), 1)

    def test_fetch_failure_is_recorded_and_tree_kept(self) -> None:
        with tempfile.TemporaryDirectory() as raw, Ledger() as ledger:
            repos = Repos(Path(raw))
            head = git("rev-parse", "HEAD", cwd=repos.docs)
            git("remote", "set-url", "origin", str(Path(raw) / "missing.git"), cwd=repos.docs)
            result = promotion._refresh_procedure_docs(config(repos.docs, Path(raw) / "unused"))
            self.assertEqual((result["status"], result["code"]), ("failed", "docs_fetch_failed"))
            self.assertEqual(git("rev-parse", "HEAD", cwd=repos.docs), head)
            self.assertEqual(len(ledger.docs_rows()), 1)


if __name__ == "__main__":
    unittest.main()
