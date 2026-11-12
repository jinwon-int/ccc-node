"""Delegated-approval collect/reconcile (spec §10.2 5-d slice 2, ccc-node#2137).

Hermetic: gh and ssh are replaced by an in-process runner, ledgers live in a temp dir. Wired through
scripts/delegated-approval-shadow.test.sh.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import delegated_approval_reconcile as rc  # noqa: E402
import delegated_approval_shadow as sh  # noqa: E402

REPO = "jinwon-int/ccc-node"
T0 = 1_794_000_000.0  # 2026-11-06T21:20:00Z
DAY = 86400.0


def done(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def pr(number, paths, *, merged=None, closed=None, author="jinon86", bot=False, reviews=(), additions=10, deletions=2,
       base="main", title="t"):
    return {
        "number": number, "title": title, "author": {"login": author, "is_bot": bot},
        "additions": additions, "deletions": deletions, "files": [{"path": p} for p in paths],
        "reviews": [{"state": s, "body": "REVIEW-TEXT-NOT-KEPT"} for s in reviews],
        "headRefOid": "a" * 40, "baseRefName": base,
        "mergedAt": rc._iso(merged) if merged is not None else None,
        "closedAt": rc._iso(closed if closed is not None else merged) if (closed is not None or merged is not None) else None,
    }


class FakeGh:
    """Answers the three gh calls reconcile makes; records every argv."""

    def __init__(self, merged=(), unmerged=(), reverts=(), comments=None, fail_first=0, always_fail=False):
        self.merged, self.unmerged, self.reverts = list(merged), list(unmerged), list(reverts)
        self.comments = comments or {}
        self.fail_first, self.always_fail = fail_first, always_fail
        self.calls: list = []

    def __call__(self, argv):
        self.calls.append(argv)
        if self.always_fail:
            return done(returncode=1)
        if self.fail_first:
            self.fail_first -= 1
            return done(returncode=1)
        if argv[:3] == ["gh", "pr", "view"]:
            return done(json.dumps({"comments": [{"body": b} for b in self.comments.get(int(argv[3]), [])]}))
        search = argv[argv.index("--search") + 1]
        if search.startswith("Revert in:title"):
            return done(json.dumps(self.reverts))
        return done(json.dumps(self.unmerged if search.startswith("is:unmerged") else self.merged))


def run(gh, *, ledger_rows=None, since=T0, until=T0 + 10 * DAY, **kw):
    return rc.reconcile([REPO], since, until, ledger_rows=ledger_rows, runner=gh, sleep=lambda _s: None, **kw)


class ReconcileTest(unittest.TestCase):
    def test_tiers_outcomes_and_l1_refusals(self) -> None:
        gh = FakeGh(
            merged=[
                pr(1, ["docs/a.md"], merged=T0 + 100),                                  # L1, clean
                pr(2, ["docs/b.md"], merged=T0 + 200, reviews=("CHANGES_REQUESTED", "APPROVED")),  # L1, sent back once
                pr(3, ["README.md"], merged=T0 + 300),                                  # L1, held by a comment
                pr(4, ["scripts/x.py"], merged=T0 + 400),                               # L2
                pr(5, [".github/workflows/ci.yml"], merged=T0 + 500),                   # L3 path
                pr(6, ["docs/c.md"], merged=T0 + 600, additions=900),                   # L3 by size
                pr(7, ["docs/d.md"], merged=T0 + 700, base="stable"),                   # L3 promotion base
            ],
            unmerged=[pr(8, ["docs/e.md"], closed=T0 + 800), pr(9, ["scripts/y.py"], closed=T0 + 900)],
            comments={3: ["looks fine", "  ⛔ hold until the owner is back SECRET-COMMENT"]},
        )
        report = run(gh)
        self.assertEqual((report["complete"], report["closed"]), (True, {"merged": 7, "unmerged": 2}))
        self.assertEqual(report["by_tier"], {"L1": 4, "L2": 2, "L3": 3})
        l1 = report["l1"]
        self.assertEqual((l1["total"], l1["merged"], l1["closed_unmerged"], l1["changes_requested"], l1["hold_comment"]),
                         (4, 3, 1, 1, 1))
        self.assertEqual(l1["refused"], [f"{REPO}#2", f"{REPO}#3", f"{REPO}#8"])
        self.assertEqual(l1["approved_as_asked_rate"], 0.25)
        text = json.dumps(report, ensure_ascii=False)
        for leaked in ("SECRET-COMMENT", "REVIEW-TEXT-NOT-KEPT", "looks fine"):
            self.assertNotIn(leaked, text)  # titles, review and comment text never reach the report

    def test_comments_are_read_for_l1_only_and_can_be_skipped(self) -> None:
        prs = [pr(1, ["docs/a.md"], merged=T0 + 1), pr(2, ["scripts/x.py"], merged=T0 + 2)]
        gh = FakeGh(merged=prs, comments={1: ["⛔ hold"], 2: ["⛔ hold"]})
        report = run(gh)
        self.assertEqual([c[3] for c in gh.calls if c[:3] == ["gh", "pr", "view"]], ["1"])
        self.assertEqual(report["l1"]["hold_comment"], 1)
        gh = FakeGh(merged=prs, comments={1: ["⛔ hold"]})
        report = run(gh, check_comments=False)
        self.assertEqual((report["l1"]["hold_comment"], report["comments_checked"]), (0, False))
        self.assertFalse([c for c in gh.calls if c[:3] == ["gh", "pr", "view"]])

    def test_window_is_half_open_and_searched_in_pieces(self) -> None:
        gh = FakeGh(merged=[pr(1, ["docs/a.md"], merged=T0), pr(2, ["docs/b.md"], merged=T0 + 10 * DAY),
                            pr(3, ["docs/c.md"], merged=T0 - 1)])
        report = run(gh, span_days=4)
        self.assertEqual(report["closed"]["merged"], 1)  # the start is in, the end and anything earlier are out
        searches = [c[c.index("--search") + 1] for c in gh.calls if "--search" in c and "Revert" not in c[c.index("--search") + 1]]
        self.assertEqual(len(searches), 6)  # 10 days in 4-day pieces = 3 spans, merged + unmerged
        self.assertEqual(rc.spans(T0, T0 + 10 * DAY, 4)[-1], (rc._iso(T0 + 8 * DAY), rc._iso(T0 + 10 * DAY)))

    def test_revert_within_seven_days_of_an_l1_merge(self) -> None:
        merged = [pr(1, ["docs/a.md"], merged=T0 + 100), pr(2, ["docs/b.md"], merged=T0 + 100),
                  pr(3, ["scripts/x.py"], merged=T0 + 100)]
        reverts = [
            {"number": 50, "title": 'Revert "docs: a (#1)" SECRET-TITLE', "createdAt": rc._iso(T0 + 2 * DAY)},
            {"number": 51, "title": 'Revert "docs: b (#2)"', "createdAt": rc._iso(T0 + 9 * DAY)},   # too late
            {"number": 52, "title": 'Revert "feat: x (#3)"', "createdAt": rc._iso(T0 + DAY)},       # not L1
        ]
        report = run(FakeGh(merged=merged, reverts=reverts))
        self.assertEqual((report["l1"]["reverted"], report["l1"]["reverted_prs"]), (1, [f"{REPO}#1"]))
        self.assertNotIn("SECRET-TITLE", json.dumps(report))

    def test_coverage_against_collected_ledgers(self) -> None:
        gh = FakeGh(merged=[
            pr(1, ["docs/a.md"], merged=T0 + 1), pr(2, ["scripts/x.py"], merged=T0 + 2),
            pr(3, ["docs/b.md"], merged=T0 + 3), pr(4, ["package.json"], merged=T0 + 4, author="app/dependabot", bot=True),
        ])
        ledger = [
            {"repo": REPO, "pr": 1, "ts_epoch": T0},                      # v1 approval row
            {"kind": "approve", "repo": REPO, "pr": 2, "ts_epoch": T0},
            {"kind": "ask", "repo": REPO, "pr": 3, "ts_epoch": T0},        # asked, never approved through the recorder
            {"kind": "decision", "repo": REPO, "pr": 4, "ts_epoch": T0},
        ]
        report = run(gh, ledger_rows=ledger)
        self.assertEqual(report["coverage"], {"merged": 4, "in_ledger": 2, "outside_ledger": 2, "outside_by_bot": 1,
                                              "l1_outside_ledger": 1})
        self.assertEqual(report["l1"]["asked"], 1)

    def test_gh_failures_are_retried_then_reported_as_incomplete(self) -> None:
        gh = FakeGh(merged=[pr(1, ["docs/a.md"], merged=T0 + 1)], fail_first=2)
        self.assertEqual(run(gh)["closed"]["merged"], 1)
        report = rc.reconcile([REPO, "jinwon-int/other"], T0, T0 + DAY, runner=FakeGh(always_fail=True), sleep=lambda _s: None)
        self.assertEqual((report["complete"], report["repos"], sorted(e["repo"] for e in report["errors"])),
                         (False, [], [REPO, "jinwon-int/other"]))

    def test_a_repository_that_fails_midway_adds_nothing_to_the_totals(self) -> None:
        class FailsOnReverts(FakeGh):
            def __call__(self, argv):
                if "--search" in argv and argv[argv.index("--search") + 1].startswith("Revert in:title"):
                    self.calls.append(argv)
                    return done(returncode=1)
                return super().__call__(argv)

        report = run(FailsOnReverts(merged=[pr(1, ["docs/a.md"], merged=T0 + 1), pr(2, ["scripts/x.py"], merged=T0 + 2)]))
        self.assertEqual((report["complete"], report["closed"], report["by_tier"], report["l1"]["total"]),
                         (False, {"merged": 0, "unmerged": 0}, {"L1": 0, "L2": 0, "L3": 0}, 0))


class CollectTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ok_no_ledger_unreachable_and_local(self) -> None:
        state = self.tmp / "state"
        sh.append_ledger(sh.ledger_path(state), {"repo": REPO, "pr": 9, "ts_epoch": 1})
        answers = {
            "alpha": done('{"repo": "jinwon-int/ccc-node", "pr": 1}\nnot json\n[1]\n{"repo": "x", "pr": 2}\n'),
            "beta": done(rc.NO_LEDGER + "\n"),
            "gamma": done(returncode=255),
        }
        seen: list = []

        def runner(argv):
            seen.append(argv)
            return answers[argv[-2]]

        out = self.tmp / "ledgers"
        report = rc.collect(["alpha", "beta", "gamma", "here"], out, self_node="here", state_dir=state, runner=runner)
        self.assertEqual({n: (e["status"], e["rows"], e["dropped"]) for n, e in report["nodes"].items()},
                         {"alpha": ("ok", 2, 2), "beta": ("no-ledger", 0, 0), "gamma": ("unreachable", 0, 0), "here": ("ok", 1, 0)})
        self.assertEqual((report["complete"], report["without_recorder"], report["unreachable"], report["rows"]),
                         (False, ["beta"], ["gamma"], 3))
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["alpha.jsonl", "here.jsonl"])
        self.assertEqual(stat.S_IMODE(os.stat(out / "alpha.jsonl").st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o700)
        self.assertEqual(len(seen), 3)  # the local node is read from disk, not over ssh
        self.assertEqual(seen[0][-1], rc.REMOTE_CAT)  # one fixed remote command, the node name is its own argv item
        self.assertIn("BatchMode=yes", seen[0])
        self.assertEqual(len(sh.read_ledger(out / "alpha.jsonl")), 2)

    def test_collected_ledgers_feed_summary_and_reconcile(self) -> None:
        row = sh.build_row(repo=REPO, pr=1, head="a" * 40, approver="seoseo-ai",
                           meta={"files": [{"path": "docs/a.md"}], "additions": 1, "deletions": 0, "title": "docs (#1)",
                                 "author": {"login": "jinon86"}, "baseRefName": "main"},
                           promotion=False, ledger=[], now=T0 + 5, node="alpha")
        out = self.tmp / "ledgers"
        rc.collect(["alpha"], out, runner=lambda argv: done(json.dumps(row) + "\n"))
        rows = sh.read_ledger(out / "alpha.jsonl")
        self.assertEqual(sh.summarize(rows, since=T0, until=T0 + DAY)["by_tier"]["L1"], 1)
        report = run(FakeGh(merged=[pr(1, ["docs/a.md"], merged=T0 + 6)]), ledger_rows=rows)
        self.assertEqual(report["coverage"]["in_ledger"], 1)


class CliTest(unittest.TestCase):
    def _run(self, argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        saved = dict(os.environ)
        os.environ.update(env or {})
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = rc.main(argv)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        return code, out.getvalue(), err.getvalue()

    def test_collect_needs_valid_node_names(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            code, out, err = self._run(["collect", "--out", raw], env={"CCC_FLEET_NODES": ""})
            self.assertEqual((code, out), (2, ""))
            code, out, err = self._run(["collect", "--out", raw, "--node", "ok-node", "--node", "bad;rm -rf"])
            self.assertEqual((code, out), (2, ""))
            self.assertIn("rejected", err)

    def test_collect_through_an_ssh_stub(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            stub = Path(raw) / "ssh-stub"
            stub.write_text("#!/bin/sh\nfor a in \"$@\"; do last2=$prev; prev=$a; done\n"
                            "[ \"$last2\" = down ] && exit 255\necho '{\"repo\": \"jinwon-int/x\", \"pr\": 1}'\n", encoding="utf-8")
            stub.chmod(0o755)
            code, out, _ = self._run(["collect", "--out", str(Path(raw) / "o")],
                                     env={"CCC_FLEET_NODES": "up down", "CCC_FLEET_SSH": str(stub), "CCC_FLEET_SELF": "_none_"})
            report = json.loads(out)
            self.assertEqual((code, report["nodes"]["up"]["rows"], report["unreachable"]), (1, 1, ["down"]))

    def test_reconcile_argument_checks(self) -> None:
        base = ["reconcile", "--since", "2026-11-06T00:00:00+09:00", "--until", "2026-11-07T00:00:00+09:00"]
        self.assertEqual(self._run(base)[0], 2)                                     # no repo, no ledger
        self.assertEqual(self._run(base + ["--repo", "evil/x"])[0], 2)
        self.assertEqual(self._run(["reconcile", "--repo", REPO, "--since", "2026-11-07T00:00:00Z",
                                    "--until", "2026-11-06T00:00:00Z"])[0], 2)      # inverted window
        code, out, err = self._run(["reconcile", "--repo", REPO, "--since", "2026-11-06T00:00:00", "--until", "2026-11-07T00:00:00Z"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("offset", err)


if __name__ == "__main__":
    unittest.main()
