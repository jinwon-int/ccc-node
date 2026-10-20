#!/usr/bin/env python3
"""Tests for scripts/ccc-skill-validate.py (ccc-node#2386). Stdlib unittest."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "ccc-skill-validate.py"
_spec = importlib.util.spec_from_file_location("ccc_skill_validate", SCRIPT)
assert _spec is not None and _spec.loader is not None
sv = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sv  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(sv)

GOOD_DESC = "Use when checking the fixture tree, so the validator has something to read."


def skill_md(name: str | None, desc: str | None = GOOD_DESC, body: str = "Body.\n") -> str:
    lines = ["---"]
    if name is not None:
        lines.append(f"name: {name}")
    if desc is not None:
        lines.append(f"description: {desc}")
    lines.append("---")
    return "\n".join(lines) + "\n\n# Title\n\n" + body


class RepoFixture(unittest.TestCase):
    """A throwaway repo layout; every test runs in repo mode against it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        for rel in ("skills", "codex/skills", "piri/skills", "claude/hooks", "claude/output-styles"):
            (self.repo / rel).mkdir(parents=True)
        self.write("claude/skill-listing-core.txt", "# core\nalpha\nabsent-core\n")
        self.write("claude/CLAUDE.md.template", "Use the alpha skill for fixtures.\n")
        self.write("claude/hooks/tools-cheatsheet.md", "nothing here\n")
        self.write("claude/settings.base.json", json.dumps({"hooks": {}}))
        self.add_skill("skills", "alpha")
        self._cwd = os.getcwd()
        os.chdir(self.repo)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def write(self, rel: str, text: str) -> Path:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def add_skill(self, root: str, name: str, text: str | None = None) -> Path:
        return self.write(f"{root}/{name}/SKILL.md", skill_md(name) if text is None else text)

    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = sv.main(["--repo-root", str(self.repo), *args])
        return rc, out.getvalue()

    def findings(self, *args: str) -> tuple[int, list[dict]]:
        rc, out = self.run_cli("--json", *args)
        return rc, json.loads(out)["findings"]

    def rules(self, *args: str) -> dict[str, str]:
        _, found = self.findings(*args)
        return {f"{f['rule']}:{f['skill'] or f['path']}": f["severity"] for f in found}


class BaselineTests(RepoFixture):
    def test_clean_tree_passes(self) -> None:
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)
        self.assertIn("skill-validate: 1 skill(s) in 3 root(s) — 0 error(s), 0 warning(s)", out)

    def test_output_is_deterministic_and_sorted(self) -> None:
        for name in ("zeta", "beta", "mu"):
            self.add_skill("skills", name, skill_md("other"))
        first = self.run_cli("--json")
        second = self.run_cli("--json")
        self.assertEqual(first, second)
        paths = [f["path"] for f in json.loads(first[1])["findings"]]
        self.assertEqual(paths, sorted(paths))


