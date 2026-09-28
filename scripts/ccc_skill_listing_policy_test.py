#!/usr/bin/env python3
"""Hermetic tests for ccc-skill-listing-policy.py (ccc-node#2011 A).

Every case builds a private CCC_CLAUDE_DIR under a temp dir and drives the
real CLI through a subprocess, so the live ~/.claude is never read or written.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ccc-skill-listing-policy.py"
NOW = "2026-09-27T12:00:00Z"
RECENT = "2026-09-20T00:00:00Z"
STALE = "2026-07-01T00:00:00Z"


def described_cost(name: str, desc: str) -> int:
    """Estimate of one described entry (mirrors entry_chars in the script)."""
    return len(name) + 5 + len(desc)


def name_only_cost(name: str) -> int:
    return len(name) + 3


def extra_cost(name: str, desc: str) -> int:
    return described_cost(name, desc) - name_only_cost(name)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PolicyCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.claude = self.root / "claude"
        (self.claude / "skills").mkdir(parents=True)
        (self.claude / "state" / "skill-usage").mkdir(parents=True)
        self.core = self.root / "core.txt"
        self.core.write_text("# core\ncore-flow\nabsent-core  # not installed\n", encoding="utf-8")
        self.descs: dict[str, str] = {}
        for name in ("core-flow", "recent-jsonl", "recent-autosave", "stale-one", "stale-two",
                     "codex-lane-only"):
            self.skill(name, f"Describe {name} in enough words to matter for the listing.")
        self.usage(
            {"ts": RECENT, "skill": "recent-jsonl", "tool": "Skill"},
            {"ts": STALE, "skill": "stale-one", "tool": "Skill"},
        )
        self.autosave({
            "claude:recent-autosave": {"last_used_at": RECENT},
            "claude:stale-two": {"last_used_at": STALE, "last_viewed_at": STALE},
            "codex:codex-lane-only": {"last_used_at": RECENT},
        })
        self.write_settings({"theme": "dark", "hooks": {"Stop": []}})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- fixtures ---------------------------------------------------------
    def skill(self, name: str, description: str, extra: str = "") -> None:
        if "disable-model-invocation: true" not in extra:
            self.descs[name] = description
        d = self.claude / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n# {name}\n",
            encoding="utf-8")

    def usage(self, *rows: dict) -> None:
        path = self.claude / "state" / "skill-usage" / "usage.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def autosave(self, records: dict) -> None:
        (self.claude / "state" / "skill-autosave-usage.json").write_text(
            json.dumps({"schema_version": 1, "records": records}), encoding="utf-8")

    @property
    def settings_path(self) -> Path:
        return self.claude / "settings.json"

    def write_settings(self, doc: dict) -> None:
        self.settings_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    def settings(self) -> dict:
        return json.loads(self.settings_path.read_text(encoding="utf-8"))

    def state(self) -> dict:
        return json.loads((self.claude / "state" / "skill-listing-policy.json").read_text(encoding="utf-8"))

    def run_cli(self, *args: str, env: dict | None = None, core: bool = True) -> subprocess.CompletedProcess:
        full_env = {k: v for k, v in os.environ.items() if not k.startswith("CCC_")}
        full_env.update({"CCC_CLAUDE_DIR": str(self.claude), "CCC_SKILL_LISTING_POLICY_NOW": NOW,
                         "HOME": str(self.root / "home")})
        full_env.update(env or {})
        argv = [sys.executable, str(SCRIPT), *args]
        if core:
            argv += ["--core", str(self.core)]
        return subprocess.run(argv, capture_output=True, text=True, env=full_env, timeout=60)

    def apply(self, *args: str, **kw) -> subprocess.CompletedProcess:
        proc = self.run_cli("apply", *args, **kw)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def backups(self) -> list[Path]:
        d = self.claude / "backups" / "skill-listing-policy"
        return sorted(d.glob("settings.json.*")) if d.is_dir() else []


class DecisionTests(PolicyCase):
    def test_core_and_recent_keep_description_stale_gets_name_only(self) -> None:
        self.apply()
        ov = self.settings()["skillOverrides"]
        self.assertNotIn("core-flow", ov)
        self.assertNotIn("recent-jsonl", ov)
        self.assertNotIn("recent-autosave", ov)
        self.assertEqual(ov["stale-one"], "name-only")
        self.assertEqual(ov["stale-two"], "name-only")
        # Another provider's lane usage does not count for the Claude listing.
        self.assertEqual(ov["codex-lane-only"], "name-only")
        self.assertNotIn("absent-core", ov)

    def test_unrelated_settings_keys_survive(self) -> None:
        self.apply()
        doc = self.settings()
        self.assertEqual(doc["theme"], "dark")
        self.assertEqual(doc["hooks"], {"Stop": []})

    def test_never_writes_off_or_any_value_but_name_only(self) -> None:
        self.write_settings({"skillOverrides": {"stale-one": "off"}})
        self.apply()
        doc = self.settings()
        owned = self.state()["owned_overrides"]
        self.assertTrue(owned)
        self.assertEqual(set(owned.values()), {"name-only"})
        for key, value in doc["skillOverrides"].items():
            if key != "stale-one":  # the operator's own entry
                self.assertEqual(value, "name-only", key)

    def test_skill_files_are_never_touched(self) -> None:
        before = sorted(p.relative_to(self.claude) for p in (self.claude / "skills").rglob("*"))
        self.apply()
        after = sorted(p.relative_to(self.claude) for p in (self.claude / "skills").rglob("*"))
        self.assertEqual(before, after)

    def test_disabled_model_invocation_and_block_scalar_frontmatter(self) -> None:
        self.skill("hidden", "never listed", extra="disable-model-invocation: true\n")
        d = self.claude / "skills" / "folded"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: folded\ndescription: >-\n  first line\n  second line\n---\n", encoding="utf-8")
        proc = self.run_cli("plan", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plan = json.loads(proc.stdout)
        by = {d["skill"]: d["decision"] for d in plan["decisions"]}
        self.assertEqual(by["hidden"], "unlisted")
        self.assertEqual(by["folded"], "name-only")


class OwnershipTests(PolicyCase):
    def test_operator_entries_always_win(self) -> None:
        self.write_settings({"skillOverrides": {"stale-one": "on", "core-flow": "off",
                                                "not-installed": "user-invocable-only"}})
        self.apply()
        ov = self.settings()["skillOverrides"]
        self.assertEqual(ov["stale-one"], "on")
        self.assertEqual(ov["core-flow"], "off")
        self.assertEqual(ov["not-installed"], "user-invocable-only")
        self.assertNotIn("stale-one", self.state()["owned_overrides"])

    def test_operator_edit_of_owned_entry_is_adopted_and_left_alone(self) -> None:
        self.apply()
        doc = self.settings()
        doc["skillOverrides"]["stale-two"] = "on"
        self.write_settings(doc)
        self.apply()
        self.assertEqual(self.settings()["skillOverrides"]["stale-two"], "on")
        self.assertNotIn("stale-two", self.state()["owned_overrides"])
        # Even when the skill later becomes recent, the operator value stays.
        self.usage({"ts": RECENT, "skill": "stale-two", "tool": "Skill"})
        self.apply()
        self.assertEqual(self.settings()["skillOverrides"]["stale-two"], "on")

    def test_owned_entry_released_when_skill_becomes_recent_or_disappears(self) -> None:
        self.apply()
        self.usage({"ts": RECENT, "skill": "stale-one", "tool": "Read"})
        (self.claude / "skills" / "stale-two" / "SKILL.md").unlink()
        (self.claude / "skills" / "stale-two").rmdir()
        self.apply()
        ov = self.settings()["skillOverrides"]
        self.assertNotIn("stale-one", ov)
        self.assertNotIn("stale-two", ov)
        self.assertEqual(ov["codex-lane-only"], "name-only")

    def test_budget_fraction_only_when_absent(self) -> None:
        self.apply()
        self.assertEqual(self.settings()["skillListingBudgetFraction"], 0.02)
        self.write_settings({"skillListingBudgetFraction": 0.05})
        self.apply()
        self.assertEqual(self.settings()["skillListingBudgetFraction"], 0.05)
        self.assertIsNone(self.state()["owned_budget_fraction"])

    def test_release_removes_only_owned_entries(self) -> None:
        self.write_settings({"skillOverrides": {"stale-one": "on"}, "theme": "dark"})
        self.apply()
        proc = self.run_cli("release", core=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = self.settings()
        self.assertEqual(doc["skillOverrides"], {"stale-one": "on"})
        self.assertNotIn("skillListingBudgetFraction", doc)
        self.assertEqual(doc["theme"], "dark")
        self.assertEqual(self.state()["owned_overrides"], {})


class SafetyTests(PolicyCase):
    def test_idempotent_second_apply_is_a_no_op(self) -> None:
        self.apply()
        first = self.settings_path.read_bytes()
        n_backups = len(self.backups())
        self.assertEqual(n_backups, 1)
        proc = self.apply()
        self.assertIn("no change", proc.stdout)
        self.assertEqual(self.settings_path.read_bytes(), first)
        self.assertEqual(len(self.backups()), n_backups)

    def test_backup_holds_previous_bytes_and_result_is_valid_json(self) -> None:
        original = self.settings_path.read_bytes()
        os.chmod(self.settings_path, 0o640)
        self.apply()
        self.assertEqual(self.backups()[0].read_bytes(), original)
        self.assertEqual(self.settings_path.stat().st_mode & 0o777, 0o640)
        json.loads(self.settings_path.read_text(encoding="utf-8"))

    def test_plan_is_read_only(self) -> None:
        before = self.settings_path.read_bytes()
        proc = self.run_cli("plan")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("name-only", proc.stdout)
        self.assertIn("estimated listing chars", proc.stdout)
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.assertFalse((self.claude / "state" / "skill-listing-policy.json").exists())
        self.assertEqual(self.backups(), [])

    def test_invalid_settings_json_fails_closed(self) -> None:
        self.settings_path.write_text("{not json", encoding="utf-8")
        proc = self.run_cli("apply")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.settings_path.read_text(encoding="utf-8"), "{not json")

    def test_missing_core_list_fails_closed(self) -> None:
        before = self.settings_path.read_bytes()
        proc = self.run_cli("apply", "--core", str(self.root / "missing.txt"), core=False)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.settings_path.read_bytes(), before)

    def test_absent_settings_is_not_created(self) -> None:
        self.settings_path.unlink()
        self.apply()
        self.assertFalse(self.settings_path.exists())

    def test_kill_switches(self) -> None:
        before = self.settings_path.read_bytes()
        proc = self.apply(env={"CCC_SKILL_LISTING_POLICY": "0"})
        self.assertIn("disabled", proc.stdout)
        (self.claude / "skill-listing-policy.disabled").touch()
        self.apply()
        self.assertEqual(self.settings_path.read_bytes(), before)

    def test_repo_core_list_parses_and_is_default(self) -> None:
        proc = self.run_cli("plan", "--summary", core=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        names = [ln.split("#", 1)[0].strip() for ln in
                 (REPO / "claude" / "skill-listing-core.txt").read_text(encoding="utf-8").splitlines()]
        names = [n for n in names if n]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("gh-pr-flow", names)
        # Every repo-shipped name in the core list must still exist in skills/.
        repo_skills = {p.name for p in (REPO / "skills").iterdir() if p.is_dir()}
        external = {"a2a-task-poll", "remote-node-harness-sync", "model-migrate"}
        self.assertEqual(sorted(set(names) - repo_skills - external), [])


class BudgetFitTests(PolicyCase):
    """Recent (non-core) skills are described only while the estimate fits (#2031)."""

    def recent_skill(self, name: str, ts: str, desc_len: int = 1000, uses: int = 1) -> None:
        self.skill(name, "d" * desc_len)
        self.usage(*({"ts": ts, "skill": name, "tool": "Skill"} for _ in range(uses)))

    def fixed_chars(self, described: tuple[str, ...] = ("core-flow",)) -> int:
        return sum(described_cost(n, d) if n in described else name_only_cost(n)
                   for n, d in self.descs.items())

    def plan(self, *args: str, env: dict | None = None) -> dict:
        proc = self.run_cli("plan", "--json", *args, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def setUp(self) -> None:
        super().setUp()
        # Budget = 0.25 x ctx x 4 = ctx chars exactly, so a test can pick it.
        self.write_settings({"skillListingBudgetFraction": 0.25})
        self.recent_skill("r-new", "2026-09-26T00:00:00Z")
        self.recent_skill("r-mid", "2026-09-24T00:00:00Z")
        self.recent_skill("r-old", "2026-09-22T00:00:00Z")

    def test_most_recent_fit_rest_become_recent_over_budget(self) -> None:
        budget = self.fixed_chars() + extra_cost("r-new", self.descs["r-new"]) \
            + extra_cost("r-mid", self.descs["r-mid"])
        self.apply("--context-tokens", str(budget))
        ov = self.settings()["skillOverrides"]
        self.assertNotIn("r-new", ov)
        self.assertNotIn("r-mid", ov)
        # r-old does not fit; everything ranked after it is demoted too.
        for name in ("r-old", "recent-jsonl", "recent-autosave"):
            self.assertEqual(ov[name], "name-only", name)
            self.assertEqual(self.state()["owned_overrides"][name], "name-only")
        self.assertNotIn("core-flow", ov)
        plan = self.plan("--context-tokens", str(budget))
        reasons = {d["skill"]: d["reason"] for d in plan["decisions"]}
        self.assertTrue(reasons["r-old"].startswith("recent, over budget"), reasons["r-old"])
        self.assertTrue(reasons["r-new"].startswith("used "))
        self.assertEqual(reasons["stale-one"], "not core, not recently used")
        self.assertEqual(plan["after"]["listing_chars"], budget)
        self.assertFalse(plan["after"]["over_budget"])
        self.assertIsNone(plan["warning"])
        self.assertFalse(plan["changed"])
        # Idempotent: a second apply with the same inputs writes nothing.
        proc = self.apply("--context-tokens", str(budget))
        self.assertIn("no change", proc.stdout)

    def test_skill_that_fits_again_loses_its_policy_entry(self) -> None:
        budget = self.fixed_chars() + extra_cost("r-new", self.descs["r-new"])
        self.apply("--context-tokens", str(budget))
        self.assertEqual(self.settings()["skillOverrides"]["r-mid"], "name-only")
        bigger = budget + extra_cost("r-mid", self.descs["r-mid"])
        self.apply("--context-tokens", str(bigger))
        ov = self.settings()["skillOverrides"]
        self.assertNotIn("r-mid", ov)
        self.assertNotIn("r-mid", self.state()["owned_overrides"])
        self.assertEqual(ov["r-old"], "name-only")

    def test_prefix_rule_never_skips_over_a_skill_that_does_not_fit(self) -> None:
        self.recent_skill("r-new", "2026-09-26T00:00:00Z", desc_len=1500)
        self.recent_skill("r-mid", "2026-09-24T00:00:00Z", desc_len=600)
        self.recent_skill("r-old", "2026-09-22T00:00:00Z", desc_len=600)
        budget = self.fixed_chars() + extra_cost("r-mid", self.descs["r-mid"]) \
            + extra_cost("r-old", self.descs["r-old"])
        self.apply("--context-tokens", str(budget))
        ov = self.settings()["skillOverrides"]
        for name in ("r-new", "r-mid", "r-old"):
            self.assertEqual(ov[name], "name-only", name)

    def test_ties_break_on_use_count_then_name(self) -> None:
        same = "2026-09-27T00:00:00Z"
        self.recent_skill("t-c", same)
        self.recent_skill("t-a", same)
        self.recent_skill("t-b", same, uses=3)
        one = self.fixed_chars() + extra_cost("t-b", self.descs["t-b"])
        self.apply("--context-tokens", str(one))
        ov = self.settings()["skillOverrides"]
        self.assertNotIn("t-b", ov)
        self.assertEqual((ov["t-a"], ov["t-c"]), ("name-only", "name-only"))
        self.apply("--context-tokens", str(one + extra_cost("t-a", self.descs["t-a"])))
        ov = self.settings()["skillOverrides"]
        self.assertNotIn("t-a", ov)
        self.assertEqual(ov["t-c"], "name-only")

    def test_operator_pins_are_counted_and_left_alone(self) -> None:
        self.write_settings({"skillListingBudgetFraction": 0.25,
                             "skillOverrides": {"r-new": "on", "stale-one": "name-only"}})
        budget = self.fixed_chars(("core-flow", "r-new")) + extra_cost("r-mid", self.descs["r-mid"])
        self.apply("--context-tokens", str(budget))
        ov = self.settings()["skillOverrides"]
        self.assertEqual(ov["r-new"], "on")
        self.assertNotIn("r-mid", ov)
        self.assertEqual(ov["r-old"], "name-only")
        owned = self.state()["owned_overrides"]
        self.assertNotIn("r-new", owned)
        self.assertNotIn("stale-one", owned)  # operator-written, even though "name-only"

    def test_context_tokens_env_knob(self) -> None:
        budget = self.fixed_chars() + extra_cost("r-new", self.descs["r-new"])
        plan = self.plan(env={"CCC_SKILL_LISTING_CONTEXT_TOKENS": str(budget)})
        by = {d["skill"]: d["decision"] for d in plan["decisions"]}
        self.assertEqual((by["r-new"], by["r-mid"]), ("keep", "name-only"))
        self.assertEqual(plan["after"]["budget_chars"], budget)
        proc = self.run_cli("plan", env={"CCC_SKILL_LISTING_CONTEXT_TOKENS": "12"})
        self.assertEqual(proc.returncode, 2)
        self.assertIn("CCC_SKILL_LISTING_CONTEXT_TOKENS", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_release_keeps_recent_described_without_fitting(self) -> None:
        budget = self.fixed_chars() + extra_cost("r-new", self.descs["r-new"])
        self.apply("--context-tokens", str(budget))
        self.assertEqual(self.settings()["skillOverrides"]["r-mid"], "name-only")
        proc = self.run_cli("release", "--context-tokens", str(budget), core=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("skillOverrides", self.settings())


class OverBudgetReportTests(PolicyCase):
    def test_core_descriptions_dominate(self) -> None:
        self.skill("core-flow", "c" * 1400)
        proc = self.run_cli("plan", "--json", "--context-tokens", "1000")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plan = json.loads(proc.stdout)
        self.assertEqual(plan["after"]["budget_chars"], 80)
        self.assertTrue(plan["after"]["over_budget"])
        self.assertIn("core descriptions", plan["warning"])
        self.assertEqual(plan["after"]["core_desc_chars"], described_cost("core-flow", "c" * 1400))
        by = {d["skill"]: d for d in plan["decisions"]}
        for name in ("recent-jsonl", "recent-autosave"):
            self.assertEqual(by[name]["decision"], "name-only")
            self.assertTrue(by[name]["reason"].startswith("recent, over budget"))
        text = self.run_cli("plan", "--summary", "--context-tokens", "1000").stdout
        self.assertIn("over_budget=true", text)
        self.assertIn("WARNING: still over the estimated budget", text)
        self.assertIn("core descriptions", text)

    def test_name_list_dominates(self) -> None:
        for i in range(30):
            self.skill(f"stale-extra-skill-{i:02d}", "short")
        plan = json.loads(self.run_cli("plan", "--json", "--context-tokens", "1000").stdout)
        self.assertTrue(plan["after"]["over_budget"])
        self.assertIn("name list", plan["warning"])
        self.assertIn("reduce the installed skill count", plan["warning"])

    def test_under_budget_has_no_warning(self) -> None:
        proc = self.run_cli("plan", "--summary")
        self.assertIn("over_budget=false", proc.stdout)
        self.assertNotIn("WARNING", proc.stdout)


class RepoCoreDescriptionTests(unittest.TestCase):
    """Core skills are described on every turn: keep them short and trigger-first."""

    HARD_CAP = 350

    def test_repo_core_descriptions_are_short_and_trigger_first(self) -> None:
        policy = load_module(SCRIPT, "ccc_skill_listing_policy_mod")
        trigger = load_module(REPO / "claude" / "hooks" / "skill-review" / "description_trigger.py",
                              "description_trigger_mod")
        core = policy.load_core(REPO / "claude" / "skill-listing-core.txt")
        checked = 0
        for name in core:
            path = REPO / "skills" / name / "SKILL.md"
            if not path.is_file():
                continue  # fleet-installed (a2a-task-poll, ...), not shipped here
            fm = policy.parse_frontmatter(path.read_text(encoding="utf-8"))
            desc = fm.get("description", "")
            with self.subTest(skill=name):
                self.assertLessEqual(len(desc), self.HARD_CAP)
                self.assertTrue(desc.startswith("Use "), desc[:40])
                self.assertTrue(trigger.has_trigger_wording(desc))
            checked += 1
        self.assertGreaterEqual(checked, 10)


if __name__ == "__main__":
    # validate-harness requires a `PASS=<n> FAIL=<n>` summary line.
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    failed = len(result.failures) + len(result.errors)
    print(f"PASS={result.testsRun - failed} FAIL={failed}")
    raise SystemExit(0 if result.wasSuccessful() else 1)
