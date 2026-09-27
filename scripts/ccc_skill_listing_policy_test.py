#!/usr/bin/env python3
"""Hermetic tests for ccc-skill-listing-policy.py (ccc-node#2011 A).

Every case builds a private CCC_CLAUDE_DIR under a temp dir and drives the
real CLI through a subprocess, so the live ~/.claude is never read or written.
"""

from __future__ import annotations

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


class PolicyCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.claude = self.root / "claude"
        (self.claude / "skills").mkdir(parents=True)
        (self.claude / "state" / "skill-usage").mkdir(parents=True)
        self.core = self.root / "core.txt"
        self.core.write_text("# core\ncore-flow\nabsent-core  # not installed\n", encoding="utf-8")
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

    def apply(self, **kw) -> subprocess.CompletedProcess:
        proc = self.run_cli("apply", **kw)
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
