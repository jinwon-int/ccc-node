"""Line-by-line SKILL.md frontmatter readers decode YAML-quoted values (#2032).

Writers now quote descriptions YAML would misread (fleet-skills#328). Every
script that reads frontmatter line by line must decode such a value with the
shared helper — otherwise quotes leak into lengths, listings, and registries,
and each reader disagrees with the runtimes (which parse YAML).
"""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import ccc_skill_frontmatter as sf  # noqa: E402  (repository adapter)


def load(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


sync = load("ccc-fleet-skills-sync.py", "readers_test_fleet_sync")
listing = load("ccc-skill-listing-policy.py", "readers_test_listing")
registry = load("ccc-skill-registry.py", "readers_test_registry")
codex = load("ccc_codex_skills.py", "readers_test_codex")
promotion = load("ccc-skill-promotion.py", "readers_test_promotion")

VALUES = [
    "Use when X: do Y after the rollout finishes",
    "Use when a PR fixes issue #42 and the changelog must follow",
    'Use when quoting "inner" text and a \\ backslash matters',
    "Use when the plain description has no YAML hazards at all",
    "- Use when a leading dash would start a YAML sequence",
    "Use when escapes like tab\tand bell\x07 need decoding",
]


def skill_md(description_line: str, name: str = "demo-skill") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description_line}\n"
        "---\n"
        f"# {name}\n\nStep one.\nStep two.\nStep three.\n"
    )


class ReaderAgreementTests(unittest.TestCase):
    def test_every_reader_sees_the_rendered_value(self):
        for value in VALUES:
            line = sf.render_scalar(value)
            text = skill_md(line)
            with self.subTest(value=value):
                self.assertEqual(listing.parse_frontmatter(text)["description"], value)
                self.assertEqual(promotion._frontmatter(text.encode(), "demo-skill"), value)
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "SKILL.md"
                    path.write_text(text, encoding="utf-8")
                    self.assertEqual(registry._frontmatter(path)["description"], value)
                    self.assertEqual(codex._frontmatter(path)["description"], value)
                # The installer validates and returns None on success.
                self.assertIsNone(sync.frontmatter(text.encode(), "demo-skill"))

    def test_installer_measures_the_decoded_length(self):
        short = "Use when short x: y"  # 19 decoded chars, 21 once quoted
        self.assertEqual(len(short), 19)
        with self.assertRaises(sync.SyncError):
            sync.frontmatter(skill_md(sf.render_scalar(short)).encode(), "demo-skill")
        exact = "Use when short x: yz"  # 20 decoded chars
        self.assertIsNone(sync.frontmatter(skill_md(sf.render_scalar(exact)).encode(), "demo-skill"))

    def test_listing_policy_decodes_yaml_escapes_json_would_reject(self):
        # The old json.loads-based unquote returned `\\x41` escapes verbatim.
        text = skill_md('"Use when \\x41 and \\e and \\N decode"')
        self.assertEqual(
            listing.parse_frontmatter(text)["description"], "Use when A and \x1b and \x85 decode")

    def test_single_quoted_and_plain_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SKILL.md"
            path.write_text(skill_md("'Use when it''s single-quoted text'"), encoding="utf-8")
            self.assertEqual(registry._frontmatter(path)["description"],
                             "Use when it's single-quoted text")
            path.write_text(skill_md("Use when plain text # keeps its tail"), encoding="utf-8")
            self.assertEqual(codex._frontmatter(path)["description"],
                             "Use when plain text # keeps its tail")

    def test_lone_script_copy_degrades_to_verbatim(self):
        # ccc-doctor fixtures copy ccc_codex_skills.py alone into a bare repo.
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp) / "scripts"
            scripts.mkdir()
            copy = scripts / "ccc_codex_skills.py"
            copy.write_bytes((HERE / "ccc_codex_skills.py").read_bytes())
            saved_path, saved_module = list(sys.path), sys.modules.pop("ccc_skill_frontmatter", None)
            sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE]
            try:
                spec = importlib.util.spec_from_file_location("readers_test_lone_codex", copy)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertEqual(module._unquote_scalar('  "quoted"  '), '"quoted"')
            finally:
                sys.path[:] = saved_path
                if saved_module is not None:
                    sys.modules["ccc_skill_frontmatter"] = saved_module


class PromotionGateParityTests(unittest.TestCase):
    """#1822: the install gate and the promotion snapshot share one validator."""

    CASES = [
        skill_md("Use when X: do Y after the rollout finishes"),
        skill_md('"Use when X: do Y after the rollout finishes"'),
        skill_md("Use when checking backups\nmetadata:\n  short-description: backups"),
        skill_md("Use when checking backups\ncompatibility: any"),
        skill_md("too short"),
        skill_md("Use when checking the backup rotation", name="other-skill"),
    ]

    def test_promotion_and_shared_validator_agree(self):
        for text in self.CASES:
            with self.subTest(text=text):
                try:
                    shared = ("ok", sf.strict_frontmatter_fields(text, "demo-skill", require_yaml_safe=True))
                except sf.FrontmatterError as error:
                    shared = ("error", error.code)
                try:
                    promoted = ("ok", promotion._frontmatter_fields(
                        text.encode(), "demo-skill", require_yaml_safe=True))
                except promotion.PromotionError as error:
                    promoted = ("error", error.code)
                self.assertEqual(shared, promoted)


if __name__ == "__main__":
    unittest.main()
