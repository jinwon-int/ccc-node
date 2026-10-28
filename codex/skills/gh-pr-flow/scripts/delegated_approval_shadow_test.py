"""Delegated-approval shadow recorder (a2a-nexus#2317 Phase 1, ccc-node#2137): classification, Q2 window, ledger, summary.

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

    def test_summary_cli(self) -> None:
        self._run(["record", "--repo", "jinwon-int/ccc-node", "--pr", "4", "--head", "a" * 40, "--approver", "seoseo-ai",
                   "--pr-json-file", str(self.pr_json), "--state-dir", str(self.state)])
        rc, out, _ = self._run(["summary", "--days", "14", "--state-dir", str(self.state)])
        s = json.loads(out)
        self.assertEqual((rc, s["total"], s["by_tier"]["L1"], s["agreement_rate"]), (0, 1, 1, 1.0))


if __name__ == "__main__":
    unittest.main()
