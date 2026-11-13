#!/usr/bin/env python3
"""Tests for scripts/ccc_owner_queue.py (#2617) — offline, gh is stubbed."""
from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

_PATH = pathlib.Path(__file__).with_name("ccc_owner_queue.py")
_SPEC = importlib.util.spec_from_file_location("ccc_owner_queue", _PATH)
oq = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(oq)

NOW = dt.datetime(2026, 11, 1, 3, 0, tzinfo=dt.timezone.utc)
GREEN_UNREVIEWED = {"reviewDecision": "REVIEW_REQUIRED",
                    "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"state": "SUCCESS"}]}


def _raw(repo: str, n: int, created: str, title: str = "t", author: str = "dev") -> dict:
    return {"number": n, "title": title, "repository": {"nameWithOwner": repo},
            "url": f"https://example.invalid/{repo}/{n}", "createdAt": created,
            "author": {"login": author}}


class StubGh:
    def __init__(self, review=(), labeled=(), body="", views=None):
        self.review, self.labeled, self.body = list(review), list(labeled), body
        # default: every searched PR is green and unreviewed
        self.views = views or {}
        self.calls: list[list[str]] = []

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["search", "prs"]:
            return json.dumps(self.review)
        if args[:2] == ["search", "issues"]:
            return json.dumps(self.labeled)
        if args[:2] == ["pr", "view"]:
            key = f"{args[args.index('--repo') + 1]}#{args[2]}"
            return json.dumps(self.views.get(key, GREEN_UNREVIEWED))
        if args[:2] == ["issue", "view"]:
            return json.dumps({"body": self.body})
        if args[:2] == ["issue", "edit"]:
            self.body = args[args.index("--body") + 1]
            return ""
        raise AssertionError(f"unexpected gh call {args}")


class CollectTest(unittest.TestCase):
    def test_queries_are_read_only_and_scoped(self):
        stub = StubGh(review=[_raw("o/a", 1, "2026-11-01T01:00:00Z")])
        oq.collect("org-x", "owner-action", stub, NOW)
        prs, view, issues = stub.calls
        self.assertEqual(prs[:2], ["search", "prs"])
        self.assertIn("--draft=false", prs)
        self.assertEqual(prs[prs.index("--owner") + 1], "org-x")
        # search filters are unreliable — never delegated to them
        self.assertNotIn("--checks", prs)
        self.assertNotIn("--review", prs)
        self.assertEqual(view[:3], ["pr", "view", "1"])
        self.assertTrue(all(c[:2] in (["search", "prs"], ["pr", "view"], ["search", "issues"])
                            for c in stub.calls))
        self.assertIn("--include-prs", issues)
        self.assertEqual(issues[issues.index("--label") + 1], "owner-action")

    def test_oldest_first_and_dedup(self):
        stub = StubGh(
            review=[_raw("o/a", 1, "2026-11-01T01:00:00Z"), _raw("o/b", 7, "2026-10-30T03:00:00Z")],
            labeled=[_raw("o/a", 1, "2026-11-01T01:00:00Z"), _raw("o/c", 3, "2026-10-31T03:00:00Z")],
        )
        items = oq.collect("o", "owner-action", stub, NOW)
        self.assertEqual([(i["repo"], i["number"]) for i in items], [("o/b", 7), ("o/c", 3), ("o/a", 1)])
        self.assertEqual(items[0]["age_hours"], 48)
        kinds = {(i["repo"], i["number"]): i["kind"] for i in items}
        self.assertEqual(kinds[("o/a", 1)], "review-wait+owner-action")
        self.assertEqual(kinds[("o/c", 3)], "owner-action")


class ReviewReadyTest(unittest.TestCase):
    def test_classification(self):
        rr = oq.review_ready
        self.assertTrue(rr(GREEN_UNREVIEWED))
        self.assertTrue(rr({"reviewDecision": "", "statusCheckRollup": [{"conclusion": "NEUTRAL"}]}))
        self.assertTrue(rr({"reviewDecision": None, "statusCheckRollup": [{"conclusion": "SKIPPED"}]}))
        self.assertFalse(rr({"reviewDecision": "APPROVED", "statusCheckRollup": [{"conclusion": "SUCCESS"}]}))
        self.assertFalse(rr({"reviewDecision": "CHANGES_REQUESTED", "statusCheckRollup": [{"conclusion": "SUCCESS"}]}))
        self.assertFalse(rr({"reviewDecision": "", "statusCheckRollup": []}))
        self.assertFalse(rr({"reviewDecision": "", "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS"}]}))
        self.assertFalse(rr({"reviewDecision": "", "statusCheckRollup": [{"conclusion": "FAILURE"}]}))
        self.assertFalse(rr({"reviewDecision": "", "statusCheckRollup": [{"state": "PENDING"}]}))

    def test_not_ready_pr_is_dropped(self):
        stub = StubGh(review=[_raw("o/a", 1, "2026-11-01T01:00:00Z"), _raw("o/b", 2, "2026-11-01T01:00:00Z")],
                      views={"o/b#2": {"reviewDecision": "", "statusCheckRollup": [{"conclusion": "FAILURE"}]}})
        self.assertEqual([(i["repo"], i["number"]) for i in oq.collect("o", "l", stub, NOW)], [("o/a", 1)])


