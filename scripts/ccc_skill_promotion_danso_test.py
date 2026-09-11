"""#1663: the danso provider root chain and revise vocabulary contracts.

The staging/publish flow lives in ccc-skill-promotion.test.sh (it needs the
full harness); these are the config-level contracts that are awkward to
observe from bash: the #1659/#1662 root chain (explicit
CCC_SKILL_PROMOTION_DANSO_SKILLS_DIR > DANSO_SKILLS_DIR >
$CCC_DANSO_STATE_DIR/home/.pi/agent/skills), entry omission when the chain is
unresolved, fail-closed selection without a root, and the revise vocabulary
accepting the danso lane.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "danso_promotion_test", Path(__file__).with_name("ccc-skill-promotion.py"))
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)


class DansoRootChainTests(unittest.TestCase):
    def setUp(self):
        self.home = Path("/danso-test-home")
        self.base = {
            "HOME": str(self.home),
            "CCC_CLAUDE_DIR": str(self.home / ".claude"),
            "CCC_STATE_DIR": str(self.home / ".claude" / "state"),
            "CCC_SKILL_PROMOTION_REPO": "test/repo",
            "CCC_NODE": "testnode",
        }

    def roots(self, **extra):
        env = dict(self.base)
        env.update(extra)
        return promotion._config(env).provider_roots.get("danso")

    def test_explicit_promotion_env_wins(self):
        explicit = self.home / "explicit-danso-skills"
        self.assertEqual(
            self.roots(CCC_SKILL_PROMOTION_DANSO_SKILLS_DIR=str(explicit)),
            explicit)

    def test_explicit_beats_danso_skills_dir_fallback(self):
        explicit = self.home / "explicit-danso-skills"
        self.assertEqual(
            self.roots(
                CCC_SKILL_PROMOTION_DANSO_SKILLS_DIR=str(explicit),
                DANSO_SKILLS_DIR=str(self.home / "fallback-skills")),
            explicit)

    def test_danso_skills_dir_fallback(self):
        self.assertEqual(
            self.roots(DANSO_SKILLS_DIR=str(self.home / "fallback-skills")),
            self.home / "fallback-skills")

    def test_state_dir_home_chain_fallback(self):
        self.assertEqual(
            self.roots(CCC_DANSO_STATE_DIR=str(self.home / "danso-state")),
            self.home / "danso-state" / "home" / ".pi" / "agent" / "skills")

    def test_unresolved_chain_omits_entry(self):
        self.assertIsNone(self.roots())

    def test_selection_without_root_fails_closed(self):
        env = dict(self.base)
        env["CCC_SKILL_PROMOTION_PROVIDERS"] = "claude,danso"
        with self.assertRaises(promotion.PromotionError) as caught:
            promotion._config(env)
        self.assertEqual(caught.exception.code, "provider_root_unresolved")


class DansoReviseVocabularyTests(unittest.TestCase):
    def test_revise_vocabulary_accepts_danso_lane(self):
        self.assertIn("danso", promotion._REVISE_PROVIDER_VOCABULARY)


if __name__ == "__main__":
    unittest.main()
