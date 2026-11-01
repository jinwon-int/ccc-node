#!/usr/bin/env python3
"""Tests for scripts/ccc_owner_queue.py (#2617) — offline, gh is stubbed."""
from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import pathlib
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

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


if __name__ == "__main__":
    unittest.main(verbosity=1, argv=[sys.argv[0]])
