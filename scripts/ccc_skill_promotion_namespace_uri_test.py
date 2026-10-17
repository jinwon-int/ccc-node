#!/usr/bin/env python3
"""`possible-token` must not fire on public XML namespace URIs (ccc-node#2371).

Field case, 2026-10-16 (fleet-skills#484, `precedent-holding-docx-export`): a
reviser returned a correct docx skill whose OOXML templates carry the standard
relationship-type URIs. The generic `possible-token` pattern
(`[A-Za-z0-9+/]{40,}`) allows `/`, so the path after
`http://schemas.openxmlformats.` (52 and 47 chars) read as a token, the revise
result was rejected as `secret_possible-token`, and the PR hit the 2/2 revise
cap. Only public schema hosts are masked, and only when no single path segment
reaches the token threshold — a long run under such a host, or any other URL,
still fails.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "promotion_namespace_uri_test", Path(__file__).with_name("ccc-skill-promotion.py")
)
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)

OOXML_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
    '  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/>\n'
    '  <Relationship Id="rIdSource" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/hyperlink" Target="{{SOURCE_URL}}" TargetMode="External"/>\n'
    "</Relationships>\n"
)
# Synthetic, not a credential: a 44-char mixed-case alphanumeric run, built
# in code so no literal looks like a key to the repo's gitleaks scan.
LONG_RUN = "".join("AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"[i % 36] for i in range(44))


def scan_code(text: str) -> str | None:
    try:
        promotion._scan_text(text.encode("utf-8"))
    except promotion.PromotionError as error:
        return error.code
    return None


class NamespaceUriMask(unittest.TestCase):
    def test_ooxml_relationship_uris_pass(self) -> None:
        self.assertIsNone(scan_code(OOXML_RELS))

    def test_other_public_schema_hosts_pass(self) -> None:
        text = (
            'xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
            'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml/extensions/core"\n'
            'xmlns:svg="https://www.w3.org/2000/svg/namespace/definitions/elements"\n'
        )
        self.assertIsNone(scan_code(text))

    def test_bare_token_still_fails(self) -> None:
        self.assertEqual(scan_code(f"value: {LONG_RUN}\n"), "secret_possible-token")

    def test_token_hidden_under_schema_host_still_fails(self) -> None:
        text = f'Type="http://schemas.openxmlformats.org/officeDocument/{LONG_RUN}"\n'
        self.assertEqual(scan_code(text), "secret_possible-token")

    def test_token_after_schema_uri_on_same_line_still_fails(self) -> None:
        text = (
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            f'officeDocument" data="{LONG_RUN}"\n'
        )
        self.assertEqual(scan_code(text), "secret_possible-token")

    def test_other_url_paths_are_not_masked(self) -> None:
        text = "https://hooks.example.com/services/team/channel/abcdefghij/klmnopqrstuv\n"
        self.assertEqual(scan_code(text), "secret_possible-token")

    def test_lookalike_host_is_not_masked(self) -> None:
        text = (
            "http://schemas.openxmlformats.org.example.net/officeDocument/2006/"
            "relationships/officeDocument\n"
        )
        self.assertEqual(scan_code(text), "secret_possible-token")


if __name__ == "__main__":
    unittest.main()
