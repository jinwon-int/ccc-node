"""Retention-class fixtures for the erasure planner/apply boundary (#1468).

Owner decision 2026-09-29: group a (legacy stores) and group b (sensitive
backups) are kept 30 days, then become eligible for destruction at the
existing apply boundary; key files are always kept. Owner decision
2026-10-23: unclassified files directly under the harness state dir fall back
to the operator-scratch class (90 days, scheduler-referenced files kept). Every file here lives in
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
        # Scheduler guard sources stay inside the fixture: a stub crontab
        # (the repo's CCC_CRONTAB_CMD seam) printing self.crontab, and a
        # fixture /etc/systemd/system stand-in.
        self.crontab = self.root / "crontab.txt"
        self.crontab_cmd = self.stub("crontab-stub",
                                     f'[ -f "{self.crontab}" ] && /bin/cat "{self.crontab}"\nexit 0\n')
        self.etc_units = self.root / "etc-systemd-system"
        self.enterContext(patch.dict(os.environ, {
            "CCC_ERASURE_BACKUP_DIR": str(self.backups),
            "CCC_CRONTAB_CMD": str(self.crontab_cmd),
        }, clear=True))
        self.enterContext(patch.object(planner, "_expand", lambda p:
            str(self.home / p[2:]) if p.startswith("~/") else p))
        self.enterContext(patch.object(planner, "_now", lambda: self.T, create=True))
        self.enterContext(patch.object(planner, "SYSTEMD_UNIT_DIRS",
                                       ("~/.config/systemd/user", str(self.etc_units))))
        self.inventory = json.loads(Path(planner.DEFAULT_INVENTORY).read_text())

    def stub(self, name, body):
        path = self.root / "bin" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o700)
        return path

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

    # --- re-review N1/N2: which copy is "newest" -----------------------------
    def seed_past(self, path, mtime_days_ago, after_ctime=None):
        """Real-past mtime; ctime strictly later than ``after_ctime`` so a
        ctime-ranked pick is deterministic (the shape an rsync -a / cp -a /
        chmod sweep leaves: every ctime ≈ now, mtimes spread in the past)."""
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text("fixture\n")
        stamp = time.time() - mtime_days_ago * DAY
        while True:
            os.utime(path, (stamp, stamp))          # utime also sets ctime
            path.chmod(0o600)
            if after_ctime is None or path.stat().st_ctime > after_ctime:
                return path
            time.sleep(0.01)

    def test_symlink_never_counts_as_the_kept_copy(self):
        self.T = time.time() + 60 * DAY
        real = self.seed_past(self.bot / ".env.bak-old", 1)
        if os.utime not in os.supports_follow_symlinks:
            self.skipTest("cannot set a symlink's own mtime here")
        link = self.bot / ".env.bak-latest"
        link.symlink_to(".env.bak-old")
        newer = self.T - 1 * DAY                    # the link looks newest
        os.utime(link, (newer, newer), follow_symlinks=False)
        self.assertGreater(os.lstat(link).st_mtime, real.stat().st_ctime)
        report = self.report()
        self.assertFalse(report[str(real)]["eligible"])
        self.assertEqual(report[str(real)]["reason"], "last-copy-live-missing")
        actions = self.plan_actions()
        self.assertEqual(actions[str(real)], "retain (last copy; live missing)")
        self.assertEqual(actions[str(link)], "retain (symlink)")

    def test_newest_copy_ranked_by_mtime_not_swept_ctime(self):
        self.T = time.time() + 60 * DAY
        aug = self.seed_past(self.bot / ".env.bak-aug", 45)
        old = self.seed_past(self.bot / ".env.pre-2025", 300,
                             after_ctime=aug.stat().st_ctime)
        actions = self.plan_actions()
        self.assertEqual(actions[str(aug)], "retain (last copy; live missing)")
        self.assertEqual(actions[str(old)], "delete")

    def test_crontab_newest_copy_ranked_by_mtime(self):
        self.T = time.time() + 60 * DAY
        state = self.home / ".claude/state"
        recent = self.seed_past(state / "crontab.bak-20260801", 60)
        ancient = self.seed_past(state / "crontab.bak-20250101", 400,
                                 after_ctime=recent.stat().st_ctime)
        actions = self.plan_actions()
        self.assertEqual(actions[str(recent)], "retain (last copy; live missing)")
        self.assertEqual(actions[str(ancient)], "delete")

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
            if entry.get("fallback_scope"):
                # A fallback class has no primary path at all.
                self.assertEqual(entry["resolve"]["candidates"], [], entry["id"])
                self.assertNotIn("extra_paths", entry, entry["id"])
                continue
            if entry.get("retention_policy"):
                kinds = {c.get("kind") for c in entry["resolve"]["candidates"]}
                self.assertEqual(kinds, {"pattern"}, entry["id"])
                self.assertNotIn("extra_paths", entry, entry["id"])

    # --- #1468 c-batch-2: observed group-b name variants --------------------
    GROUP_B_VARIANTS = {
        ".telegram_bot": (".env.bak.20261001120000", ".env.bak.20261001_120000",
                          ".env.bak.pretimeout", ".env.bak-pre-delegated7200-20261001T1200+0900",
                          ".env.pre-webmcp-20261001T120000+0900",
                          "sessions.json.bak-effort-high-20261001T1200+0900",
                          "sessions.json.bak-opus5-20261001T1200+0900"),
        ".claude/state": ("crontab-backup-20261001-120000.txt",
                          "crontab-backup-20261001-120000-lanes.txt",
                          "crontab-backup-20261001-120000-before-both-mode.txt",
                          "crontab.bak-nightly-0040-20261001T1200+0900",
                          "crontab.bak.20261001-120000", "crontab.bak.20261001T120000Z",
                          "memory-facts.jsonl.bak-names-20261001T1200"),
        ".nunchi": ("crontab.bak-pre-piri-20261001T1200", "crontab.bak-pre-repair-20261001T1200"),
    }

    def seed_live_counterparts(self):
        self.live_env()
        self.seed(self.bot / "sessions.json", 0)
        self.seed(self.home / ".claude/state/memory-facts.jsonl", 0)

    def test_observed_group_b_variants_classified_and_eligible_after_30_days(self):
        self.seed_live_counterparts()
        seeded = {}
        for sub, names in self.GROUP_B_VARIANTS.items():
            for name in names:
                seeded[name] = self.seed(self.home / sub / name, 40)
            # crontab has no live file: the newest copy per directory is
            # always kept, so a newer sentinel absorbs the last-copy rule.
            self.seed(self.home / sub / "crontab.bak-sentinel", 31)
        unknown = {u["path"] for u in planner._run_scan(self.inventory)["unknown"]}
        report = self.report()
        actions = self.plan_actions()
        for name, path in seeded.items():
            with self.subTest(name=name):
                self.assertNotIn(str(path), unknown)
                self.assertTrue(report[str(path)]["eligible"])
                self.assertEqual(report[str(path)]["group"], "b-sensitive-backup")
                self.assertEqual(actions[str(path)], "delete")
        for sub in self.GROUP_B_VARIANTS:
            sentinel = str(self.home / sub / "crontab.bak-sentinel")
            self.assertEqual(actions[sentinel], "retain (last copy; live missing)")

    def test_observed_group_b_variants_retained_within_30_days(self):
        self.seed_live_counterparts()
        for sub, names in self.GROUP_B_VARIANTS.items():
            for name in names:
                path = self.seed(self.home / sub / name, 5)
                with self.subTest(name=name):
                    self.assertTrue(self.plan_actions()[str(path)].startswith("retain-until:"))

    def test_memory_facts_backup_keeps_last_copy_while_lane_missing(self):
        state = self.home / ".claude/state"
        older = self.seed(state / "memory-facts.jsonl.bak-a", 60)
        newest = self.seed(state / "memory-facts.jsonl.bak-b", 50)
        actions = self.plan_actions()
        self.assertEqual(actions[str(older)], "delete")
        self.assertEqual(actions[str(newest)], "retain (last copy; live missing)")
        self.seed(state / "memory-facts.jsonl", 0)
        self.assertEqual(self.plan_actions()[str(newest)], "delete")

    def test_live_files_and_lookalikes_never_match_group_b(self):
        self.seed_live_counterparts()
        state, nunchi = self.home / ".claude/state", self.home / ".nunchi"
        # The bridge's previous-good recovery copies (session store, task
        # ledger) are read back on primary corruption: live, never group b.
        live_bak = [self.seed(self.bot / n, HORIZON_DAYS)
                    for n in ("sessions.json.bak", "tasks.json.bak")]
        lookalikes = [self.seed(self.bot / n, HORIZON_DAYS) for n in (
            ".env.bak", ".env.backup", ".env.local", ".env.pre", "x.env.bak-1",
            ".sessions.json.bak.tmp-abc", "sessions.json.bak.1",
            "crontab")]
        # State-dir lookalikes are not group b either; since the owner
        # decision of 2026-10-23 they fall back to operator scratch.
        state_lookalikes = [self.seed(state / n, HORIZON_DAYS) for n in (
            "crontab-backup.txt", "crontab-backup-x.txt", "crontab-backup-20261001.txt.gz",
            "memory-facts.jsonl.bak", "memory-facts.jsonl.bak.1")]
        live_bak.append(state / "memory-facts.jsonl")
        lookalikes += [self.seed(nunchi / n, HORIZON_DAYS) for n in ("crontab.bak", "crontab.txt")]
        report = self.report()
        actions = self.plan_actions()
        for path in live_bak + lookalikes:
            with self.subTest(path=path.name):
                self.assertNotIn(str(path), report)
                self.assertNotEqual(actions.get(str(path)), "delete")
        for path in state_lookalikes:
            with self.subTest(path=path.name):
                self.assertEqual(report[str(path)]["artifact"], "state.operator_scratch")
        unknown = {u["path"] for u in planner._run_scan(self.inventory)["unknown"]}
        for path in live_bak:
            self.assertNotIn(str(path), unknown)   # classified with the live store
        decommission = self.plan_actions("node-decommission")
        self.assertEqual(decommission[str(live_bak[0])], "delete-or-handoff")
        self.assertEqual(decommission[str(live_bak[1])], "retain")
        self.assertNotIn(str(state / "crontab-backup-x.txt"), unknown)
        self.assertIn(str(self.bot / ".env.bak"), unknown)    # no fallback there

    def test_key_files_in_widened_families_stay_retained(self):
        self.seed_live_counterparts()
        state = self.home / ".claude/state"
        keys = [self.seed(state / "cert.pem.bak-tool-20261001T1200", HORIZON_DAYS),
                self.seed(self.bot / ".env.bak.20261001.PEM", HORIZON_DAYS),
                self.seed(self.bot / ".env.pre-x+0900.token", HORIZON_DAYS),
                self.seed(state / "crontab.bak.id_ed25519", HORIZON_DAYS),
                self.seed(state / "memory-facts.jsonl.bak-secrets-1", HORIZON_DAYS)]
        report = self.report()
        actions = self.plan_actions()
        unknown = {u["path"] for u in planner._run_scan(self.inventory)["unknown"]}
        for path in keys:
            with self.subTest(name=path.name):
                self.assertNotIn(str(path), unknown)
                self.assertEqual(report[str(path)]["reason"], "key-file")
                self.assertTrue(actions[str(path)].startswith("retain"))
        for path in keys[1:]:                      # delete classes: the guard wins
            self.assertEqual(actions[str(path)], "retain (key-file)")
        cert = str(keys[0])                        # PEM class: explicit retain too
        self.assertEqual(report[cert]["artifact"], "state.cert_backups")
        self.assertEqual(actions[cert], "retain")
        self.assertEqual(self.plan_actions("node-decommission")[cert], "retain")

    def test_skill_review_zai_env_is_retained_live_config(self):
        live = self.seed(self.home / ".claude/state/skill-review.zai.env", HORIZON_DAYS)
        unknown = {u["path"] for u in planner._run_scan(self.inventory)["unknown"]}
        self.assertNotIn(str(live), unknown)
        self.assertNotIn(str(live), self.report())
        for request in ("prune-expired", "node-decommission", "telegram-user-erasure"):
            self.assertNotEqual(self.plan_actions(request).get(str(live)), "delete", request)
        self.assertEqual(self.plan_actions("node-decommission")[str(live)], "retain")

    # --- #1468 c-batch-2: SQLite sidecars follow their DB --------------------
    def test_sidecars_of_the_live_default_db_are_never_retention_targets(self):
        nunchi = self.home / ".nunchi"
        self.seed(nunchi / "facts.db", HORIZON_DAYS)
        self.seed(nunchi / ".legacy-retired", 0)
        sidecars = [self.seed(nunchi / f"facts.db{s}", HORIZON_DAYS) for s in ("-wal", "-shm")]
        # NUNCHI_DB unset: ~/.nunchi/facts.db IS live, so are its sidecars.
        report = self.report()
        unknown = {u["path"] for u in planner._run_scan(self.inventory)["unknown"]}
        for path in sidecars:
            self.assertNotIn(str(path), report)
            self.assertNotIn(str(path), unknown)
            self.assertNotEqual(self.plan_actions().get(str(path)), "delete")
            self.assertEqual(self.plan_actions("node-decommission")[str(path)], "handoff-or-drop")

    def test_legacy_db_sidecars_share_the_db_verdict(self):
        nunchi = self.home / ".nunchi"
        scoped = self.seed(self.root / "scope/facts.db", 0)
        db = self.seed(nunchi / "facts.db", 60)
        wal = self.seed(nunchi / "facts.db-wal", 1)        # younger than its DB
        shm = self.seed(nunchi / "facts.db-shm", 1)
        with patch.dict(os.environ, {"NUNCHI_DB": str(scoped)}):
            report = self.report()
            for path in (db, wal, shm):                    # marker absent: live
                self.assertNotIn(str(path), report)
            self.seed(nunchi / ".legacy-retired", 0)
            actions = self.plan_actions()
            for path in (db, wal, shm):
                self.assertEqual(actions[str(path)], "delete", path.name)
            os.utime(db, (self.T - 5 * DAY,) * 2)          # DB young, WAL old
            os.utime(wal, (self.T - 90 * DAY,) * 2)
            report = self.report()
            self.assertFalse(report[str(wal)]["eligible"])
            self.assertEqual(report[str(wal)]["eligible_at"], report[str(db)]["eligible_at"])
            self.assertEqual(self.plan_actions()[str(wal)],
                             f"retain-until:{report[str(db)]['eligible_at']}")

    def test_gotgan_store_and_sidecars_are_body_bearing_index(self):
        nunchi = self.home / ".nunchi"
        files = [self.seed(nunchi / n, 3) for n in ("gotgan.db", "gotgan.db-wal", "gotgan.db-shm")]
        orphan = self.seed(nunchi / "gotgan.db.old", 3)
        unknown = {u["path"] for u in planner._run_scan(self.inventory)["unknown"]}
        actions = self.plan_actions("node-decommission")
        for path in files:
            self.assertNotIn(str(path), unknown)
            self.assertEqual(actions[str(path)], "delete", path.name)
        self.assertIn(str(orphan), unknown)
        entry = next(a for a in self.inventory["artifacts"] if a["id"] == "nunchi.gotgan_store")
        self.assertIn("transcript-excerpts", entry["contains"])
        self.assertEqual(entry["requests"]["cache-rebuild"], "re-index")

    def test_harness_runtime_artifacts_classified_one_offs_are_scratch(self):
        state, nunchi = self.home / ".claude/state", self.home / ".nunchi"
        harness = [self.seed(state / n, 1) for n in (
            "repo-commands.manifest", "repo-output-styles.manifest",
            "skill-autosave-cron.history.jsonl", ".skill-listing-policy.lock",
            "skill-listing-policy.json", "prescreen-last.json",
            "self-update.pending-activation.json", "skill-autosave-curator-state.json",
            "nunchi-judge-human.flag", "nunchi-wiki-promote-report.md",
            "nunchi.auto-distill-feed", "distill.scope", "distill.log.1.gz", "distill.log.2",
            "skill-autosave.codex-seen", "skill-autosave.piri-seen",
            "skill-autosave.danso-seen")]
        harness += [self.seed(nunchi / n, 1) for n in (
            ".jev-review.lock", ".journal-feed.lock", ".auto-distill-feed.lock",
            ".danso-feed.lock", "gotgan-refresh.lock", "backend-health.json.lock",
            "ingest.status.json", "judge.status.json", "journal-feed.status.json",
            "matrix-journal-feed.status.json", "gotgan-refresh.status.json",
            "matrix-ingest.status.json", "ingest.matrix.status.json",
            "auto-distill-feed.state.json", "jev-review.db", "jev-review.db-journal",
            "journal-receipts.jsonl", "journal-matrix-0123456789abcdef-receipts.jsonl",
            "journal-configured-fedcba9876543210-receipts.jsonl",
            "codex-receipts.jsonl", "piri-receipts.jsonl", "codex-seen", "danso-seen",
            "gotgan-refresh.cron.log", "jev-review.py.log", "journal-feed.py.log",
            "matrix-ingest.log", "verbatim-backend")]
        harness.append(self.seed(self.bot / "jev-skill-advice.json", 1))
        harness.append(self.seed(state / "timed-test-deadline-scan.notify-high.json", 1))
        # State-dir one-offs fall back to operator scratch (2026-10-23);
        # one-offs in ~/.nunchi keep blocking (no fallback there).
        scratch = [self.seed(state / n, 1) for n in (
            "label-restart.sh", "handover-x-20261001.md", "mcp-discover-verdicts.json",
            "self-update.lock.stalefile-removed-20261001", "distill.disabled.archive-20261001T0000Z",
            "wiki-candidates.md.bak-20261001", "fleet-update-abc1234.log.validate",
            "distill.log.1.gz.tmp")]
        one_offs = [self.seed(nunchi / n, 1) for n in (
            "gotgan-stage2-manual.time", "journal-matrix-XYZ-receipts.jsonl",
            "jev-review.db-wal2")]
        unknown = {u["path"] for u in planner._run_scan(self.inventory)["unknown"]}
        for path in harness:
            self.assertNotIn(str(path), unknown, path.name)
        for path in one_offs:
            self.assertIn(str(path), unknown, path.name)
        report = self.report()
        for path in harness:
            self.assertNotIn(str(path), report, path.name)
        for path in scratch:
            self.assertNotIn(str(path), unknown, path.name)
            self.assertEqual(report[str(path)]["artifact"], "state.operator_scratch", path.name)
        self.assertEqual(self.plan_actions("node-decommission")[str(nunchi / "jev-review.db")],
                         "delete")

    # --- operator scratch (#1468, owner decision 2026-10-23) ---------------
    def scratch_fixture(self):
        """A state dir with one classified file, so it is a sweep root."""
        state = self.home / ".claude/state"
        self.seed(state / "resume.md", 0)
        return state

    def scratch_entry(self):
        return next(a for a in self.inventory["artifacts"]
                    if a["id"] == "state.operator_scratch")

    def test_unknown_state_file_is_operator_scratch_kept_90_days(self):
        state = self.scratch_fixture()
        young = self.seed(state / "label-restart.sh", 89)
        old = self.seed(state / "handover-x-20260701.md", 91)
        report = self.report()
        for path in (young, old):
            self.assertEqual(report[str(path)]["artifact"], "state.operator_scratch")
            self.assertEqual(report[str(path)]["group"], "operator-scratch")
            self.assertEqual(report[str(path)]["max_age_days"], 90)
        self.assertFalse(report[str(young)]["eligible"])
        self.assertTrue(report[str(old)]["eligible"])
        for request in ("prune-expired", "node-decommission"):
            actions = self.plan_actions(request)
            self.assertTrue(actions[str(young)].startswith("retain-until:2"), request)
            self.assertEqual(actions[str(old)], "delete", request)
        doc = planner.plan("prune-expired", self.inventory, None, None)
        self.assertEqual(doc["scheduler_guard"]["status"], "ok")
        self.assertNotIn(str(state), {t["path"] for t in doc["targets"]})

    def test_state_dir_with_only_scratch_has_no_blockers(self):
        state = self.scratch_fixture()
        for i, name in enumerate(("apply-908.sh", "nexus-followup-issues.md",
                                  "working-state.md.bak-20260929", "working-state.md.lock",
                                  "skill-autosave.mode.bak-auto-20261001",
                                  "wiki-candidates.md.bak2-20260805",
                                  "manual-self-update-20261001T0000Z.log",
                                  "fleet-idle-finalize.no-services")):
            self.seed(state / name, 10 * i)
        blockers = planner._unknown_blockers(self.inventory, 10_000)
        self.assertEqual([b for b in blockers if b["path"].startswith(str(state))], [])
        self.assertEqual(planner._run_scan(self.inventory)["fallback"][0]["files"], 8)

    def test_file_of_a_real_class_keeps_its_class(self):
        state = self.scratch_fixture()
        backup = self.seed(state / "crontab.bak-1", 40)
        self.seed(state / "crontab.bak-2", 35)          # absorbs last-copy
        log = self.seed(state / "checkpoint.log", 200)
        scratch = self.seed(state / "one-off.sh", 200)
        report = self.report()
        self.assertEqual(report[str(backup)]["artifact"], "state.crontab_backups")
        self.assertNotIn(str(log), report)              # live class, not scratch
        doc = planner.plan("prune-expired", self.inventory, None, None)
        owners = {}
        for target in doc["targets"]:
            if target["present"] and target["path"]:
                owners.setdefault(target["path"], set()).add(target["artifact"])
        self.assertEqual(owners[str(backup)], {"state.crontab_backups"})
        self.assertEqual(owners[str(scratch)], {"state.operator_scratch"})
        self.assertTrue(all(len(v) == 1 for v in owners.values()))
        apply.partition_targets(doc)                    # no conflicting actions

    def test_non_owner_only_scratch_is_retained_not_planned(self):
        # #1468 owner decision 2026-10-23 (option 2): apply refuses a run with
        # any non-owner-only target, so such scratch is kept instead.
        state = self.scratch_fixture()
        readable = self.seed(state / "handover-notes.md", HORIZON_DAYS)
        readable.chmod(0o644)
        script = self.seed(state / "rollout-once.sh", HORIZON_DAYS)
        script.chmod(0o755)
        group_only = self.seed(state / "probe-once.py", HORIZON_DAYS)
        group_only.chmod(0o640)
        private = self.seed(state / "one-off.sh", HORIZON_DAYS)
        report = self.report()
        for path in (readable, script, group_only):
            self.assertEqual(report[str(path)]["reason"], "not-owner-only", path.name)
            self.assertFalse(report[str(path)]["eligible"], path.name)
        for request in ("prune-expired", "node-decommission"):
            actions = self.plan_actions(request)
            for path in (readable, script, group_only):
                self.assertEqual(actions[str(path)], "retain (not owner-only)", (request, path.name))
            self.assertEqual(actions[str(private)], "delete", request)
        # Only owner-only scratch reaches apply: its executable set passes the
        # same owner-only verification the run enforces.
        doc = planner.plan("prune-expired", self.inventory, None, None)
        executable, _ = apply.partition_targets(doc)
        deletes = [t["path"] for t in executable
                   if t.get("artifact") == "state.operator_scratch" and t["action"] == "delete"]
        self.assertEqual(deletes, [str(private)])
        self.assertIsNone(apply.check_ancestors(str(private)))
        for path in (readable, script, group_only):   # what apply would refuse
            self.assertEqual(apply.check_ancestors(str(path)), "group-other-bits-on-target")

    def test_owner_only_rule_does_not_touch_non_scratch_classes(self):
        state = self.scratch_fixture()
        self.seed(state / "crontab.bak-2", 35)          # absorbs last-copy
        backup = self.seed(state / "crontab.bak-1", 40)
        backup.chmod(0o644)
        report = self.report()
        self.assertEqual(report[str(backup)]["artifact"], "state.crontab_backups")
        self.assertNotEqual(report[str(backup)]["reason"], "not-owner-only")

    def test_key_file_names_in_scratch_stay_retained(self):
        state = self.scratch_fixture()
        keys = [self.seed(state / n, HORIZON_DAYS) for n in (
            "deploy.PEM", "relay.token.bak", "id_ed25519-old", "gh-credentials.json")]
        actions = self.plan_actions()
        for path in keys:
            self.assertEqual(actions[str(path)], "retain (key-file)", path.name)

    def test_scheduler_referenced_scratch_is_retained_at_any_age(self):
        state = self.scratch_fixture()
        cron = self.seed(state / "watch-pr878.sh", HORIZON_DAYS)
        task = self.seed(state / "observe-collect.sh", HORIZON_DAYS)
        user_unit = self.seed(state / "unit-run.sh", HORIZON_DAYS)
        sys_unit = self.seed(state / "sys-run.sh", HORIZON_DAYS)
        indirect = self.seed(state / "cd-run.sh", HORIZON_DAYS)
        free = self.seed(state / "unreferenced.sh", HORIZON_DAYS)
        self.crontab.write_text(
            "*/5 * * * * bash ~/.claude/state/watch-pr878.sh >/dev/null 2>&1\n"
            "0 3 * * * cd $HOME/.claude/state && ./cd-run.sh\n")
        store = state / "agent-cron" / "tasks.json"
        store.parent.mkdir(parents=True)
        store.write_text(json.dumps({"tasks": [{"id": "t1", "argv": ["bash", str(task)]}]}))
        units = self.home / ".config/systemd/user"
        units.mkdir(parents=True)
        (units / "x.service").write_text(
            "[Service]\nExecStart=/bin/sh %h/.claude/state/unit-run.sh\n"
            "# ExecStart=%h/.claude/state/unreferenced.sh\n")
        self.etc_units.mkdir()
        (self.etc_units / "y.service").write_text(f"[Service]\nExecStartPre={sys_unit}\n")
        actions = self.plan_actions()
        for path in (cron, task, user_unit, sys_unit, indirect):
            self.assertEqual(actions[str(path)], "retain (scheduler-referenced)", path.name)
        self.assertEqual(actions[str(free)], "delete")
        self.assertEqual(self.plan_actions("node-decommission")[str(cron)],
                         "retain (scheduler-referenced)")
        doc = planner.plan("prune-expired", self.inventory, None, None)
        self.assertNotIn("watch-pr878", json.dumps(doc["scheduler_guard"]))  # body-free

    def test_unreadable_scheduler_source_keeps_all_scratch(self):
        state = self.scratch_fixture()
        old = self.seed(state / "one-off.sh", HORIZON_DAYS)
        broken = {
            "crontab exits non-zero": lambda: os.environ.__setitem__(
                "CCC_CRONTAB_CMD", str(self.stub("bad", "echo denied >&2\nexit 2\n"))),
            "crontab missing": lambda: os.environ.__setitem__(
                "CCC_CRONTAB_CMD", str(self.root / "no-such-crontab")),
            "agent-cron store corrupt": lambda: (
                (state / "agent-cron").mkdir(exist_ok=True),
                (state / "agent-cron" / "tasks.json").write_text("{truncated")),
        }
        for label, breaker in broken.items():
            with self.subTest(label), patch.dict(os.environ):
                breaker()
                doc = planner.plan("prune-expired", self.inventory, None, None)
                actions = {t["path"]: t["action"] for t in doc["targets"] if t["present"]}
                self.assertEqual(actions[str(old)], "retain (scheduler references unreadable)")
                self.assertEqual(doc["scheduler_guard"]["status"], "unavailable")
                self.assertTrue(doc["scheduler_guard"]["errors"])
                report = planner.retention_report(self.inventory)
                self.assertEqual(report["scheduler_guard"]["status"], "unavailable")
                self.assertFalse(any(e["eligible"] for e in report["entries"]
                                     if e["artifact"] == "state.operator_scratch"))
            shutil.rmtree(state / "agent-cron", ignore_errors=True)
        # "no crontab for <user>" is an empty crontab, not a failure.
        os.environ["CCC_CRONTAB_CMD"] = str(self.stub(
            "none", "echo 'no crontab for fixture' >&2\nexit 1\n"))
        self.assertEqual(self.plan_actions()[str(old)], "delete")

    def test_fallback_stays_out_of_private_memory_and_bot_dirs(self):
        state = self.scratch_fixture()
        self.seed(self.home / ".nunchi/facts.db", 0)
        self.live_env()
        nunchi_one_off = self.seed(self.home / ".nunchi/gotgan-stage2-manual.time", HORIZON_DAYS)
        bot_one_off = self.seed(self.bot / "jev-skill-advice.json.incoming.ABC123", HORIZON_DAYS)
        nested = self.seed(state / "somedir" / "old.sh", HORIZON_DAYS)
        unknown = {u["path"] for u in planner._run_scan(self.inventory)["unknown"]}
        self.assertIn(str(nunchi_one_off), unknown)
        self.assertIn(str(bot_one_off), unknown)
        report = self.report()
        for path in (nunchi_one_off, bot_one_off, nested):
            self.assertNotIn(str(path), report, path.name)
        roots = planner.fallback_roots(self.scratch_entry())
        self.assertEqual(roots, [os.path.realpath(state)])

    def test_scratch_sidecars_follow_their_db_orphans_block(self):
        state = self.scratch_fixture()
        db = self.seed(state / "probe.db", 10)
        wal = self.seed(state / "probe.db-wal", HORIZON_DAYS)
        orphan = self.seed(state / "gone.db-wal", HORIZON_DAYS)
        report = self.report()
        self.assertFalse(report[str(db)]["eligible"])
        self.assertFalse(report[str(wal)]["eligible"])     # shares the DB verdict
        self.assertNotIn(str(orphan), report)
        self.assertIn(str(orphan), {u["path"] for u in planner._run_scan(self.inventory)["unknown"]})

    def test_scratch_retention_env_only_lengthens(self):
        state = self.scratch_fixture()
        old = self.seed(state / "one-off.sh", 100)
        with patch.dict(os.environ, {"CCC_ERASURE_RETENTION_DAYS": "30"}):
            self.assertEqual(self.report()[str(old)]["max_age_days"], 90)
            self.assertTrue(self.report()[str(old)]["eligible"])
        with patch.dict(os.environ, {"CCC_ERASURE_RETENTION_DAYS": "120"}):
            self.assertFalse(self.report()[str(old)]["eligible"])

    def test_scratch_not_touched_by_audience_or_user_erasure(self):
        state = self.scratch_fixture()
        old = self.seed(state / "one-off.sh", HORIZON_DAYS)
        self.assertNotIn(str(old), self.plan_actions("audience-erasure"))
        doc = planner.plan("telegram-user-erasure", self.inventory, None, "42")
        self.assertNotIn(str(old), {t["path"] for t in doc["targets"]})
        self.assertNotIn(str(old), self.plan_actions("cache-rebuild"))

    def test_armed_apply_deletes_only_expired_unreferenced_scratch(self):
        state = self.scratch_fixture()
        state.chmod(0o700)
        state.parent.chmod(0o700)
        old = self.seed(state / "one-off.sh", 91)
        young = self.seed(state / "fresh.sh", 10)
        referenced = self.seed(state / "cron-run.sh", 200)
        self.crontab.write_text(f"0 * * * * {referenced}\n")
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
        self.assertTrue(referenced.exists())

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