class FrontmatterTests(RepoFixture):
    def test_missing_frontmatter_fails(self) -> None:
        self.add_skill("skills", "bare", "# no frontmatter\n")
        rc, found = self.findings()
        self.assertEqual(rc, 1)
        hit = [f for f in found if f["rule"] == "frontmatter-missing"]
        self.assertEqual(hit[0]["path"], "skills/bare/SKILL.md")
        self.assertEqual(hit[0]["line"], 1)

    def test_unterminated_frontmatter_fails(self) -> None:
        self.add_skill("skills", "open", "---\nname: open\ndescription: x\n# body\n")
        self.assertEqual(self.rules()["frontmatter-missing:open"], "error")

    def test_missing_and_empty_description_fail(self) -> None:
        self.add_skill("skills", "nodesc", skill_md("nodesc", desc=None))
        self.add_skill("codex/skills", "emptydesc", skill_md("emptydesc", desc='""'))
        rules = self.rules()
        self.assertEqual(rules["description-missing:nodesc"], "error")
        self.assertEqual(rules["description-missing:emptydesc"], "error")

    def test_block_scalar_description_counts(self) -> None:
        text = "---\nname: folded\ndescription: >\n  Use when folding.\n  Second line.\n---\nbody\n"
        self.add_skill("skills", "folded", text)
        self.assertNotIn("description-missing:folded", self.rules())

    def test_name_mismatch_fails_with_line(self) -> None:
        self.add_skill("piri/skills", "web", skill_md("webby"))
        rc, found = self.findings()
        self.assertEqual(rc, 1)
        hit = [f for f in found if f["rule"] == "name-mismatch"][0]
        self.assertEqual((hit["path"], hit["line"]), ("piri/skills/web/SKILL.md", 2))
        self.assertIn("'webby' != directory 'web'", hit["message"])

    def test_missing_name_warns_then_strict_fails(self) -> None:
        self.add_skill("skills", "noname", skill_md(None))
        rc, found = self.findings()
        self.assertEqual(rc, 0)
        self.assertEqual([f["severity"] for f in found if f["rule"] == "name-missing"], ["warn"])
        rc, found = self.findings("--strict")
        self.assertEqual(rc, 1)
        self.assertEqual([f["severity"] for f in found if f["rule"] == "name-missing"], ["error"])

    def test_banned_words_warn_by_default_and_fail_strict(self) -> None:
        desc = "Use when a Rubric or the grader scores a benchmark evaluation of the run."
        self.add_skill("skills", "gamey", skill_md("gamey", desc=desc))
        rc, found = self.findings()
        self.assertEqual(rc, 0)
        words = sorted(f["message"].split("'")[1] for f in found if f["rule"] == "description-banned-word")
        self.assertEqual(words, ["benchmark", "evaluation", "grader", "rubric", "scores"])
        self.assertEqual(self.findings("--strict")[0], 1)

    def test_banned_words_need_word_boundaries(self) -> None:
        desc = "Use when scoreboard or underscore or reevaluation text shows up in logs."
        self.add_skill("skills", "fine", skill_md("fine", desc=desc))
        self.assertNotIn("description-banned-word:fine", self.rules())

    def test_dot_directories_are_skipped(self) -> None:
        self.add_skill("skills", ".stage-alpha", skill_md("alpha"))
        self.assertEqual(self.run_cli()[0], 0)


class TriggerTests(RepoFixture):
    def test_core_skill_without_trigger_fails(self) -> None:
        self.write("claude/CLAUDE.md.template", "the alphabet is not a trigger\n")
        rc, found = self.findings()
        self.assertEqual(rc, 1)
        self.assertEqual([f["skill"] for f in found if f["rule"] == "trigger-core-missing"], ["alpha"])

    def test_trigger_in_output_style_or_cheatsheet_counts(self) -> None:
        self.write("claude/CLAUDE.md.template", "nothing\n")
        self.write("claude/output-styles/report.md", "load `/alpha` when needed\n")
        self.assertEqual(self.run_cli()[0], 0)
        (self.repo / "claude/output-styles/report.md").unlink()
        self.write("claude/hooks/tools-cheatsheet.md", "- alpha: fixture skill\n")
        self.assertEqual(self.run_cli()[0], 0)

    def test_name_must_match_whole_token(self) -> None:
        self.write("claude/CLAUDE.md.template", "alpha-beta and alphas\n")
        self.assertIn("trigger-core-missing:alpha", self.rules())

    def test_non_core_untriggered_is_a_note_even_strict(self) -> None:
        self.add_skill("skills", "loner")
        rc, found = self.findings("--strict")
        self.assertEqual(rc, 0)
        self.assertEqual([f["severity"] for f in found if f["skill"] == "loner"], ["note"])

    def test_absent_core_names_are_ignored(self) -> None:
        self.assertNotIn("absent-core", json.dumps(self.findings()[1]))

    def test_warn_rule_demotes_core_trigger(self) -> None:
        self.write("claude/CLAUDE.md.template", "nothing\n")
        rc, found = self.findings("--warn-rule", "trigger-core-missing")
        self.assertEqual(rc, 0)
        self.assertEqual([f["severity"] for f in found if f["rule"] == "trigger-core-missing"], ["warn"])

    def test_autotrigger_dry_run_suggests_line(self) -> None:
        self.write("claude/CLAUDE.md.template", "nothing\n")
        rc, out = self.run_cli("--json", "--autotrigger", "--dry-run")
        self.assertEqual(rc, 1)
        line = json.loads(out)["autotrigger"][0]["line"]
        self.assertEqual(
            line,
            "- When checking the fixture tree, so the validator has something to read, "
            "load the alpha skill.",
        )

    def test_autotrigger_without_dry_run_is_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            sv.main(["--repo-root", str(self.repo), "--autotrigger"])
        self.assertEqual(cm.exception.code, 2)


