#!/usr/bin/env python3
"""Opt-in auto-drain: supersede-close older intake PRs, merge mechanical promote PRs.

Default OFF — a publisher that has not opted in must behave exactly as before.
Reject lineages stay open (policies/REVIEW.md). Identity-hit promote batches
stay draft for a human.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "promotion_auto_drain_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

TREE_A = "a" * 64
TREE_B = "b" * 64


def iso(*, days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def publish_row(pr: str, tree: str, name: str = "s1", node: str = "gwakga") -> dict:
    return {
        "ts": iso(days_ago=40),
        "outcome": "pr-opened",
        "branch": f"skill-intake/{node}/{name}-claude-{tree[:12]}",
        "name": name,
        "node": node,
        "provider": "claude",
        "source": node,
        "state": "OPEN",
        "tree_sha256": tree,
        "url": f"https://github.com/jinwon-int/fleet-skills/pull/{pr}",
    }


def dispatch_row(task: str, pr: str) -> dict:
    return {
        "kind": "a2a-dispatch",
        "dispatched_task": task,
        "pr_url": f"https://github.com/jinwon-int/fleet-skills/pull/{pr}",
        "ts": iso(days_ago=40),
    }


def verdict_row(task: str, verdict: str) -> dict:
    return {
        "kind": "a2a-verdict",
        "task_id": task,
        "verdict": verdict,
        "ts": iso(days_ago=9),
    }


def state_row(pr: str, state: str) -> dict:
    return {"ts": iso(days_ago=1), "kind": "a2a-intake-state", "pr": pr, "state": state}


class SupersedeSweepTests(unittest.TestCase):
    def sweep(self, rows, *, dry_run=False, enabled=True, window=32, close_fails=False):
        written: list[dict] = []
        closed: list[str] = []

        def fake_run(args, **kwargs):
            if args[:3] == ["gh", "pr", "close"]:
                if close_fails:
                    raise promotion.PromotionError("close_denied")
                closed.append(args[3])
            return types.SimpleNamespace(stdout=b"{}")

        cfg = types.SimpleNamespace(
            node="seoseo",
            repo="jinwon-int/fleet-skills",
            collect_window=window,
            supersede_autoclose_enabled=enabled,
        )
        with patch.object(promotion, "_ledger_rows", return_value=rows), \
             patch.object(promotion, "_append_ledger",
                          side_effect=lambda _c, row: written.append(row)), \
             patch.object(promotion, "_run", side_effect=fake_run):
            out = promotion._sweep_superseded_intakes(cfg, dry_run=dry_run)
        return out, written, closed

    def test_disabled_by_default_does_nothing(self) -> None:
        rows = [publish_row("10", TREE_A), publish_row("20", TREE_B)]
        out, written, closed = self.sweep(rows, enabled=False)
        self.assertEqual((out, written, closed), ([], [], []))

    def test_newer_tree_closes_the_older_pr(self) -> None:
        rows = [publish_row("10", TREE_A), publish_row("20", TREE_B)]
        out, written, closed = self.sweep(rows)
        self.assertEqual(closed, ["10"])
        self.assertEqual([r["outcome"] for r in out], ["intake-superseded-closed"])
        self.assertEqual(out[0]["superseded_by"], "20")
        self.assertEqual(written[0]["kind"], "a2a-intake-superseded")
        self.assertEqual(written[1]["state"], "CLOSED")

    def test_single_pr_lineage_is_left_alone(self) -> None:
        out, _, closed = self.sweep([publish_row("10", TREE_A)])
        self.assertEqual((out, closed), ([], []))

    def test_already_closed_old_pr_is_skipped(self) -> None:
        rows = [
            publish_row("10", TREE_A),
            publish_row("20", TREE_B),
            state_row("10", "CLOSED"),
        ]
        out, _, closed = self.sweep(rows)
        self.assertEqual((out, closed), ([], []))

    def test_reject_lineage_is_never_closed(self) -> None:
        task = "skills_intake_review-pr10-gwakga-x"
        rows = [
            publish_row("10", TREE_A),
            publish_row("20", TREE_B),
            dispatch_row(task, "10"),
            verdict_row(task, "reject"),
        ]
        out, _, closed = self.sweep(rows)
        self.assertEqual((out, closed), ([], []))

    def test_different_names_are_not_one_lineage(self) -> None:
        rows = [
            publish_row("10", TREE_A, name="alpha"),
            publish_row("20", TREE_B, name="beta"),
        ]
        out, _, closed = self.sweep(rows)
        self.assertEqual((out, closed), ([], []))

    def test_dry_run_closes_nothing(self) -> None:
        rows = [publish_row("10", TREE_A), publish_row("20", TREE_B)]
        out, written, closed = self.sweep(rows, dry_run=True)
        self.assertEqual((written, closed), ([], []))
        self.assertEqual([r["outcome"] for r in out], ["would-close-superseded-intake"])

    def test_close_failure_writes_no_ledger_row(self) -> None:
        rows = [publish_row("10", TREE_A), publish_row("20", TREE_B)]
        out, written, closed = self.sweep(rows, close_fails=True)
        self.assertEqual(written, [])
        self.assertEqual([r["outcome"] for r in out], ["close-failed"])

    def test_window_bounds_the_closes(self) -> None:
        rows = []
        for n in range(6):
            rows.append(publish_row(str(10 + n), f"{n}" * 64))
        out, _, closed = self.sweep(rows, window=2)
        self.assertEqual(len(closed), 2)


class PromoteMergeSweepTests(unittest.TestCase):
    def sweep(self, rows, *, view=None, dry_run=False, enabled=True, merge_fails=False):
        written: list[dict] = []
        calls: list[list[str]] = []
        payload = view or {
            "isDraft": True,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
            "state": "OPEN",
        }

        def fake_run(args, **kwargs):
            calls.append(list(args))
            if args[:3] == ["gh", "pr", "view"]:
                return types.SimpleNamespace(stdout=json.dumps(payload).encode())
            if args[:3] == ["gh", "pr", "merge"] and merge_fails:
                raise promotion.PromotionError("merge_denied")
            return types.SimpleNamespace(stdout=b"")

        cfg = types.SimpleNamespace(
            node="seoseo",
            repo="jinwon-int/fleet-skills",
            collect_window=32,
            auto_merge_promote_enabled=enabled,
        )
        with patch.object(promotion, "_ledger_rows", return_value=rows), \
             patch.object(promotion, "_append_ledger",
                          side_effect=lambda _c, row: written.append(row)), \
             patch.object(promotion, "_run", side_effect=fake_run):
            out = promotion._sweep_promote_merge(cfg, dry_run=dry_run)
        return out, written, calls

    def open_promote(self, *, hits=None):
        return {
            "kind": "a2a-promote-pr",
            "pr": "999",
            "url": "https://github.com/jinwon-int/fleet-skills/pull/999",
            "state": "OPEN",
            "identity_hits": hits or [],
        }

    def test_disabled_by_default_does_nothing(self) -> None:
        out, written, calls = self.sweep([self.open_promote()], enabled=False)
        self.assertEqual((out, written, calls), ([], [], []))

    def test_identity_hits_stay_draft(self) -> None:
        hits = [{"name": "s1", "hits": [{"line": 1, "kind": "node-name", "match": "gwakga"}]}]
        out, _, calls = self.sweep([self.open_promote(hits=hits)])
        self.assertEqual([r["outcome"] for r in out], ["needs-human-identity"])
        self.assertEqual(calls, [])

    def test_green_draft_is_marked_ready_and_merged(self) -> None:
        out, written, calls = self.sweep([self.open_promote()])
        self.assertEqual([r["outcome"] for r in out], ["promote-pr-merged"])
        self.assertEqual(written[0]["state"], "MERGED")
        verbs = [c[2] for c in calls if c[:2] == ["gh", "pr"]]
        self.assertEqual(verbs, ["view", "ready", "merge"])
        self.assertIn("--squash", calls[-1])
        self.assertNotIn("--admin", calls[-1])

    def test_pending_checks_do_not_merge(self) -> None:
        out, written, _ = self.sweep(
            [self.open_promote()],
            view={
                "isDraft": False,
                "mergeable": "UNKNOWN",
                "mergeStateStatus": "BLOCKED",
                "statusCheckRollup": [{"conclusion": "", "status": "IN_PROGRESS"}],
                "state": "OPEN",
            },
        )
        self.assertEqual([r["outcome"] for r in out], ["waiting-checks"])
        self.assertEqual(written, [])

    def test_failed_checks_do_not_merge(self) -> None:
        out, written, _ = self.sweep(
            [self.open_promote()],
            view={
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "UNSTABLE",
                "statusCheckRollup": [{"conclusion": "FAILURE", "status": "COMPLETED"}],
                "state": "OPEN",
            },
        )
        self.assertEqual([r["outcome"] for r in out], ["promote-pr-checks-failed"])
        self.assertEqual(written, [])

    def test_dry_run_merges_nothing(self) -> None:
        out, written, calls = self.sweep([self.open_promote()], dry_run=True)
        self.assertEqual((written, calls), ([], []))
        self.assertEqual([r["outcome"] for r in out], ["would-merge-promote-pr"])

    def test_latest_row_wins_so_merged_is_not_retried(self) -> None:
        rows = [
            self.open_promote(),
            {**self.open_promote(), "state": "MERGED"},
        ]
        out, _, calls = self.sweep(rows)
        self.assertEqual((out, calls), ([], []))

    def test_merge_failure_writes_no_merged_row(self) -> None:
        out, written, _ = self.sweep([self.open_promote()], merge_fails=True)
        self.assertEqual(written, [])
        self.assertEqual([r["outcome"] for r in out], ["merge-failed"])


AA_HEAD = "c" * 40
AA_TREE = "d" * 64
AA_SCRIPT = "/opt/example/codex/skills/gh-pr-flow/scripts/approve-via-relay.sh"


def aa_diff(files: dict[str, str], *, extra_header: str = "") -> str:
    """A `gh pr diff` body that adds each file whole (like a promote PR)."""
    out: list[str] = []
    for path, content in files.items():
        lines = content.split("\n")
        out += [f"diff --git a/{path} b/{path}", "new file mode 100644"]
        if extra_header:
            out.append(extra_header)
        out += ["index 0000000..1111111", "--- /dev/null", f"+++ b/{path}",
                f"@@ -0,0 +1,{len(lines)} @@"]
        out += ["+" + line for line in lines]
    return "\n".join(out) + "\n"


def aa_files(name: str = "s1", *, support: str = "echo ok", tree: str = AA_TREE) -> dict[str, str]:
    approval = {
        "schema_version": 1,
        "source_candidate_id": f"{name}-{tree[:12]}",
        "source_tree_sha256": tree,
        "approved_at": "2026-10-08T14:00:10Z",
    }
    return {
        f"approved/shared/{name}/SKILL.md":
            f"---\nname: {name}\ndescription: Use when checking things\n---\n# {name}\nSteps.",
        f"approved/shared/{name}/approval.json": json.dumps(approval, indent=2),
        f"approved/shared/{name}/scripts/run.sh": support,
    }


class PromoteAutoApproveTests(unittest.TestCase):
    """#2239 L3: policy-gated approval of mechanical promote PRs.

    Off must be byte-identical to the pre-L3 sweep; dry-run only records;
    true approves through the relay helper on the exact head after a full-diff
    path allowlist + rescan, and any doubt degrades to today's flow.
    """

    def sweep(self, *, mode="true", rows=None, diff=None, view=None, ssh_target="relay-node",
              approve_rc=0, approve_out=None, merge_fails=False, head_after=None,
              cmd_present=True, spool=None):
        written: list[dict] = []
        calls: list[list[str]] = []
        envs: list[dict] = []
        payload = view or {
            "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
            "state": "OPEN", "headRefOid": AA_HEAD, "reviewRequests": [],
        }
        diff_text = aa_diff(aa_files()) if diff is None else diff
        views = {"n": 0}

        def fake_run(args, **kwargs):
            calls.append(list(args))
            if args[:3] == ["gh", "pr", "view"]:
                views["n"] += 1
                if views["n"] > 1 and head_after is not None:
                    return types.SimpleNamespace(stdout=json.dumps({"headRefOid": head_after}).encode())
                return types.SimpleNamespace(stdout=json.dumps(payload).encode())
            if args[:3] == ["gh", "pr", "diff"]:
                return types.SimpleNamespace(stdout=diff_text.encode())
            if args[:3] == ["gh", "pr", "merge"] and merge_fails:
                raise promotion.PromotionError("merge_denied")
            if args[:2] == ["bash", AA_SCRIPT]:
                envs.append(dict(kwargs.get("env") or {}))
                out = approve_out if approve_out is not None else json.dumps(
                    {"ok": True, "approved": True, "head": AA_HEAD, "pr": 999})
                return types.SimpleNamespace(stdout=out.encode(), returncode=approve_rc)
            return types.SimpleNamespace(stdout=b"", returncode=0)

        cfg = types.SimpleNamespace(
            node="publisher", repo="jinwon-int/fleet-skills", collect_window=32,
            auto_merge_promote_enabled=True, auto_approve_mode=mode,
            approve_cmd=("bash", AA_SCRIPT) if cmd_present else (),
            approve_cmd_present=cmd_present, approve_profile="seoseo-ai",
            approve_ssh_target=ssh_target, push_spool=spool,
        )
        ledger = rows if rows is not None else [self.promote_row()]
        with patch.object(promotion, "_ledger_rows", return_value=ledger), \
             patch.object(promotion, "_append_ledger",
                          side_effect=lambda _c, row: written.append(row)), \
             patch.object(promotion, "_promote_worker_nodes", return_value=["workerx"]), \
             patch.object(promotion, "_run", side_effect=fake_run):
            out = promotion._sweep_promote_merge(cfg, dry_run=False)
        return out, written, calls, envs

    @staticmethod
    def promote_row(**extra) -> dict:
        row = {
            "kind": "a2a-promote-pr", "pr": "999", "state": "OPEN",
            "url": "https://github.com/jinwon-int/fleet-skills/pull/999",
            "identity_hits": [], "staged_prs": ["140"], "staged_names": ["s1"],
            "candidates": [f"140:{AA_TREE}"],
        }
        row.update(extra)
        return row

    @staticmethod
    def outcomes(out) -> list[str]:
        return [r["outcome"] for r in out]

    @staticmethod
    def verbs(calls) -> list[str]:
        return [c[2] if c[:2] == ["gh", "pr"] else c[0] for c in calls]

    def merge_call(self, calls) -> list[str]:
        merges = [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
        self.assertEqual(len(merges), 1)
        return merges[0]

    def test_off_is_the_pre_l3_flow(self) -> None:
        out, written, calls, _ = self.sweep(mode="off")
        self.assertEqual(self.outcomes(out), ["promote-pr-merged"])
        self.assertEqual(self.verbs(calls), ["view", "merge"])
        self.assertNotIn("--delete-branch", self.merge_call(calls))
        self.assertNotIn("--match-head-commit", self.merge_call(calls))
        self.assertEqual([r["kind"] for r in written], ["a2a-promote-pr"])

    def test_dry_run_records_would_approve_and_never_approves(self) -> None:
        out, written, calls, _ = self.sweep(mode="dry-run")
        self.assertEqual(self.outcomes(out), ["would-approve-promote-pr", "promote-pr-merged"])
        self.assertTrue(out[0]["new"])
        self.assertNotIn("bash", self.verbs(calls))
        self.assertNotIn("edit", self.verbs(calls))
        aa = [r for r in written if r["kind"] == "a2a-promote-autoapprove"]
        self.assertEqual(len(aa), 1)
        self.assertEqual((aa[0]["pr"], aa[0]["head_sha"], aa[0]["mode"]), ("999", AA_HEAD, "dry-run"))
        self.assertIn("ts", aa[0])
        # today's merge attempt is unchanged
        self.assertNotIn("--delete-branch", self.merge_call(calls))

    def test_dry_run_already_recorded_for_head_is_not_rescanned(self) -> None:
        rows = [self.promote_row(), {"kind": "a2a-promote-autoapprove", "pr": "999",
                                     "head_sha": AA_HEAD, "mode": "dry-run"}]
        out, written, calls, _ = self.sweep(mode="dry-run", rows=rows)
        self.assertEqual(out[0]["outcome"], "would-approve-promote-pr")
        self.assertFalse(out[0]["new"])
        self.assertNotIn("diff", self.verbs(calls))
        self.assertFalse([r for r in written if r["kind"] == "a2a-promote-autoapprove"])

    def test_dry_run_reports_missing_true_mode_config(self) -> None:
        out, _, _, _ = self.sweep(mode="dry-run", ssh_target="")
        self.assertEqual(out[0]["not_ready"], "approve_ssh_target_unset")

    def test_true_clean_approves_then_merges_the_exact_head(self) -> None:
        out, written, calls, envs = self.sweep()
        self.assertEqual(self.outcomes(out), ["promote-autoapproved", "promote-pr-merge-requested"])
        self.assertEqual(self.verbs(calls), ["view", "diff", "view", "edit", "bash", "merge"])
        edit = [c for c in calls if c[:3] == ["gh", "pr", "edit"]][0]
        self.assertEqual(edit[-2:], ["--add-reviewer", "seoseo-ai"])
        approve = [c for c in calls if c[:1] == ["bash"]][0]
        self.assertEqual(approve[2:], [
            "--review-profile", "seoseo-ai", "--repo", "jinwon-int/fleet-skills", "--pr", "999",
            "--expected-head", AA_HEAD, "--operator-approved", "--ssh-target", "relay-node",
        ])
        self.assertEqual(envs[0].get("CCC_EXPLICIT_USER_APPROVAL"), "1")
        merge = self.merge_call(calls)
        self.assertIn("--squash", merge)
        self.assertEqual(merge[merge.index("--match-head-commit") + 1], AA_HEAD)
        self.assertNotIn("--admin", merge)
        self.assertNotIn("--delete-branch", merge, "gh refuses -d on a merge-queue branch")
        aa = [r for r in written if r["kind"] == "a2a-promote-autoapprove"]
        self.assertEqual([(r["mode"], r["profile"], r["head_sha"]) for r in aa],
                         [("approved", "seoseo-ai", AA_HEAD)])
        self.assertFalse([r for r in written if r.get("state") == "MERGED"],
                         "an enqueue is not a merge; the next live read records it")

    def test_requested_reviewer_is_not_requested_again(self) -> None:
        view = {
            "isDraft": False, "mergeable": "MERGEABLE", "state": "OPEN",
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
            "headRefOid": AA_HEAD, "reviewRequests": [{"login": "seoseo-ai"}],
        }
        _, _, calls, _ = self.sweep(view=view)
        self.assertNotIn("edit", self.verbs(calls))

    def test_already_approved_head_skips_the_relay(self) -> None:
        rows = [self.promote_row(), {"kind": "a2a-promote-autoapprove", "pr": "999",
                                     "head_sha": AA_HEAD, "mode": "approved"}]
        out, written, calls, _ = self.sweep(rows=rows)
        self.assertEqual(self.outcomes(out), ["promote-autoapproved", "promote-pr-merge-requested"])
        self.assertFalse(out[0]["new"])
        self.assertNotIn("bash", self.verbs(calls))
        self.assertNotIn("diff", self.verbs(calls))
        self.assertEqual(written, [])

    def test_identity_hit_in_a_support_file_blocks(self) -> None:
        diff = aa_diff(aa_files(support="ssh workerx uptime"))
        out, written, calls, _ = self.sweep(diff=diff)
        self.assertEqual(out[0]["outcome"], "needs-human-autoapprove-blocked")
        self.assertEqual(out[0]["reasons"], ["identity_node_name"])
        self.assertNotIn("workerx", json.dumps(out), "reason codes only, never values")
        self.assertNotIn("bash", self.verbs(calls))
        self.assertNotIn("--delete-branch", self.merge_call(calls), "merge-queue repo: gh refuses -d")
        self.assertFalse([r for r in written if r["kind"] == "a2a-promote-autoapprove"])

    def test_secret_shaped_support_file_blocks(self) -> None:
        diff = aa_diff(aa_files(support="export TOKEN=" + "Q" * 30))
        out, _, calls, _ = self.sweep(diff=diff)
        self.assertEqual(out[0]["outcome"], "needs-human-autoapprove-blocked")
        self.assertTrue(all(code.startswith("scan_secret_") for code in out[0]["reasons"]))
        self.assertNotIn("bash", self.verbs(calls))

    def test_path_outside_the_allowlist_blocks(self) -> None:
        for path in (".github/workflows/ci.yml", "approved/shared/other/SKILL.md",
                     "intake/n/claude/s1/skill/SKILL.md", "approved/nowhere/s1/SKILL.md"):
            with self.subTest(path=path):
                files = aa_files()
                files[path] = "harmless"
                out, _, calls, _ = self.sweep(diff=aa_diff(files))
                self.assertEqual(out[0]["outcome"], "needs-human-autoapprove-blocked")
                self.assertIn("path_outside_allowlist", out[0]["reasons"])
                self.assertNotIn("bash", self.verbs(calls))

    def test_row_without_staged_names_blocks(self) -> None:
        rows = [self.promote_row(staged_names=None)]
        out, _, calls, _ = self.sweep(rows=rows)
        self.assertEqual(out[0]["reasons"], ["staged_names_missing"])
        self.assertNotIn("diff", self.verbs(calls))

    def test_tampered_approval_json_blocks(self) -> None:
        diff = aa_diff(aa_files(tree="e" * 64))  # not a reviewed tree of this batch
        out, _, _, _ = self.sweep(diff=diff)
        self.assertEqual(out[0]["reasons"], ["approval_json_invalid"])

    def test_binary_and_symlink_entries_block(self) -> None:
        binary = aa_diff(aa_files()) + (
            "diff --git a/approved/shared/s1/logo.png b/approved/shared/s1/logo.png\n"
            "new file mode 100644\nindex 0000000..2222222\n"
            "Binary files /dev/null and b/approved/shared/s1/logo.png differ\n")
        out, _, _, _ = self.sweep(diff=binary)
        self.assertIn("diff_binary_file", out[0]["reasons"])
        link = aa_diff({"approved/shared/s1/link": "../../x"}).replace(
            "new file mode 100644", "new file mode 120000")
        out, _, _, _ = self.sweep(diff=link)
        self.assertIn("diff_symlink", out[0]["reasons"])

    def test_head_moving_during_the_diff_blocks(self) -> None:
        out, _, calls, _ = self.sweep(head_after="f" * 40)
        self.assertEqual(out[0]["reasons"], ["head_changed"])
        self.assertNotIn("bash", self.verbs(calls))

    def test_just_marked_ready_defers_one_sweep(self) -> None:
        view = {
            "isDraft": True, "mergeable": "MERGEABLE", "state": "OPEN",
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}],
            "headRefOid": AA_HEAD, "reviewRequests": [],
        }
        out, _, calls, _ = self.sweep(view=view)
        self.assertEqual(out[0]["outcome"], "autoapprove-deferred")
        self.assertNotIn("bash", self.verbs(calls))

    def test_unset_ssh_target_is_not_configured(self) -> None:
        out, written, calls, _ = self.sweep(ssh_target="")
        self.assertEqual(out[0], {"outcome": "approve-not-configured", "pr": "999",
                                  "code": "approve_ssh_target_unset"})
        self.assertNotIn("diff", self.verbs(calls))
        self.assertNotIn("bash", self.verbs(calls))
        self.assertNotIn("--delete-branch", self.merge_call(calls))
        self.assertFalse([r for r in written if r["kind"] == "a2a-promote-autoapprove"])

    def test_missing_approve_cmd_is_not_configured(self) -> None:
        out, _, calls, _ = self.sweep(cmd_present=False)
        self.assertEqual(out[0]["code"], "approve_cmd_missing")
        self.assertNotIn("bash", self.verbs(calls))

    def test_approve_failure_falls_back_to_todays_merge_attempt(self) -> None:
        out, written, calls, _ = self.sweep(approve_rc=65, approve_out="", merge_fails=True)
        self.assertEqual(self.outcomes(out), ["approve-failed", "merge-failed"])
        self.assertEqual(out[0]["code"], "approve_exit_65")
        aa = [r for r in written if r["kind"] == "a2a-promote-autoapprove"]
        self.assertEqual([(r["mode"], r["code"]) for r in aa], [("approve-failed", "approve_exit_65")])
        merge = self.merge_call(calls)
        self.assertNotIn("--delete-branch", merge)
        self.assertNotIn("--match-head-commit", merge)
        self.assertNotIn("--admin", merge)

    def test_unverifiable_relay_output_is_a_failure(self) -> None:
        out, _, _, _ = self.sweep(approve_out=json.dumps({"ok": True, "approved": True, "head": "0" * 40}))
        self.assertEqual(out[0]["outcome"], "approve-failed")
        self.assertEqual(out[0]["code"], "approve_output_invalid")

    def test_failed_attempts_are_capped_per_head(self) -> None:
        rows = [self.promote_row()] + [
            {"kind": "a2a-promote-autoapprove", "pr": "999", "head_sha": AA_HEAD,
             "mode": "approve-failed", "code": "approve_exit_65"}
        ] * 3
        out, _, calls, _ = self.sweep(rows=rows)
        self.assertEqual(out[0]["code"], "approve_attempts_exhausted")
        self.assertNotIn("bash", self.verbs(calls))

    def test_owner_is_notified_once_with_pr_numbers_only(self) -> None:
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            spool = Path(tmp) / "spool"
            self.sweep(spool=spool)
            files = sorted(spool.glob("*.json"))
            self.assertEqual(len(files), 1)
            record = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(record["event"], "SkillPromoteAutoApprove")
            self.assertIn("#999", record["text"])
            self.assertIn("1건", record["text"])
            self.assertNotIn(AA_HEAD, record["text"])
            self.assertFalse(list(spool.glob(".*.tmp")))
            # second sweep: approval already recorded -> no new notice
            rows = [self.promote_row(), {"kind": "a2a-promote-autoapprove", "pr": "999",
                                         "head_sha": AA_HEAD, "mode": "approved"}]
            self.sweep(rows=rows, spool=spool)
            self.assertEqual(len(list(spool.glob("*.json"))), 1)

    def test_dry_run_notice_says_nothing_was_approved(self) -> None:
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            spool = Path(tmp) / "spool"
            self.sweep(mode="dry-run", spool=spool)
            record = json.loads(next(spool.glob("*.json")).read_text(encoding="utf-8"))
            self.assertIn("dry-run", record["text"])
            self.assertIn("#999", record["text"])

    def test_blocked_or_off_never_notifies(self) -> None:
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            spool = Path(tmp) / "spool"
            self.sweep(mode="off", spool=spool)
            self.sweep(diff=aa_diff(aa_files(support="workerx")), spool=spool)
            self.assertFalse(spool.exists() and list(spool.glob("*.json")))


class ResultsOnlyTests(unittest.TestCase):
    def test_results_only_never_reads_or_acks_outboxes(self):
        from tempfile import TemporaryDirectory
        from contextlib import ExitStack
        with TemporaryDirectory() as tmp, ExitStack() as stack:
            cfg = promotion._config({"HOME": tmp, "CCC_NODE": "testnode",
                                     "CCC_SKILL_PROMOTION_PUBLISHER": "true"})
            stack.enter_context(patch.object(promotion, "_run"))
            private = stack.enter_context(patch.object(promotion, "_require_private_repo"))
            completed = stack.enter_context(patch.object(promotion, "_collect_completed", return_value={"revise": None}))
            for name in ("_collect_envelopes", "_write_collect_cursor", "_publish", "_remote_ack", "_ack_local"):
                stack.enter_context(patch.object(promotion, name, side_effect=AssertionError("new intake forbidden")))
            result = promotion._collect(cfg, dry_run=False, results_only=True)
            self.assertEqual(result["mode"], "collect-results")
            self.assertEqual(result["published"], [])
            private.assert_called_once_with(cfg)
            completed.assert_called_once_with(cfg, dry_run=False)
            self.assertFalse((cfg.promotion_state_dir / "outbox").exists())

    def test_results_only_honors_disabled_kill_dryrun_and_lock(self):
        from tempfile import TemporaryDirectory
        from dataclasses import replace
        with TemporaryDirectory() as tmp:
            cfg = promotion._config({"HOME": tmp, "CCC_NODE": "testnode",
                                     "CCC_SKILL_PROMOTION_PUBLISHER": "true"})
            with patch.object(promotion, "_collect_results_unlocked", return_value={"ok": True}) as run:
                promotion._collect(replace(cfg, publisher_enabled=False), dry_run=False, results_only=True)
                promotion._collect(replace(cfg, autonomy="kill"), dry_run=False, results_only=True)
                run.assert_not_called()
                with promotion._promotion_lock(cfg) as acquired:
                    self.assertTrue(acquired)
                    result = promotion._collect(cfg, dry_run=False, results_only=True)
                    self.assertEqual(result["status"], "locked")
                run.assert_not_called()
                promotion._collect(replace(cfg, autonomy="dry-run"), dry_run=False, results_only=True)
                run.assert_called_once_with(replace(cfg, autonomy="dry-run"), dry_run=True)

    def test_android_state_anchor_preserves_descendant_checks(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            system = Path(tmp) / "system"
            home = system / "app-home"
            home.mkdir(parents=True, mode=0o700)
            system.chmod(0o777)
            state = home / "state" / "promotion"
            with self.assertRaises(promotion.PromotionError):
                promotion._private_state_dir(state)
            promotion._private_state_dir(state, trust_root=home)
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            (home / "state").chmod(0o777)
            with self.assertRaises(promotion.PromotionError):
                promotion._private_state_dir(state, trust_root=home)
            (home / "state").chmod(0o700)
            link = home / "link"
            link.symlink_to(state, target_is_directory=True)
            with self.assertRaises(promotion.PromotionError):
                promotion._private_state_dir(link / "child", trust_root=home)
            with self.assertRaises(promotion.PromotionError):
                promotion._private_state_dir(home / ".." / "escaped", trust_root=home)
            self.assertFalse((system / "escaped").exists())


class AndroidOwnershipTests(unittest.TestCase):
    def test_real_helper_stages_and_exports_under_app_home(self):
        import hashlib
        import shutil
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            system = Path(tmp) / "system"
            home = system / "app-home"
            hooks = home / ".claude" / "hooks"
            helper = hooks / "skill-review" / "ownership.py"
            helper.parent.mkdir(parents=True)
            system.chmod(0o777)
            repo = Path(__file__).resolve().parents[1]
            shutil.copyfile(repo / "claude/hooks/skill-review/ownership.py", helper)
            for source, installed in (("secure_fs.py", "ccc_secure_fs.py"),
                                      ("skill_frontmatter.py", "ccc_skill_frontmatter.py")):
                shutil.copyfile(repo / "bridge/utils" / source, hooks / installed)
            skill = home / ".claude/skills/release-verification"
            skill.mkdir(parents=True)
            payload = ("---\nname: release-verification\n"
                       "description: Use when verifying a release through a reusable procedure.\n"
                       "---\n\n# Procedure\n\n1. Inspect the release.\n2. Verify the result.\n")
            (skill / "SKILL.md").write_text(payload)
            (skill / ".autosave-meta.json").write_text(json.dumps({
                "schema_version": 2, "manager": "ccc-node-skill-autosave",
                "ownership": "autosave-managed", "provider": "claude",
                "name": skill.name, "target_id": hashlib.sha256(
                    f"claude\0{hashlib.sha256(str(skill.parent).encode()).hexdigest()}\0{skill.name}".encode()
                ).hexdigest(),
                "skill_sha256": hashlib.sha256(payload.encode()).hexdigest(),
                "created_by": "ccc-node", "provenance_revision": 1,
                "rollback_eligible": True,
            }))
            for path in [home, *home.rglob("*")]:
                path.chmod(0o700 if path.is_dir() else 0o600)
            cfg = promotion._config({"HOME": str(home), "CCC_NODE": "testnode",
                                     "CCC_SKILL_PROMOTION_ENABLED": "true",
                                     "CCC_SKILL_PROMOTION_PROVIDERS": "claude"})
            result = promotion._execute(cfg, dry_run=False)
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["blocked"], [])
            self.assertEqual(len(result["staged"]), 1)
            exported = promotion._export_result(cfg, limit=1)
            self.assertEqual(len(exported["envelopes"]), 1)
            self.assertEqual(exported["envelopes"][0]["node"], "testnode")
            again = promotion._execute(cfg, dry_run=False)
            self.assertEqual(again["staged"][0]["outcome"], "already-staged")

    def test_helper_anchor_keeps_unsafe_paths_closed(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            system = Path(tmp) / "system"
            home = system / "app-home"
            parent = home / "hooks"
            parent.mkdir(parents=True)
            system.chmod(0o777)
            tool = parent / "ownership.py"
            tool.write_text("print('{}')\n")
            tool.chmod(0o600)
            self.assertFalse(promotion._safe_tool(tool))
            self.assertTrue(promotion._safe_tool(tool, trust_root=home))
            for path in [tool, parent, home]:
                original = path.stat().st_mode & 0o777
                path.chmod(0o777)
                self.assertFalse(promotion._safe_tool(tool, trust_root=home))
                path.chmod(original)
            link = home / "linked-tool"
            link.symlink_to(tool)
            self.assertFalse(promotion._safe_tool(link, trust_root=home))
            linked_parent = home / "linked-parent"
            linked_parent.symlink_to(parent, target_is_directory=True)
            self.assertFalse(promotion._safe_tool(linked_parent / tool.name, trust_root=home))
            outside = system / "outside.py"
            outside.write_text("print('{}')\n")
            outside.chmod(0o600)
            self.assertFalse(promotion._safe_tool(outside, trust_root=home))
            self.assertFalse(promotion._safe_tool(home / ".." / outside.name, trust_root=home))
            linked_home = system / "linked-home"
            linked_home.symlink_to(home, target_is_directory=True)
            self.assertFalse(promotion._safe_tool(linked_home / "hooks" / tool.name,
                                                  trust_root=linked_home))

    def test_traversal_through_safe_parent_cannot_execute_outside_helper(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            system = Path(tmp) / "untrusted"
            parent = system / "safe-parent"
            home = parent / "app-home"
            skills = home / ".claude/skills"
            skills.mkdir(parents=True)
            system.chmod(0o777)
            parent.chmod(0o700)
            home.chmod(0o700)
            marker = parent / "executed"
            outside = parent / "outside.py"
            outside.write_text("from pathlib import Path\n"
                               f"Path({str(marker)!r}).touch()\n"
                               "print('{\"skills\":[]}')\n")
            outside.chmod(0o600)
            cfg = promotion._config({
                "HOME": str(home), "CCC_NODE": "testnode",
                "CCC_SKILL_PROMOTION_OWNERSHIP_TOOL": str(home / ".." / outside.name),
            })
            self.assertFalse(promotion._safe_tool(outside, trust_root=home))
            with self.assertRaisesRegex(promotion.PromotionError, "ownership_tool_unsafe"):
                promotion._ownership_rows(cfg, "claude")
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main(verbosity=0)
