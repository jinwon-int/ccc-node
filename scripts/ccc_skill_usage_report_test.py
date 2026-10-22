#!/usr/bin/env python3
"""Hermetic tests for the read-only skill usage report (#2239, #1739).

Pins what makes the report usable as curation evidence: origins come from the
real ownership contract (including the fleet-skills marker), usage is the
curator's ledger union without double counting, missing telemetry never reads
as non-use, the node command writes nothing, and the fleet aggregation picks
archive/graduation/unused sets by the documented thresholds.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "ccc-skill-usage-report.py"
OWNERSHIP = HERE.parent / "claude" / "hooks" / "skill-review" / "ownership.py"

_SPEC = importlib.util.spec_from_file_location("ccc_skill_usage_report", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
report_mod = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = report_mod
_SPEC.loader.exec_module(report_mod)

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
BODY = "UNIQUE-BODY-SENTINEL a sufficiently detailed recurring workflow"


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scrubbed_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CCC_", "NUNCHI_", "CLAUDE_SKILLS"))}
    env.update(extra)
    return env


class NodeFixture:
    """A temp ~/.claude with one skill per origin and both usage ledgers."""

    def __init__(self, root: Path) -> None:
        self.claude = root / ".claude"
        self.skills = self.claude / "skills"
        self.state = self.claude / "state"
        self.skills.mkdir(parents=True, mode=0o700)
        self.state.mkdir(mode=0o700)
        os.chmod(self.claude, 0o700)
        self.env = _scrubbed_env(
            HOME=str(root),
            CCC_CLAUDE_DIR=str(self.claude),
            CCC_NODE="node-a",
            CCC_SKILL_USAGE_REPORT_NOW=NOW.isoformat(),
        )

    def skill(self, name: str) -> Path:
        path = self.skills / name
        path.mkdir(mode=0o700)
        (path / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {BODY}.\n---\n\n# {name}\n\n{BODY}\n",
            encoding="utf-8",
        )
        os.chmod(path / "SKILL.md", 0o600)
        return path

    def private_json(self, path: Path, value: object) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(path, 0o600)

    def autosave(self, name: str) -> None:
        self.skill(name)
        subprocess.run(
            [sys.executable, str(OWNERSHIP), "--provider", "claude", "mark-created", name],
            env=self.env, check=True, capture_output=True,
        )

    def fleet(self, name: str) -> None:
        path = self.skill(name)
        self.private_json(
            path / ".ccc-fleet-skill.json",
            {
                "schema_version": 1, "manager": "ccc-node-fleet-skills",
                "repo": "jinwon-int/fleet-skills", "commit": "0" * 40, "provider": "claude",
                "audience": "all", "name": name, "tree_sha256": "00",
                "source_candidate_id": None, "source_tree_sha256": "00", "reviewed_by": None,
            },
        )
        stamp = (NOW - timedelta(days=20)).timestamp()
        os.utime(path / ".ccc-fleet-skill.json", (stamp, stamp))

    def repo_managed(self, name: str) -> None:
        path = self.skill(name)
        sha = hashlib.sha256((path / "SKILL.md").read_bytes()).hexdigest()
        self.private_json(
            path / ".ccc-node-managed.json",
            {"schema_version": 1, "manager": "ccc-node", "name": name,
             "source": f"skills/{name}", "source_hash": sha, "files": {"SKILL.md": sha}},
        )

    def usage_records(self, records: dict[str, dict[str, object]]) -> None:
        full = {}
        for name, fields in records.items():
            record = {
                "target_id": "t", "view_count": 0, "use_count": 0, "patch_count": 0,
                "created_at": iso(100), "last_viewed_at": None, "last_used_at": None,
                "last_patched_at": None, "state": "active", "stale_marked_at": None,
                "archived_at": None, "archive_name": None,
            }
            record.update(fields)
            full[f"claude:{name}"] = record
        self.private_json(self.state / "skill-autosave-usage.json", {"schema_version": 1, "records": full})

    def ledger(self, rows: list[dict[str, object]]) -> None:
        directory = self.state / "skill-usage"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / "usage.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n", encoding="utf-8")
        os.chmod(path, 0o600)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], env=self.env, capture_output=True, text=True
        )

    def snapshot(self) -> dict[str, tuple[int, int, bytes]]:
        out: dict[str, tuple[int, int, bytes]] = {}
        for path in sorted(self.claude.rglob("*")):
            meta = path.lstat()
            content = path.read_bytes() if path.is_file() else b""
            out[str(path)] = (meta.st_mode, meta.st_mtime_ns, content)
        return out


class NodeReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.node = NodeFixture(Path(self.tmp.name))
        n = self.node
        n.autosave("auto-idle")
        n.fleet("fleet-one")
        n.repo_managed("repo-one")
        n.skill("user-one")
        n.skill("ext-one")
        (n.skills / "ext-one" / ".git").mkdir()
        n.usage_records(
            {
                "auto-idle": {"state": "stale", "stale_marked_at": iso(60), "created_at": iso(100)},
                "fleet-one": {"use_count": 2, "last_used_at": iso(3), "created_at": iso(20)},
            }
        )
        n.ledger(
            [
                {"ts": iso(1), "skill": "fleet-one", "tool": "Skill", "runtime": "claude"},
                {"ts": iso(2), "skill": "fleet-one", "tool": "Read", "runtime": "codex"},
                {"ts": iso(10), "skill": "fleet-one", "tool": "Read"},
                {"ts": iso(40), "skill": "user-one", "tool": "Read"},
                # Older than the auto-idle record: an earlier same-name skill.
                {"ts": iso(150), "skill": "auto-idle", "tool": "Read"},
                {"ts": "garbage", "skill": "user-one", "tool": "Read"},
            ]
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def node_json(self) -> dict:
        result = self.node.run("node", "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_node_report_origins_and_usage_union(self) -> None:
        report = self.node_json()
        self.assertEqual(report["kind"], "ccc-skill-usage-node-report")
        self.assertEqual(report["node"], "node-a")
        self.assertEqual(report["generated_at"], "2026-10-01T12:00:00Z")
        self.assertEqual(report["usage_ledgers"]["claude"]["evidence"], "ok")
        skills = {s["name"]: s for s in report["skills"]}
        self.assertEqual(
            {name: s["origin"] for name, s in skills.items()},
            {
                "auto-idle": "autosave",
                "ext-one": "external",
                "fleet-one": "fleet-managed",
                "repo-one": "repo-managed",
                "user-one": "user",
            },
        )
        fleet = skills["fleet-one"]
        # Skill-tool 2, ledger 3: overlapping ledgers -> max, never the sum.
        self.assertEqual((fleet["use_count_skill_tool"], fleet["use_count_ledger"], fleet["use_count"]), (2, 3, 3))
        self.assertEqual(fleet["window_use_count"], 3)
        self.assertEqual(fleet["runtimes"], {"claude": 2, "codex": 1})
        self.assertEqual(fleet["last_used_at"], iso(1))
        self.assertEqual(fleet["installed_at"], iso(20))
        idle = skills["auto-idle"]
        self.assertEqual(idle["use_count"], 0, "pre-creation ledger row must not count")
        self.assertIsNone(idle["last_used_at"])
        self.assertEqual((idle["curator_state"], idle["stale_marked_at"]), ("stale", iso(60)))
        self.assertEqual(skills["user-one"]["window_use_count"], 0)
        self.assertEqual(skills["user-one"]["use_count"], 1)
        self.assertIsNone(skills["repo-one"]["curator_state"])

    def test_node_report_is_body_free_and_read_only(self) -> None:
        before = self.node.snapshot()
        result = self.node.run("node", "--json")
        text = self.node.run("node")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(text.returncode, 0)
        self.assertNotIn("UNIQUE-BODY-SENTINEL", result.stdout + text.stdout)
        self.assertEqual(before, self.node.snapshot(), "node report must not write state")

    def test_text_output_lists_every_skill(self) -> None:
        result = self.node.run("node")
        self.assertEqual(result.returncode, 0)
        self.assertIn("node node-a", result.stdout)
        for name in ("auto-idle", "fleet-one", "repo-one", "user-one", "ext-one"):
            self.assertIn(name, result.stdout)

    def test_missing_ledgers_are_not_zero_use(self) -> None:
        (self.node.state / "skill-usage" / "usage.jsonl").unlink()
        (self.node.state / "skill-autosave-usage.json").unlink()
        report = self.node_json()
        self.assertEqual(report["usage_ledgers"]["claude"]["evidence"], "missing")
        self.assertTrue(all(s["usage_evidence"] == "missing" for s in report["skills"]))

    def test_unsafe_usage_store_reports_unreadable(self) -> None:
        os.chmod(self.node.state / "skill-autosave-usage.json", 0o644)
        report = self.node_json()
        self.assertEqual(report["usage_ledgers"]["claude"]["skill_tool"], "unreadable")
        self.assertEqual(report["usage_ledgers"]["claude"]["evidence"], "unreadable")

    def test_claude_skills_dir_override_is_respected(self) -> None:
        other = Path(self.tmp.name) / "other-skills"
        other.mkdir(mode=0o700)
        self.node.env["CLAUDE_SKILLS_DIR"] = str(other)
        report = self.node_json()
        self.assertEqual(report["skills"], [])
        self.assertEqual(report["skills_roots"]["claude"], str(other))


def entry(name: str, origin: str, **fields: object) -> dict:
    base = {
        "name": name, "provider": "claude", "origin": origin, "use_count": 0,
        "window_use_count": 0, "last_used_at": None, "created_at": iso(100),
        "installed_at": None, "curator_state": None, "stale_marked_at": None,
        "usage_evidence": "ok",
    }
    base.update(fields)
    return base


def node_report(node: str, skills: list[dict], *, generated: datetime = NOW, evidence: str = "ok") -> dict:
    for skill in skills:
        skill.setdefault("usage_evidence", evidence)
        if evidence != "ok":
            skill["usage_evidence"] = evidence
    return {
        "schema_version": 1, "kind": "ccc-skill-usage-node-report", "node": node,
        "generated_at": generated.strftime("%Y-%m-%dT%H:%M:%SZ"), "window_days": 30,
        "usage_ledgers": {"claude": {"evidence": evidence}}, "skills": skills,
    }


def fleet_reports() -> list[dict]:
    return [
        # An older snapshot of node-a: replaced by the newer one below.
        node_report("node-a", [entry("stale-snapshot-only", "autosave")], generated=NOW - timedelta(days=5)),
        node_report(
            "node-a",
            [
                entry("auto-x", "autosave", last_used_at=iso(40), curator_state="stale", stale_marked_at=iso(10)),
                entry("auto-y", "autosave", last_used_at=iso(40)),
                entry("fleet-g", "fleet-managed", window_use_count=2, use_count=5, last_used_at=iso(2), installed_at=iso(20)),
                entry("fleet-h", "fleet-managed", window_use_count=3, use_count=3, last_used_at=iso(1), installed_at=iso(3)),
                entry("fleet-i", "fleet-managed", window_use_count=1, use_count=1, last_used_at=iso(1), installed_at=iso(30)),
                entry("never", "user"),
                entry("young-auto", "autosave", created_at=iso(5)),
            ],
        ),
        node_report(
            "node-b",
            [
                entry("auto-x", "autosave", last_used_at=iso(35), curator_state="active"),
                entry("auto-y", "user", last_used_at=iso(5), use_count=1),
                entry("fleet-g", "fleet-managed", window_use_count=1, use_count=1, last_used_at=iso(4), installed_at=iso(16)),
                entry("never", "fleet-managed", created_at=None, installed_at=iso(50)),
            ],
            generated=NOW - timedelta(hours=1),
        ),
        node_report("node-c", [entry("auto-z", "autosave", last_used_at=iso(90))], evidence="missing"),
    ]


class AggregateTests(unittest.TestCase):
    def aggregate(self, **overrides: int) -> dict:
        kwargs = {"stale_days": 30, "min_nodes": 2, "min_uses": 3, "dwell_days": 14}
        kwargs.update(overrides)
        return report_mod.build_aggregate(fleet_reports(), **kwargs)

    def test_archive_candidates_require_idle_on_every_node(self) -> None:
        agg = self.aggregate()
        names = [c["name"] for c in agg["archive_candidates"]]
        self.assertEqual(names, ["auto-x"])
        cand = agg["archive_candidates"][0]
        self.assertEqual(cand["min_idle_days"], 35)
        self.assertEqual(cand["nodes"]["node-a"]["curator_state"], "stale")
        self.assertEqual(cand["nodes"]["node-a"]["stale_marked_at"], iso(10))
        self.assertEqual(cand["nodes"]["node-b"]["curator_state"], "active")

    def test_missing_evidence_never_reads_as_idle_or_unused(self) -> None:
        agg = self.aggregate()
        self.assertNotIn("auto-z", [c["name"] for c in agg["archive_candidates"]])
        self.assertNotIn("auto-z", [u["name"] for u in agg["unused"]])
        self.assertEqual([g["node"] for g in agg["evidence_gaps"]], ["node-c"])

    def test_graduation_candidates_by_nodes_or_uses(self) -> None:
        agg = self.aggregate()
        grads = {g["name"]: g for g in agg["graduation_candidates"]}
        self.assertEqual(sorted(grads), ["fleet-g", "fleet-h"])
        self.assertEqual(grads["fleet-g"]["used_nodes"], ["node-a", "node-b"])
        self.assertEqual(grads["fleet-g"]["window_use_count"], 3)
        self.assertEqual(grads["fleet-g"]["earliest_installed_at"], iso(20))
        self.assertEqual(grads["fleet-g"]["dwell"], "met")
        self.assertEqual(grads["fleet-h"]["used_nodes"], ["node-a"])
        self.assertEqual(grads["fleet-h"]["dwell"], "unconfirmed")

    def test_unused_and_origin_totals(self) -> None:
        agg = self.aggregate()
        self.assertEqual([u["name"] for u in agg["unused"]], ["never", "young-auto"])
        self.assertEqual(agg["unused"][0]["origins"], ["fleet-managed", "user"])
        totals = agg["totals"]["by_origin"]
        self.assertEqual(totals["autosave"], {"skills": 4, "installs": 5, "used_installs": 4})
        self.assertEqual(totals["fleet-managed"], {"skills": 4, "installs": 5, "used_installs": 4})
        self.assertEqual(totals["user"], {"skills": 2, "installs": 2, "used_installs": 1})
        self.assertEqual(agg["totals"]["nodes"], 3)
        self.assertNotIn("stale-snapshot-only", json.dumps(agg), "older snapshot must be replaced")

    def test_threshold_flags(self) -> None:
        self.assertEqual(self.aggregate(stale_days=50)["archive_candidates"], [])
        self.assertEqual(
            [g["name"] for g in self.aggregate(min_nodes=2, min_uses=4)["graduation_candidates"]],
            ["fleet-g"],
        )
        self.assertEqual(
            [g["name"] for g in self.aggregate(min_uses=1)["graduation_candidates"]],
            ["fleet-g", "fleet-h", "fleet-i"],
        )

    def test_cli_reads_json_lines_from_stdin(self) -> None:
        lines = "\n".join(json.dumps(r) for r in fleet_reports()) + "\n"
        with patch("sys.stdin", io.StringIO(lines)), patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = report_mod.main(["aggregate", "--json"])
        self.assertEqual(rc, 0)
        agg = json.loads(out.getvalue())
        self.assertEqual(agg["kind"], "ccc-skill-usage-aggregate")
        self.assertEqual([c["name"] for c in agg["archive_candidates"]], ["auto-x"])
        with patch("sys.stdin", io.StringIO(lines)), patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = report_mod.main(["aggregate"])
        self.assertEqual(rc, 0)
        for heading in ("per-origin totals", "archive candidates", "graduation candidates", "never used anywhere", "evidence gaps"):
            self.assertIn(heading, out.getvalue())

    def test_cli_reads_report_files(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = []
            for index, report in enumerate(fleet_reports()):
                path = Path(tmp) / f"r{index}.json"
                path.write_text(json.dumps(report, indent=2), encoding="utf-8")
                paths.append(str(path))
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = report_mod.main(["aggregate", "--json", *paths])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["totals"]["nodes"], 3)

    def test_cli_rejects_foreign_input(self) -> None:
        with patch("sys.stdin", io.StringIO('{"kind": "something-else"}\n')), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as out:
            rc = report_mod.main(["aggregate"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out.getvalue())["code"], "input_not_a_node_report")


def retire_reports() -> list[dict]:
    """#2239 L1 retirement fixture: fleet-managed, unused, on >= 3 nodes."""
    def fleet(name: str, **fields: object) -> dict:
        return entry(name, "fleet-managed", created_at=None, installed_at=iso(1), **fields)

    return [
        node_report("node-a", [
            fleet("fleet-old"), fleet("fleet-two"), fleet("fleet-used", use_count=1, last_used_at=iso(30)),
            fleet("fleet-mixed"), fleet("fleet-gap"), fleet("fleet-viewed"),
        ]),
        node_report("node-b", [
            fleet("fleet-old"), fleet("fleet-two"), fleet("fleet-used"),
            entry("fleet-mixed", "autosave", created_at=iso(120)), fleet("fleet-gap"),
            fleet("fleet-viewed", last_used_at=iso(3)),
        ]),
        node_report("node-c", [fleet("fleet-old"), fleet("fleet-used"), fleet("fleet-mixed"), fleet("fleet-viewed")]),
        node_report("node-d", [fleet("fleet-gap")], evidence="missing"),
    ]


