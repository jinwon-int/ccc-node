#!/usr/bin/env python3
"""`possible-token` must not fire on scheme-less API paths (ccc-node#2399).

Field case, 2026-10-20 (fleet-skills#413, `github-actions-runner-fleet-
recovery-after-billing-block`): a correct revision was rejected as
`secret_possible-token` because three `gh api` REST paths (41-45 chars,
slash-joined) matched `[A-Za-z0-9+/]{40,}`. A slash path is masked only when
every segment reads as a word (lowercase/Capitalised word, letters-only caps
placeholder, number); a random base64 run, any URL, and a path with one
non-word segment still fail. A re-gating failure records file:line and match
length — never the value.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "promotion_api_path_mask_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

# Synthetic, not credentials: built in code so no literal looks like a key to
# the repo's gitleaks scan. 44 chars of mixed case + digits, and the same with
# slashes every 11 chars (the shape of a base64 secret that happens to carry /).
ALPHABET = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
LONG_RUN = "".join(ALPHABET[i % 36] for i in range(44))
SLASHED_RUN = "/".join(LONG_RUN[i:i + 11] for i in range(0, 44, 11))

FIELD_PATHS = (
    "gh api repos/OWNER/REPO/actions/runners/registration-token\n"
    "gh api /organizations/ORG/actions/runners/generate-jitconfig\n"
    "gh api -X POST repos/OWNER/REPO/actions/runners/registration-token\n"
    "gh api repos/OWNER/REPO/actions/permissions/selected/actions\n"
)


def scan_code(text: str) -> str | None:
    try:
        promotion._scan_text(text.encode("utf-8"))
    except promotion.PromotionError as error:
        return error.code
    return None


class WordPathMask(unittest.TestCase):
    def test_field_gh_api_paths_pass(self) -> None:
        for line in FIELD_PATHS.splitlines():
            self.assertTrue(any(len(run) >= 40 for run in promotion._CREDENTIAL_PATTERNS[-1][1].findall(line)),
                            line)  # each fixture really trips the bare pattern
        self.assertIsNone(scan_code(FIELD_PATHS))

    def test_bare_and_slashed_random_runs_still_fail(self) -> None:
        self.assertEqual(scan_code(f"value: {LONG_RUN}\n"), "secret_possible-token")
        self.assertEqual(scan_code(f"value: {SLASHED_RUN}\n"), "secret_possible-token")

    def test_one_non_word_segment_keeps_the_whole_path(self) -> None:
        text = f"gh api repos/owner/repo/actions/{LONG_RUN[:30]}/x\n"
        self.assertEqual(scan_code(text), "secret_possible-token")

    def test_urls_are_never_masked(self) -> None:
        text = "see https://hooks.example.com/services/team/channel/abcdefghij/klmnopqrstuv\n"
        self.assertEqual(scan_code(text), "secret_possible-token")

    def test_plus_keeps_the_run(self) -> None:
        text = "gh api repos/owner/repo/actions/runners+registration/token/value\n"
        self.assertEqual(scan_code(text), "secret_possible-token")

    def test_masking_preserves_line_positions(self) -> None:
        self.assertEqual(len(promotion._token_haystack(FIELD_PATHS)), len(FIELD_PATHS))


class SecretScanLocations(unittest.TestCase):
    def test_locations_name_file_line_and_length_only(self) -> None:
        files = [("SKILL.md", b"# x\nok\n"), ("references/a.md", f"one\ntwo {LONG_RUN}\n".encode())]
        found = promotion._secret_scan_locations(files, "secret_possible-token")
        self.assertEqual(found, [{"path": "references/a.md", "line": 2, "length": 44}])
        self.assertNotIn(LONG_RUN, repr(found))

    def test_non_secret_codes_have_no_locations(self) -> None:
        self.assertEqual(promotion._secret_scan_locations([("a", b"x")], "node_specific_ipv4"), [])


if __name__ == "__main__":
    unittest.main()