class RenderTest(unittest.TestCase):
    def test_counts_sections_and_empty(self):
        out = oq.render([], NOW)
        self.assertIn("오너 할 일 0건", out)
        self.assertEqual(out.count("- 없음"), 2)
        self.assertIn("12:00 KST", out)

    def test_labelled_review_counts_once_as_review(self):
        stub = StubGh(review=[_raw("o/a", 1, "2026-10-31T00:00:00Z")],
                      labeled=[_raw("o/a", 1, "2026-10-31T00:00:00Z")])
        out = oq.render(oq.collect("o", "owner-action", stub, NOW), NOW)
        self.assertIn("승인 대기 PR 1 · owner-action 0", out)
        self.assertIn("o/a#1 t — 1d 3h · @dev · 라벨도 있음", out)


class SpliceTest(unittest.TestCase):
    def test_replaces_only_marker_block(self):
        body = f"intro\n{oq.START}\nold\n{oq.END}\noutro"
        new = oq.splice(body, "fresh")
        self.assertEqual(new, f"intro\n{oq.START}\nfresh\n{oq.END}\noutro")

    def test_appends_when_absent_and_handles_backslashes(self):
        self.assertEqual(oq.splice("", "x"), f"{oq.START}\nx\n{oq.END}\n")
        self.assertIn(r"a\1b", oq.splice(f"{oq.START}\n{oq.END}", r"a\1b"))


class UpdateIssueTest(unittest.TestCase):
    def test_dry_run_never_edits(self):
        stub = StubGh(body="keep")
        self.assertTrue(oq.update_issue("o/r#5", "blk", False, stub).startswith("would-update"))
        self.assertFalse(any(c[:2] == ["issue", "edit"] for c in stub.calls))

    def test_apply_edits_and_is_idempotent(self):
        stub = StubGh(body="keep")
        self.assertEqual(oq.update_issue("o/r#5", "blk", True, stub), "updated")
        self.assertIn("keep", stub.body)
        self.assertEqual(oq.update_issue("o/r#5", "blk", True, stub), "unchanged")

    def test_bad_target(self):
        with self.assertRaises(ValueError):
            oq.update_issue("o/r/5", "blk", True, StubGh())