class ScriptRefTests(RepoFixture):
    def body_skill(self, body: str, name: str = "tool") -> None:
        self.add_skill("skills", name, skill_md(name, body=textwrap.dedent(body)))

    def test_skill_anchored_ref_must_exist(self) -> None:
        self.body_skill('```\npython3 "$SKILL_DIR/scripts/run.py" --x\n```\n')
        rc, found = self.findings()
        self.assertEqual(rc, 1)
        hit = [f for f in found if f["rule"] == "script-ref-missing"][0]
        self.assertEqual((hit["path"], hit["line"]), ("skills/tool/SKILL.md", 9))  # line after the fence
        self.write("skills/tool/scripts/run.py", "print('ok')\n")
        self.assertEqual(self.run_cli()[0], 0)

    def test_placeholder_and_named_skill_paths_are_skill_anchored(self) -> None:
        self.body_skill(
            "python3 <skill-root>/scripts/a.py\n"
            "bash ${CODEX_HOME:-$HOME/.codex}/skills/tool/scripts/b.sh\n"
        )
        missing = sorted(f["message"] for f in self.findings()[1] if f["rule"] == "script-ref-missing")
        self.assertEqual(len(missing), 2, missing)

    def test_bare_ref_resolves_against_repo_base(self) -> None:
        self.body_skill("run `scripts/repo-tool.sh --json` first\n")
        self.assertEqual(self.rules()["script-ref-unresolved:tool"], "warn")
        self.write("scripts/repo-tool.sh", "#!/bin/sh\n")
        self.assertNotIn("script-ref-unresolved:tool", self.rules())

    def test_unresolved_ref_fails_under_strict(self) -> None:
        self.body_skill('bash "$ROOT/scripts/nowhere.sh"\n')
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(self.run_cli("--strict")[0], 1)

    def test_urls_are_ignored(self) -> None:
        self.body_skill("see https://example.com/raw/scripts/x.py\n")
        self.assertEqual(self.run_cli("--strict")[0], 0)

    def test_referenced_python_must_compile(self) -> None:
        self.body_skill('python3 "$SKILL_DIR/scripts/bad.py"\n')
        self.write("skills/tool/scripts/bad.py", "def broken(:\n")
        rc, found = self.findings()
        self.assertEqual(rc, 1)
        hit = [f for f in found if f["rule"] == "py-compile"]
        self.assertEqual(len(hit), 1)  # once, though both the ref and the tree walk see it
        self.assertEqual((hit[0]["path"], hit[0]["line"]), ("skills/tool/scripts/bad.py", 1))

    def test_unreferenced_python_in_skill_is_compiled(self) -> None:
        self.write("skills/alpha/helper.py", "x = (\n")
        self.assertEqual(self.rules()["py-compile:alpha"], "error")


