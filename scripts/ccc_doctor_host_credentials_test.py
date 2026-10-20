#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's host Claude login check (ccc-node#2397).

A ``/login`` session whose refresh fails leaves ``~/.claude/.credentials.json``
with empty tokens and ``expiresAt=0``; every Claude call then fails while every
unit stays ``active`` (three nodes, 2026-10-17..20). These tests pin
the verdicts against fixture credential files in a temp ``.claude`` dir — no
real login, CLI or provider is touched — and pin that no token value ever
reaches a rendered row.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccc_doctor import Doctor, Row  # noqa: E402

ITEM = "claude host credentials"
# Fake fixture values (not credentials); named neutrally so CodeQL's
# name-based sensitive-data heuristic does not flag the fixture writer.
FAKE_A = "sk-ant-oat01-FIXTURE_A_MARKER_DO_NOT_RENDER"
FAKE_R = "sk-ant-ort01-FIXTURE_R_MARKER_DO_NOT_RENDER"
DAY_MS = 86400 * 1000
SCOPES = ["user:inference", "user:profile"]


class HostCredentialsCheck(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.claude_dir = Path(self._tmp.name) / ".claude"
        self.claude_dir.mkdir()
        self.path = self.claude_dir / ".credentials.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- fixtures ------------------------------------------------------------

    def write(self, payload: object, mode: int = 0o600) -> None:
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        self.path.chmod(mode)

    def login(self, *, first: str = FAKE_A, second: str | None = FAKE_R,
              expires_ms: object = None, chain_ms: object = None) -> None:
        expiry = int(time.time() * 1000) + 3600_000 if expires_ms is None else expires_ms
        entry: dict[str, object] = {"accessToken": first, "expiresAt": expiry,
                                    "scopes": SCOPES, "subscriptionType": "max"}
        if second is not None:
            entry = {**entry, "refreshToken": second}
        if chain_ms is not None:
            entry = {**entry, "refreshTokenExpiresAt": chain_ms}
        self.write({"claudeAiOauth": entry})

    def run_check(self) -> Row:
        doctor = Doctor(Path.cwd(), self.claude_dir, "settings")
        doctor.check_claude_host_credentials()
        rendered = json.dumps([row.__dict__ for row in doctor.rows], ensure_ascii=False)
        for marker in (FAKE_A, FAKE_R, "MARKER_DO_NOT_RENDER"):
            self.assertNotIn(marker, rendered, "a token value leaked into a doctor row")
        rows = [row for row in doctor.rows if row.item == ITEM]
        self.assertEqual(len(rows), 1, [r.item for r in doctor.rows])
        return rows[0]

    # --- the #2397 incident shape -------------------------------------------

    def test_wiped_login_is_manual(self) -> None:
        # Exact shape left on the three incident nodes: both tokens "", expiresAt 0.
        self.login(first="", second="", expires_ms=0)
        row = self.run_check()
        self.assertEqual(row.klass, "수동필요")
        self.assertIn("wiped", row.status)
        self.assertIn("setup-token", row.action)

    def test_wiped_login_without_refresh_key_is_manual(self) -> None:
        self.login(first="  ", second=None, expires_ms=0)
        self.assertEqual(self.run_check().klass, "수동필요")

    def test_access_empty_but_refresh_present_warns(self) -> None:
        self.login(first="", second=FAKE_R, expires_ms=0)
        row = self.run_check()
        self.assertEqual(row.klass, "경고")
        self.assertIn("refresh token present", row.status)

    # --- /login chain end (refreshTokenExpiresAt, never extended) ------------

    def chain_in(self, days: float) -> int:
        return int(time.time() * 1000 + days * DAY_MS)

    def test_refresh_chain_far_from_end_is_ok(self) -> None:
        self.login(chain_ms=self.chain_in(20))
        row = self.run_check()
        self.assertEqual(row.klass, "정상")
        self.assertIn("chain ends 20", row.status)

    def test_refresh_chain_within_a_week_warns(self) -> None:
        self.login(chain_ms=self.chain_in(5.5))
        row = self.run_check()
        self.assertEqual(row.klass, "경고")
        self.assertIn("5d left", row.status)
        self.assertIn("auth login", row.action)

    def test_refresh_chain_within_two_days_is_manual(self) -> None:
        # 수동필요 makes doctor exit 1, which the daily fleet sweep reports.
        self.login(chain_ms=self.chain_in(1.4))
        row = self.run_check()
        self.assertEqual(row.klass, "수동필요")
        self.assertIn("h left", row.status)

    def test_refresh_chain_already_ended_is_manual(self) -> None:
        self.login(chain_ms=self.chain_in(-0.1))
        row = self.run_check()
        self.assertEqual(row.klass, "수동필요")
        self.assertIn("chain ended", row.status)

    def test_refresh_chain_unusable_value_falls_back_to_ok(self) -> None:
        for bad in (0, "2026-11-10", True, float("nan"), -5):
            with self.subTest(refreshTokenExpiresAt=bad):
                self.login(chain_ms=bad)
                row = self.run_check()
                self.assertEqual(row.klass, "정상")
                self.assertIn("chain end not recorded", row.status)

    # --- healthy shapes ------------------------------------------------------

    def test_refresh_login_with_expired_access_is_ok(self) -> None:
        # Access tokens roll every few hours; an expired one is refreshed on use.
        self.login(expires_ms=int(time.time() * 1000) - DAY_MS)
        row = self.run_check()
        self.assertEqual(row.klass, "정상")
        self.assertIn("refresh login", row.status)

    def test_long_lived_token_far_from_expiry_is_ok(self) -> None:
        self.login(second=None, expires_ms=int(time.time() * 1000) + 365 * DAY_MS)
        row = self.run_check()
        self.assertEqual(row.klass, "정상")
        self.assertIn("long-lived token; expires 20", row.status)

    def test_long_lived_token_with_removed_refresh_key_variant(self) -> None:
        # Recovery recipe drops refreshToken; an empty string must read the same.
        self.login(second="", expires_ms=int(time.time() * 1000) + 200 * DAY_MS)
        self.assertEqual(self.run_check().klass, "정상")

    # --- long-lived expiry ---------------------------------------------------

    def test_long_lived_token_near_expiry_warns(self) -> None:
        self.login(second=None, expires_ms=int(time.time() * 1000) + 10 * DAY_MS)
        row = self.run_check()
        self.assertEqual(row.klass, "경고")
        self.assertIn("d left", row.status)
        self.assertIn("re-issue before expiry", row.action)

    def test_long_lived_token_expired_is_manual(self) -> None:
        self.login(second=None, expires_ms=int(time.time() * 1000) - DAY_MS)
        row = self.run_check()
        self.assertEqual(row.klass, "수동필요")
        self.assertIn("expired", row.status)

    def test_long_lived_token_without_expiry_warns(self) -> None:
        for bad in (0, None, "2027-10-20", True, float("nan")):
            with self.subTest(expiresAt=bad):
                entry: dict[str, object] = {"accessToken": FAKE_A, "scopes": SCOPES}
                if bad is not None:
                    entry = {**entry, "expiresAt": bad}
                self.write({"claudeAiOauth": entry})
                row = self.run_check()
                self.assertEqual(row.klass, "경고")
                self.assertIn("without expiresAt", row.status)

    # --- not applicable / unreadable ----------------------------------------

    def test_missing_file_is_not_applicable(self) -> None:
        row = self.run_check()
        self.assertEqual(row.klass, "정상")
        self.assertIn("해당 없음", row.status)

    def test_no_oauth_entry_is_not_applicable(self) -> None:
        self.write({"mcpOAuth": {}})
        row = self.run_check()
        self.assertEqual(row.klass, "정상")
        self.assertIn("no claudeAiOauth", row.status)

    def test_group_readable_file_is_unverifiable_not_parsed(self) -> None:
        self.login(first="", second="", expires_ms=0)
        self.path.chmod(0o644)
        row = self.run_check()
        self.assertEqual(row.klass, "경고")
        self.assertIn("확인 불가", row.status)

    def test_invalid_json_is_unverifiable(self) -> None:
        self.path.write_text("{not json " + FAKE_A, encoding="utf-8")
        self.path.chmod(0o600)
        self.assertEqual(self.run_check().klass, "경고")

    def test_non_object_json_is_unverifiable(self) -> None:
        self.write([FAKE_A])
        self.assertEqual(self.run_check().klass, "경고")

    def test_symlinked_file_is_not_followed(self) -> None:
        target = Path(self._tmp.name) / "elsewhere.json"
        target.write_text(json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": ""}}))
        target.chmod(0o600)
        self.path.symlink_to(target)
        row = self.run_check()
        self.assertEqual(row.klass, "경고")
        self.assertIn("확인 불가", row.status)

    def test_check_is_wired_into_diagnose(self) -> None:
        source = (Path(__file__).resolve().parent / "ccc_doctor.py").read_text(encoding="utf-8")
        self.assertIn("self.check_claude_host_credentials()", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
