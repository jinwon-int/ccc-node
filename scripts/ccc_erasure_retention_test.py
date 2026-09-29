"""Retention-class fixtures for the erasure planner/apply boundary (#1468).

Owner decision 2026-09-29: group a (legacy stores) and group b (sensitive
backups) are kept 30 days, then become eligible for destruction at the
existing apply boundary; key files are always kept. Every file here lives in
a TemporaryDirectory with a fake HOME and a cleared environment — no live
path is ever resolved, and nothing outside the fixture is touched.

Clock model: the age basis is max(mtime, ctime) and ctime cannot be set
backwards, so tests run the planner at a fixed future instant ``self.T``
(the planner's ``_now`` seam) and seed each file's mtime at ``T - age``.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("erasure_apply_retention", HERE / "ccc-erasure-apply.py")
apply = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apply)
planner = apply.planner

DAY = 86400
HORIZON_DAYS = 400   # seeded ages up to this are exact under the test clock


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.bot = self.home / ".telegram_bot"
        self.backups = self.root / "backups"
        self.T = time.time() + HORIZON_DAYS * DAY
        self.enterContext(patch.dict(os.environ, {
            "CCC_ERASURE_BACKUP_DIR": str(self.backups),
        }, clear=True))
        self.enterContext(patch.object(planner, "_expand", lambda p:
            str(self.home / p[2:]) if p.startswith("~/") else p))
        self.enterContext(patch.object(planner, "_now", lambda: self.T, create=True))
        self.inventory = json.loads(Path(planner.DEFAULT_INVENTORY).read_text())

    def seed(self, path, age_days, body="fixture\n"):
        """File whose age at the test clock is ``age_days`` (≤ HORIZON)."""
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(body)
        path.chmod(0o600)
        stamp = self.T - age_days * DAY
        os.utime(path, (stamp, stamp))
        return path

    def live_env(self):
        return self.seed(self.bot / ".env", 0)

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
        self.live_env()
        old = self.seed(self.bot / ".env.bak-x", 31)
        verdict = self.report()[str(old)]
        self.assertTrue(verdict["eligible"])
        self.assertEqual(verdict["reason"], "retention-expired")
        self.assertEqual(verdict["group"], "b-sensitive-backup")
        self.assertEqual(verdict["max_age_days"], 30)
        self.assertEqual(self.plan_actions()[str(old)], "delete")
        self.assertEqual(self.plan_actions("node-decommission")[str(old)], "delete")

    def test_5_day_old_env_backup_is_retained_with_eligible_date(self):
        self.live_env()
        young = self.seed(self.bot / ".env.bak-x", 5)
        verdict = self.report()[str(young)]
        self.assertFalse(verdict["eligible"])
        self.assertEqual(verdict["reason"], "within-retention")
        meta = young.stat()
        expected = planner._iso(int(max(meta.st_mtime, meta.st_ctime)) + 30 * DAY)
        self.assertEqual(verdict["eligible_at"], expected)
        self.assertEqual(self.plan_actions()[str(young)], f"retain-until:{expected}")

    def test_key_file_of_any_age_is_retained(self):
        self.live_env()
        for age in (0, 31, HORIZON_DAYS):
            with self.subTest(age=age):
                key = self.seed(self.bot / f"memory-audience.key.bak-{age}", age)
                verdict = self.report()[str(key)]
                self.assertFalse(verdict["eligible"])
                self.assertEqual(verdict["reason"], "key-file")
                self.assertIsNone(verdict["eligible_at"])
                self.assertTrue(self.plan_actions()[str(key)].startswith("retain"))

    def test_key_file_rule_overrides_a_delete_action(self):
        # A broad retention class that WOULD delete: the key-file guard wins,
        # case-insensitively and anywhere in the name (review m1).
        inventory = {"schema": planner.INVENTORY_SCHEMA, "artifacts": [{
            "id": "fixture.backups", "path_class": "node-local backup",
            "resolve": {"candidates": [{"kind": "pattern", "path": "~/.telegram_bot/.*\\.bak-.*"}]},
            "retention_policy": {"group": "b-sensitive-backup"},
            "requests": {"prune-expired": "delete"}}]}
        keys = ("id_ed25519.bak-1", "tls.pem.bak-1", "service.key.bak-1",
                ".credentials.json.bak-1", ".env.bak-x.PEM", ".env.bak-ID_ED25519",
                ".env.bak-CREDENTIALS", ".env.bak-x.P12", ".env.bak-x.gpg",
                ".env.bak-x.age", ".env.bak-secret", ".env.bak-token.json",
                ".env.bak-oauth", "sessions.json.bak-auth.json", ".env.bak-netrc",
                ".env.bak-hosts.yml")
        plain = ("plain.txt.bak-1", ".env.bak-monkey", ".env.bak-page")
        for name in keys + plain:
            self.seed(self.bot / name, HORIZON_DAYS)
        actions = self.plan_actions(inventory=inventory)
        for name in keys:
            self.assertEqual(actions[str(self.bot / name)], "retain (key-file)", name)
        for name in plain:
            self.assertEqual(actions[str(self.bot / name)], "delete", name)

    def test_dry_run_never_deletes(self):
        self.live_env()
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
        self.assertIn("delete", {t["action"] for t in doc["targets"] if t["present"]})
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps(doc))
        before = self.tree()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = apply.main(["apply", "prune-expired", "--plan", str(plan_path),
                             "--plan-digest", apply.canonical_digest(doc)])
        self.assertEqual(rc, 0)
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.backups.exists())

    # --- review M1: live file reached through a symlink / hard link ----------
    def test_symlinked_live_env_protects_its_target(self):
        target = self.seed(self.bot / ".env.pre-mig", 60)
        (self.bot / ".env").symlink_to(".env.pre-mig")
        self.assertNotIn(str(target), self.report())
        self.assertNotIn(str(target), self.plan_actions())
        self.assertNotIn(str(target), self.plan_actions("node-decommission"))

    def test_symlinked_nunchi_db_protects_the_legacy_file(self):
        legacy = self.seed(self.home / ".nunchi/facts.db", 60)
        # Marker present: M3's legacy claim is released, so only M1 protects.
        self.seed(self.home / ".nunchi/.legacy-retired", 0)
        link = self.root / "scope/nunchi/facts.db"
        link.parent.mkdir(parents=True)
        link.symlink_to(legacy)
        with patch.dict(os.environ, {"NUNCHI_DB": str(link)}):
            self.assertNotIn(str(legacy), self.report())
            self.assertNotEqual(self.plan_actions().get(str(legacy)), "delete")

    @unittest.skipUnless(hasattr(os, "link"), "hard links unavailable (Android)")
    def test_hard_link_of_live_env_is_not_a_target(self):
        live = self.live_env()
        twin = self.bot / ".env.bak-hard"
        try:
            os.link(live, twin)
        except OSError as exc:
            self.skipTest(f"hard link refused: {exc}")
        os.utime(twin, (self.T - 60 * DAY,) * 2)
        self.assertNotIn(str(twin), self.report())

    # --- review M2: copies that preserve an old mtime ------------------------
    def test_copy_preserving_old_mtime_is_not_instantly_eligible(self):
        self.T = time.time()                       # real clock: ctime is "now"
        live = self.bot / ".env"
        live.parent.mkdir(parents=True, mode=0o700)
        live.write_text("fixture\n")
        os.utime(live, (self.T - 90 * DAY,) * 2)   # an old live file ...
        copy = self.bot / ".env.bak-20260929"
        shutil.copy2(live, copy)                   # ... backed up today (cp -p)
        self.assertLess(copy.stat().st_mtime, self.T - 89 * DAY)
        verdict = self.report()[str(copy)]
        self.assertFalse(verdict["eligible"])
        self.assertEqual(verdict["age_source"], "max(mtime,ctime)")
        self.assertTrue(self.plan_actions()[str(copy)].startswith("retain-until:"))

    # --- review M3: legacy nunchi store liveness is env-independent ----------
    def test_legacy_nunchi_store_stays_claimed_while_env_points_elsewhere(self):
        legacy = {n: self.seed(self.home / ".nunchi" / n, 60)
                  for n in ("facts.db", "snapshot.md", "backend-health.json")}
        scoped = self.seed(self.root / "scope/facts.db", 0)
        env = {"NUNCHI_DB": str(scoped), "NUNCHI_SNAPSHOT": str(self.root / "scope/snapshot.md"),
               "NUNCHI_HOME": str(self.root / "scope")}
        with patch.dict(os.environ, env):
            report = self.report()
            for path in legacy.values():
                self.assertNotIn(str(path), report)
                self.assertNotEqual(self.plan_actions().get(str(path)), "delete")
            # The operator's explicit retirement marker releases the store.
            self.seed(self.home / ".nunchi/.legacy-retired", 0)
            report = self.report()
            self.assertTrue(report[str(legacy["facts.db"])]["eligible"])
            self.assertEqual(report[str(legacy["facts.db"])]["group"], "a-legacy-store")
            self.assertEqual(self.plan_actions()[str(legacy["facts.db"])], "delete")
            # Decommission never bypasses the handoff contract for the DB.
            self.assertEqual(self.plan_actions("node-decommission")[str(legacy["facts.db"])],
                             "handoff-or-drop")
            unknown = {u["path"] for u in planner._run_scan(self.inventory)["unknown"]}
            self.assertNotIn(str(self.home / ".nunchi/.legacy-retired"), unknown)

    def test_default_nunchi_db_is_live_without_env(self):
        legacy = self.seed(self.home / ".nunchi/facts.db", 60)
        self.seed(self.home / ".nunchi/.legacy-retired", 0)
        # NUNCHI_DB unset: ~/.nunchi/facts.db IS the live store → excluded.
        self.assertNotIn(str(legacy), self.report())

    # --- review m2: never destroy the only remaining copy --------------------
    def test_newest_copy_kept_while_live_counterpart_missing(self):
        older = self.seed(self.bot / ".env.bak-a", 40)
        newest = self.seed(self.bot / ".env.bak-b", 35)
        report = self.report()
        self.assertTrue(report[str(older)]["eligible"])
        self.assertFalse(report[str(newest)]["eligible"])
        self.assertEqual(report[str(newest)]["reason"], "last-copy-live-missing")
        actions = self.plan_actions()
        self.assertEqual(actions[str(older)], "delete")
        self.assertEqual(actions[str(newest)], "retain (last copy; live missing)")
        self.live_env()                            # live file back → both expire
        self.assertEqual(self.plan_actions()[str(newest)], "delete")

    def test_sessions_and_crontab_families_keep_newest_copy(self):
        s_old = self.seed(self.bot / "sessions.json.bak-1", 60)
        s_new = self.seed(self.bot / "sessions.json.bak-2", 50)
        c_old = self.seed(self.bot / "crontab.bak-1", 60)
        c_new = self.seed(self.bot / "crontab.bak-2", 50)
        actions = self.plan_actions()
        self.assertEqual(actions[str(s_old)], "delete")
        self.assertEqual(actions[str(s_new)], "retain (last copy; live missing)")
        self.assertEqual(actions[str(c_old)], "delete")
        # crontab has no checkable live file: the newest copy is always kept.
        self.assertEqual(actions[str(c_new)], "retain (last copy; live missing)")
        self.seed(self.bot / "sessions.json", 0)
        self.assertEqual(self.plan_actions()[str(s_new)], "delete")

    # --- policy details ------------------------------------------------------
    def test_retention_days_configurable_but_env_only_lengthens(self):
        self.live_env()
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

    def test_live_env_file_is_never_a_target(self):
        live = self.seed(self.bot / ".env", HORIZON_DAYS)
        self.assertNotIn(str(live), self.report())
        self.assertNotEqual(self.plan_actions().get(str(live)), "delete")

    def test_report_never_calls_a_retain_class_eligible(self):
        inventory = {"schema": planner.INVENTORY_SCHEMA, "artifacts": [{
            "id": "fixture.kept", "path_class": "node-local backup",
            "resolve": {"candidates": [{"kind": "pattern", "path": "~/.telegram_bot/.*\\.bak-.*"}]},
            "retention_policy": {"group": "b-sensitive-backup"},
            "requests": {"prune-expired": "retain"}}]}
        old = self.seed(self.bot / "plain.txt.bak-1", HORIZON_DAYS)
        entry = self.report(inventory)[str(old)]
        self.assertFalse(entry["eligible"])
        self.assertEqual(entry["reason"], "class-retain")
        self.assertEqual(entry["planned_action"], "retain")

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
        live = self.live_env()
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
        for kept in (live, young, key):
            self.assertTrue(kept.exists(), kept)
        manifest = json.loads(next(self.backups.glob("*/manifest.json")).read_text())
        self.assertEqual(manifest["deleted"], [str(old)])
        self.assertTrue(manifest["verified"])


if __name__ == "__main__":
    unittest.main()