class MainTest(unittest.TestCase):
    def test_owner_required(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(oq.main(["--owner", ""], StubGh()), 2)
        self.assertIn("required", err.getvalue())

    def test_json_output_and_gh_failure(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(oq.main(["--owner", "o", "--json"], StubGh()), 0)
        self.assertEqual(json.loads(out.getvalue())["items"], [])

        def boom(_args):
            raise RuntimeError("gh search failed: auth")
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(oq.main(["--owner", "o"], boom), 1)
        self.assertIn("auth", err.getvalue())


class DigestTest(unittest.TestCase):
    """--digest: one tier=info push-spool record for the 09:00 KST digest (#2617 decision 2)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spool = pathlib.Path(self.tmp.name) / "spool"
        self.env = mock.patch.dict(os.environ, {"CCC_PUSH_SPOOL": str(self.spool), "CCC_NODE": "n1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _items(self):
        stub = StubGh(review=[_raw("o/a", 1, "2026-10-30T03:00:00Z"), _raw("o/b", 2, "2026-11-01T01:00:00Z")],
                      labeled=[_raw("o/c", 3, "2026-10-31T03:00:00Z")])
        return oq.collect("o", "owner-action", stub, NOW)

    def _files(self):
        return sorted(self.spool.glob("*.json")) if self.spool.exists() else []

    def test_text_is_short_counts_only(self):
        text = oq.build_digest_text(self._items(), "https://github.com/o/r/issues/9")
        lines = text.splitlines()
        self.assertLessEqual(len(lines), 3)
        self.assertEqual(lines[0], "📋 오너 할 일 3건 — 승인 대기 PR 2 · owner-action 1")
        self.assertEqual(lines[1], "가장 오래된 것: o/a#1 2d 0h · o/c#3 1d 0h")
        self.assertEqual(lines[2], "전체 목록: https://github.com/o/r/issues/9")
        self.assertNotIn(" t ", text)  # no titles

    def test_dry_run_writes_nothing(self):
        self.assertTrue(oq.write_digest(self._items(), "n1", apply=False).startswith("would-spool"))
        self.assertEqual(self._files(), [])

    def test_empty_queue_writes_nothing_even_with_apply(self):
        self.assertEqual(oq.write_digest([], "n1", apply=True), "skipped (queue empty)")
        self.assertEqual(self._files(), [])

    def test_apply_writes_one_owner_only_info_record(self):
        result = oq.write_digest(self._items(), "n1", apply=True, link=None, now=NOW)
        files = self._files()
        self.assertEqual(len(files), 1)
        self.assertEqual(result, f"spooled {files[0].name}")
        self.assertIn("-OwnerQueue-", files[0].name)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        rec = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(rec["event"], "OwnerQueue")
        self.assertEqual(rec["tier"], "info")
        self.assertEqual(rec["node"], "n1")
        # NOW = 2026-11-01 03:00 UTC = 12:00 KST → KST date, once per day per node
        self.assertEqual(rec["dedup"], "OwnerQueue:2026-11-01:n1")
        self.assertEqual(rec["ts"], "2026-11-01T03:00:00Z")
        self.assertTrue(rec["text"].startswith("📋 오너 할 일 3건"))

    def test_main_digest_flags(self):
        stub = StubGh(review=[_raw("o/a", 1, "2026-10-31T01:00:00Z")], body="x")
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(oq.main(["--owner", "o", "--digest"], stub), 0)
        self.assertIn("digest: would-spool", err.getvalue())
        self.assertEqual(self._files(), [])
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(oq.main(["--owner", "o", "--digest", "--apply",
                                      "--update-issue", "o/r#9"], stub), 0)
        self.assertIn("digest: spooled", err.getvalue())
        rec = json.loads(self._files()[0].read_text(encoding="utf-8"))
        self.assertEqual(rec["node"], "n1")  # CCC_NODE
        self.assertIn("전체 목록: https://github.com/o/r/issues/9", rec["text"])

    def test_without_digest_flag_nothing_is_spooled(self):
        stub = StubGh(review=[_raw("o/a", 1, "2026-10-31T01:00:00Z")])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(oq.main(["--owner", "o", "--apply"], stub), 0)
        self.assertEqual(self._files(), [])

    def test_node_resolution_order(self):
        with mock.patch.dict(os.environ, {"CCC_NODE": ""}):
            state = pathlib.Path(self.tmp.name) / "state"
            state.mkdir()
            (state / "node.txt").write_text("from-file\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CCC_STATE_DIR": str(state)}):
                self.assertEqual(oq.resolve_node(), "from-file")


if __name__ == "__main__":
    unittest.main(verbosity=1, argv=[sys.argv[0]])