class RetireCandidateTests(unittest.TestCase):
    """#2239 L1 (owner decision 2026-10-22, DOC-3903 P9): fleet retirement candidates.

    Usage facts only. The age rule is applied by scripts/ccc-skill-retire.py
    from fleet-skills approval.json: node installed_at is the fleet marker
    mtime, rewritten by every sync ref change (all 12 nodes read 2026-10-21).
    """

    def aggregate(self, **overrides: int) -> dict:
        kwargs = {"stale_days": 30, "min_nodes": 2, "min_uses": 3, "dwell_days": 14}
        kwargs.update(overrides)
        return report_mod.build_aggregate(retire_reports(), **kwargs)

    def test_defaults_select_unused_fleet_skills_on_enough_nodes(self) -> None:
        agg = self.aggregate()
        self.assertEqual(agg["thresholds"]["retire_min_nodes"], 3)
        self.assertNotIn("retire_idle_days", agg["thresholds"])
        self.assertEqual(agg["retire_candidates"], [
            {"name": "fleet-old", "nodes": ["node-a", "node-b", "node-c"], "holder_count": 3},
        ])
        self.assertNotIn("installed_at", json.dumps(agg["retire_candidates"]))

    def test_exclusions(self) -> None:
        names = {c["name"] for c in self.aggregate()["retire_candidates"]}
        self.assertNotIn("fleet-two", names, "fewer holders than retire_min_nodes")
        self.assertNotIn("fleet-used", names, "a single use anywhere is value evidence")
        self.assertNotIn("fleet-viewed", names, "a last_used_at without a count still counts as use")
        self.assertNotIn("fleet-mixed", names, "an autosave original on one node keeps the name")
        self.assertNotIn("fleet-gap", names, "a telemetry gap never reads as non-use")

    def test_min_nodes_flag(self) -> None:
        self.assertEqual(
            [c["name"] for c in self.aggregate(retire_min_nodes=2)["retire_candidates"]],
            ["fleet-old", "fleet-two"],
        )
        self.assertEqual(self.aggregate(retire_min_nodes=4)["retire_candidates"], [])

    def test_graduation_candidates_are_never_retired(self) -> None:
        agg = report_mod.build_aggregate(fleet_reports(), stale_days=30, min_nodes=2, min_uses=3, dwell_days=14)
        grads = {g["name"] for g in agg["graduation_candidates"]}
        self.assertTrue(grads)
        self.assertFalse(grads & {c["name"] for c in agg["retire_candidates"]})
        # fleet_reports(): "never" is fleet-managed+unused on node-b only (and user on node-a).
        self.assertEqual(agg["retire_candidates"], [])

    def test_cli_flag_and_text_rendering(self) -> None:
        lines = "\n".join(json.dumps(r) for r in retire_reports()) + "\n"
        with patch("sys.stdin", io.StringIO(lines)), patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = report_mod.main(["aggregate", "--json", "--retire-min-nodes", "2"])
        self.assertEqual(rc, 0)
        agg = json.loads(out.getvalue())
        self.assertEqual(agg["thresholds"]["retire_min_nodes"], 2)
        self.assertEqual([c["name"] for c in agg["retire_candidates"]], ["fleet-old", "fleet-two"])
        with patch("sys.stdin", io.StringIO(lines)), patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = report_mod.main(["aggregate"])
        self.assertEqual(rc, 0)
        self.assertIn("retire candidates (fleet-managed on >= 3 nodes", out.getvalue())
        self.assertIn("fleet-old", out.getvalue())


