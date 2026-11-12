"""Delegated-approval shadow recorder (a2a-nexus#2317 Phase 1, ccc-node#2137): classification, Q2 window, ledger,
requests and refusals (spec §10.2 5-d), summary.

Hermetic: temp state dir, PR metadata from JSON files, no gh. Wired through
scripts/delegated-approval-shadow.test.sh.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import delegated_approval_shadow as sh  # noqa: E402


def meta(paths, additions=10, deletions=2, title="fix(x): y (#123)", author="jinon86", base="main"):
    return {
        "files": [{"path": p, "additions": 1, "deletions": 0} for p in paths],
        "additions": additions, "deletions": deletions, "title": title,
        "author": {"login": author}, "baseRefName": base,
    }


class ClassifyTest(unittest.TestCase):
    def tier(self, paths, **kw):
        q2 = kw.pop("q2", {"prior_count_24h": 0, "prior_diff_24h": 0})
        args = dict(paths=paths, additions=10, deletions=2, author="jinon86", approver="seoseo-ai", promotion=False, q2=q2)
        args.update(kw)
        return sh.classify(**args)[:2]

    def test_docs_and_tests_only_is_L1(self) -> None:
        self.assertEqual(self.tier(["docs/a.md", "README.md", "changelog.d/1-x.md"]), ("L1", "v0.L1.docs-test-only"))
        self.assertEqual(self.tier(["scripts/foo_test.py", "scripts/bar.test.sh", "tests/test_x.py", "fixtures/a.json"])[0], "L1")
        self.assertEqual(self.tier(["pages/nodes/x/CHANGES.md"])[0], "L1")  # wiki pages

    def test_code_is_L2(self) -> None:
        self.assertEqual(self.tier(["scripts/foo.py", "docs/a.md"]), ("L2", "v0.L2.code"))
        self.assertEqual(self.tier(["bridge/core/x.py"])[0], "L2")
        self.assertEqual(self.tier(["claude/settings.base.json"])[0], "L2")

    def test_L3_paths(self) -> None:
        for p in (".github/workflows/ci.yml", ".github/required-checks.json", "deploy/x.sh", "bridge/.env.example",
                  "secrets/a", "conf/secret.json", "scripts/release.sh", ".github/workflows/publish.yml",
                  "docs/specs/delegated-approval-matrix.md", "tools/api_key_rotate.py"):
            self.assertEqual(self.tier([p, "docs/a.md"]), ("L3", "v0.L3.path"), p)

    def test_fail_closed_cases(self) -> None:
        self.assertEqual(self.tier([]), ("L3", "v0.L3.no-evidence"))
        self.assertEqual(self.tier(["docs/a.md"], promotion=True), ("L3", "v0.L3.promotion-branch"))
        self.assertEqual(self.tier(["docs/a.md"], author="seoseo-ai"), ("L3", "v0.L3.self-approval"))
        self.assertEqual(self.tier(["docs/a.md"], q2={"prior_count_24h": 5, "prior_diff_24h": 0}), ("L3", "v0.L3.q2-count"))
        self.assertEqual(self.tier(["docs/a.md"], q2={"prior_count_24h": 1, "prior_diff_24h": 790}), ("L3", "v0.L3.q2-diff"))
        self.assertEqual(self.tier(["docs/a.md"], q2={"prior_count_24h": 4, "prior_diff_24h": 700})[0], "L1")

    def test_issue_refs_from_title_only(self) -> None:
        self.assertEqual(sh.issue_refs("feat(x): y (#2413) refs #460", "jinwon-int/ccc-node"),
                         ["jinwon-int/ccc-node#2413", "jinwon-int/ccc-node#460"])
        self.assertEqual(sh.issue_refs("no refs", "jinwon-int/ccc-node"), [])
        # Cross-repo refs keep their repo (first live row attributed a2a-nexus#2317 to ccc-node).
        self.assertEqual(sh.issue_refs("feat: shadow recorder (a2a-nexus#2317, #2137)", "jinwon-int/ccc-node"),
                         ["jinwon-int/a2a-nexus#2317", "jinwon-int/ccc-node#2137"])
        self.assertEqual(sh.issue_refs("see other-org/their.repo#5 and #5", "jinwon-int/ccc-node"),
                         ["jinwon-int/ccc-node#5", "other-org/their.repo#5"])
        self.assertEqual(sh.issue_refs("fix(x): ratchet #1451 vs seoyoon-family-wiki#8397", "jinwon-int/ccc-node"),
                         ["jinwon-int/ccc-node#1451", "jinwon-int/seoyoon-family-wiki#8397"])


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "state"
        self.path = sh.ledger_path(self.state)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _record(self, pr, paths, now, **kw):
        m = meta(paths, **{k: v for k, v in kw.items() if k in ("additions", "deletions", "title", "author", "base")})
        row = sh.build_row(repo="jinwon-int/ccc-node", pr=pr, head="a" * 40, approver="seoseo-ai", meta=m,
                           promotion=kw.get("promotion", False), ledger=sh.read_ledger(self.path), now=now, node="node-x")
        sh.append_ledger(self.path, row)
        return row

    def test_row_shape_permissions_and_no_body_text(self) -> None:
        row = self._record(1, ["docs/a.md"], 1_000_000.0, title="docs: a (#9) SECRET-BODY-NOT-HERE")
        self.assertEqual((row["schema"], row["shadow_tier"], row["human_decision"], row["shadow_would"]),
                         (sh.SCHEMA, "L1", "approve", "delegate"))
        self.assertEqual(row["issue_refs"], ["jinwon-int/ccc-node#9"])
        self.assertEqual(row["categories"], {"docs": 1, "tests": 0, "code": 0, "l3": 0})
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("SECRET-BODY", text)  # only structured fields are written, never the title text
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.path.parent).st_mode), 0o700)

    def test_q2_window_counts_same_repo_and_issue_within_24h(self) -> None:
        t0 = 1_000_000.0
        for i in range(5):
            r = self._record(10 + i, ["scripts/x.py"], t0 + i * 60, title=f"feat: part {i} (#77)")
            self.assertEqual(r["shadow_tier"], "L2", i)
        r6 = self._record(99, ["scripts/x.py"], t0 + 600, title="feat: part 6 (#77)")
        self.assertEqual((r6["shadow_tier"], r6["rule_id"], r6["q2"]["prior_count_24h"]), ("L3", "v0.L3.q2-count", 5))
        other = self._record(100, ["scripts/x.py"], t0 + 700, title="feat: unrelated (#78)")
        self.assertEqual(other["shadow_tier"], "L2")
        later = self._record(101, ["scripts/x.py"], t0 + sh.Q2_WINDOW_S + 1000, title="feat: part 7 (#77)")
        self.assertEqual(later["shadow_tier"], "L2")  # window rolled past

    def test_q2_diff_accumulates(self) -> None:
        t0 = 2_000_000.0
        self._record(1, ["scripts/x.py"], t0, title="feat (#5)", additions=500, deletions=0)
        r = self._record(2, ["scripts/x.py"], t0 + 60, title="feat (#5)", additions=300, deletions=10)
        self.assertEqual((r["shadow_tier"], r["rule_id"]), ("L3", "v0.L3.q2-diff"))

    def test_summary_metrics(self) -> None:
        t0 = 3_000_000.0
        self._record(1, ["docs/a.md"], t0, title="docs (#1)")
        self._record(2, ["scripts/a.py"], t0 + 1, title="feat (#2)")
        self._record(3, [".github/workflows/ci.yml"], t0 + 2, title="ci (#3)")
        s = sh.summarize(sh.read_ledger(self.path), days=14, now=t0 + 10)
        self.assertEqual((s["total"], s["by_tier"], s["agreement_rate"], s["escalation_rate"]),
                         (3, {"L1": 1, "L2": 1, "L3": 1}, round(2 / 3, 4), round(1 / 3, 4)))
        self.assertEqual(s["by_rule"], {"v0.L1.docs-test-only": 1, "v0.L2.code": 1, "v0.L3.path": 1})
        self.assertEqual(sh.summarize(sh.read_ledger(self.path), days=14, now=t0 + 30 * 86400)["total"], 0)

    def test_corrupt_ledger_lines_are_skipped(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"repo":"jinwon-int/ccc-node","ts_epoch":1}\nnot json\n\n', encoding="utf-8")
        self.assertEqual(len(sh.read_ledger(self.path)), 1)


class AskDecisionTest(unittest.TestCase):
    """Spec §10.2 5-d: requests and refusals are rows too; how an ask ended is derived, not written."""

    REPO = "jinwon-int/ccc-node"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "state"
        self.path = sh.ledger_path(self.state)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ask(self, pr, head, now, paths=("docs/a.md",), session=None, **kw):
        row = sh.build_ask_row(repo=self.REPO, pr=pr, head=head * 40, meta=meta(list(paths), **kw), promotion=False,
                               ledger=sh.read_ledger(self.path), session=session, now=now, node="node-x")
        sh.append_ledger(self.path, row)
        return row

    def _approve(self, pr, head, now, paths=("docs/a.md",), **kw):
        row = sh.build_row(repo=self.REPO, pr=pr, head=head * 40, approver="seoseo-ai", meta=meta(list(paths), **kw),
                           promotion=False, ledger=sh.read_ledger(self.path), now=now, node="node-x")
        sh.append_ledger(self.path, row)
        return row

    def _decide(self, pr, head, decision, now, source="reply"):
        sh.append_ledger(self.path, sh.build_decision_row(repo=self.REPO, pr=pr, head=head * 40, decision=decision,
                                                          source=source, now=now, node="node-x"))

    def _resolution(self, pr, head, until):
        return sh.resolve_asks(sh.read_ledger(self.path), until=until)[(self.REPO, pr, head * 40)]

    def test_ask_row_has_the_tier_but_no_approver_and_no_text(self) -> None:
        row = self._ask(1, "a", 1_000.0, title="docs: a (#9) SECRET-TITLE", session="chat-session-id")
        self.assertEqual((row["kind"], row["schema"], row["shadow_tier"], row["rule_id"]),
                         ("ask", sh.SCHEMA, "L1", "v0.L1.docs-test-only"))
        for absent in ("approver", "human_decision", "agreement"):
            self.assertNotIn(absent, row)
        self.assertEqual(len(row["session"]), 12)
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("SECRET-TITLE", text)
        self.assertNotIn("chat-session-id", text)  # only a digest of the session is stored

    def test_every_way_an_ask_can_end(self) -> None:
        t0, day = 10_000.0, 86400.0
        self._ask(1, "a", t0); self._approve(1, "a", t0 + 60)                      # approved
        self._ask(2, "a", t0); self._decide(2, "a", "decline", t0 + 60)           # owner chose another option
        self._ask(3, "a", t0); self._decide(3, "a", "hold", t0 + 60)              # explicit hold
        self._ask(4, "a", t0); self._decide(4, "a", "closed-unmerged", t0 + 60, source="github")
        self._ask(5, "a", t0); self._ask(5, "b", t0 + 60)                          # new head before any answer
        self._ask(6, "a", t0)                                                      # never answered
        self._ask(7, "a", t0 + day)                                                # asked just now
        until = t0 + day + 3600
        got = {pr: self._resolution(pr, "a", until) for pr in range(1, 8)}
        self.assertEqual(got, {1: "approve", 2: "decline", 3: "hold", 4: "closed-unmerged",
                               5: "superseded", 6: "expired", 7: "open"})
        self.assertEqual(self._resolution(5, "b", until), "expired")

    def test_the_first_answer_wins_and_notes_do_not_close_an_ask(self) -> None:
        t0 = 20_000.0
        self._ask(1, "a", t0); self._decide(1, "a", "decline", t0 + 10); self._approve(1, "a", t0 + 20)
        self.assertEqual(self._resolution(1, "a", t0 + 100), "decline")  # a refusal stays a refusal
        self._ask(2, "a", t0); self._decide(2, "a", "reply-unclassified", t0 + 10)
        self.assertEqual(self._resolution(2, "a", t0 + 100), "open")
        self._ask(3, "a", t0); self._approve(3, "a", t0 + 3 * 86400)             # late approval, not "expired"
        self.assertEqual(self._resolution(3, "a", t0 + 4 * 86400), "approve")
        self._ask(4, "a", t0); self._approve(4, "b", t0 + 10)                     # approved at another head
        self.assertEqual(self._resolution(4, "a", t0 + 100), "superseded")

    def test_a_decision_on_another_head_or_before_the_ask_is_ignored(self) -> None:
        t0 = 30_000.0
        self._decide(1, "a", "decline", t0 - 10)
        self._ask(1, "a", t0)
        self._decide(1, "b", "hold", t0 + 10)
        self.assertEqual(self._resolution(1, "a", t0 + 100), "open")

    def test_summary_counts_l1_refusals_inside_the_window_only(self) -> None:
        t0 = 40_000.0
        self._ask(1, "a", t0, paths=("docs/a.md",)); self._decide(1, "a", "decline", t0 + 5)       # L1 refused
        self._ask(2, "a", t0, paths=("scripts/x.py",)); self._decide(2, "a", "hold", t0 + 5)       # L2 refused
        self._ask(3, "a", t0, paths=("docs/b.md",)); self._approve(3, "a", t0 + 5)                 # L1 approved
        self._ask(4, "a", t0 - 5000, paths=("docs/c.md",)); self._decide(4, "a", "decline", t0 - 4000)  # before the window
        self._decide(3, "a", "reply-unclassified", t0 + 6)
        s = sh.summarize(sh.read_ledger(self.path), since=t0 - 100, until=t0 + 100)
        a = s["asks"]
        self.assertEqual((a["total"], a["l1"]["total"]), (3, 2))
        self.assertEqual((a["by_resolution"]["decline"], a["by_resolution"]["hold"], a["by_resolution"]["approve"]), (1, 1, 1))
        self.assertEqual(a["upward_misjudgement"], {"count": 1, "prs": ["jinwon-int/ccc-node#1"]})
        self.assertEqual(a["reply_unclassified"], 1)
        self.assertEqual((s["total"], s["by_tier"]["L1"]), (1, 1))  # approval metrics still count approvals only
        self.assertEqual(sh.summarize(sh.read_ledger(self.path), since=t0 - 6000, until=t0 + 100)["asks"]["upward_misjudgement"]["count"], 2)

    def test_asks_and_decisions_do_not_feed_the_q2_window(self) -> None:
        t0 = 50_000.0
        for i in range(6):
            self._ask(10 + i, "a", t0 + i, paths=("scripts/x.py",), title="feat (#77)", additions=300)
            self._decide(10 + i, "a", "hold", t0 + i + 0.5)
        # Same repo and issue ref as the six asks: only real approvals may count towards Q2.
        row = self._approve(99, "a", t0 + 100, paths=("scripts/x.py",), title="feat (#77)")
        self.assertEqual((row["shadow_tier"], row["q2"]), ("L2", {"prior_count_24h": 0, "prior_diff_24h": 0}))
        again = self._approve(100, "a", t0 + 200, paths=("scripts/x.py",), title="feat (#77)")
        self.assertEqual(again["q2"]["prior_count_24h"], 1)  # the approval above, not the asks

    def test_v1_rows_without_kind_are_approvals_and_same_ask_on_two_nodes_counts_once(self) -> None:
        t0 = 60_000.0
        v1 = {"schema": "ccc.delegated-approval-shadow.v1", "repo": self.REPO, "pr": 1, "head": "a" * 40,
              "shadow_tier": "L2", "rule_id": "v0.L2.code", "ts": "x", "ts_epoch": t0 + 5}
        sh.append_ledger(self.path, v1)
        self._ask(1, "a", t0); self._ask(1, "a", t0 + 1)
        s = sh.summarize(sh.read_ledger(self.path), since=t0 - 1, until=t0 + 100)
        self.assertEqual((s["total"], s["asks"]["total"], s["asks"]["by_resolution"]["approve"]), (1, 1, 1))
        self.assertEqual(s["implicit_ask"], {"approvals": 0, "implicit": 0, "rate": None})  # v1 rows carry no flag


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "state"
        self.pr_json = Path(self._tmp.name) / "pr.json"
        self.pr_json.write_text(json.dumps(meta(["docs/a.md", "scripts/a_test.py"], title="docs (#4)")), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        saved = dict(os.environ)
        os.environ.update(env or {})
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = sh.main(argv)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        return rc, out.getvalue(), err.getvalue()

    def test_record_is_quiet_on_stdout_and_writes_a_row(self) -> None:
        rc, out, err = self._run(["record", "--repo", "jinwon-int/ccc-node", "--pr", "4", "--head", "A" * 40,
                                  "--approver", "seoseo-ai", "--pr-json-file", str(self.pr_json),
                                  "--state-dir", str(self.state), "--node", "n"])
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("L1 v0.L1.docs-test-only", err)
        rows = sh.read_ledger(sh.ledger_path(self.state))
        self.assertEqual((len(rows), rows[0]["head"], rows[0]["approver"]), (1, "a" * 40, "seoseo-ai"))

    def test_record_never_fails_the_caller(self) -> None:
        rc, out, err = self._run(["record", "--repo", "jinwon-int/ccc-node", "--pr", "4", "--head", "a" * 40,
                                  "--approver", "x", "--pr-json-file", "/nonexistent.json", "--state-dir", str(self.state)])
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("not recorded", err)
        rc, out, _ = self._run(["record", "--repo", "evil/x", "--pr", "4", "--head", "a" * 40, "--approver", "x"])
        self.assertEqual((rc, out), (0, ""))

    def test_off_switch(self) -> None:
        rc, out, err = self._run(["record", "--repo", "jinwon-int/ccc-node", "--pr", "4", "--head", "a" * 40,
                                  "--approver", "seoseo-ai", "--pr-json-file", str(self.pr_json), "--state-dir", str(self.state)],
                                 env={"CCC_DELEGATED_SHADOW": "0"})
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertFalse(sh.ledger_path(self.state).exists())

    def _ask_argv(self, pr="4", head="a" * 40):
        return ["ask", "--repo", "jinwon-int/ccc-node", "--pr", pr, "--head", head, "--session", "s",
                "--pr-json-file", str(self.pr_json), "--state-dir", str(self.state), "--node", "n"]

    def test_ask_then_record_marks_the_request_as_recorded(self) -> None:
        rc, out, err = self._run(self._ask_argv(head="A" * 40))
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("ask L1", err)
        rc, out, err = self._run(self._ask_argv())  # asking twice for the same head adds nothing
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("already open", err)
        self._run(["record", "--repo", "jinwon-int/ccc-node", "--pr", "4", "--head", "a" * 40, "--approver", "seoseo-ai",
                   "--pr-json-file", str(self.pr_json), "--state-dir", str(self.state)])
        self._run(["record", "--repo", "jinwon-int/ccc-node", "--pr", "5", "--head", "b" * 40, "--approver", "seoseo-ai",
                   "--pr-json-file", str(self.pr_json), "--state-dir", str(self.state)])
        rows = sh.read_ledger(sh.ledger_path(self.state))
        self.assertEqual([(sh.row_kind(r), r["pr"], r.get("implicit_ask")) for r in rows],
                         [("ask", 4, None), ("approve", 4, False), ("approve", 5, True)])
        rc, out, _ = self._run(["summary", "--state-dir", str(self.state)])
        self.assertEqual(json.loads(out)["implicit_ask"], {"approvals": 2, "implicit": 1, "rate": 0.5})

    def test_decide_attaches_to_the_newest_ask_and_never_fails(self) -> None:
        self._run(self._ask_argv())
        base = ["decide", "--repo", "jinwon-int/ccc-node", "--pr", "4", "--state-dir", str(self.state), "--node", "n"]
        rc, out, err = self._run(base + ["--decision", "decline", "--source", "reply", "--detail", "other-option"])
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("decline via reply", err)
        row = sh.read_ledger(sh.ledger_path(self.state))[-1]
        self.assertEqual((row["kind"], row["head"], row["decision"], row["source"], row["detail"]),
                         ("decision", "a" * 40, "decline", "reply", "other-option"))
        before = len(sh.read_ledger(sh.ledger_path(self.state)))
        rc, out, err = self._run(base + ["--decision", "hold", "--source", "reply", "--detail", "Free text is refused"])
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("class label", err)
        rc, out, err = self._run(["decide", "--repo", "jinwon-int/ccc-node", "--pr", "404", "--decision", "hold",
                                  "--source", "reply", "--state-dir", str(self.state)])
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("no ask", err)
        self.assertEqual(len(sh.read_ledger(sh.ledger_path(self.state))), before)
        rc, _, _ = self._run(["decide", "--repo", "jinwon-int/ccc-node", "--pr", "404", "--head", "c" * 40,
                              "--decision", "closed-unmerged", "--source", "github", "--state-dir", str(self.state)])
        self.assertEqual((rc, len(sh.read_ledger(sh.ledger_path(self.state)))), (0, before + 1))  # reconcile path

    def test_off_switch_covers_ask_and_decide(self) -> None:
        off = {"CCC_DELEGATED_SHADOW": "0"}
        self.assertEqual(self._run(self._ask_argv(), env=off), (0, "", ""))
        self.assertEqual(self._run(["decide", "--repo", "jinwon-int/ccc-node", "--pr", "4", "--head", "a" * 40,
                                    "--decision", "hold", "--source", "manual", "--state-dir", str(self.state)], env=off),
                         (0, "", ""))
        self.assertFalse(sh.ledger_path(self.state).exists())

    def test_summary_window_and_extra_ledgers(self) -> None:
        other = Path(self._tmp.name) / "other-node.jsonl"
        rows = [dict(sh.build_row(repo="jinwon-int/ccc-node", pr=n, head="d" * 40, approver="seoseo-ai",
                                  meta=meta(["scripts/a.py"]), promotion=False, ledger=[], now=ts, node="other"))
                for n, ts in ((1, 1_794_000_000.0), (2, 1_794_100_000.0))]
        other.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        local = sh.ledger_path(self.state)
        sh.append_ledger(local, sh.build_row(repo="jinwon-int/ccc-node", pr=3, head="e" * 40, approver="seoseo-ai",
                                             meta=meta(["docs/a.md"]), promotion=False, ledger=[], now=1_794_050_000.0, node="n"))
        argv = ["summary", "--ledger", str(local), "--ledger", str(other),
                "--since", "2026-11-06T00:00:00+00:00", "--until", "2026-11-08T00:00:00Z"]
        rc, out, _ = self._run(argv)
        s = json.loads(out)
        self.assertEqual((rc, s["total"], s["by_tier"]["L1"], s["since"], s["until"]),
                         (0, 2, 1, "2026-11-06T00:00:00Z", "2026-11-08T00:00:00Z"))
        rc, out, err = self._run(["summary", "--since", "2026-11-06T00:00:00", "--state-dir", str(self.state)])
        self.assertEqual((rc, out), (2, ""))  # a window edge without an offset is ambiguous
        self.assertIn("offset", err)

    def test_summary_cli(self) -> None:
        self._run(["record", "--repo", "jinwon-int/ccc-node", "--pr", "4", "--head", "a" * 40, "--approver", "seoseo-ai",
                   "--pr-json-file", str(self.pr_json), "--state-dir", str(self.state)])
        rc, out, _ = self._run(["summary", "--days", "14", "--state-dir", str(self.state)])
        s = json.loads(out)
        self.assertEqual((rc, s["total"], s["by_tier"]["L1"], s["agreement_rate"]), (0, 1, 1, 1.0))


if __name__ == "__main__":
    unittest.main()
