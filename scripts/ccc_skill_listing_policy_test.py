#!/usr/bin/env python3
"""Hermetic tests for ccc-skill-listing-policy.py (ccc-node#2011 A, fill mode #2748).

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
        full_env = {k: v for k, v in os.environ.items()
                    if not k.startswith("CCC_") and k != "ANTHROPIC_MODEL"}
        # #2358: never let the host's bridge unit or model env leak into a case.
        full_env.update({"CCC_CLAUDE_DIR": str(self.claude), "CCC_SKILL_LISTING_POLICY_NOW": NOW,
                         "HOME": str(self.root / "home"), "CCC_SKILL_LISTING_SYSTEMCTL": "off"})
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


class FillModeTests(PolicyCase):
    """Fill mode (#2748): describe more skills within F x budget; default off."""

    FILL_FILE = "skill-listing-policy.fill"
    FILL_ENV = "CCC_SKILL_LISTING_FILL_FRACTION"
    # Expected fill order: tier 1 (autosave, never used), tier 2 (fleet, newest
    # install first), tier 3 (everything else, by name). The names are chosen
    # so that plain name order would give a different result.
    ORDER = ("z-auto-new", "n-fleet-new", "m-fleet-old", "a-auto-used",
             "codex-lane-only", "stale-one", "stale-two")
    DESCRIBED = ("core-flow", "r-big", "recent-autosave", "recent-jsonl")

    def setUp(self) -> None:
        super().setUp()
        # Budget = 0.25 x ctx x 4 = ctx chars exactly, so a test can pick it.
        self.write_settings({"skillListingBudgetFraction": 0.25, "theme": "dark"})
        self.skill("r-big", "d" * 1000)
        self.usage({"ts": RECENT, "skill": "r-big", "tool": "Skill"})
        for name in ("z-auto-new", "a-auto-used"):
            self.skill(name, f"Autosave draft {name} " + "x" * 80)
            self.marker(name, ".autosave-meta.json", {
                "schema_version": 2, "manager": "ccc-node-skill-autosave",
                "ownership": "autosave-managed", "provider": "claude", "name": name})
        self.skill("m-fleet-old", "Fleet skill installed long ago " + "y" * 70)
        self.skill("n-fleet-new", "Fleet skill installed yesterday " + "y" * 70)
        fleet = {"schema_version": 1, "manager": "ccc-node-fleet-skills", "provider": "claude"}
        self.marker("m-fleet-old", ".ccc-fleet-skill.json", dict(fleet, name="m-fleet-old"),
                    mtime=1_780_000_000)
        self.marker("n-fleet-new", ".ccc-fleet-skill.json", dict(fleet, name="n-fleet-new"),
                    mtime=1_790_000_000)
        # A Skill-tool use outside the recent window: a-auto-used is not
        # waiting for its first use, so it belongs to the last tier.
        self.usage({"ts": STALE, "skill": "a-auto-used", "tool": "Skill"})
        # A Read of SKILL.md is a view, not a use: z-auto-new stays in tier 1.
        self.usage({"ts": STALE, "skill": "z-auto-new", "tool": "Read"})

    # -- helpers ----------------------------------------------------------
    def marker(self, name: str, filename: str, doc: dict, mtime: int | None = None) -> None:
        path = self.claude / "skills" / name / filename
        path.write_text(json.dumps(doc) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        if mtime is not None:
            os.utime(path, (mtime, mtime))

    def chars(self, described: tuple[str, ...]) -> int:
        return sum(described_cost(n, d) if n in described else name_only_cost(n)
                   for n, d in self.descs.items())

    def limit_for(self, n_filled: int) -> int:
        """Estimated listing chars with exactly the first ``n_filled`` candidates described."""
        return self.chars(self.DESCRIBED + self.ORDER[:n_filled])

    def plan(self, *args: str, env: dict | None = None) -> dict:
        proc = self.run_cli("plan", "--json", *args, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def filled(self, plan: dict) -> list[str]:
        return [d["skill"] for d in plan["decisions"] if d["reason"].startswith("fill: ")]

    def baseline_bytes(self) -> bytes:
        """settings.json after a plain apply (no fill switch anywhere)."""
        self.apply("--context-tokens", str(self.limit_for(len(self.ORDER))))
        return self.settings_path.read_bytes()

    # -- default off ------------------------------------------------------
    def test_default_off_is_the_pre_fill_result(self) -> None:
        ctx = str(self.limit_for(len(self.ORDER)))
        proc = self.apply("--context-tokens", ctx)
        self.assertNotIn("fill", proc.stdout)
        ov = self.settings()["skillOverrides"]
        # Exactly the pre-fill rule: core + recent described, the rest name-only,
        # whatever markers the skill directories carry and however much fits.
        self.assertEqual(ov, {name: "name-only" for name in sorted(self.ORDER)})
        self.assertEqual(self.state()["owned_overrides"], ov)
        plan = self.plan("--context-tokens", ctx)
        self.assertEqual(plan["fill"], {"enabled": False, "fraction": None, "source": None, "note": None})
        self.assertEqual({d["reason"] for d in plan["decisions"] if d["skill"] in self.ORDER},
                         {"not core, not recently used"})
        text = self.run_cli("plan", "--context-tokens", ctx).stdout
        self.assertNotIn("fill", text)

    def test_turning_fill_off_again_restores_the_baseline_entries(self) -> None:
        ctx = str(self.limit_for(len(self.ORDER)))
        baseline = json.loads(self.baseline_bytes())
        owned = self.state()
        # Partial fill, then off: the same entries come back (re-added entries
        # are appended, so only the key order inside skillOverrides may differ).
        self.apply("--context-tokens", str(self.limit_for(3)), "--fill-budget-fraction", "1")
        self.assertNotEqual(self.settings(), baseline)
        self.apply("--context-tokens", ctx)
        self.assertEqual(self.settings(), baseline)
        self.assertEqual(self.state(), owned)
        # Full fill (no entry left), then off: the same entries again.
        self.apply("--context-tokens", ctx, "--fill-budget-fraction", "1")
        self.assertNotIn("skillOverrides", self.settings())
        self.apply("--context-tokens", ctx)
        self.assertEqual(self.settings(), baseline)

    # -- order ------------------------------------------------------------
    def test_tiers_fill_in_order_autosave_unused_then_fleet_newest_then_name(self) -> None:
        for n in range(len(self.ORDER) + 1):
            plan = self.plan("--context-tokens", str(self.limit_for(n)), "--fill-budget-fraction", "1")
            with self.subTest(filled=n):
                self.assertEqual(sorted(self.filled(plan)), sorted(self.ORDER[:n]))
                self.assertEqual(plan["after"]["listing_chars"], self.limit_for(n))
                self.assertFalse(plan["after"]["over_budget"])
                self.assertEqual(plan["fill"]["promoted_total"], n)
        reasons = {d["skill"]: d["reason"] for d in plan["decisions"]}
        self.assertEqual(reasons["z-auto-new"], "fill: autosave-unused")
        self.assertEqual(reasons["n-fleet-new"], "fill: fleet")
        self.assertEqual(reasons["a-auto-used"], "fill: other")
        self.assertEqual(plan["fill"]["promoted"], {"autosave-unused": 1, "fleet": 2, "other": 4})
        self.assertEqual(plan["fill"]["candidates"], {"autosave-unused": 1, "fleet": 2, "other": 4})

    def test_fleet_tier_ties_break_on_name_and_a_fleet_marker_outranks_autosave(self) -> None:
        fleet = {"schema_version": 1, "manager": "ccc-node-fleet-skills", "provider": "claude"}
        for name in ("m-fleet-old", "n-fleet-new"):
            self.marker(name, ".ccc-fleet-skill.json", dict(fleet, name=name), mtime=1_790_000_000)
        # A fleet-adopted draft is fleet-managed, not "waiting for a first use".
        self.marker("z-auto-new", ".ccc-fleet-skill.json", dict(fleet, name="z-auto-new"),
                    mtime=1_700_000_000)
        order = ("m-fleet-old", "n-fleet-new", "z-auto-new")
        for n in (1, 2, 3):
            limit = self.chars(self.DESCRIBED + order[:n])
            plan = self.plan("--context-tokens", str(limit), "--fill-budget-fraction", "1")
            with self.subTest(filled=n):
                self.assertEqual(sorted(self.filled(plan)), sorted(order[:n]))
        self.assertEqual(plan["fill"]["candidates"]["autosave-unused"], 0)

    def test_malformed_or_foreign_markers_fall_back_to_the_last_tier(self) -> None:
        (self.claude / "skills" / "n-fleet-new" / ".ccc-fleet-skill.json").write_text("{not json")
        self.marker("m-fleet-old", ".ccc-fleet-skill.json", {"manager": "someone-else"})
        self.marker("z-auto-new", ".autosave-meta.json", {
            "manager": "ccc-node-skill-autosave", "ownership": "autosave-managed", "name": "other-dir"})
        plan = self.plan("--context-tokens", str(self.limit_for(len(self.ORDER))),
                         "--fill-budget-fraction", "1")
        self.assertEqual(plan["fill"]["candidates"], {"autosave-unused": 0, "fleet": 0, "other": 7})
        self.assertEqual(sorted(self.filled(plan)), sorted(self.ORDER))

    def test_autosave_use_count_counts_as_a_recorded_use(self) -> None:
        self.autosave({"claude:z-auto-new": {"use_count": 2, "last_used_at": STALE},
                       "codex:z-auto-new": {"use_count": 9}})
        plan = self.plan("--context-tokens", str(self.limit_for(len(self.ORDER))),
                         "--fill-budget-fraction", "1")
        self.assertEqual(plan["fill"]["candidates"]["autosave-unused"], 0)
        self.autosave({"codex:z-auto-new": {"use_count": 9}, "claude:z-auto-new": {"use_count": 0}})
        plan = self.plan("--context-tokens", str(self.limit_for(len(self.ORDER))),
                         "--fill-budget-fraction", "1")
        self.assertEqual(plan["fill"]["candidates"]["autosave-unused"], 1)

    # -- budget boundary --------------------------------------------------
    def test_fraction_limit_is_inclusive_and_one_char_less_stops_the_fill(self) -> None:
        limit = self.limit_for(2)
        # F = 0.5 of a budget of 2 x limit: the second candidate lands exactly on the limit.
        plan = self.plan("--context-tokens", str(2 * limit), "--fill-budget-fraction", "0.5")
        self.assertEqual(sorted(self.filled(plan)), ["n-fleet-new", "z-auto-new"])
        self.assertEqual(plan["fill"]["limit_chars"], limit)
        self.assertEqual(plan["after"]["listing_chars"], limit)
        self.assertEqual(plan["after"]["budget_chars"], 2 * limit)
        plan = self.plan("--context-tokens", str(2 * limit - 1), "--fill-budget-fraction", "0.5")
        self.assertEqual(self.filled(plan), ["z-auto-new"])
        self.assertEqual(plan["fill"]["limit_chars"], limit - 1)
        self.assertLessEqual(plan["after"]["listing_chars"], limit - 1)

    def test_fill_stops_at_the_first_candidate_that_does_not_fit(self) -> None:
        self.skill("n-fleet-new", "y" * 900)
        # Room for every candidate except the big second one: nothing after it is described.
        limit = self.chars(self.DESCRIBED + self.ORDER) - extra_cost("n-fleet-new", "y" * 900)
        plan = self.plan("--context-tokens", str(limit), "--fill-budget-fraction", "1")
        self.assertEqual(self.filled(plan), ["z-auto-new"])

    def test_no_fill_while_a_recent_skill_is_over_budget(self) -> None:
        # Budget below what the recent skills need: r-big is name-only'd, and
        # fill must not describe a never-used skill in the room that leaves.
        budget = self.limit_for(0) - 1
        plan = self.plan("--context-tokens", str(budget), "--fill-budget-fraction", "1")
        self.assertEqual(self.filled(plan), [])
        self.assertTrue(any(d["reason"].startswith("recent, over budget") for d in plan["decisions"]))
        self.assertEqual(plan["fill"]["promoted_total"], 0)

    # -- ownership and writes ---------------------------------------------
    def test_fill_only_removes_owned_entries_and_never_writes_another_value(self) -> None:
        ctx = str(self.limit_for(3))
        self.baseline_bytes()
        self.assertEqual(self.settings()["skillOverrides"]["z-auto-new"], "name-only")
        self.apply("--context-tokens", ctx, "--fill-budget-fraction", "1")
        ov = self.settings()["skillOverrides"]
        owned = self.state()["owned_overrides"]
        for name in self.ORDER[:3]:
            self.assertNotIn(name, ov)
            self.assertNotIn(name, owned)
        self.assertEqual(ov, {name: "name-only" for name in sorted(self.ORDER[3:])})
        self.assertEqual(owned, ov)
        self.assertEqual(self.settings()["theme"], "dark")

    def test_operator_entries_are_untouched_and_never_fill_candidates(self) -> None:
        operator = {"z-auto-new": "name-only", "n-fleet-new": "off", "stale-one": "on",
                    "not-installed": "user-invocable-only"}
        self.write_settings({"skillListingBudgetFraction": 0.25, "skillOverrides": dict(operator)})
        self.apply("--context-tokens", "100000", "--fill-budget-fraction", "1")
        ov = self.settings()["skillOverrides"]
        for name, value in operator.items():
            self.assertEqual(ov[name], value, name)
            self.assertNotIn(name, self.state()["owned_overrides"])
        # Everything else fits a 100000-char budget and is described by fill.
        self.assertEqual(ov, operator)
        plan = self.plan("--context-tokens", "100000", "--fill-budget-fraction", "1")
        self.assertEqual(sorted(self.filled(plan)),
                         ["a-auto-used", "codex-lane-only", "m-fleet-old", "stale-two"])
        self.assertEqual(plan["fill"]["candidates"], {"autosave-unused": 0, "fleet": 1, "other": 3})
        self.assertFalse(plan["changed"])

    def test_idempotent_second_apply_with_fill(self) -> None:
        args = ("--context-tokens", str(self.limit_for(4)), "--fill-budget-fraction", "1")
        self.apply(*args)
        first = self.settings_path.read_bytes()
        n_backups = len(self.backups())
        self.assertEqual(self.backups()[0].read_bytes(),
                         (json.dumps({"skillListingBudgetFraction": 0.25, "theme": "dark"}, indent=2)
                          + "\n").encode())
        proc = self.apply(*args)
        self.assertIn("no change", proc.stdout)
        self.assertEqual(self.settings_path.read_bytes(), first)
        self.assertEqual(len(self.backups()), n_backups)

    def test_release_removes_everything_owned_and_ignores_the_fill_switch(self) -> None:
        self.write_settings({"theme": "dark", "skillOverrides": {"stale-one": "off"}})
        (self.claude / self.FILL_FILE).write_text("not-a-number\n", encoding="utf-8")
        # The policy sets the budget key itself here (0.02 -> 0.08 x ctx chars);
        # pick a window where fill describes some candidates but not all.
        ctx = str(round(self.limit_for(2) / 0.08))
        self.apply("--context-tokens", ctx, "--fill-budget-fraction", "1")
        owned = self.state()["owned_overrides"]
        self.assertTrue(owned)
        self.assertNotIn("z-auto-new", owned)  # described by fill
        self.assertEqual(self.settings()["skillListingBudgetFraction"], 0.02)
        proc = self.run_cli("release", core=False, env={self.FILL_ENV: "bogus"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        doc = self.settings()
        self.assertEqual(doc, {"theme": "dark", "skillOverrides": {"stale-one": "off"}})
        self.assertEqual(self.state()["owned_overrides"], {})
        proc = self.run_cli("release", "--fill-budget-fraction", "0.5", core=False)
        self.assertEqual(proc.returncode, 2)  # not a release option

    def test_kill_switch_wins_over_fill(self) -> None:
        before = self.settings_path.read_bytes()
        (self.claude / self.FILL_FILE).write_text("1\n", encoding="utf-8")
        proc = self.apply(env={"CCC_SKILL_LISTING_POLICY": "0"})
        self.assertIn("disabled", proc.stdout)
        self.assertEqual(self.settings_path.read_bytes(), before)

    # -- the switch -------------------------------------------------------
    def test_node_local_file_is_picked_up_by_the_scheduled_quiet_apply(self) -> None:
        ctx = str(self.limit_for(len(self.ORDER)))
        (self.claude / self.FILL_FILE).write_text("# experiment A (#2748)\n\n1.0  # all of it\n",
                                                  encoding="utf-8")
        proc = self.apply("--quiet", "--context-tokens", ctx)
        self.assertEqual(proc.stdout.count("\n"), 1)
        self.assertEqual(proc.stderr, "")
        self.assertNotIn("skillOverrides", self.settings())
        plan = self.plan("--context-tokens", ctx)
        self.assertEqual((plan["fill"]["enabled"], plan["fill"]["fraction"]), (True, 1.0))
        self.assertEqual(plan["fill"]["source"], f"file {self.claude / self.FILL_FILE}")
        self.assertFalse(plan["changed"])

    def test_switch_precedence_flag_then_env_then_file(self) -> None:
        ctx = str(2 * self.limit_for(2))
        (self.claude / self.FILL_FILE).write_text("1\n", encoding="utf-8")
        plan = self.plan("--context-tokens", ctx)
        self.assertEqual(len(self.filled(plan)), len(self.ORDER))
        plan = self.plan("--context-tokens", ctx, env={self.FILL_ENV: "0.5"})
        self.assertEqual(sorted(self.filled(plan)), ["n-fleet-new", "z-auto-new"])
        self.assertEqual(plan["fill"]["source"], f"env {self.FILL_ENV}")
        plan = self.plan("--context-tokens", ctx, "--fill-budget-fraction", "1", env={self.FILL_ENV: "0.5"})
        self.assertEqual(len(self.filled(plan)), len(self.ORDER))
        self.assertEqual(plan["fill"]["source"], "--fill-budget-fraction")
        # An explicit off in the env beats an enabling file, for one run.
        for word in ("0", "off", "OFF", "false", "no"):
            proc = self.run_cli("plan", "--json", "--context-tokens", ctx, env={self.FILL_ENV: word})
            self.assertEqual(proc.stderr, "", word)
            plan = json.loads(proc.stdout)
            self.assertEqual((plan["fill"]["enabled"], plan["fill"]["note"]), (False, "off"), word)
            self.assertEqual(self.filled(plan), [], word)

    def test_invalid_flag_value_is_rejected_before_anything_is_written(self) -> None:
        before = self.settings_path.read_bytes()
        for bad in ("0", "-0.5", "1.01", "2", "nan", "inf", "abc", ""):
            proc = self.run_cli("apply", f"--fill-budget-fraction={bad}")
            with self.subTest(value=bad):
                self.assertEqual(proc.returncode, 2)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(self.settings_path.read_bytes(), before)
                self.assertFalse((self.claude / "state" / "skill-listing-policy.json").exists())

    def test_invalid_env_or_file_value_means_fill_off_with_a_warning(self) -> None:
        ctx = str(self.limit_for(len(self.ORDER)))
        baseline = self.baseline_bytes()
        n_backups = len(self.backups())
        for bad in ("abc", "1.5", "-1", "nan", "inf", "50%"):
            proc = self.apply("--quiet", "--context-tokens", ctx, env={self.FILL_ENV: bad})
            with self.subTest(env=bad):
                self.assertEqual(self.settings_path.read_bytes(), baseline)
                self.assertIn("no change", proc.stdout)
                self.assertIn(f"fill mode off — env {self.FILL_ENV}: invalid value ignored", proc.stderr)
                self.assertNotIn(bad, proc.stderr)
        for bad in ("", "\n# only a comment\n", "abc\n", "7\n", "0.5 0.6\n", "x" * 5000):
            (self.claude / self.FILL_FILE).write_text(bad, encoding="utf-8")
            proc = self.apply("--quiet", "--context-tokens", ctx)
            with self.subTest(file=bad[:12]):
                self.assertEqual(self.settings_path.read_bytes(), baseline)
                self.assertIn("invalid value ignored", proc.stderr)
        self.assertEqual(len(self.backups()), n_backups)
        # An invalid env value does not fall through to a valid file.
        (self.claude / self.FILL_FILE).write_text("1\n", encoding="utf-8")
        plan = self.plan("--context-tokens", ctx, env={self.FILL_ENV: "abc"})
        self.assertEqual((plan["fill"]["enabled"], self.filled(plan)), (False, []))
        text = self.run_cli("plan", "--summary", "--context-tokens", ctx, env={self.FILL_ENV: "abc"}).stdout
        self.assertIn(f"fill: off (env {self.FILL_ENV}: invalid value ignored", text)

    # -- reporting --------------------------------------------------------
    def test_plan_reports_fraction_tiers_and_estimate_and_stays_read_only(self) -> None:
        limit = self.limit_for(4)
        before = self.settings_path.read_bytes()
        proc = self.run_cli("plan", "--context-tokens", str(2 * limit), "--fill-budget-fraction", "0.5")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("keep      z-auto-new  [fill: autosave-unused]", proc.stdout)
        self.assertIn("keep      n-fleet-new  [fill: fleet]", proc.stdout)
        self.assertIn("name-only stale-two  [not core, not recently used]", proc.stdout)
        self.assertIn("keep=8 (core=1 recent<30d=3 fill=4) name-only=3 (recent over budget=0)", proc.stdout)
        self.assertIn(
            f"  fill: fraction=0.5 (--fill-budget-fraction) limit={limit} chars; "
            "described by fill=4 (autosave-unused=1/1 fleet=2/2 other=1/4); "
            f"estimated listing={limit}/{2 * limit} chars (50.0% of budget)", proc.stdout)
        self.assertIn("over_budget=false", proc.stdout)
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.assertFalse((self.claude / "state" / "skill-listing-policy.json").exists())
        self.assertEqual(self.backups(), [])
        proc = self.apply("--context-tokens", str(2 * limit), "--fill-budget-fraction", "0.5")
        self.assertIn("described by fill=4", proc.stdout)


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


class ContextWindowTests(PolicyCase):
    """The budget follows the settings.json model's context window unless overridden."""

    def plan(self, *args: str, env: dict | None = None) -> dict:
        proc = self.run_cli("plan", "--json", *args, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_model_mapping(self) -> None:
        mod = load_module(SCRIPT, "ccc_skill_listing_policy_ctx")
        one_m, k200 = 1_000_000, 200_000
        cases = {
            "claude-fable-5-1": one_m, "claude-fable-5": one_m, "claude-mythos-5-1": one_m,
            "claude-opus-5-5": one_m, "claude-opus-5": one_m, "claude-opus-4-8": one_m,
            "claude-opus-4-6": one_m, "claude-sonnet-5": one_m, "claude-sonnet-4-6": one_m,
            "us.anthropic.claude-opus-5": one_m, "claude-opus-4-5[1m]": one_m,
            "opus": one_m, "sonnet": one_m, "fable": one_m, "opusplan": one_m, "Opus": one_m,
            "claude-opus-4-5": k200, "claude-opus-4-5-20251101": k200,
            "claude-sonnet-4-20250514": k200, "claude-opus-4-1": k200,
            "claude-haiku-4-5": k200, "haiku": k200,
            "default": None, "gpt-5": None, "": None, None: None, 42: None,
        }
        for model, want in cases.items():
            self.assertEqual(mod.context_tokens_for_model(model), want, model)

    def test_plan_uses_model_window(self) -> None:
        self.write_settings({"model": "claude-fable-5-1"})
        plan = self.plan()
        self.assertEqual(plan["context_tokens"], 1_000_000)
        self.assertEqual(plan["context_source"], "model claude-fable-5-1")
        self.assertEqual(plan["after"]["budget_chars"], int(0.02 * 1_000_000 * 4))

    def test_unknown_or_missing_model_keeps_200k(self) -> None:
        for doc, source in (({"model": "claude-haiku-4-5"}, "model claude-haiku-4-5"),
                            ({"model": "default"}, "default (model 'default' not recognised)"),
                            ({"theme": "dark"}, "default (no model in settings)")):
            self.write_settings(doc)
            plan = self.plan()
            self.assertEqual(plan["context_tokens"], 200_000, doc)
            self.assertEqual(plan["context_source"], source, doc)
            self.assertEqual(plan["after"]["budget_chars"], int(0.02 * 200_000 * 4), doc)

    def test_env_and_flag_override_model(self) -> None:
        self.write_settings({"model": "claude-opus-5-5"})
        plan = self.plan(env={"CCC_SKILL_LISTING_CONTEXT_TOKENS": "300000"})
        self.assertEqual((plan["context_tokens"], plan["context_source"]),
                         (300_000, "env CCC_SKILL_LISTING_CONTEXT_TOKENS"))
        plan = self.plan("--context-tokens", "250000",
                         env={"CCC_SKILL_LISTING_CONTEXT_TOKENS": "300000"})
        self.assertEqual((plan["context_tokens"], plan["context_source"]),
                         (250_000, "--context-tokens"))

    def test_text_plan_and_apply_name_the_source(self) -> None:
        self.write_settings({"model": "claude-opus-5-5"})
        proc = self.run_cli("plan", "--summary")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("context window: 1000k (model claude-opus-5-5)", proc.stdout)
        proc = self.apply()
        self.assertIn("context window: 1000k (model claude-opus-5-5)", proc.stdout)
        self.assertEqual(self.apply("--quiet").stdout.count("\n"), 1)


class BridgeModelWindowTests(PolicyCase):
    """#2358: without a settings.json model, the bridge's ANTHROPIC_MODEL sets the window."""

    def setUp(self) -> None:
        super().setUp()
        self.write_settings({"theme": "dark"})
        self.shows = self.root / "shows"
        self.shows.mkdir()
        self.fake = self.root / "fake-systemctl"
        # Answers `<scope> show <unit> -p ...` from shows/<unit>; unknown units
        # are not-found, like systemctl. Every call is logged.
        self.fake.write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> "{self.root}/calls"\n'
            f'f="{self.shows}/$3"\n'
            'if [ -f "$f" ]; then cat "$f"; else echo "LoadState=not-found"; fi\n',
            encoding="utf-8")
        self.fake.chmod(0o755)
        self.scope = "system" if os.geteuid() == 0 else "user"

    def show(self, unit: str, *lines: str) -> None:
        (self.shows / unit).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def envfile(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def plan(self, env: dict | None = None) -> dict:
        full = {"CCC_SKILL_LISTING_SYSTEMCTL": str(self.fake)}
        full.update(env or {})
        proc = self.run_cli("plan", "--json", env=full)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_process_env_model(self) -> None:
        plan = self.plan({"ANTHROPIC_MODEL": "claude-opus-5-5"})
        self.assertEqual((plan["context_tokens"], plan["context_source"]),
                         (1_000_000, "env ANTHROPIC_MODEL claude-opus-5-5"))
        self.assertEqual(plan["after"]["budget_chars"], int(0.02 * 1_000_000 * 4))
        self.assertFalse((self.root / "calls").exists(), "env model must not ask systemd")

    def test_settings_model_still_wins(self) -> None:
        self.write_settings({"model": "claude-haiku-4-5"})
        self.show("ccc-matrix-bridge.service", "LoadState=loaded",
                  "Environment=ANTHROPIC_MODEL=claude-opus-5-5")
        plan = self.plan({"ANTHROPIC_MODEL": "claude-fable-5-1"})
        self.assertEqual((plan["context_tokens"], plan["context_source"]),
                         (200_000, "model claude-haiku-4-5"))

    def test_bridge_unit_environment_files_last_wins(self) -> None:
        first = self.envfile("a.env", "ANTHROPIC_MODEL=claude-haiku-4-5\nOTHER_TOKEN=x\n")
        last = self.envfile("b.env", "# ANTHROPIC_MODEL=claude-haiku-4-5\n; comment\n"
                                     "export ANTHROPIC_MODEL=\"claude-opus-5-5\"\n"
                                     "CCC_MODEL_LABEL=Claude Opus 5.5 (high)\n")
        self.show("ccc-matrix-bridge.service", "LoadState=loaded",
                  'Environment=HOME=/x ANTHROPIC_MODEL=claude-haiku-4-5 "CCC_MODEL_LABEL=Some label"',
                  f"EnvironmentFiles={first} (ignore_errors=no)",
                  f"EnvironmentFiles={self.root}/missing.env (ignore_errors=yes)",
                  f"EnvironmentFiles={last} (ignore_errors=no)")
        plan = self.plan()
        self.assertEqual(plan["context_tokens"], 1_000_000)
        self.assertEqual(plan["context_source"],
                         f"bridge {self.scope} ccc-matrix-bridge.service ANTHROPIC_MODEL claude-opus-5-5")
        self.assertEqual(self.settings(), {"theme": "dark"}, "plan stays read-only")

    def test_unit_environment_without_files(self) -> None:
        self.show("ccc-matrix-bridge.service", "LoadState=loaded",
                  'Environment="CCC_MODEL_LABEL=Claude Fable 5.1 (high)" ANTHROPIC_MODEL=claude-fable-5-1')
        plan = self.plan()
        self.assertEqual(plan["context_tokens"], 1_000_000)
        self.assertIn("ANTHROPIC_MODEL claude-fable-5-1", plan["context_source"])

    def test_telegram_unit_when_matrix_has_no_model(self) -> None:
        self.show("ccc-matrix-bridge.service", "LoadState=loaded", "Environment=HOME=/x")
        self.show("ccc-telegram-bridge.service", "LoadState=loaded",
                  "Environment=ANTHROPIC_MODEL=claude-opus-5-5")
        plan = self.plan()
        self.assertEqual(plan["context_source"],
                         f"bridge {self.scope} ccc-telegram-bridge.service ANTHROPIC_MODEL claude-opus-5-5")

    def test_not_loaded_unrecognised_or_missing_systemctl_keep_default(self) -> None:
        self.show("ccc-matrix-bridge.service", "LoadState=not-found",
                  "Environment=ANTHROPIC_MODEL=claude-opus-5-5")
        plan = self.plan()
        self.assertEqual((plan["context_tokens"], plan["context_source"]),
                         (200_000, "default (no model in settings)"))
        self.show("ccc-matrix-bridge.service", "LoadState=loaded",
                  "Environment=ANTHROPIC_MODEL=glm-5.3")
        plan = self.plan({"ANTHROPIC_MODEL": "kimi-k3"})
        self.assertEqual((plan["context_tokens"], plan["context_source"]),
                         (200_000, "default (no model in settings)"))
        plan = self.plan({"CCC_SKILL_LISTING_SYSTEMCTL": str(self.root / "no-such-systemctl")})
        self.assertEqual(plan["context_tokens"], 200_000)

    def test_off_switch_never_runs_systemctl(self) -> None:
        self.show("ccc-matrix-bridge.service", "LoadState=loaded",
                  "Environment=ANTHROPIC_MODEL=claude-opus-5-5")
        plan = self.plan({"CCC_SKILL_LISTING_SYSTEMCTL": "off"})
        self.assertEqual(plan["context_tokens"], 200_000)
        self.assertFalse((self.root / "calls").exists())

    def test_show_parser_reads_only_the_model_key(self) -> None:
        mod = load_module(SCRIPT, "ccc_skill_listing_policy_bridge")
        env = self.envfile("c.env", "SECRET_TOKEN=abc\nANTHROPIC_MODEL='claude-sonnet-5'\n")
        self.assertEqual(mod.env_file_model(env), "claude-sonnet-5")
        self.assertIsNone(mod.env_file_model(self.envfile("d.env", "SECRET_TOKEN=abc\n")))
        self.assertIsNone(mod.env_file_model(self.root / "absent.env"))
        self.assertIsNone(mod.unit_model_from_show("Environment=ANTHROPIC_MODEL=claude-opus-5-5\n"))
        self.assertIsNone(mod.unit_model_from_show(
            'LoadState=loaded\nEnvironment="unterminated ANTHROPIC_MODEL=claude-opus-5-5\n'))
        self.assertEqual(mod.unit_model_from_show(
            "LoadState=loaded\nEnvironmentFiles=relative.env (ignore_errors=no)\n"
            "Environment=ANTHROPIC_MODEL=claude-opus-5-5\n"), "claude-opus-5-5")


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
