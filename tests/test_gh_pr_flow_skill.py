from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "claude/skills/gh-pr-flow/SKILL.md"
HELPER = ROOT / "claude/skills/gh-pr-flow/scripts/seoseo-jinon-gh"


def test_seoseo_merge_lane_has_secret_and_identity_guards() -> None:
    text = SKILL.read_text()
    helper = HELPER.read_text()

    assert "--confirm-user-approved" in text
    assert "Never copy, print, return, or persist the Seoseo token" in text
    assert "expected 'jinon86'" in helper
    assert "jinon86 cannot approve a jinon86-authored PR" in helper
    assert "--match-head-commit" in helper
    assert "--admin" not in helper
    assert "gh auth token" not in helper


def test_seoseo_merge_helper_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(HELPER)], check=True)


def test_seoseo_merge_helper_rejects_mutation_without_confirmation() -> None:
    result = subprocess.run(
        [str(HELPER), "merge", "jinwon-int/ccc-node", "1"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--confirm-user-approved" in result.stderr
