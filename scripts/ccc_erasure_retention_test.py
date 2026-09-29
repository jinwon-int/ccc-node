"""Retention-class fixtures for the erasure planner/apply boundary (#1468).

Owner decision 2026-09-29: group a (legacy stores) and group b (sensitive
backups) are kept 30 days from mtime, then become eligible for destruction at
the existing apply boundary; key files are always kept. Every file here lives
in a TemporaryDirectory with a fake HOME and a cleared environment — no live
path is ever resolved, and nothing outside the fixture is touched.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import importlib.util

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("erasure_apply_retention", HERE / "ccc-erasure-apply.py")
apply = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apply)
planner = apply.planner

DAY = 86400


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.bot = self.home / ".telegram_bot"
        self.backups = self.root / "backups"
        self.enterContext(patch.dict(os.environ, {
            "CCC_ERASURE_BACKUP_DIR": str(self.backups),
        }, clear=True))
        self.enterContext(patch.object(planner, "_expand", lambda p:
            str(self.home / p[2:]) if p.startswith("~/") else p))
        self.inventory = json.loads(Path(planner.DEFAULT_INVENTORY).read_text())

    def seed(self, path, age_days, body="fixture\n"):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(body)
        path.chmod(0o600)
        stamp = time.time() - age_days * DAY
        os.utime(path, (stamp, stamp))
        return path

    def report(self, inventory=None):
        return {e["path"]: e for e in
                planner.retention_report(inventory or self.inventory)["entries"]}

    def plan_actions(self, request="prune-expired", inventory=None):
        doc = planner.plan(request, inventory or self.inventory, None, None)
        return {t["path"]: t["action"] for t in doc["targets"] if t["present"]}

    def tree(self):
        return sorted((str(p), p.read_bytes()) for p in self.root.rglob("*") if p.is_file())

    # --- acceptance cases ----------------------------------------------------
    def test_31_day_old_env_backup_is_eligible(self):
        old = self.seed(self.bot / ".env.bak-x", 31)
        verdict = self.report()[str(old)]
        self.assertTrue(verdict["eligible"])
        self.assertEqual(verdict["reason"], "retention-expired")
        self.assertEqual(verdict["group"], "b-sensitive-backup")
        self.assertEqual(verdict["max_age_days"], 30)
        self.assertEqual(self.plan_actions()[str(old)], "delete")
        self.assertEqual(self.plan_actions("node-decommission")[str(old)], "delete")

    def test_5_day_old_env_backup_is_retained_with_eligible_date(self):
        young = self.seed(self.bot / ".env.bak-x", 5)
        verdict = self.report()[str(young)]
        self.assertFalse(verdict["eligible"])
        self.assertEqual(verdict["reason"], "within-retention")
        expected = planner._iso(int(young.stat().st_mtime) + 30 * DAY)
        self.assertEqual(verdict["eligible_at"], expected)
        self.assertEqual(self.plan_actions()[str(young)], f"retain-until:{expected}")

    def test_key_file_of_any_age_is_retained(self):
        for age in (0, 31, 4000):
            with self.subTest(age=age):
                key = self.seed(self.bot / f"memory-audience.key.bak-{age}", age)
                verdict = self.report()[str(key)]
                self.assertFalse(verdict["eligible"])
                self.assertEqual(verdict["reason"], "key-file")
                self.assertIsNone(verdict["eligible_at"])
                self.assertTrue(self.plan_actions()[str(key)].startswith("retain"))

    def test_key_file_rule_overrides_a_delete_action(self):
        # A broad retention class that WOULD delete: the key-file guard wins.
        inventory = {"schema": planner.INVENTORY_SCHEMA, "artifacts": [{
            "id": "fixture.backups", "path_class": "node-local backup",
            "resolve": {"candidates": [{"kind": "pattern", "path": "~/.telegram_bot/.*\\.bak-.*"}]},
            "retention_policy": {"group": "b-sensitive-backup"},
            "requests": {"prune-expired": "delete"}}]}
        names = ("id_ed25519.bak-1", "tls.pem.bak-1", "service.key.bak-1",
                 ".credentials.json.bak-1", "plain.txt.bak-1")
        for name in names:
            self.seed(self.bot / name, 400)
        actions = self.plan_actions(inventory=inventory)
        for name in names[:-1]:
            self.assertEqual(actions[str(self.bot / name)], "retain (key-file)", name)
        self.assertEqual(actions[str(self.bot / "plain.txt.bak-1")], "delete")

    def test_report_never_calls_a_retain_class_eligible(self):
        inventory = {"schema": planner.INVENTORY_SCHEMA, "artifacts": [{
            "id": "fixture.kept", "path_class": "node-local backup",
            "resolve": {"candidates": [{"kind": "pattern", "path": "~/.telegram_bot/.*\\.bak-.*"}]},
            "retention_policy": {"group": "b-sensitive-backup"},
            "requests": {"prune-expired": "retain"}}]}
        old = self.seed(self.bot / "plain.txt.bak-1", 400)
        entry = self.report(inventory)[str(old)]
        self.assertFalse(entry["eligible"])
        self.assertEqual(entry["reason"], "class-retain")
        self.assertEqual(entry["planned_action"], "retain")

    def test_dry_run_never_deletes(self):
        self.seed(self.bot / ".env.bak-old", 31)
        self.seed(self.bot / ".env.bak-new", 5)
        self.seed(self.bot / "sessions.json.bak-1", 90)
        self.seed(self.bot / "memory-audience.key.bak-1", 400)
        before = self.tree()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(planner.main(["planner", "retention"]), 0)
            self.assertEqual(planner.main(["planner", "retention", "--json"]), 0)
            self.assertEqual(planner.main(["planner", "prune-expired", "--json"]), 0)
        self.assertEqual(self.tree(), before)
        # Plan-only apply (ERASURE_APPLY unset) of the very same plan.
        doc = planner.plan("prune-expired", self.inventory, None, None)
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps(doc))
        before = self.tree()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = apply.main(["apply", "prune-expired", "--plan", str(plan_path),
                             "--plan-digest", apply.canonical_digest(doc)])
        self.assertEqual(rc, 0)
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.backups.exists())

    # --- policy details ------------------------------------------------------
    def test_retention_days_configurable_but_env_only_lengthens(self):
        young = self.seed(self.bot / ".env.bak-x", 12)
        inventory = json.loads(json.dumps(self.inventory))
        inventory["retention_defaults"]["max_age_days"] = 10
        self.assertTrue(self.report(inventory)[str(young)]["eligible"])
        with patch.dict(os.environ, {planner.RETENTION_DAYS_ENV: "60"}):
            verdict = self.report(inventory)[str(young)]
            self.assertFalse(verdict["eligible"])
            self.assertEqual(verdict["max_age_days"], 60)
        old = self.seed(self.bot / ".env.bak-y", 31)
        for bogus in ("1", "0", "-5", "abc"):
            with self.subTest(env=bogus), patch.dict(os.environ, {planner.RETENTION_DAYS_ENV: bogus}):
                self.assertEqual(self.report()[str(old)]["max_age_days"], 30)
        with patch.dict(os.environ, {planner.RETENTION_DAYS_ENV: "9" * 30}):
            verdict = self.report()[str(old)]   # clamped, no overflow
            self.assertEqual(verdict["max_age_days"], planner.MAX_RETENTION_DAYS)
            self.assertFalse(verdict["eligible"])
        inventory["retention_defaults"]["max_age_days"] = 0   # invalid → default
        self.assertEqual(self.report(inventory)[str(old)]["max_age_days"], 30)

    def test_live_resolver_path_is_never_a_retention_target(self):
        legacy = self.seed(self.home / ".nunchi/facts.db", 400)
        # NUNCHI_DB unset: ~/.nunchi/facts.db IS the live store → excluded.
        self.assertNotIn(str(legacy), self.report())
        self.assertNotIn("legacy.nunchi_unscoped_store",
                         {t["artifact"] for t in planner.plan(
                             "prune-expired", self.inventory, None, None)["targets"]
                          if t["path"] == str(legacy)})
        scoped = self.root / "scoped.db"
        with patch.dict(os.environ, {"NUNCHI_DB": str(scoped)}):
            # Env names a DB that does not exist: the resolver falls back to
            # the default path, so the legacy copy still counts as live.
            self.assertNotIn(str(legacy), self.report())
            # Scoped DB present elsewhere: the default-home copy is a relic.
            self.seed(scoped, 0)
            verdict = self.report()[str(legacy)]
            self.assertTrue(verdict["eligible"])
            self.assertEqual(verdict["group"], "a-legacy-store")

    def test_live_env_file_is_never_a_target(self):
        live = self.seed(self.bot / ".env", 400)
        self.assertNotIn(str(live), self.report())
        self.assertNotEqual(self.plan_actions().get(str(live)), "delete")

    def test_retention_classes_are_classified_not_blockers(self):
        self.seed(self.home / ".claude/state/resume.md", 0)
        names = (".env.bak-1", ".env.pre-webmcp-1", "sessions.json.bak-1",
                 "crontab.bak-1", "memory-audience.key.bak-1")
        for name in names:
            self.seed(self.bot / name, 3)
        self.seed(self.home / ".claude/state/crontab.bak-2", 3)
        unknown = {os.path.basename(u["path"]) for u in planner._run_scan(self.inventory)["unknown"]}
        for name in names + ("crontab.bak-2",):
            self.assertNotIn(name, unknown)

    def test_retention_classes_are_pattern_only(self):
        # A literal candidate would become an ungated primary target.
        for entry in self.inventory["artifacts"]:
            if entry.get("retention_policy"):
                kinds = {c.get("kind") for c in entry["resolve"]["candidates"]}
                self.assertEqual(kinds, {"pattern"}, entry["id"])
                self.assertNotIn("extra_paths", entry, entry["id"])

    def test_armed_apply_in_fixture_deletes_only_eligible(self):
        old = self.seed(self.bot / ".env.bak-old", 31)
        young = self.seed(self.bot / ".env.bak-new", 5)
        key = self.seed(self.bot / "memory-audience.key.bak-1", 400)
        doc = planner.plan("prune-expired", self.inventory, None, None)
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps(doc))
        with patch.dict(os.environ, {"ERASURE_APPLY": "1"}), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = apply.main(["apply", "prune-expired", "--plan", str(plan_path),
                             "--plan-digest", apply.canonical_digest(doc)])
        self.assertEqual(rc, 0)
        self.assertFalse(old.exists())
        self.assertTrue(young.exists())
        self.assertTrue(key.exists())
        manifest = json.loads(next(self.backups.glob("*/manifest.json")).read_text())
        self.assertEqual(manifest["deleted"], [str(old)])
        self.assertTrue(manifest["verified"])


if __name__ == "__main__":
    unittest.main()