class LifecycleTableTests(unittest.TestCase):
    """#2283 (DOC-3903 TM-3906): node lifecycle counters + fleet lifecycle table."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.node = NodeFixture(Path(self.tmp.name))
        self.node.skill("user-one")
        pending = self.node.state / "pending-skills"
        pending.mkdir(mode=0o700)
        rows = [
            # new-style rows carry lifecycle
            {"id": "a", "dir": "a", "status": "pending", "lifecycle": "drafted", "staged_at": iso(10)},
            {"id": "b", "dir": "b", "status": "pending", "lifecycle": "prescreened:revise", "staged_at": iso(30)},
            # legacy rows (before #2283): derived from status / prescreen
            {"id": "c", "dir": "c", "status": "pending", "prescreen": "approve", "staged_at": iso(2)},
            {"id": "d", "dir": "d.installed-1", "status": "installed", "staged_at": iso(50)},
            {"id": "e", "dir": "e.rejected-1", "status": "rejected"},
            {"id": "f", "dir": "f", "status": "archived:pending-90d"},
            {"id": "g", "dir": "g", "status": "archived:draft-rejected"},
            # #2373: an owner reject reindexed before #2373 kept the P6 name
            {"id": "h", "dir": "h.rejected-1", "status": "rejected", "lifecycle": "verdict:reject"},
            # #2374: a pre-#2374 autosave install kept the P7-shaped name
            {"id": "i", "dir": "i.installed-1", "status": "installed", "lifecycle": "installed@0123456789ab"},
            # #2374: reindexed after #2374 — the owner/manual split comes from reindex
            {"id": "j", "dir": "j.approved-1", "status": "approved", "lifecycle": "local-installed:manual"},
            # duplicate key: the later row wins (staged a, then reindexed as approved)
            {"id": "a2", "dir": "a2", "status": "pending", "lifecycle": "drafted", "staged_at": iso(5)},
            {"id": "a2", "dir": "a2", "status": "approved", "lifecycle": "approved", "staged_at": iso(5)},
        ]
        (pending / "index.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows) + "not json\n", encoding="utf-8"
        )
        fleet = self.node.state / "fleet-skills"
        fleet.mkdir(mode=0o700)
        self.node.private_json(
            fleet / "installed.json",
            {"schema_version": 1, "commit": "0" * 40, "skills": [{"name": "x"}, {"name": "y"}]},
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def node_report(self) -> dict:
        proc = self.node.run("node", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_node_counts_stages_dwell_and_receipt(self) -> None:
        lc = self.node_report()["lifecycle"]
        self.assertEqual(lc["index"], "ok")
        self.assertEqual(lc["malformed_rows"], 1)
        self.assertEqual(lc["rows"], 11)
        self.assertEqual(
            lc["stages"],
            {
                "archived:draft-rejected": 1, "archived:expired": 1,
                "archived:owner-rejected": 2, "drafted": 1,
                "local-installed:auto": 2, "local-installed:manual": 1, "local-installed:owner": 1,
                "prescreened:approve": 1, "prescreened:revise": 1,
            },
        )
        # undecided drafts only: a (10d), b (30d), c (2d); a2 became approved
        self.assertEqual(lc["pending_dwell_days"], [2, 10, 30])
        self.assertEqual(lc["fleet_installed"], {"receipt": "ok", "skills": 2})

    def test_missing_index_is_reported_not_guessed(self) -> None:
        (self.node.state / "pending-skills" / "index.jsonl").unlink()
        lc = self.node_report()["lifecycle"]
        self.assertEqual(lc["index"], "missing")
        self.assertEqual(lc["stages"], {})
        self.assertEqual(lc["pending_dwell_days"], [])

    def test_report_stays_body_free(self) -> None:
        out = self.node.run("node", "--json").stdout
        self.assertNotIn(BODY, out)

    def test_aggregate_merges_nodes_and_renders_the_table(self) -> None:
        one = self.node_report()
        two = json.loads(json.dumps(one))
        two["node"] = "node-b"
        two["lifecycle"]["pending_dwell_days"] = [40]
        # a node that has not picked up #2373 yet still reports verdict:reject
        two["lifecycle"]["stages"] = {
            "drafted": 1, "archived:expired": 2, "verdict:reject": 1,
            "approved": 1, "installed": 2, "installed@0123456789ab": 1,
        }
        legacy = json.loads(json.dumps(one))
        legacy["node"] = "node-old"
        del legacy["lifecycle"]  # a report from before #2283
        agg = report_mod.build_aggregate(
            [one, two, legacy], stale_days=30, min_nodes=2, min_uses=3, dwell_days=14
        )
        lc = agg["lifecycle"]
        self.assertEqual(lc["stages"]["archived:expired"], 3)
        self.assertEqual(lc["stages"]["drafted"], 2)
        self.assertEqual(
            lc["archive_reasons"],
            {"archived:draft-rejected": 1, "archived:expired": 3, "archived:owner-rejected": 3},
        )
        self.assertNotIn("verdict:reject", lc["stages"])
        # #2374: node-local installs never surface under the P6/P7 names
        for p6_p7 in ("approved", "installed", "installed@0123456789ab"):
            self.assertNotIn(p6_p7, lc["stages"])
        self.assertEqual(lc["stages"]["local-installed:auto"], 5)
        self.assertEqual(lc["stages"]["local-installed:owner"], 2)
        self.assertEqual(lc["stages"]["local-installed:manual"], 1)
        self.assertEqual(lc["nodes"]["node-b"]["drafts"], 8)
        self.assertEqual(lc["pending_dwell_days"], {"count": 4, "median": 20.0, "p90": 40, "max": 40})
        self.assertEqual(lc["nodes_without_index"], ["node-old"])
        self.assertEqual(lc["nodes"]["node-a"]["fleet_installed"], 2)
        text = report_mod.render_aggregate(agg)
        self.assertIn("skill lifecycle (#2283, DOC-3903)", text)
        self.assertIn("pending dwell: 4 undecided draft(s)  median 20.0d  p90 40d  max 40d", text)
        self.assertIn("archive reasons: 7", text)
        self.assertIn("nodes without a readable pending index: node-old", text)


if __name__ == "__main__":
    unittest.main()