class HookTests(RepoFixture):
    def hooks(self, block: object, rel: str = "claude/hooks/hooks.json") -> list[dict]:
        self.write(rel, json.dumps({"hooks": block}))
        return [f for f in self.findings("--strict")[1] if f["rule"].startswith("hooks-")]

    def test_valid_hooks_pass(self) -> None:
        block = {
            "PostToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "x", "timeout": 5}]}],
            "Stop": [{"hooks": [{"type": "prompt", "prompt": "check"}]}],
        }
        self.assertEqual(self.hooks(block), [])

    def test_shape_errors(self) -> None:
        block = {
            "Stop": {"hooks": []},
            "SessionStart": [
                {"matcher": 3, "hooks": [{"type": "command", "command": "  "}]},
                {"hooks": []},
                {"hooks": [{"command": "x"}, {"type": "command", "command": "y", "timeout": 0}]},
            ],
        }
        messages = sorted(f["message"] for f in self.hooks(block))
        self.assertEqual(
            messages,
            [
                "hooks.SessionStart[0].hooks[0].command is empty (the hook never fires)",
                "hooks.SessionStart[0].matcher must be a string",
                "hooks.SessionStart[1].hooks must be a non-empty list",
                "hooks.SessionStart[2].hooks[0].type is missing (expected 'command')",
                "hooks.SessionStart[2].hooks[1].timeout must be a positive number",
                "hooks.Stop must be a list of matcher groups",
            ],
        )

    def test_unknown_event_and_type_warn(self) -> None:
        self.write(
            "claude/hooks/hooks.json",
            json.dumps({"hooks": {"PostTooluse": [{"hooks": [{"type": "shell", "command": "x"}]}]}}),
        )
        rc, found = self.findings()
        self.assertEqual(rc, 0)
        self.assertEqual(
            sorted((f["rule"], f["severity"]) for f in found if f["rule"].startswith("hooks-")),
            [("hooks-unknown-event", "warn"), ("hooks-unknown-type", "warn")],
        )

    def test_invalid_json_fails(self) -> None:
        self.write("claude/settings.base.json", "{not json")
        self.assertEqual(self.rules()["hooks-schema:claude/settings.base.json"], "error")


class NodeModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.claude = Path(self._tmp.name) / "claude-home"
        (self.claude / "skills").mkdir(parents=True)
        (self.claude / "hooks").mkdir()
        (self.claude / "CLAUDE.md").write_text("load the core-one skill\n", encoding="utf-8")
        (self.claude / "core.txt").write_text("core-one\ncore-two\n", encoding="utf-8")
        for name in ("core-one", "core-two"):
            (self.claude / "skills" / name).mkdir()
            (self.claude / "skills" / name / "SKILL.md").write_text(skill_md(name), encoding="utf-8")
        (self.claude / "settings.json").write_text(
            json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": ""}]}]}}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_node(self, *extra: str) -> subprocess.CompletedProcess[str]:
        cmd = [
            sys.executable, str(SCRIPT), "--root", str(self.claude / "skills"),
            "--claude-dir", str(self.claude), "--core-list", str(self.claude / "core.txt"), *extra,
        ]
        return subprocess.run(cmd, capture_output=True, text=True, check=False)

    def test_node_root_reads_claude_dir_layout(self) -> None:
        proc = self.run_node("--json")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["skills_checked"], 2)
        got = sorted((f["rule"], f["skill"]) for f in data["findings"])
        self.assertEqual(got, [("hooks-schema", ""), ("trigger-core-missing", "core-two")])

    def test_settings_override_and_text_output(self) -> None:
        good = self.claude / "ok.json"
        good.write_text("{}", encoding="utf-8")
        (self.claude / "CLAUDE.md").write_text("core-one core-two\n", encoding="utf-8")
        proc = self.run_node("--settings", str(good))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("2 skill(s) in 1 root(s) — 0 error(s), 0 warning(s), 0 note(s)", proc.stdout)

    def test_missing_root_is_usage_error(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.claude / "nope")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("root not found", proc.stderr)


class ShippedTreeTests(unittest.TestCase):
    """The repo's own skills pass in the CI mode validate-harness.sh uses."""

    def test_repo_tree_passes_ci_mode(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--warn-rule", "trigger-core-missing"],
            capture_output=True, text=True, check=False, cwd=HERE.parent,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
