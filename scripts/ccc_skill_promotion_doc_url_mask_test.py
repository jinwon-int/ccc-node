#!/usr/bin/env python3
"""`possible-token` must not fire on allowlisted public docs URLs (ccc-node#2694).

Field case, 2026-11-06 (fleet-skills#617 r2, `service-decommission-multi-
dependency-safety`): the revision added a reference file whose line 8 cited a
Docker CLI reference URL. `https://docs.docker.com/` followed by five word
segments leaves a 40-char `[A-Za-z0-9+/]` run after the host, so the re-gate
voided an otherwise valid revision. Owner decision A on #2694: mask only a
fixed allowlist of public docs hosts, only when every path segment reads as a
word; every other URL (webhooks above all, #2371) still fails.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "promotion_doc_url_mask_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

# Synthetic, not credentials: built in code so no literal looks like a key to
# the repo's gitleaks scan.
ALPHABET = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
LONG_RUN = "".join(ALPHABET[i % 36] for i in range(44))
WORD_PATH = "/reference/cli/docker/container/exec/"
FIELD = f"- **Exit codes.** See the reference (https://docs.docker.com{WORD_PATH}).\n"


def scan_code(text: str) -> str | None:
    try:
        promotion._scan_text(text.encode("utf-8"))
    except promotion.PromotionError as error:
        return error.code
    return None


def bare_trips(text: str) -> bool:
    return any(len(run) >= 40 for run in promotion._CREDENTIAL_PATTERNS[-1][1].findall(text))


class PublicDocUrlMask(unittest.TestCase):
    def test_field_docs_url_passes(self) -> None:
        self.assertTrue(bare_trips(FIELD))  # the fixture really trips the bare pattern
        self.assertIsNone(scan_code(FIELD))

    def test_every_allowlisted_host_is_masked(self) -> None:
        for host in promotion._PUBLIC_DOC_HOSTS:
            text = f"see https://{host}{WORD_PATH}manual/page\n"
            self.assertIsNone(scan_code(text), host)

    def test_random_segment_under_an_allowlisted_host_still_fails(self) -> None:
        text = f"see https://docs.docker.com/reference/{LONG_RUN[:30]}/x\n"
        self.assertEqual(scan_code(text), "secret_possible-token")

    def test_host_outside_the_list_still_fails(self) -> None:
        text = f"see https://docs.example.com{WORD_PATH}\n"
        self.assertTrue(bare_trips(text))
        self.assertEqual(scan_code(text), "secret_possible-token")

    def test_lookalike_host_still_fails(self) -> None:
        text = f"see https://docs.docker.com.evil.example{WORD_PATH}\n"
        self.assertEqual(scan_code(text), "secret_possible-token")

    def test_longer_host_is_not_an_allowlisted_host(self) -> None:
        # Without the exact-host boundary the allowlisted prefix would be
        # blanked and the #2399 word-path mask would then hide the rest.
        text = f"see https://docs.docker.community{WORD_PATH}x\n"
        self.assertEqual(scan_code(text), "secret_possible-token")

    def test_userinfo_trick_is_left_to_the_other_gates(self) -> None:
        # The docs mask must not touch it; the user-at-host gate rejects it.
        text = f"see https://docs.docker.com@hooks.example{WORD_PATH}\n"
        self.assertEqual(promotion._mask_public_doc_urls(text), text)
        self.assertIsNotNone(scan_code(text))

    def test_query_or_fragment_keeps_the_url(self) -> None:
        for tail in ("?ref=x", "#section", "%2Fx"):
            text = f"see https://docs.docker.com{WORD_PATH}{tail}\n"
            self.assertEqual(scan_code(text), "secret_possible-token", tail)

    def test_plus_keeps_the_url(self) -> None:
        text = "see https://docs.docker.com/reference/cli/docker/container+exec/run/\n"
        self.assertEqual(scan_code(text), "secret_possible-token")

    def test_webhook_shaped_url_still_fails(self) -> None:
        text = "see https://hooks.example.com/services/team/channel/abcdefghij/klmnopqrstuv\n"
        self.assertEqual(scan_code(text), "secret_possible-token")

    def test_masking_preserves_line_positions(self) -> None:
        self.assertEqual(len(promotion._token_haystack(FIELD)), len(FIELD))

    def test_secret_scan_locations_report_nothing_for_the_field_line(self) -> None:
        found = promotion._secret_scan_locations(
            [("references/exit-and-status-codes.md", FIELD.encode())], "secret_possible-token"
        )
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
