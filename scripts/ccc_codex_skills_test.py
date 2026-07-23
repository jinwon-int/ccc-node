#!/usr/bin/env python3
"""Hermetic contracts for repo-shipped Codex managed skills (#647)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "ccc_codex_skills.py"


def run_tool(
    *args: str,
    repo: Path = ROOT,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        "python3",
        str(TOOL),
        *args,
        "--repo-root",
        str(repo),
    ]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        check=False,
    )


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        if path.is_symlink():
            digest.update(b"L")
            digest.update(os.readlink(path).encode())
        elif path.is_dir():
            digest.update(b"D")
            digest.update(f"{stat.S_IMODE(path.stat().st_mode):04o}".encode())
        else:
            digest.update(b"F")
            digest.update(f"{stat.S_IMODE(path.stat().st_mode):04o}".encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


class CodexManagedSkillsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "termux-like" / "data" / ".codex"

    def copy_repo_surface(self) -> Path:
        repo = self.base / "repo"
        for relative in ("claude/commands", "claude/skills", "claude/agents", "claude/hooks"):
            source = ROOT / relative
            shutil.copytree(source, repo / relative)
        shutil.copytree(ROOT / "codex", repo / "codex")
        return repo

    def apply(self, *, repo: Path = ROOT, env: dict[str, str] | None = None):
        return run_tool(
            "apply",
            "--codex-home",
            str(self.home),
            repo=repo,
            env=env,
        )

    def test_catalog_is_complete_and_codex_skills_are_valid(self) -> None:
        result = run_tool("validate")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertGreater(payload["classified_assets"], 20)
        self.assertEqual(payload["managed_skills"], 7)

    def test_plan_is_body_free_and_does_not_create_codex_home(self) -> None:
        result = run_tool("plan", "--codex-home", str(self.home))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual({item["status"] for item in payload["skills"]}, {"create"})
        self.assertEqual(len(payload["skills"]), 7)
        self.assertFalse(self.home.exists())
        self.assertNotIn("description", result.stdout.lower())
        self.assertNotIn("procedure", result.stdout.lower())

    def test_apply_installs_once_with_private_modes_and_provenance(self) -> None:
        first = self.apply()
        self.assertEqual(first.returncode, 0, first.stderr)
        first_digest = tree_digest(self.home)
        payload = json.loads(first.stdout)
        names = {item["name"] for item in payload["skills"]}
        self.assertEqual(len(names), 7)
        for name in names:
            target = self.home / "skills" / name
            marker = json.loads((target / ".ccc-node-managed.json").read_text())
            self.assertEqual(marker["manager"], "ccc-node")
            self.assertEqual(marker["name"], name)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
            for path in target.rglob("*"):
                expected = 0o700 if path.is_dir() else 0o600
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected)

        second = self.apply()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            {item["status"] for item in json.loads(second.stdout)["skills"]},
            {"unchanged"},
        )
        self.assertEqual(tree_digest(self.home), first_digest)

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is not installed")
    def test_installed_skills_appear_in_isolated_codex_discovery(self) -> None:
        applied = self.apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)

        discovered = subprocess.run(
            ["codex", "debug", "prompt-input", "inspect node status"],
            capture_output=True,
            text=True,
            env={**os.environ, "CODEX_HOME": str(self.home)},
            check=False,
        )

        self.assertEqual(discovered.returncode, 0, discovered.stderr)
        payload = json.loads(discovered.stdout)
        serialized = json.dumps(payload, ensure_ascii=False)
        for item in json.loads(applied.stdout)["skills"]:
            self.assertIn(item["name"], serialized)

    def test_user_skill_collision_is_preserved_and_blocks_all_mutation(self) -> None:
        collision = self.home / "skills" / "ccc-doctor"
        collision.mkdir(parents=True, mode=0o700)
        collision.chmod(0o700)
        (self.home / "skills").chmod(0o700)
        self.home.chmod(0o700)
        user_skill = collision / "SKILL.md"
        user_skill.write_text("user-authored\n")
        user_skill.chmod(0o600)
        before = tree_digest(self.home)

        result = self.apply()

        self.assertEqual(result.returncode, 2)
        self.assertIn("unmanaged_collision", result.stderr)
        self.assertEqual(tree_digest(self.home), before)
        self.assertEqual(user_skill.read_text(), "user-authored\n")

    def test_symlink_collision_is_fail_closed(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        skills = self.home / "skills"
        skills.mkdir(parents=True, mode=0o700)
        skills.chmod(0o700)
        self.home.chmod(0o700)
        (skills / "ccc-doctor").symlink_to(outside, target_is_directory=True)

        result = self.apply()

        self.assertEqual(result.returncode, 2)
        self.assertIn("unsafe_target", result.stderr)
        self.assertEqual(list(outside.iterdir()), [])

    def test_source_update_replaces_only_intact_managed_skill(self) -> None:
        repo = self.copy_repo_surface()
        self.assertEqual(self.apply(repo=repo).returncode, 0)
        source = repo / "codex" / "skills" / "ccc-doctor" / "SKILL.md"
        source.write_text(source.read_text() + "\n<!-- source update -->\n")
        before_other = tree_digest(self.home / "skills" / "ccc-node-status")

        result = self.apply(repo=repo)

        self.assertEqual(result.returncode, 0, result.stderr)
        statuses = {item["name"]: item["status"] for item in json.loads(result.stdout)["skills"]}
        self.assertEqual(statuses["ccc-doctor"], "update")
        self.assertEqual(statuses["ccc-node-status"], "unchanged")
        self.assertIn(
            "source update",
            (self.home / "skills" / "ccc-doctor" / "SKILL.md").read_text(),
        )
        self.assertEqual(
            tree_digest(self.home / "skills" / "ccc-node-status"),
            before_other,
        )

    def test_managed_drift_is_not_overwritten(self) -> None:
        self.assertEqual(self.apply().returncode, 0)
        installed = self.home / "skills" / "ccc-doctor" / "SKILL.md"
        installed.write_text(installed.read_text() + "\nmanual edit\n")
        installed.chmod(0o600)
        before = tree_digest(self.home)

        result = self.apply()

        self.assertEqual(result.returncode, 2)
        self.assertIn("managed_drift", result.stderr)
        self.assertEqual(tree_digest(self.home), before)

    def test_partial_update_failure_rolls_back_every_skill(self) -> None:
        repo = self.copy_repo_surface()
        self.assertEqual(self.apply(repo=repo).returncode, 0)
        before = tree_digest(self.home)
        for name in ("ccc-doctor", "ccc-node-status"):
            source = repo / "codex" / "skills" / name / "SKILL.md"
            source.write_text(source.read_text() + f"\n<!-- update {name} -->\n")

        result = self.apply(
            repo=repo,
            env={"CCC_CODEX_SKILLS_TEST_FAIL_AFTER": "1"},
        )

        self.assertEqual(result.returncode, 70)
        self.assertIn("transaction_rolled_back", result.stderr)
        self.assertEqual(tree_digest(self.home), before)

    def test_new_unclassified_command_fails_catalog_validation(self) -> None:
        repo = self.copy_repo_surface()
        (repo / "claude" / "commands" / "unclassified.md").write_text("x\n")

        result = run_tool("validate", repo=repo)

        self.assertEqual(result.returncode, 2)
        self.assertIn("catalog_unclassified", result.stderr)

    def test_codex_skill_with_claude_only_reference_fails_validation(self) -> None:
        repo = self.copy_repo_surface()
        skill = repo / "codex" / "skills" / "ccc-doctor" / "SKILL.md"
        skill.write_text(skill.read_text() + "\nRun ~/.claude/hooks/example.sh\n")

        result = run_tool("validate", repo=repo)

        self.assertEqual(result.returncode, 2)
        self.assertIn("codex_incompatible_reference", result.stderr)


if __name__ == "__main__":
    unittest.main()
