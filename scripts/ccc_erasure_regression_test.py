"""Regression fixtures for erasure planning and rollback preservation."""
import contextlib
from datetime import datetime, timezone
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("erasure_apply_test", HERE / "ccc-erasure-apply.py")
apply = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apply)
planner = apply.planner


class ErasureRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake_home = self.root / "home"
        self.backups = self.root / "backups"
        self.enterContext(patch.dict(os.environ, {
            "CCC_ERASURE_BACKUP_DIR": str(self.backups), "ERASURE_APPLY": "1",
        }, clear=True))
        self.enterContext(patch.object(planner, "_expand", lambda p:
            str(self.fake_home / p[2:]) if p.startswith("~/") else p))
        self.inventory = json.loads(Path(planner.DEFAULT_INVENTORY).read_text())

    def seed(self, path, body="fixture\n"):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(body)
        path.chmod(0o600)
        return path

    def single_inventory(self, source):
        return {"schema": planner.INVENTORY_SCHEMA, "artifacts": [{
            "id": "fixture.source", "path_class": "node-local derived",
            "resolve": {"candidates": [{"path": str(source)}]},
            "requests": {"node-decommission": "delete"},
        }]}

    def arguments(self, inventory):
        doc = planner.plan("node-decommission", inventory, None, None)
        plan_path = self.root / "plan.json"
        inv_path = self.root / "inventory.json"
        plan_path.write_text(json.dumps(doc))
        inv_path.write_text(json.dumps(inventory))
        return ["apply", "node-decommission", "--inventory", str(inv_path),
                "--plan", str(plan_path), "--plan-digest", apply.canonical_digest(doc)]

    def run_apply(self, inventory):
        with contextlib.redirect_stdout(io.StringIO()):
            return apply.main(self.arguments(inventory))

    def test_stock_inventory_default_paths_are_deleted_once(self):
        source = self.seed(self.fake_home / ".claude/state/resume.md")
        doc = planner.plan("node-decommission", self.inventory, None, None)
        self.assertEqual([t["path"] for t in doc["targets"] if t["present"]], [str(source)])
        self.assertEqual(self.run_apply(self.inventory), 0)
        self.assertFalse(source.exists())
        manifest = json.loads(next(self.backups.glob("*/manifest.json")).read_text())
        self.assertEqual(manifest["deleted"], [str(source)])
        self.assertTrue(manifest["verified"])

    def test_stock_request_actions_survive_scope_filter(self):
        for request in ("cache-rebuild", "prune-expired", "telegram-user-erasure"):
            with self.subTest(request=request):
                expected = {e["id"]: e["requests"][request] for e in self.inventory["artifacts"]
                            if request in e.get("requests", {})
                            and not e["requests"][request].startswith("out-of-scope")
                            and e["requests"][request] != "external-handoff"}
                doc = planner.plan(request, self.inventory, None, "fixture-user")
                self.assertEqual({t["artifact"]: t["action"] for t in doc["targets"]}, expected)

    def test_audience_erasure_stays_scoped(self):
        doc = planner.plan("audience-erasure", self.inventory, "fixture-audience", None)
        self.assertEqual({t["artifact"] for t in doc["targets"]}, {"audience.state_root"})
        self.assertTrue(doc["targets"][0]["path"].endswith("/fixture-audience"))

    def test_same_second_runs_keep_both_preimages(self):
        source = self.seed(self.root / "state/source", "first\n")
        inventory = self.single_inventory(source)

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 5, tzinfo=timezone.utc)

        with patch.object(apply, "datetime", FrozenDateTime):
            self.assertEqual(self.run_apply(inventory), 0)
            first = json.loads(next(self.backups.glob("*/manifest.json")).read_text())
            self.seed(source, "second\n")
            self.assertEqual(self.run_apply(inventory), 0)
        manifests = list(self.backups.glob("*/manifest.json"))
        self.assertEqual(len(manifests), 2)
        self.assertEqual(Path(first["backups"][0]["path"]).read_text(), "first\n")
        self.assertEqual({Path(json.loads(p.read_text())["backups"][0]["path"]).read_text()
                          for p in manifests}, {"first\n", "second\n"})

    def test_backup_base_lock_blocks_before_deletion(self):
        source = self.seed(self.root / "state/source")
        self.backups.mkdir(mode=0o700)
        lock_path = self.seed(self.backups / ".apply.lock", "")
        with lock_path.open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_apply(self.single_inventory(source)), 4)
        self.assertTrue(source.exists())
        self.assertEqual(list(self.backups.glob("*/manifest.json")), [])

    def test_conflicting_actions_block_before_deletion(self):
        source = self.seed(self.root / "state/source")
        inventory = self.single_inventory(source)
        retained = {**inventory["artifacts"][0], "id": "fixture.retained",
                    "requests": {"node-decommission": "retain"}}
        inventory["artifacts"].append(retained)
        self.assertEqual(self.run_apply(inventory), 4)
        self.assertTrue(source.exists())

    def test_recovery_manifest_exists_before_first_unlink(self):
        source = self.seed(self.root / "state/source")
        original_remove = os.remove

        def inspect_then_remove(path):
            manifests = list(self.backups.glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            prepared = json.loads(manifests[0].read_text())
            self.assertEqual(prepared["phase"], "prepared")
            self.assertEqual(prepared["backups"][0]["for"], str(source))
            self.assertEqual(Path(prepared["backups"][0]["path"]).read_text(), "fixture\n")
            return original_remove(path)

        with patch.object(apply.os, "remove", side_effect=inspect_then_remove):
            self.assertEqual(self.run_apply(self.single_inventory(source)), 0)

    def test_prepared_manifest_failure_preserves_source(self):
        source = self.seed(self.root / "state/source")
        with patch.object(apply, "write_manifest", side_effect=apply.Blocked("fixture-write-failure")):
            self.assertEqual(self.run_apply(self.single_inventory(source)), 4)
        self.assertEqual(source.read_text(), "fixture\n")

    def test_final_manifest_failure_keeps_prepared_recovery(self):
        source = self.seed(self.root / "state/source")
        writer = apply.write_manifest

        def fail_final(directory, manifest):
            if manifest["phase"] == "completed":
                raise apply.Blocked("fixture-final-write-failure")
            return writer(directory, manifest)

        with patch.object(apply, "write_manifest", side_effect=fail_final):
            self.assertEqual(self.run_apply(self.single_inventory(source)), 4)
        self.assertFalse(source.exists())
        manifest = json.loads(next(self.backups.glob("*/manifest.json")).read_text())
        self.assertEqual(manifest["phase"], "prepared")
        self.assertEqual(Path(manifest["backups"][0]["path"]).read_text(), "fixture\n")


if __name__ == "__main__":
    unittest.main()
